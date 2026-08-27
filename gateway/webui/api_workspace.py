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
import time
import weakref
from pathlib import Path
from uuid import uuid4

from aiohttp import web

from gateway.webui.workspace_models import (
    VALID_CHAT_MODES,
    VALID_PERMISSION_MODES,
    VALID_REASONING_LEVELS,
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
from gateway.webui.glue import clear_cached_agent_context, send_and_wait

logger = logging.getLogger("jk_agent.gateway")

_PURPOSES = ("project_root", "working_directory", "extra_root")
_MAX_DIRECTORY_ENTRIES = 10000
_MAX_FILE_BYTES = 8 * 1024 * 1024


def _workspace_path(workspace: Workspace, relative: str) -> tuple[Path, Path]:
    """Resolve a user path below project_path and reject traversal/symlink escapes."""
    root = Path(workspace.project_path).expanduser().resolve()
    raw = str(relative or "").replace("\\", "/").lstrip("/")
    target = (root / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes workspace root") from exc
    return root, target


def _make_files(module):
    async def handler(request):
        store = _require(module, "workspace_store")
        try:
            workspace = store.get(request.match_info["workspace_id"])
        except WorkspaceNotFound as exc:
            return _handle_store_error(exc)
        try:
            root, target = _workspace_path(workspace, request.query.get("path", ""))
        except (OSError, ValueError) as exc:
            return _err(str(exc), status=400, code="INVALID_WORKSPACE_PATH")
        if not target.exists():
            return _err("directory does not exist", status=404, code="WORKSPACE_PATH_NOT_FOUND")
        if not target.is_dir():
            return _err("path is not a directory", status=400, code="WORKSPACE_NOT_DIRECTORY")
        try:
            children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            total = len(children)
            entries = []
            for child in children[:_MAX_DIRECTORY_ENTRIES]:
                try:
                    resolved = child.resolve()
                    resolved.relative_to(root)
                    stat = child.stat()
                except (OSError, ValueError):
                    continue
                entries.append({
                    "name": child.name,
                    "path": resolved.relative_to(root).as_posix(),
                    "kind": "directory" if child.is_dir() else "file",
                    "size": 0 if child.is_dir() else stat.st_size,
                    "modified_at": stat.st_mtime,
                })
        except OSError as exc:
            return _err(f"failed to list directory: {exc}", status=500,
                        code="WORKSPACE_DIRECTORY_READ_FAILED")
        return web.json_response({
            "workspace_id": workspace.workspace_id,
            "path": target.relative_to(root).as_posix() if target != root else "",
            "entries": entries,
            "total": total,
            "truncated": total > len(entries),
        })
    return handler


def _make_file(module):
    async def handler(request):
        store = _require(module, "workspace_store")
        try:
            workspace = store.get(request.match_info["workspace_id"])
        except WorkspaceNotFound as exc:
            return _handle_store_error(exc)
        try:
            root, target = _workspace_path(workspace, request.query.get("path", ""))
        except (OSError, ValueError) as exc:
            return _err(str(exc), status=400, code="INVALID_WORKSPACE_PATH")
        if not target.exists():
            return _err("file does not exist", status=404, code="WORKSPACE_PATH_NOT_FOUND")
        if not target.is_file():
            return _err("path is not a file", status=400, code="WORKSPACE_NOT_FILE")
        try:
            size = target.stat().st_size
            with target.open("rb") as stream:
                raw = stream.read(_MAX_FILE_BYTES + 1)
        except OSError as exc:
            return _err(f"failed to read file: {exc}", status=500,
                        code="WORKSPACE_FILE_READ_FAILED")
        truncated = len(raw) > _MAX_FILE_BYTES
        visible = raw[:_MAX_FILE_BYTES]
        if b"\x00" in visible:
            return _err("binary files cannot be previewed", status=415,
                        code="WORKSPACE_BINARY_FILE")
        try:
            content = visible.decode("utf-8")
        except UnicodeDecodeError:
            content = visible.decode("utf-8", errors="replace")
        return web.json_response({
            "workspace_id": workspace.workspace_id,
            "path": target.relative_to(root).as_posix(),
            "content": content,
            "size": size,
            "truncated": truncated,
            "encoding": "utf-8",
        })
    return handler


def register_routes(app: web.Application, module):
    app.router.add_get("/api/workspaces", _make_list(module))
    app.router.add_post("/api/workspaces/validate-path", _make_validate_path(module))
    app.router.add_post("/api/workspaces", _make_create(module))
    app.router.add_get("/api/workspaces/{workspace_id}", _make_get(module))
    app.router.add_put("/api/workspaces/{workspace_id}", _make_update(module))
    app.router.add_delete("/api/workspaces/{workspace_id}", _make_delete_workspace(module))
    app.router.add_post("/api/workspaces/{workspace_id}/archive", _make_archive(module))
    app.router.add_get("/api/workspaces/{workspace_id}/summary", _make_summary(module))
    app.router.add_get("/api/workspaces/{workspace_id}/files", _make_files(module))
    app.router.add_get("/api/workspaces/{workspace_id}/file", _make_file(module))
    app.router.add_get("/api/workspaces/{workspace_id}/sessions", _make_session_list(module))
    app.router.add_post("/api/workspaces/{workspace_id}/sessions", _make_session_create(module))
    app.router.add_get("/api/workspaces/{workspace_id}/sessions/{session_id}",
                       _make_session_get(module))
    app.router.add_put("/api/workspaces/{workspace_id}/sessions/{session_id}",
                       _make_session_update(module))
    app.router.add_post("/api/workspaces/{workspace_id}/sessions/{session_id}/archive",
                       _make_session_archive(module))
    # Phase 4：运行链路（统一会话已接管聊天，保留命令/计划/预览等管理端点）
    app.router.add_get(
        "/api/workspaces/{workspace_id}/sessions/{session_id}/commands",
        _make_commands(module))
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


def _parse_float(value, default=None):
    """安全解析浮点参数；缺失/空返回 default，非法值抛 ValueError（调用方转 400）。"""
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError("参数必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise ValueError(f"参数必须是数字，收到 {value!r}")


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

def _is_workspace_root(module, base: Path) -> bool:
    """base 是否属于已建工作区的根集合（project/working/extra roots）。

    限制相对路径解析基准只能来自已建工作区，防止用任意路径作为 base
    绕过工作区根边界校验。"""
    store = getattr(module, "workspace_store", None)
    if store is None:
        return False
    target = str(base.expanduser().resolve())
    try:
        workspaces = store.list(status="active", limit=200)
    except Exception:
        return False
    for w in workspaces:
        candidates = ([w.project_path, w.working_directory or w.project_path]
                      + list(w.extra_workspace_roots or []))
        for candidate in candidates:
            try:
                if str(Path(candidate).expanduser().resolve()) == target:
                    return True
            except (OSError, ValueError):
                continue
    return False


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
            # base 限制在已建工作区根集合内（防任意 base 解析相对路径绕过校验）
            if not _is_workspace_root(module, base):
                return _err(
                    "base 必须是已建工作区的根目录（project_path/"
                    "working_directory/extra_workspace_roots）",
                    code="INVALID_BASE")
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

def _history_message_payload(messages) -> list[dict]:
    """Convert a sliced Agent history page to a safe UI payload."""
    from core.message_store import _content_to_text

    out = []
    for source in messages:
        item = dict(source)
        content = item.get("content")
        if isinstance(content, list):
            item["content_text"] = _content_to_text(content)
        out.append(item)
    return out


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
    """PUT /api/workspaces/{id} —— 更新（受门禁约束）。

    - status 禁止直改（仅专用 archive/delete 端点变更状态）
    - project_path / working_directory / extra_workspace_roots 变更必须复用
      创建时的 PathValidator + risk_confirmed 流程
    - permission_mode / chat_mode / default_model 等枚举与字段白名单校验（400）
    """
    async def handler(request):
        store = _require(module, "workspace_store")
        wid = request.match_info["workspace_id"]
        body = await _body(request)
        expected_version = body.get("version")
        if "status" in body:
            return _err("status 只能通过 archive/delete 端点变更",
                        code="WORKSPACE_STATUS_IMMUTABLE")
        patch = {k: v for k, v in body.items()
                 if k in Workspace.__dataclass_fields__ and k != "workspace_id"}
        if not patch:
            return _err("没有可更新的字段", code="NOTHING_TO_UPDATE")
        # 枚举白名单：非法值 400 不落库
        if "permission_mode" in patch:
            mode = str(patch["permission_mode"]).strip()
            if mode not in VALID_PERMISSION_MODES:
                return _err("permission_mode 必须是 readonly/ask/allow/unreviewed",
                            code="INVALID_PERMISSION_MODE")
            patch["permission_mode"] = mode
        if "chat_mode" in patch:
            mode = str(patch["chat_mode"]).strip()
            if mode not in VALID_CHAT_MODES:
                return _err(f"chat_mode 必须是 {VALID_CHAT_MODES} 之一",
                            code="INVALID_CHAT_MODE")
            patch["chat_mode"] = mode
        for str_field in ("name", "description", "default_model",
                          "default_agent_profile_id"):
            if str_field in patch:
                value = str(patch[str_field]).strip()
                if not value:
                    return _err(f"{str_field} 不能为空", code="INVALID_FIELD")
                patch[str_field] = value
        for list_field in ("extra_workspace_roots", "include_tools", "exclude_tools",
                           "include_skills", "exclude_skills",
                           "include_mcp_servers", "exclude_mcp_servers"):
            if list_field in patch and not isinstance(patch[list_field], list):
                return _err(f"{list_field} 必须是数组", code="INVALID_LIST_FIELD")
        # 路径变更门禁：复用创建时的 PathValidator + risk_confirmed 流程
        if any(k in patch for k in
               ("project_path", "working_directory", "extra_workspace_roots")):
            try:
                current = store.get(wid)
            except WorkspaceNotFound as exc:
                return _handle_store_error(exc)
            validator = _validator(module)
            risk_confirmed = bool(body.get("risk_confirmed"))
            new_project = (str(patch.get("project_path") or "").strip()
                           or current.project_path)
            v = validator.validate(new_project, purpose="project_root",
                                   risk_confirmed=risk_confirmed)
            if v.blocked:
                return _err("；".join(v.reasons) or "路径被拒绝",
                            status=422, code="WORKSPACE_PATH_BLOCKED")
            if v.status == "warning" and not risk_confirmed:
                return _err("路径存在风险项，请确认后再修改",
                            status=422, code="WORKSPACE_PATH_CONFIRMATION_REQUIRED")
            if "project_path" in patch:
                patch["path_risk_level"] = v.risk_level
                patch["path_warnings"] = v.warnings
            roots = [Path(new_project)] + [
                Path(x) for x in (patch.get("extra_workspace_roots")
                                  if "extra_workspace_roots" in patch
                                  else current.extra_workspace_roots or [])]
            new_working = (str(patch.get("working_directory") or "").strip()
                          or new_project)
            vw = validator.validate(new_working, purpose="working_directory",
                                    allowed_roots=roots,
                                    risk_confirmed=risk_confirmed)
            if vw.blocked:
                return _err("；".join(vw.reasons) or "working_directory 无效",
                            status=422, code="WORKSPACE_PATH_BLOCKED")
            if "working_directory" in patch:
                patch["working_directory"] = new_working
            # 同路径冲突：project_path 变更后不得与其他工作区重复
            if "project_path" in patch:
                try:
                    existing = store.find_by_project_path(patch["project_path"])
                except Exception:
                    existing = None
                if existing is not None and existing.workspace_id != wid:
                    return _err(
                        f"已存在指向该路径的工作区: {existing.name}",
                        status=409, code="WORKSPACE_DUPLICATE_PATH")
        try:
            updated = store.update(wid, patch, expected_version=expected_version)
        except (WorkspaceNotFound, VersionConflict, ValidationError) as exc:
            return _handle_store_error(exc)
        except ValueError as exc:
            # 模型层非法枚举/路径（如 from_dict 校验）→ 400，不落库
            return _err(str(exc), 400, code="WORKSPACE_VALIDATION_ERROR")
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

# 弱引用缓存：module 被回收后自动释放（原 id(module) 字典会随进程泄漏）
_runtime_service_cache = weakref.WeakKeyDictionary()


def _runtime_service(module):
    """构造/复用 WorkspaceRuntimeService（按 module 弱引用缓存）。"""
    svc = None
    try:
        svc = _runtime_service_cache.get(module)
    except TypeError:
        svc = None  # 不可弱引用的测试桩等：每次重建
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
        try:
            _runtime_service_cache[module] = svc
        except TypeError:
            pass
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


def _make_commands(module):
    """Return slash-command completions in the workspace session capability scope."""
    async def handler(request):
        wid = request.match_info["workspace_id"]
        sid = request.match_info["session_id"]
        try:
            _require(module, "session_store").get_owned(wid, sid)
            snapshot = _runtime_service(module).build_snapshot(wid, sid)
        except (WorkspaceSessionNotFound, WorkspaceNotFound) as exc:
            return _handle_store_error(exc)
        except Exception as exc:
            logger.warning("构建 workspace commands snapshot 失败: %s", exc)
            return _err("运行配置解析失败", 422, "SNAPSHOT_BUILD_FAILED")
        from gateway.webui.api_chat import _skill_command_items, _skills_for_session
        commands = module.dispatcher.commands_table()
        commands.extend(_skill_command_items(_skills_for_session(
            module, "", snapshot.skills)))
        return web.json_response({"commands": commands})
    return handler


def build_workspace_entry_context(module, wid: str, sid: str) -> dict:
    """构建工作区会话的统一链路运行上下文（设计方案：工作区执行上下文集成）。

    供 ConversationTurnRunner 的 workspace provider 使用——工作区会话经统一
    链路（Conversation/Turn）执行时，挂接与旧漏斗一致的可选 dict：
    {runtime_context, snapshot_id, model, permission_mode, reasoning_level,
     max_steps, mcp_servers, profile_prompt, allowed_tools, allowed_skills}；
    会话/工作区不存在或快照构建失败时返回 None（保持默认能力子集）。
    """
    try:
        sess_store = _require(module, "session_store")
        ws_store = _require(module, "workspace_store")
        session = sess_store.get_owned(wid, sid)
        if session.status != "active":
            return None
        ws_store.get(wid)
        service = _runtime_service(module)
        snapshot = service.build_snapshot(wid, sid)
        ctx = service.build_runtime_context(snapshot)
        profile = service.load_profile(snapshot.agent_profile_id)
        return {
            "runtime_context": ctx,
            "snapshot_id": snapshot.snapshot_id,
            "model": snapshot.model,
            "permission_mode": snapshot.permission_mode,
            "reasoning_level": snapshot.reasoning_level,
            "max_steps": service.resolve(
                service.load_workspace(wid), profile, session).max_steps,
            "mcp_servers": _filter_mcp_servers(module, snapshot.mcp_servers),
            "profile_prompt": profile.system_prompt if profile else None,
            "allowed_tools": snapshot.tools,
            "allowed_skills": snapshot.skills,
        }
    except (WorkspaceSessionNotFound, WorkspaceNotFound):
        # 可恢复错误：会话/工作区不存在或已归档 → 默认档位（不阻断执行）
        logger.debug("workspace entry context 不可用: %s/%s", wid, sid)
        return None
    except ValueError as exc:
        # 持久化配置损坏（模型/权限/路径等非法值）：拒绝执行，不静默降级
        logger.warning("workspace 持久化配置损坏，拒绝执行: %s/%s (%s)",
                       wid, sid, exc)
        raise web.HTTPUnprocessableEntity(
            text=json.dumps({"error": f"运行配置损坏: {exc}",
                             "code": "WORKSPACE_CONFIG_CORRUPT"},
                            ensure_ascii=False)) from exc
    except Exception as exc:
        # 可恢复错误：快照构建/上下文解析失败 → 日志 + 默认档位
        logger.warning("构建工作区执行上下文失败，降级默认档位: %s/%s (%s)",
                       wid, sid, type(exc).__name__)
        return None


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
        # busy 条件更新：并发请求只有首个获得令牌（UPDATE ... WHERE is_busy=0）
        if not sess_store.try_set_busy(wid, sid):
            return _err("session is busy", 409, "WORKSPACE_SESSION_BUSY")
        busy_acquired = True
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
            try:
                timeout = _parse_float(body.get("timeout"), default=300.0)
            except ValueError:
                return _err("timeout 必须是数字", code="INVALID_TIMEOUT")
            result = await send_and_wait(
                module, session.session_key, f"/plan-preview {text}",
                timeout=min(timeout, 600),
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
            if not module.dispatcher.task_runtime_enabled:
                return _err("TaskRuntime is disabled", 409, "TASK_RUNTIME_DISABLED")
            plan = module.glue.create_plan(session.session_key, text, preview["plan"])
            # H-B 协调项：尊重 gateway.plan.require_approval——开启时两阶段，
            # 此处返回 awaiting_approval 交由用户在 Plan 页确认。
            require_approval = bool(((module.config.get("gateway") or {})
                                     .get("plan") or {}).get("require_approval", False))
            if require_approval:
                module.bus.publish("plan.changed", {
                    "action": "awaiting_approval",
                    "plan": plan.to_dict(),
                    "session_key": session.session_key,
                    "workspace_id": wid,
                    "workspace_session_id": sid,
                })
                return web.json_response({
                    "ok": True, "started": False, "awaiting_approval": True,
                    "plan_id": plan.plan_id, "plan": plan.to_dict(),
                    "session_key": session.session_key,
                }, status=202)
            plan = module.glue.plan_manager.approve(plan.plan_id, actor="automatic")
            module.bus.publish("plan.changed", {
                "action": "approved",
                "plan": plan.to_dict(),
                "session_key": session.session_key,
                "workspace_id": wid,
                "workspace_session_id": sid,
            })
            module.plan_runtime.start(plan.plan_id)
            return web.json_response({
                "ok": True, "started": True, "plan_id": plan.plan_id,
                "plan": plan.to_dict(), "tasks": preview.get("tasks") or [],
                "snapshot_id": snapshot.snapshot_id, "session_key": session.session_key,
            }, status=202)
        finally:
            # 仅清除自身持有的 busy 令牌（token/owner 语义，防止误清他人持有）
            if busy_acquired:
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
                mcp_tools=prompt_preview.live_mcp_tools(module, config.mcp_servers),
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


# L5-P0-2：未加载 Agent 时整历史读取的短路缓存（runtime status 端点被前端
# 每 ~5s 轮询；原实现每次 read_text + json.loads + MessageStore 重建 + stats()）。
# 方案：If-None-Match（文件 mtime 指纹 ETag，客户端持有则 304）+ 最小间隔缓存
# （同 mtime 且 TTL 内直接复用上次解析结果）。
_history_stats_cache: dict = {}
_HISTORY_STATS_TTL = 2.0            # 秒：最小间隔缓存窗口
_HISTORY_ETAG_PREFIX = "ws-ctx-"


def _history_etag(history_file: Path):
    """整历史文件的 mtime 指纹 ETag；文件不存在返回 None。"""
    try:
        mtime = history_file.stat().st_mtime
    except OSError:
        return None
    return f'"{_HISTORY_ETAG_PREFIX}{history_file.name}:{mtime:.6f}"'


def _load_history_context(session_id: str, history_file: Path):
    """带 mtime 指纹 + 最小间隔缓存的整历史上下文读取（L5-P0-2）。

    key = session_id；命中条件 = 文件 mtime 未变 且 距上次解析 < TTL →
    直接复用上次的 store.stats() 结果，跳过昂贵的文件读取与解析。
    """
    now = time.time()
    try:
        mtime = history_file.stat().st_mtime
    except OSError:
        mtime = None
    cached = _history_stats_cache.get(session_id)
    if cached is not None:
        c_mtime, c_at, c_ctx = cached
        if c_mtime == mtime and (now - c_at) < _HISTORY_STATS_TTL:
            return c_ctx
    ctx = None
    if mtime is not None:
        try:
            from core.message_store import MessageStore
            payload = json.loads(history_file.read_text(encoding="utf-8"))
            store = MessageStore(session_id=session_id)
            store.load_session_data(payload)
            ctx = store.stats()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            ctx = None
    _history_stats_cache[session_id] = (mtime, now, ctx)
    if len(_history_stats_cache) > 512:
        # 防长驻进程无限增长：超限整体清空，下次按需重建
        _history_stats_cache.clear()
    return ctx


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
        context = None
        if entry is not None and entry.agent is not None and hasattr(entry.agent, "store"):
            try:
                context = entry.agent.store.stats()
            except Exception:
                context = None
        # A workspace session is lazily loaded.  Still expose persisted history
        # statistics before the first message so the context tooltip does not
        # permanently claim that the session has no active context.
        etag = None
        if context is None:
            try:
                from core.message_store import DEFAULT_SESSION_DIR
                history_file = Path(DEFAULT_SESSION_DIR) / f"{session.session_id}.json"
                # L5-P0-2：If-None-Match/mtime 短路 —— 文件未变化且客户端已持有
                # 上下文（每 ~5s 轮询的常态）→ 直接 304，跳过整历史读取。
                etag = _history_etag(history_file)
                if (etag is not None
                        and request.headers.get("If-None-Match") == etag):
                    raise web.HTTPNotModified(headers={"ETag": etag})
                if history_file.exists():
                    # 最小间隔缓存：同 mtime 且 TTL 内复用上次 stats() 结果
                    context = _load_history_context(
                        session.session_id, history_file)
            except web.HTTPNotModified:
                raise
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                context = None
        # agent 未加载时，persisted store.stats() 的 max_tokens/model_context_length
        # 为 0，导致"上下文详情"显示 0% 且缺预算行（已用几十K却占用 0%）。
        # 用 config 中该模型的 context_length 回填，让占用/预算有意义。
        if context is not None:
            try:
                from core.config_loader import load_config
                from agent import _history_budget
                _model = snapshot.model if snapshot else session.model
                mcfg = load_config().get("llm", {}).get("models", {}).get(_model or "", {})
                ctx_len = int(mcfg.get("context_length") or 0)
                if ctx_len > 0 and not context.get("model_context_length"):
                    budget = _history_budget(ctx_len)
                    context = dict(context)
                    context["model_context_length"] = ctx_len
                    context["max_tokens"] = budget
                    context["usage_ratio"] = ((context.get("total_tokens") or 0) / budget
                                              if budget > 0 else 0)
                    context["remaining_tokens"] = max(
                        0, budget - (context.get("total_tokens") or 0))
            except Exception:
                pass
        resp = web.json_response({
            "workspace_id": wid,
            "context": context,
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
        if etag is not None:
            # L5-P0-2：下发 ETag 供客户端下次 If-None-Match 短路
            resp.headers["ETag"] = etag
        return resp
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
        # 枚举白名单与 _make_switch 对齐：非法值 400 不落库
        model = body.get("model") or None
        if model is not None:
            model = str(model).strip()
            if not model:
                return _err("model 不能为空", code="INVALID_MODEL")
        permission_mode = body.get("permission_mode") or None
        if permission_mode is not None:
            permission_mode = str(permission_mode).strip()
            if permission_mode not in VALID_PERMISSION_MODES:
                return _err("permission_mode 必须是 readonly/ask/allow/unreviewed",
                            code="INVALID_PERMISSION_MODE")
        chat_mode = body.get("chat_mode") or None
        if chat_mode is not None:
            chat_mode = str(chat_mode).strip()
            if chat_mode not in VALID_CHAT_MODES:
                return _err("chat_mode must be chat; Plan is a structured capability",
                            code="INVALID_CHAT_MODE")
        reasoning_level = body.get("reasoning_level") or None
        if reasoning_level is not None:
            reasoning_level = str(reasoning_level).strip().lower()
            if reasoning_level not in VALID_REASONING_LEVELS:
                return _err(f"reasoning_level 必须是 {VALID_REASONING_LEVELS} 之一",
                            code="INVALID_REASONING_LEVEL")
        # bump client_config_version（乐观版本）
        try:
            new_session = sess_store.update_runtime_overrides(
                wid, sid,
                model=model,
                permission_mode=permission_mode,
                chat_mode=chat_mode,
                reasoning_level=reasoning_level,
            )
        except (WorkspaceSessionNotFound, ValidationError, StoreBusy,
                WorkspaceStoreError) as exc:
            return _handle_store_error(exc)
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
    # A configuration switch must not run the general SessionManager eviction
    # lifecycle. That lifecycle persists/closes unrelated resources and can
    # trip database foreign-key relationships while the Workspace session row is
    # being updated. The workspace runtime already has an explicit stale marker;
    # clearing only the resident Agent is sufficient to force Dispatcher to build
    # the next turn from the newly persisted snapshot.
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
        # 停止是幂等操作：会话未加载/未运行时也联动停止 plan/goal 并返回 ok，
        # 而不是 404（否则前端"停止"按钮在空闲会话上点一下报 404，观感像没生效）。
        dispatcher = getattr(module, "dispatcher", None)
        if entry is None or entry.agent is None:
            if dispatcher is not None:
                try:
                    await dispatcher.stop_session_runtime(session.session_key)
                except Exception:
                    logger.exception("停止会话：联动取消 Plan/Goal 失败 %s", session.session_key)
            module.bus.publish("chat.stop_requested", {
                "session_key": session.session_key,
                "workspace_id": wid, "workspace_session_id": sid,
            })
            return web.json_response({"ok": True, "stopped": False,
                                      "note": "会话未在运行（已幂等停止）"})
        entry.agent.request_stop()
        # 停止会话 = 同时暂停该会话正在运行的 Plan/Goal 后台任务，
        # 否则 goal/plan 仍占用会话（entry.is_busy），用户无法重新输入。
        if dispatcher is not None:
            try:
                await dispatcher.stop_session_runtime(session.session_key)
            except Exception:
                logger.exception("停止会话：联动取消 Plan/Goal 失败 %s", session.session_key)
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
        # 统一模型：清空该会话对应的 Conversation（设计方案：会话管理统一化）。
        # 工作区会话数据源已切换到 Conversation/Turn/Node，旧 agent.clear_history
        # 只清内存 Agent，不清统一持久化数据。
        svc = getattr(module, "conversation_service", None)
        if svc is not None:
            conv = svc.store.get_conversation_by_key(session.session_key)
            if conv is not None:
                svc.clear_history(conv.conversation_id)
                # 清空会话时联动清理该会话的 Plan/Goal：停止运行中任务、
                # 删除业务记录与 system:plan/goal 系统会话，避免界面残留。
                await _clear_session_runtime(module, session.session_key)
                # 联动清空驻留 Agent 的内存上下文（此前 conv 分支提前返回，
                # 下面的 agent 清理永远不会执行，/clear 后旧上下文仍在）。
                await clear_cached_agent_context(module, session.session_key)
                return web.json_response({"ok": True, "session_key": session.session_key,
                                          "reply": "✅ 会话已清空"})
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


async def _clear_session_runtime(module, session_key: str) -> None:
    """清空会话时联动清理该会话的 Plan/Goal 后台任务与业务记录。

    停止运行中的 plan/goal，删除其业务记录；plan/goal 轮次已落在父会话
    runtime 节点（不再有 system 会话/投影），无需额外清理。"""
    dispatcher = getattr(module, "dispatcher", None)
    svc = getattr(module, "conversation_service", None)
    if dispatcher is None:
        return
    removed = {"plans": [], "goals": []}
    try:
        removed = await dispatcher.clear_session_runtime(session_key)
    except Exception:
        logger.exception("清空会话：停止/清理 Plan/Goal 失败 %s", session_key)
    if svc is not None:
        for plan_id in removed.get("plans", []):
            try:
                module.bus.publish("plan.changed", {
                    "action": "archived", "session_key": session_key,
                    "plan": {"plan_id": plan_id, "status": "archived"}})
            except Exception:
                pass
        for goal_id in removed.get("goals", []):
            try:
                module.bus.publish("goal.changed", {
                    "action": "archived", "session_key": session_key,
                    "goal": {"goal_id": goal_id, "status": "archived"}})
            except Exception:
                pass


def _purge_conversation_records(module, session_key: str) -> dict:
    """删除工作区会话的统一会话数据（主 + subagent 子代），返回统计。

    与主会话删除（api_chat._make_delete）对齐：archive 只改
    workspace_sessions 行，conversation_sessions / turns / turn_nodes /
    tool_results 及该会话派生的 subagent 子会话需在此级联删除。
    先删子代会话再删主会话；单个删除失败仅记 warning 并继续——
    归档已成功，不让整个 DELETE 因清理失败而报错。"""
    svc = getattr(module, "conversation_service", None)
    if svc is None:
        return {"removed_conversations": 0}
    child_prefix = f"subagent:{session_key}:"
    removed = 0
    try:
        child_keys = svc.list_conversation_keys_with_prefix(child_prefix)
    except Exception as exc:
        logger.warning("查询工作区子会话失败 %s: %s", child_prefix, exc)
        child_keys = []
    for child_key in child_keys:
        try:
            if svc.delete_conversation_by_key(child_key):
                removed += 1
        except Exception as exc:
            logger.warning("删除工作区子会话数据失败 %s: %s", child_key, exc)
    try:
        if svc.delete_conversation_by_key(session_key):
            removed += 1
    except Exception as exc:
        logger.warning("删除工作区主会话数据失败 %s: %s", session_key, exc)
    return {"removed_conversations": removed}


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
        # 归档成功后级联清理统一会话数据，避免永久残留
        cleanup = _purge_conversation_records(module, session.session_key)
        # P4 资源卫生：移除该会话的 Goal 仲裁锁（未被持有时），防止
        # _session_gates 随历史会话数在长生命周期网关中缓慢累积。
        goal_driver = getattr(module, "goal_driver", None)
        if goal_driver is not None:
            try:
                goal_driver.prune_session_gates([session.session_key])
            except Exception as exc:
                logger.debug("prune_session_gates 失败（忽略）: %s", exc)
        return web.json_response({"ok": True,
                                  "session": _sess_payload(archived),
                                  **cleanup})
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
            if mode != "chat":
                return _err("chat_mode must be chat; Plan is a structured capability", code="INVALID_CHAT_MODE")
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

        try:
            updated = sess_store.update_runtime_overrides(wid, sid, **patch)
        except (WorkspaceSessionNotFound, ValidationError, StoreBusy, WorkspaceStoreError) as exc:
            return _handle_store_error(exc)
        try:
            await _rebuild_on_next_message(module, updated)
        except Exception as exc:  # configuration is safely persisted; rebuild is best-effort
            logger.exception("workspace session runtime release failed: %s", exc)
            return _err("配置已保存，但运行时重建失败: " + str(exc),
                        503, "WORKSPACE_RUNTIME_REBUILD_FAILED")
        return web.json_response(_sess_payload(updated))
    return handler

