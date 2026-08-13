"""Authenticated WebUI APIs for durable ArtifactStore content."""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from gateway.dispatcher import Dispatcher


def register_routes(app: web.Application, module) -> None:
    app.router.add_get("/api/artifacts", _make_list(module))
    app.router.add_post("/api/artifacts/text", _make_create_text(module))
    app.router.add_get("/api/artifacts/{artifact_id}", _make_get(module))
    app.router.add_get("/api/artifacts/{artifact_id}/download", _make_download(module))


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (json.JSONDecodeError, Exception):
        return {}
    return value if isinstance(value, dict) else {}


def _make_list(module):
    async def handler(request: web.Request) -> web.Response:
        session_key = (request.query.get("session_key") or "").strip()
        session_id = Dispatcher._runtime_session_id(session_key) if session_key else None
        try:
            limit = max(1, min(int(request.query.get("limit", 100)), 1000))
        except ValueError:
            return _error("limit 必须是整数")
        values = module.artifact_store.list(
            session_id=session_id, task_id=request.query.get("task_id") or None, limit=limit,
        )
        return web.json_response({"artifacts": [item.to_dict() for item in values]})
    return handler


def _make_create_text(module):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        session_key = str(body.get("session_key") or "").strip()
        name = str(body.get("name") or "").strip()
        content = body.get("content")
        if not session_key or not name or not isinstance(content, str):
            return _error("session_key、name 和文本 content 为必填项")
        try:
            artifact = module.artifact_store.create_text(
                session_id=Dispatcher._runtime_session_id(session_key), name=name, content=content,
                type=str(body.get("type") or "report"), summary=str(body.get("summary") or ""),
                media_type=str(body.get("media_type") or "text/markdown"),
                plan_id=body.get("plan_id") or None,
                plan_task_id=body.get("plan_task_id") or None, task_id=body.get("task_id") or None,
                created_by="webui",
            )
        except (TypeError, ValueError) as exc:
            return _error(str(exc), 409)
        module.bus.publish("artifact.created", {"artifact": artifact.to_dict()})
        return web.json_response({"artifact": artifact.to_dict()}, status=201)
    return handler


def _make_get(module):
    async def handler(request: web.Request) -> web.Response:
        artifact = module.artifact_store.get(request.match_info["artifact_id"])
        if artifact is None:
            return _error("Artifact 不存在", 404)
        return web.json_response({"artifact": artifact.to_dict()})
    return handler


def _make_download(module):
    async def handler(request: web.Request) -> web.StreamResponse:
        artifact = module.artifact_store.get(request.match_info["artifact_id"])
        if artifact is None:
            return _error("Artifact 不存在", 404)
        try:
            path = module.artifact_store.resolve_path(artifact)
        except FileNotFoundError:
            return _error("Artifact 文件已不存在", 410)
        except (KeyError, ValueError):
            return _error("Artifact 路径无效", 409)
        response = web.FileResponse(path)
        response.content_type = artifact.media_type
        response.headers["Content-Disposition"] = f'attachment; filename="{artifact.name}"'
        return response
    return handler
