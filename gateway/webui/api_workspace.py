# -*- coding: utf-8 -*-
"""
api_workspace.py —— Workspace / WorkspaceSession 管理 API（Phase 3）。

- validate-path（Windows 安全路径校验）
- Workspace CRUD + 归档（原子归档活跃 Session）
- Session 列表/创建/重命名/归档（归属校验）
- 配置摘要
Phase 4 追加 chat / prompt-preview / runtime status。
"""

import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4

from aiohttp import web

from gateway.webui.workspace_models import (
    Workspace, WorkspaceSession,
)
from gateway.webui.workspace_store import (
    StoreBusy,
    ValidationError,
    VersionConflict,
    WorkspaceNotFound,
    WorkspaceSessionNotFound,
    WorkspaceStoreError,
)
from gateway.webui.path_validator import PathValidator, default_validator
from gateway.webui.glue import send_and_wait

logger = logging.getLogger("jk_agent.gateway")

_PURPOSES = ("project_root", "working_directory", "extra_root")


def register_routes(app: web.Application, module):
    app.router.add_get("/api/workspaces", _make_list(module))
    app.router.add_post("/api/workspaces/validate-path", _make_validate_path(module))
    app.router.add_post("/api/workspaces", _make_create(module))
    app.router.add_get("/api/workspaces/{workspace_id}", _make_get(module))
    app.router.add_put("/api/workspaces/{workspace_id}", _make_update(module))
    app.router.add_delete("/api/workspaces/{workspace_id}", _make_delete_workspace(module))
    app.router.add_post("/api/workspaces/{workspace_id}/archive", _make_archive(module))
    app.router.add_get("/api/workspaces/{workspace_id}/summary", _make_summary(module))
    app.router.add_get("/api/workspaces/{workspace_id}/sessions", _make_session_list(module))
    app.router.add_post("/api/workspaces/{workspace_id}/sessions", _make_session_create(module))
    app.router.add_get("/api/workspaces/{workspace_id}/sessions/{session_id}",
                       _make_session_get(module))
    app.router.add_get("/api/workspaces/{workspace_id}/sessions/{session_id}/history",
                       _make_history(module))
    app.router.add_put("/api/workspaces/{workspace_id}/sessions/{session_id}",
                       _make_session_update(module))
    app.router.add_post("/api/workspaces/{workspace_id}/sessions/{session_id}/archive",
                       _make_session_archive(module))
    # Phase 4：运行链路
    app.router.add_post(
        "/api/workspaces/{workspace_id}/sessions/{session_id}/chat",
        _make_chat(module))
    app.router.add_post(
        "/api/workspaces/{workspace_id}/sessions/{session_id}/plan",
        _make_plan(module))
    app.router.add_post(
        "/api/workspaces/{workspace_id}/sessions/{session_id}/prompt-preview",
        _make_prompt_preview(module))
    app.router.add_get(
        "/api/workspaces/{workspace_id}/sessions/{session_id}/runtime-status",
        _make_runtime_status(module))
    app.router.add_post(
        "/api/workspaces/{workspace_id}/sessions/{session_id}/apply-config",
        _make_apply_config(module))
    # Phase 5：会话控制
    app.router.add_post(
        "/api/workspaces/{workspace_id}/sessions/{session_id}/stop",
        _make_stop(module))
    app.router.add_post(
        "/api/workspaces/{workspace_id}/sessions/{session_id}/clear",
        _make_clear(module))
    app.router.add_delete(
        "/api/workspaces/{workspace_id}/sessions/{session_id}",
        _make_delete(module))
    app.router.add_post(
        "/api/workspaces/{workspace_id}/sessions/{session_id}/switch",
        _make_switch(module))


def _err(text, status=400, code=""):
    payload = {"error": str(text)}
    if code:
        payload["code"] = code
    return web.json_response(payload, status=status)


def _handle_store_error(exc: WorkspaceStoreError):
    return _err(exc.args[0] if exc.args else str(exc),
                status=getattr(exc, "http_status", 500),
                code=getattr(exc, "code", "WORKSPACE_STORE_ERROR"))


async def _body(request):
    try:
        raw = await request.text()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, Exception):
        raise web.HTTPBadRequest(text=json.dumps(
            {"error": "请求体不是合法 JSON", "code": "INVALID_JSON"},
            ensure_ascii=False))


def _require(module, attr):
    store = getattr(module, attr, None)
    if store is None:
        raise web.HTTPServiceUnavailable(text=json.dumps(
            {"error": "Workspace 存储未就绪", "code": "WORKSPACE_STORE_UNAVAILABLE"},
            ensure_ascii=False))
    return store


def _validator(module) -> PathValidator:
    return getattr(module, "path_validator", None) or default_validator()


def _ws_payload(w: Workspace) -> dict:
    return w.to_dict()


def _sess_payload(s: WorkspaceSession) -> dict:
    return s.to_dict()


def _validate_session_agent(module, payload: dict):
    """A session must name an active reusable AgentProfile.

    Workspace records remain project metadata only; selecting an agent is a
    session-level responsibility. Keeping the guard at the API boundary gives
    browser and non-browser clients the same predictable validation.
    """
    profile_id = str((payload or {}).get("agent_profile_id") or "").strip()
    if not profile_id:
        return None, _err("agent_profile_id is required", code="AGENT_PROFILE_REQUIRED")
    try:
        profile = _require(module, "profile_store").get(profile_id)
    except WorkspaceStoreError as exc:
        return None, _handle_store_error(exc)
    if profile.status != "active":
        return None, _err("agent profile is not active", status=409,
                          code="AGENT_PROFILE_INACTIVE")
    return profile, None


# ---------- 列表 ----------

def _make_list(module):
    async def handler(request):
        store = _require(module, "workspace_store")
        try:
            status = request.query.get("status", "active")
            q = request.query.get("q", "")
            limit = int(request.query.get("limit", 50))
            offset = int(request.query.get("offset", 0))
        except (TypeError, ValueError):
            return _err("limit/offset 必须是整数", code="INVALID_PAGINATION")
        items = store.list(status=status, q=q, limit=limit, offset=offset)
        total = store.count(status=status, q=q)
        return web.json_response({
            "workspaces": [_ws_payload(w) for w in items],
            "total": total, "limit": limit, "offset": offset,
        })
    return handler


# ---------- 路径校验 ----------

def _make_validate_path(module):
    async def handler(request):
        validator = _validator(module)
        body = await _body(request)
        path_str = str(body.get("path") or "")
        purpose = str(body.get("purpose") or "project_root")
        if purpose not in _PURPOSES:
            return _err(f"purpose 必须是 {_PURPOSES}", code="INVALID_PURPOSE")
        base = None
        if body.get("base"):
            base = Path(str(body["base"]))
        allowed_roots = []
        for r in (body.get("allowed_roots") or []):
            try:
                allowed_roots.append(Path(str(r)))
            except Exception:
                pass
        result = validator.validate(
            path_str, purpose=purpose, base=base,
            allowed_roots=allowed_roots or None,
            risk_confirmed=bool(body.get("risk_confirmed")))
        return web.json_response(result.to_dict())
    return handler


# ---------- Workspace session history ----------

def _history_message_payload(messages, limit: int) -> list[dict]:
    """Convert Agent history to a safe UI payload without loading an Agent."""
    from core.message_store import _content_to_text

    out = []
    for source in list(messages or [])[-limit:]:
        item = dict(source)
        content = item.get("content")
        if isinstance(content, list):
            item["content_text"] = _content_to_text(content)
        out.append(item)
    return out


def _make_history(module):
    """Read a workspace session's durable history by its SQLite-owned ID.

    Workspace sessions deliberately do not use ``sessions_map.json``.  When the
    Agent is not resident, their MessageStore file is therefore addressed with
    the workspace ``session_id`` directly.
    """
    async def handler(request):
        sess_store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        try:
            session = sess_store.get_owned(wid, sid)
        except WorkspaceSessionNotFound as exc:
            return _handle_store_error(exc)
        try:
            limit = max(1, min(int(request.query.get("limit", 300)), 1000))
        except (TypeError, ValueError):
            return _err("limit must be an integer", code="INVALID_PAGINATION")

        entry = _get_entry(module, session.session_key)
        if entry is not None and getattr(entry, "agent", None) is not None:
            agent = entry.agent
            return web.json_response({
                "workspace_id": wid,
                "workspace_session_id": sid,
                "session_key": session.session_key,
                "messages": _history_message_payload(getattr(agent, "messages", []), limit),
                "source": "memory",
            })

        from core.message_store import DEFAULT_SESSION_DIR
        history_file = Path(DEFAULT_SESSION_DIR) / f"{session.session_id}.json"
        if not history_file.exists():
            return web.json_response({
                "workspace_id": wid,
                "workspace_session_id": sid,
                "session_key": session.session_key,
                "messages": [],
                "source": "empty",
            })
        try:
            payload = json.loads(history_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load workspace history %s: %s", session.session_key, exc)
            return _err("failed to read workspace session history", 500,
                        "WORKSPACE_HISTORY_READ_FAILED")
        return web.json_response({
            "workspace_id": wid,
            "workspace_session_id": sid,
            "session_key": session.session_key,
            "messages": _history_message_payload(payload.get("messages", []), limit),
            "source": "disk",
        })
    return handler


# ---------- Workspace CRUD ----------

def _make_create(module):
    async def handler(request):
        store = _require(module, "workspace_store")
        body = await _body(request)
        import uuid as _uuid
        fields = {k: v for k, v in body.items()
                  if k in Workspace.__dataclass_fields__}
        # New workspaces own project metadata only. These legacy workspace-level
        # runtime defaults remain readable for existing records, but are not
        # accepted when creating a new one; each session selects its own agent
        # and runtime settings.
        for key in (
            "default_agent_profile_id", "default_model", "permission_mode", "chat_mode",
            "include_tools", "exclude_tools", "include_skills", "exclude_skills",
            "include_mcp_servers", "exclude_mcp_servers",
        ):
            fields.pop(key, None)
        fields["workspace_id"] = fields.get("workspace_id") or f"ws_{_uuid.uuid4().hex[:8]}"
        if not fields.get("name"):
            return _err("name 为必填项", code="NAME_REQUIRED")
        if not fields.get("project_path"):
            return _err("project_path 为必填项", code="PROJECT_PATH_REQUIRED")
        # 路径风险确认策略：warning 级别必须显式确认
        validator = _validator(module)
        v = validator.validate(fields["project_path"], purpose="project_root",
                               risk_confirmed=bool(body.get("risk_confirmed")))
        if v.blocked:
            return _err("；".join(v.reasons) or "路径被拒绝",
                        status=422, code="WORKSPACE_PATH_BLOCKED")
        if v.status == "warning" and not body.get("risk_confirmed"):
            return _err("路径存在风险项，请确认后再创建",
                        status=422, code="WORKSPACE_PATH_CONFIRMATION_REQUIRED")
        fields["path_risk_level"] = v.risk_level
        fields["path_warnings"] = v.warnings
        fields["working_directory"] = (
            fields.get("working_directory") or fields["project_path"])
        # working_directory 校验
        if fields["working_directory"] != fields["project_path"]:
            roots = [Path(fields["project_path"])] + [
                Path(x) for x in (fields.get("extra_workspace_roots") or [])]
            vw = validator.validate(fields["working_directory"],
                                    purpose="working_directory",
                                    allowed_roots=roots,
                                    risk_confirmed=bool(body.get("risk_confirmed")))
            if vw.blocked:
                return _err("；".join(vw.reasons) or "working_directory 无效",
                            status=422, code="WORKSPACE_PATH_BLOCKED")
        # 同路径冲突
        try:
            existing = store.find_by_project_path(fields["project_path"])
        except Exception:
            existing = None
        if existing is not None:
            return _err(
                f"已存在指向该路径的工作区: {existing.name}",
                status=409, code="WORKSPACE_DUPLICATE_PATH")
        session_payload = dict(body.get("first_session") or {})
        if session_payload:
            _profile, profile_error = _validate_session_agent(module, session_payload)
            if profile_error is not None:
                return profile_error
        try:
            ws = Workspace.from_dict(fields)
            if session_payload:
                created, first = store.create_with_first_session(ws, session_payload)
            else:
                # A workspace can be created before any agent/session is chosen.
                created = store.create(ws)
                first = None
        except (ValidationError, WorkspaceStoreError) as exc:
            return _handle_store_error(exc)
        return web.json_response({
            "workspace": _ws_payload(created),
            "first_session": _sess_payload(first) if first is not None else None,
        }, status=201)
    return handler


def _make_get(module):
    async def handler(request):
        store = _require(module, "workspace_store")
        try:
            w = store.get(request.match_info["workspace_id"])
        except WorkspaceNotFound as exc:
            return _handle_store_error(exc)
        return web.json_response(_ws_payload(w))
    return handler


def _make_update(module):
    async def handler(request):
        store = _require(module, "workspace_store")
        wid = request.match_info["workspace_id"]
        body = await _body(request)
        expected_version = body.get("version")
        patch = {k: v for k, v in body.items()
                 if k in Workspace.__dataclass_fields__ and k != "workspace_id"}
        try:
            updated = store.update(wid, patch, expected_version=expected_version)
        except (WorkspaceNotFound, VersionConflict, ValidationError) as exc:
            return _handle_store_error(exc)
        return web.json_response(_ws_payload(updated))
    return handler


def _make_archive(module):
    async def handler(request):
        store = _require(module, "workspace_store")
        wid = request.match_info["workspace_id"]
        body = await _body(request)
        try:
            archived = store.archive(wid,
                                     expected_version=body.get("version"))
        except (WorkspaceNotFound, VersionConflict) as exc:
            return _handle_store_error(exc)
        return web.json_response(_ws_payload(archived))
    return handler


def _make_delete_workspace(module):
    """彻底删除工作区：驱逐运行中会话、删除长期记忆目录并标记 deleted。"""
    async def handler(request):
        ws_store = _require(module, "workspace_store")
        sess_store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        try:
            ws_store.get(wid)
        except WorkspaceNotFound as exc:
            return _handle_store_error(exc)
        try:
            sessions = sess_store.list_for_workspace(wid, status="active")
        except Exception:
            sessions = []
        for s in sessions:
            entry = _get_entry(module, s.session_key)
            if entry is not None:
                try:
                    await module.session_mgr.evict(s.session_key, save=True)
                except Exception as exc:
                    logger.warning("驱逐工作区会话失败: %s", exc)
        try:
            deleted = ws_store.delete(wid)
        except (WorkspaceNotFound, VersionConflict) as exc:
            return _handle_store_error(exc)
        memory_removed = False
        try:
            from memory.manager import delete_workspace_memory
            memory_removed = bool(delete_workspace_memory(wid))
        except Exception as exc:
            logger.warning("删除工作区长期记忆目录失败: %s", exc)
        payload = _ws_payload(deleted)
        payload["memory_removed"] = memory_removed
        return web.json_response(payload)
    return handler


def _make_summary(module):
    async def handler(request):
        ws_store = _require(module, "workspace_store")
        sess_store = _require(module, "session_store")
        prof_store = _require(module, "profile_store")
        wid = request.match_info["workspace_id"]
        try:
            w = ws_store.get(wid)
            sessions = sess_store.list_for_workspace(wid, status="active")
        except WorkspaceNotFound as exc:
            return _handle_store_error(exc)
        profile = None
        if w.default_agent_profile_id:
            try:
                profile = prof_store.get(w.default_agent_profile_id)
            except WorkspaceStoreError:
                profile = None
        return web.json_response({
            "workspace": _ws_payload(w),
            "sessions": [_sess_payload(s) for s in sessions],
            "default_profile": profile.to_dict() if profile else None,
        })
    return handler


# ---------- Session ----------

def _make_session_list(module):
    async def handler(request):
        store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        status = request.query.get("status", "active")
        try:
            sessions = store.list_for_workspace(wid, status=status)
        except WorkspaceStoreError as exc:
            return _handle_store_error(exc)
        return web.json_response({"sessions": [_sess_payload(s) for s in sessions]})
    return handler


def _make_session_create(module):
    async def handler(request):
        store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        body = await _body(request)
        _profile, profile_error = _validate_session_agent(module, body)
        if profile_error is not None:
            return profile_error
        try:
            session = store.create(wid, body)
        except (WorkspaceNotFound, ValidationError) as exc:
            return _handle_store_error(exc)
        return web.json_response(_sess_payload(session), status=201)
    return handler


def _make_session_get(module):
    async def handler(request):
        store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        try:
            session = store.get_owned(wid, sid)
        except WorkspaceSessionNotFound as exc:
            return _handle_store_error(exc)
        return web.json_response(_sess_payload(session))
    return handler


def _make_session_update(module):
    async def handler(request):
        store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        body = await _body(request)
        try:
            session = store.update_runtime_overrides(
                wid, sid, name=body.get("name"))
        except (WorkspaceSessionNotFound, StoreBusy) as exc:
            return _handle_store_error(exc)
        return web.json_response(_sess_payload(session))
    return handler


def _make_session_archive(module):
    async def handler(request):
        store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        try:
            session = store.archive(wid, sid)
        except (WorkspaceSessionNotFound, StoreBusy) as exc:
            return _handle_store_error(exc)
        return web.json_response(_sess_payload(session))
    return handler

# ============================================================
# Phase 4：运行链路端点（chat / prompt-preview / runtime status）
# ============================================================

_runtime_service_cache = {}


def _runtime_service(module):
    """构造/复用 WorkspaceRuntimeService（按 module 缓存）。"""
    svc = _runtime_service_cache.get(id(module))
    if svc is None:
        from gateway.webui.workspace_runtime import WorkspaceRuntimeService
        from core.config_loader import _find_project_root, load_config
        cfg = load_config()
        gateway_agent = cfg.get("gateway", {}).get("agent", {})
        svc = WorkspaceRuntimeService(
            module=module,
            gateway_config={
                **gateway_agent,
                "framework_root": str(_find_project_root()),
                "agent_data_root": str(Path(
                    cfg.get("permission", {}).get("workspace", "./workspace"))),
            },
        )
        _runtime_service_cache[id(module)] = svc
    return svc


def _filter_mcp_servers(module, selected_names: list) -> list:
    """从 config 中过滤出快照选中的 MCP server 配置（含完整连接信息，仅内部使用）。"""
    if not selected_names:
        return None
    from core.config_loader import load_config
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    servers = cfg.get("mcp", {}).get("servers", []) or []
    selected = set(selected_names)
    return [s for s in servers if (s.get("name") or "") in selected]


def _make_chat(module):
    async def handler(request):
        ws_store = _require(module, "workspace_store")
        sess_store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        body = await _body(request)
        text = str(body.get("message") or "").strip()
        if not text:
            return _err("message 为必填", code="MESSAGE_REQUIRED")
        # 归属 + busy 校验（P4-I-02/P4-I-03）
        try:
            session = sess_store.get_owned(wid, sid)
        except WorkspaceSessionNotFound as exc:
            return _handle_store_error(exc)
        if session.status != "active":
            return _err("会话已归档，不能发送", 409, "WORKSPACE_SESSION_ARCHIVED")
        if session.is_busy:
            return _err("会话正在运行，请等待完成", 409, "WORKSPACE_SESSION_BUSY")
        # 工作区必须 active
        try:
            ws_store.get(wid)
        except WorkspaceNotFound as exc:
            return _handle_store_error(exc)
        # 构建/复用快照（当前消息冻结）
        service = _runtime_service(module)
        try:
            snapshot = service.build_snapshot(wid, sid)
        except Exception as exc:
            logger.warning("构建快照失败: %s", exc)
            return _err("运行配置解析失败: " + str(exc), 422, "SNAPSHOT_BUILD_FAILED")
        message_id = f"ws-{wid[:8]}-{uuid4().hex[:8]}"
        sess_store.set_busy(wid, sid, True)
        sess_store.touch_snapshot(wid, sid, snapshot.snapshot_id)
        entry = None
        try:
            session_key = session.session_key
            entry = module.session_mgr.get_or_create(session_key)
            if entry is None:
                return _err("会话池已满", 503, "SESSION_POOL_FULL")
            ctx = service.build_runtime_context(snapshot)
            entry.runtime_context = ctx
            entry.runtime_snapshot_id = snapshot.snapshot_id
            entry.runtime_model = snapshot.model
            entry.runtime_permission_mode = snapshot.permission_mode
            entry.runtime_reasoning_level = snapshot.reasoning_level
            entry.runtime_max_steps = service.resolve(
                service.load_workspace(wid), service.load_profile(snapshot.agent_profile_id), session).max_steps
            entry.runtime_mcp_servers = _filter_mcp_servers(module, snapshot.mcp_servers)
            profile = service.load_profile(snapshot.agent_profile_id)
            entry.runtime_profile_prompt = profile.system_prompt if profile else None
            entry.runtime_allowed_tools = snapshot.tools
            entry.runtime_allowed_skills = snapshot.skills
            entry.config_stale = False
            # 记录 agent 元数据（审批归属等）
            meta = {
                "workspace_id": wid,
                "workspace_session_id": sid,
                "snapshot_id": snapshot.snapshot_id,
                "message_id": message_id,
                "session_key": session_key,
            }
            result = await send_and_wait(
                module, session_key, text, timeout=float(body.get("timeout") or 120),
                metadata=meta, message_id=message_id)
            return web.json_response({
                "ok": result.get("ok", False),
                "reply": result.get("reply"),
                "message_id": message_id,
                "snapshot_id": snapshot.snapshot_id,
                "session_key": session_key,
                "error": result.get("error"),
            })
        finally:
            sess_store.set_busy(wid, sid, False)
            if entry is not None and entry.config_stale:
                # stale 且空闲：由 janitor/下次消息重建；此处仅保留标记
                pass
    return handler


def _make_plan(module):
    """Create a plan through the workspace runtime boundary.

    Unlike the global plan endpoint this first freezes the selected workspace
    model, reasoning, permissions and capability scope on the SessionEntry.
    """
    async def handler(request):
        ws_store = _require(module, "workspace_store")
        sess_store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        body = await _body(request)
        text = str(body.get("message") or body.get("text") or "").strip()
        if not text:
            return _err("message is required", code="MESSAGE_REQUIRED")
        try:
            session = sess_store.get_owned(wid, sid)
            workspace = ws_store.get(wid)
        except (WorkspaceSessionNotFound, WorkspaceNotFound) as exc:
            return _handle_store_error(exc)
        if session.status != "active":
            return _err("session is archived", 409, "WORKSPACE_SESSION_ARCHIVED")
        if session.is_busy:
            return _err("session is busy", 409, "WORKSPACE_SESSION_BUSY")
        service = _runtime_service(module)
        try:
            snapshot = service.build_snapshot(wid, sid)
        except Exception as exc:
            logger.warning("Failed to build workspace plan snapshot: %s", exc)
            return _err("runtime configuration could not be resolved", 422,
                        "SNAPSHOT_BUILD_FAILED")
        sess_store.set_busy(wid, sid, True)
        sess_store.touch_snapshot(wid, sid, snapshot.snapshot_id)
        try:
            entry = module.session_mgr.get_or_create(session.session_key)
            if entry is None:
                return _err("session pool is full", 503, "SESSION_POOL_FULL")
            entry.runtime_context = service.build_runtime_context(snapshot)
            entry.runtime_snapshot_id = snapshot.snapshot_id
            entry.runtime_model = snapshot.model
            entry.runtime_permission_mode = snapshot.permission_mode
            entry.runtime_reasoning_level = snapshot.reasoning_level
            entry.runtime_max_steps = service.resolve(
                workspace, service.load_profile(snapshot.agent_profile_id), session).max_steps
            entry.runtime_mcp_servers = _filter_mcp_servers(module, snapshot.mcp_servers)
            profile = service.load_profile(snapshot.agent_profile_id)
            entry.runtime_profile_prompt = profile.system_prompt if profile else None
            entry.runtime_allowed_tools = snapshot.tools
            entry.runtime_allowed_skills = snapshot.skills
            entry.config_stale = False
            message_id = f"ws-plan-{wid[:8]}-{uuid4().hex[:8]}"
            result = await send_and_wait(
                module, session.session_key, f"/plan-preview {text}",
                timeout=min(float(body.get("timeout") or 300), 600),
                metadata={
                    "workspace_id": wid,
                    "workspace_session_id": sid,
                    "snapshot_id": snapshot.snapshot_id,
                    "message_id": message_id,
                    "session_key": session.session_key,
                }, message_id=message_id)
            if not result.get("ok"):
                return web.json_response(result, status=504)
            try:
                preview = json.loads(result.get("reply") or "{}")
            except json.JSONDecodeError:
                return _err("plan preview response could not be parsed", 500,
                            "PLAN_PREVIEW_PARSE_FAILED")
            if not preview.get("ok"):
                return _err(preview.get("error") or "plan preview failed", 422,
                            "PLAN_PREVIEW_FAILED")
            plan = module.glue.create_plan(session.session_key, text, preview["plan"])
            return web.json_response({
                "ok": True,
                "plan_id": plan.plan_id,
                "plan": plan.to_dict(),
                "tasks": preview.get("tasks") or [],
                "snapshot_id": snapshot.snapshot_id,
                "session_key": session.session_key,
            })
        finally:
            sess_store.set_busy(wid, sid, False)
    return handler


def _make_prompt_preview(module):
    async def handler(request):
        sess_store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        body = await _body(request)
        try:
            session = sess_store.get_owned(wid, sid)
        except WorkspaceSessionNotFound as exc:
            return _handle_store_error(exc)
        service = _runtime_service(module)
        workspace = service.load_workspace(wid)
        profile = service.load_profile(session.agent_profile_id)
        config = service.resolve(workspace, profile, session)
        from gateway.webui import prompt_preview
        try:
            from memory.manager import workspace_memory_dir
            result = prompt_preview.build_preview(
                profile or _blank_profile(),
                workspace=workspace, session=session,
                tool_registry=_preview_registry(),
                skill_manager=_preview_skill_manager(),
                framework_root=service.framework_root,
                project_root=workspace.project_path,
                working_directory=workspace.working_directory or workspace.project_path,
                memory_path=str(workspace_memory_dir(wid)),
                memory_instruction="当用户询问与当前项目相关的问题时，优先调用 memory_search 检索本工作区长期记忆，再作答。",
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("prompt preview failed")
            return _err(str(exc), 500, "PREVIEW_FAILED")
        result["effective_config"] = config.to_dict()
        return web.json_response(result)
    return handler


def _blank_profile():
    from gateway.webui.workspace_models import AgentProfile
    return AgentProfile(profile_id="preview", name="preview")


_preview_registry_cache = None


def _preview_registry():
    global _preview_registry_cache
    if _preview_registry_cache is None:
        from tools import ToolRegistry
        from tools.builtin_tools import register_all_tools
        from tools.web_tools import register_web_tools
        reg = ToolRegistry()
        register_all_tools(reg, memory_manager=None, sandbox=None, process_manager=None)
        register_web_tools(reg)
        _preview_registry_cache = reg
    return _preview_registry_cache


_preview_skill_cache = None


def _preview_skill_manager():
    global _preview_skill_cache
    if _preview_skill_cache is None:
        from skills.manager import SkillManager
        from core.config_loader import _find_project_root
        mgr = SkillManager(skills_dir=str(_find_project_root() / "SKILLS"))
        try:
            mgr.load_all()
        except Exception:
            pass
        _preview_skill_cache = mgr
    return _preview_skill_cache


def _make_runtime_status(module):
    async def handler(request):
        sess_store = _require(module, "session_store")
        ws_store = _require(module, "workspace_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        try:
            session = sess_store.get_owned(wid, sid)
            workspace = ws_store.get(wid)
        except (WorkspaceSessionNotFound, WorkspaceNotFound) as exc:
            return _handle_store_error(exc)
        service = _runtime_service(module)
        entry = None
        if module.session_mgr is not None:
            entry = module.session_mgr._sessions.get(session.session_key)
        snapshot = None
        if session.last_snapshot_id:
            snapshot = service.snap_store.get(session.last_snapshot_id)
        stale = False
        if snapshot is not None:
            stale = service.is_stale(snapshot, workspace=workspace, session=session)
        return web.json_response({
            "workspace_id": wid,
            "session_id": sid,
            "session_key": session.session_key,
            "is_busy": session.is_busy or bool(entry and entry.is_busy),
            "status": session.status,
            "last_snapshot_id": session.last_snapshot_id,
            "snapshot_stale": stale,
            "config_stale": bool(entry and entry.config_stale),
            "agent_loaded": bool(entry and entry.agent is not None),
            "model": snapshot.model if snapshot else session.model,
            "permission_mode": snapshot.permission_mode if snapshot else session.permission_mode,
            "reasoning_level": snapshot.reasoning_level if snapshot else session.reasoning_level,
            "tools": snapshot.tools if snapshot else [],
            "skills": snapshot.skills if snapshot else [],
            "mcp_servers": snapshot.mcp_servers if snapshot else [],
            "workspace_version": workspace.version,
            "session_client_config_version": session.client_config_version,
        })
    return handler


def _make_apply_config(module):
    """应用新配置：bump client_config_version + 标记 stale（Phase 4 重建入口）。"""
    async def handler(request):
        sess_store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        body = await _body(request)
        try:
            session = sess_store.get_owned(wid, sid)
        except WorkspaceSessionNotFound as exc:
            return _handle_store_error(exc)
        if session.is_busy:
            return _err("会话正在运行，不能切换配置", 409, "WORKSPACE_SESSION_BUSY")
        from gateway.webui.workspace_store import utc_now as _now
        # bump client_config_version（乐观版本）
        new_session = sess_store.update_runtime_overrides(
            wid, sid,
            model=body.get("model") or None,
            permission_mode=body.get("permission_mode") or None,
            chat_mode=body.get("chat_mode") or None,
        )
        # 标记 stale（内存 entry；agent 由空闲后重建）
        service = _runtime_service(module)
        service.mark_stale(wid, sid)
        return web.json_response(_sess_payload(new_session))
    return handler

# ============================================================
# Phase 5：会话控制（stop / clear / delete / switch）
# ============================================================


def _get_entry(module, session_key):
    mgr = getattr(module, "session_mgr", None)
    if mgr is None:
        return None
    return mgr._sessions.get(session_key)


async def _rebuild_on_next_message(module, session: WorkspaceSession) -> None:
    """Release an idle in-memory Agent so the next message uses its frozen config.

    Session overrides live in SQLite.  Keeping an old Agent after changing one
    would make the toolbar look updated while model/permission/reasoning still
    execute with stale values, so an idle entry is deliberately evicted.
    """
    entry = _get_entry(module, session.session_key)
    if entry is None:
        return
    service = _runtime_service(module)
    service.mark_stale(session.workspace_id, session.session_id)
    manager = getattr(module, "session_mgr", None)
    evict = getattr(manager, "evict", None)
    if callable(evict):
        try:
            await evict(session.session_key, save=True)
            return
        except Exception as exc:
            logger.warning("Failed to release workspace session for rebuild: %s", exc)
    # Test/minimal session managers may not implement eviction.  Clearing the
    # object still guarantees Dispatcher constructs a new Agent next time.
    entry.agent = None


def _make_stop(module):
    async def handler(request):
        sess_store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        try:
            session = sess_store.get_owned(wid, sid)
        except WorkspaceSessionNotFound as exc:
            return _handle_store_error(exc)
        entry = _get_entry(module, session.session_key)
        if entry is None or entry.agent is None:
            return _err("会话未加载或未在运行", 404, "SESSION_NOT_LOADED")
        entry.agent.request_stop()
        module.bus.publish("chat.stop_requested", {
            "session_key": session.session_key,
            "workspace_id": wid, "workspace_session_id": sid,
        })
        return web.json_response({"ok": True, "session_key": session.session_key})
    return handler


def _make_clear(module):
    async def handler(request):
        sess_store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        try:
            session = sess_store.get_owned(wid, sid)
        except WorkspaceSessionNotFound as exc:
            return _handle_store_error(exc)
        if session.is_busy:
            return _err("会话正在运行，不能清空", 409, "WORKSPACE_SESSION_BUSY")
        entry = _get_entry(module, session.session_key)
        if entry is not None and entry.agent is not None:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    module.session_mgr.get_executor(),
                    entry.agent.clear_history)
            except Exception as exc:
                logger.warning("清空工作区会话失败: %s", exc)
        return web.json_response({"ok": True, "session_key": session.session_key})
    return handler


def _make_delete(module):
    async def handler(request):
        sess_store = _require(module, "session_store")
        ws_store = _require(module, "workspace_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        try:
            session = sess_store.get_owned(wid, sid)
        except WorkspaceSessionNotFound as exc:
            return _handle_store_error(exc)
        if session.is_busy:
            return _err("会话正在运行，不能删除", 409, "WORKSPACE_SESSION_BUSY")
        # 释放内存 Agent / MCP / ProcessManager（不删用户文件）
        entry = _get_entry(module, session.session_key)
        if entry is not None:
            try:
                await module.session_mgr.evict(session.session_key, save=True)
            except Exception as exc:
                logger.warning("驱逐工作区会话失败: %s", exc)
        archived = sess_store.archive(wid, sid)
        return web.json_response({"ok": True,
                                  "session": _sess_payload(archived)})
    return handler


def _make_switch(module):
    """Persist workspace-session controls and rebuild the idle runtime.

    The single endpoint intentionally mirrors the base chat controls while
    retaining workspace ownership and the RuntimeSnapshot boundary.
    """
    async def handler(request):
        sess_store = _require(module, "session_store")
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        body = await _body(request)
        try:
            session = sess_store.get_owned(wid, sid)
        except WorkspaceSessionNotFound as exc:
            return _handle_store_error(exc)
        if session.is_busy:
            return _err("session is busy; configuration cannot be changed", 409, "WORKSPACE_SESSION_BUSY")

        patch = {}
        if body.get("model") is not None:
            model = str(body["model"]).strip()
            if not model:
                return _err("model is required", code="INVALID_MODEL")
            patch["model"] = model
        if body.get("permission_mode") is not None:
            mode = str(body["permission_mode"]).strip()
            if mode not in ("readonly", "ask", "allow", "unreviewed"):
                return _err("permission_mode must be readonly/ask/allow/unreviewed",
                            code="INVALID_PERMISSION_MODE")
            patch["permission_mode"] = mode
        if body.get("chat_mode") is not None:
            mode = str(body["chat_mode"]).strip()
            if mode not in ("chat", "plan"):
                return _err("chat_mode must be chat/plan", code="INVALID_CHAT_MODE")
            patch["chat_mode"] = mode
        if body.get("reasoning_level") is not None:
            level = str(body["reasoning_level"]).strip().lower()
            from gateway.webui.workspace_models import VALID_REASONING_LEVELS
            if level not in VALID_REASONING_LEVELS:
                return _err("reasoning_level is invalid", code="INVALID_REASONING_LEVEL")
            patch["reasoning_level"] = level
        if body.get("agent_profile_id") is not None:
            patch["agent_profile_id"] = str(body["agent_profile_id"]).strip()
        if not patch:
            return _err("no configuration values supplied", code="NOTHING_TO_SWITCH")

        updated = sess_store.update_runtime_overrides(wid, sid, **patch)
        await _rebuild_on_next_message(module, updated)
        return web.json_response(_sess_payload(updated))
    return handler

