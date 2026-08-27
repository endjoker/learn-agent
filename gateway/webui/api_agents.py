# -*- coding: utf-8 -*-
"""
api_agents.py —— Agent Profile CRUD / duplicate / references / preview
+ Tools/Skills/MCP/Models Catalog（Phase 2）。

- 统一 JSON 解析、错误响应（error 为字符串 + 稳定 code）。
- 乐观锁版本冲突返回 409。
- Catalog 全部脱敏，系统保留工具不可选。
"""

import json
import logging
from pathlib import Path

from aiohttp import web

from gateway.webui.workspace_models import AgentProfile
from gateway.webui.workspace_store import (
    AgentProfileNotFound,
    ReferenceConflict,
    ValidationError,
    VersionConflict,
    WorkspaceStoreError,
)
from gateway.webui import catalog_service
from gateway.webui import prompt_preview

logger = logging.getLogger("jk_agent.gateway")


def register_routes(app: web.Application, module):
    app.router.add_get("/api/agents", _make_list(module))
    app.router.add_post("/api/agents", _make_create(module))
    app.router.add_get("/api/agents/catalog", _make_catalog(module))
    app.router.add_post("/api/agents/preview", _make_preview(module))
    app.router.add_get("/api/agents/{profile_id}", _make_get(module))
    app.router.add_put("/api/agents/{profile_id}", _make_update(module))
    app.router.add_post("/api/agents/{profile_id}/duplicate", _make_duplicate(module))
    app.router.add_get("/api/agents/{profile_id}/references", _make_references(module))
    app.router.add_post("/api/agents/{profile_id}/archive", _make_archive(module))
    app.router.add_post("/api/agents/{profile_id}/activate", _make_activate(module))
    app.router.add_delete("/api/agents/{profile_id}", _make_delete(module))


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


def _require_store(module):
    if module.profile_store is None:
        raise web.HTTPServiceUnavailable(text=json.dumps(
            {"error": "Workspace 存储未就绪", "code": "WORKSPACE_STORE_UNAVAILABLE"},
            ensure_ascii=False))
    return module.profile_store


def _payload(profile: AgentProfile) -> dict:
    return profile.to_dict()


# ---------- 列表 / 创建 ----------

def _make_list(module):
    async def handler(request):
        store = _require_store(module)
        try:
            status = request.query.get("status", "active")
            q = request.query.get("q", "")
            limit = int(request.query.get("limit", 50))
            offset = int(request.query.get("offset", 0))
        except (TypeError, ValueError):
            return _err("limit/offset 必须是整数", code="INVALID_PAGINATION")
        try:
            items = store.list(status=status, q=q, limit=limit, offset=offset)
            total = store.count(status=status, q=q)
        except WorkspaceStoreError as exc:
            logger.exception("智能体列表读取失败")
            return _handle_store_error(exc)
        except (TypeError, ValueError) as exc:
            logger.exception("智能体列表包含无效记录")
            return _err(str(exc), 500, "AGENT_PROFILE_DATA_INVALID")
        return web.json_response({
            "agents": [_payload(p) for p in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    return handler


def _make_create(module):
    async def handler(request):
        store = _require_store(module)
        body = await _body(request)
        body = {k: v for k, v in body.items()
                if k in AgentProfile.__dataclass_fields__}
        body["is_system"] = False  # 内置标记只由系统 seed 设置
        import uuid as _uuid
        body["profile_id"] = body.get("profile_id") or f"agent_{_uuid.uuid4().hex[:8]}"
        if not body.get("name"):
            return _err("name 为必填项", code="NAME_REQUIRED")
        try:
            profile = AgentProfile.from_dict(body)
            created = store.create(profile)
        except ValidationError as exc:
            return _handle_store_error(exc)
        except WorkspaceStoreError as exc:
            return _handle_store_error(exc)
        return web.json_response(_payload(created), status=201)
    return handler


# ---------- 详情 / 更新 / 复制 / 引用 / 归档 ----------

def _make_get(module):
    async def handler(request):
        store = _require_store(module)
        profile_id = request.match_info["profile_id"]
        try:
            profile = store.get(profile_id)
        except AgentProfileNotFound as exc:
            return _handle_store_error(exc)
        return web.json_response(_payload(profile))
    return handler


def _make_update(module):
    async def handler(request):
        store = _require_store(module)
        profile_id = request.match_info["profile_id"]
        body = await _body(request)
        expected_version = body.get("version")
        patch = {k: v for k, v in body.items()
                 if k in AgentProfile.__dataclass_fields__
                 and k not in ("profile_id", "is_system")}
        try:
            store.get(profile_id)  # validates existence before versioned update
            updated = store.update(profile_id, patch,
                                   expected_version=expected_version)
        except (AgentProfileNotFound, VersionConflict, ValidationError) as exc:
            return _handle_store_error(exc)
        except WorkspaceStoreError as exc:
            return _handle_store_error(exc)
        return web.json_response(_payload(updated))
    return handler


def _make_duplicate(module):
    async def handler(request):
        store = _require_store(module)
        profile_id = request.match_info["profile_id"]
        body = await _body(request)
        try:
            dup = store.duplicate(profile_id, name=body.get("name"))
        except AgentProfileNotFound as exc:
            return _handle_store_error(exc)
        except ValidationError as exc:
            return _handle_store_error(exc)
        return web.json_response(_payload(dup), status=201)
    return handler


def _make_references(module):
    async def handler(request):
        store = _require_store(module)
        profile_id = request.match_info["profile_id"]
        try:
            refs = store.references(profile_id)
        except AgentProfileNotFound as exc:
            return _handle_store_error(exc)
        return web.json_response({
            "references": [{"workspace_id": w.workspace_id, "name": w.name,
                            "status": w.status} for w in refs]})
    return handler


def _make_archive(module):
    async def handler(request):
        store = _require_store(module)
        profile_id = request.match_info["profile_id"]
        body = await _body(request)
        try:
            archived = store.archive(profile_id, expected_version=body.get("version"))
        except (AgentProfileNotFound, VersionConflict, ReferenceConflict, ValidationError) as exc:
            return _handle_store_error(exc)
        return web.json_response(_payload(archived))
    return handler


def _make_activate(module):
    async def handler(request):
        store = _require_store(module)
        profile_id = request.match_info["profile_id"]
        body = await _body(request)
        try:
            activated = store.activate(profile_id, expected_version=body.get("version"))
        except (AgentProfileNotFound, VersionConflict, ReferenceConflict, ValidationError) as exc:
            return _handle_store_error(exc)
        return web.json_response(_payload(activated))
    return handler


def _make_delete(module):
    async def handler(request):
        store = _require_store(module)
        profile_id = request.match_info["profile_id"]
        body = await _body(request)
        try:
            deleted = store.delete(profile_id, expected_version=body.get("version"))
        except (AgentProfileNotFound, VersionConflict, ReferenceConflict, ValidationError) as exc:
            return _handle_store_error(exc)
        return web.json_response(_payload(deleted))
    return handler


# ---------- Preview ----------

def _make_preview(module):
    async def handler(request):
        _require_store(module)
        body = await _body(request)
        try:
            profile = AgentProfile.from_dict({
                **body.get("profile", {}),
                "profile_id": body.get("profile", {}).get("profile_id") or "preview",
            })
        except ValueError as exc:
            return _err(str(exc), code="INVALID_PROFILE")
        # B8：复用 catalog_service 的注册表单例（只读）与 SkillManager 单例，
        # 避免每次 preview 重建 ~40 个内置工具实例与重复读盘解析。
        registry = catalog_service.get_catalog_registry()
        skill_mgr = catalog_service.get_skills_manager()
        try:
            skill_mgr.load_all()
        except Exception:
            pass
        try:
            from core.config_loader import _find_project_root, load_config
            project_root = body.get("project_root") or str(_find_project_root())
            framework_root = body.get("framework_root") or project_root
            working_directory = body.get("working_directory")
            if not working_directory:
                config = load_config()
                configured = (config.get("permission", {}).get("workspace")
                              or "./workspace")
                configured_path = Path(configured)
                if not configured_path.is_absolute():
                    configured_path = _find_project_root() / configured_path
                working_directory = str(configured_path.resolve())
            selected_mcp = list(getattr(profile, "mcp_servers", None) or [])
            live_mcp_tools = prompt_preview.live_mcp_tools(module, selected_mcp)
            result = prompt_preview.build_preview(
                profile,
                workspace=body.get("workspace"),
                session=body.get("session"),
                tool_registry=registry,
                skill_manager=skill_mgr,
                framework_root=framework_root,
                project_root=project_root,
                working_directory=working_directory,
                mcp_tools=live_mcp_tools,
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("preview failed")
            return _err(str(exc), status=500, code="PREVIEW_FAILED")
        return web.json_response(result)
    return handler


# ---------- Catalog ----------

def _make_catalog(module):
    async def handler(request):
        _require_store(module)
        # B8：get_all_catalogs 内部 tools/skills 已走缓存
        # （get_tools_catalog TTL 30s + SkillManager mtime 签名失效）
        return web.json_response(catalog_service.get_all_catalogs(module))
    return handler
