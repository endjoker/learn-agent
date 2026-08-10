# -*- coding: utf-8 -*-
"""
api_chat.py —— 会话页全部 REST 端点（P3b）

chat / sessions / history / delete / clear / model / permission /
plan 两阶段 / approvals / commands / modes
"""

import json
import logging

from aiohttp import web

from gateway.channels.base import InboundMessage
from gateway.webui.glue import send_and_wait

logger = logging.getLogger("hello_agent.gateway")

_MODES = [
    {"id": "chat", "label": "会话", "available": True},
    {"id": "plan", "label": "plan", "available": True},
    {"id": "code", "label": "编程", "available": False},
    {"id": "team", "label": "团队", "available": False},
]


def register_routes(app: web.Application, module):
    app.router.add_get("/api/sessions", _make_sessions(module))
    app.router.add_get("/api/sessions/{key}/history", _make_history(module))
    app.router.add_delete("/api/sessions/{key}", _make_delete(module))
    app.router.add_post("/api/sessions/{key}/clear", _make_clear(module))
    app.router.add_post("/api/chat", _make_chat(module))
    app.router.add_post("/api/sessions/{key}/stop", _make_stop(module))
    app.router.add_post("/api/sessions/{key}/model", _make_model(module))
    app.router.add_post("/api/sessions/{key}/permission", _make_perm_set(module))
    app.router.add_get("/api/sessions/{key}/permission", _make_perm_get(module))
    app.router.add_get("/api/modes", _make_modes(module))
    app.router.add_post("/api/plan", _make_plan(module))
    app.router.add_post("/api/plan/{plan_id}/approve", _make_plan_approve(module))
    app.router.add_post("/api/plan/{plan_id}/reject", _make_plan_reject(module))
    app.router.add_get("/api/approvals", _make_approvals(module))
    app.router.add_post("/api/approvals/{aid}", _make_approval_answer(module))
    app.router.add_get("/api/commands", _make_commands(module))


def _err(text, status=400):
    return web.json_response({"error": text}, status=status)


async def _body(request) -> dict:
    try:
        return await request.json()
    except (json.JSONDecodeError, Exception):
        return {}


# ---------- 会话列表 / 历史 ----------

def _make_sessions(module):
    async def handler(request):
        from gateway.agent_factory import _load_map
        from core.message_store import MessageStore

        memory = {e["session_key"]: e for e in module.session_mgr.list_entries()}
        out = [dict(e, source="memory") for e in memory.values()]

        # 磁盘：sessions_map 里有、内存里没有的
        try:
            file_index = {f["session_id"]: f
                          for f in MessageStore.list_session_files()}
        except Exception:
            file_index = {}
        for key, meta in _load_map().items():
            if key in memory:
                continue
            sid = meta.get("session_id") if isinstance(meta, dict) else meta
            finfo = file_index.get(sid, {})
            out.append({
                "session_key": key,
                "session_id": sid or "",
                "model": meta.get("model", "") if isinstance(meta, dict) else "",
                "message_count": finfo.get("message_count", 0),
                "is_busy": False,
                "created_at": finfo.get("created_at", 0),
                "last_active": finfo.get("created_at", 0),
                "loaded": False,
                "source": "disk",
            })
        return web.json_response({"sessions": out})
    return handler


def _make_history(module):
    async def handler(request):
        from gateway.agent_factory import _load_map
        from core.message_store import DEFAULT_SESSION_DIR, _content_to_text
        from pathlib import Path

        key = request.match_info["key"]
        limit = min(int(request.query.get("limit", 200)), 1000)

        # 内存会话优先：直接取 agent.messages（比磁盘实时，命令路径不落盘也能读）
        entry = module.session_mgr._sessions.get(key)
        if entry is not None and entry.agent is not None:
            agent = entry.agent
            messages = []
            for m in agent.messages[-limit:]:
                item = dict(m)
                c = item.get("content")
                if isinstance(c, list):
                    item["content_text"] = _content_to_text(c)
                messages.append(item)
            return web.json_response({
                "session_key": key,
                "session_id": agent.store.session_id,
                "messages": messages,
            })

        # 磁盘会话：经 sessions_map 拿 session_id 读文件
        meta = _load_map().get(key)
        sid = meta.get("session_id") if isinstance(meta, dict) else meta
        if not sid:
            return _err("会话不存在", 404)
        f = Path(DEFAULT_SESSION_DIR) / f"{sid}.json"
        if not f.exists():
            return _err("会话文件不存在", 404)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return _err(f"读取失败: {e}", 500)
        messages = data.get("messages", [])[-limit:]
        for m in messages:
            c = m.get("content")
            if isinstance(c, list):
                m["content_text"] = _content_to_text(c)
        return web.json_response({"session_key": key, "session_id": sid,
                                  "messages": messages})
    return handler


# ---------- 删除 / 清空 ----------

def _make_delete(module):
    async def handler(request):
        from gateway.agent_factory import _load_map, remove_map_entry
        from core.message_store import MessageStore

        key = request.match_info["key"]
        if module.session_mgr.is_busy(key):
            return _err("会话正忙，无法删除", 409)
        meta = _load_map().get(key)
        sid = meta.get("session_id") if isinstance(meta, dict) else meta
        removed = await module.session_mgr.evict(key, save=False)
        remove_map_entry(key)
        if sid:
            MessageStore.delete_session_file(sid)
        module.bus.publish("session.evicted",
                           {"session_key": key, "reason": "delete"})
        return web.json_response({"ok": True, "removed_from_memory": removed})
    return handler


def _make_clear(module):
    async def handler(request):
        key = request.match_info["key"]
        result = await send_and_wait(module, key, "/clear", timeout=60)
        status = 200 if result["ok"] else 504
        return web.json_response(result, status=status)
    return handler


# ---------- 聊天 / 模型 / 权限 ----------

def _make_chat(module):
    async def handler(request):
        body = await _body(request)
        text = (body.get("text") or "").strip()
        images = body.get("images") or []
        if not text and not images:
            return _err("text 不能为空")
        session_key = body.get("session_key") or "webui:default"
        timeout = min(float(body.get("timeout", 120)), 600)
        # 组装多模态图片块（base64）
        img_blocks = []
        for im in images:
            if isinstance(im, dict) and im.get("data"):
                img_blocks.append({
                    "type": "image",
                    "source": "base64",
                    "media_type": im.get("media_type", "image/png"),
                    "data": im["data"],
                })
        module.bus.publish("chat.started",
                           {"session_key": session_key, "text_len": len(text)})
        result = await send_and_wait(module, session_key, text,
                                     timeout=timeout, images=img_blocks or None)
        status = 200 if result["ok"] else 504
        return web.json_response(result, status=status)
    return handler


def _make_stop(module):
    async def handler(request):
        key = request.match_info["key"]
        entry = module.session_mgr._sessions.get(key)
        if entry is None or entry.agent is None:
            return _err("会话未加载或未在运行", 404)
        entry.agent.request_stop()
        module.bus.publish("chat.stop_requested", {"session_key": key})
        return web.json_response({"ok": True, "session_key": key})
    return handler


def _make_model(module):
    async def handler(request):
        key = request.match_info["key"]
        body = await _body(request)
        model = (body.get("model") or "").strip()
        if not model:
            return _err("model 不能为空")
        result = await send_and_wait(module, key, f"/model {model}", timeout=90)
        status = 200 if result["ok"] else 504
        return web.json_response(result, status=status)
    return handler


def _make_perm_set(module):
    async def handler(request):
        key = request.match_info["key"]
        body = await _body(request)
        mode = (body.get("mode") or "").strip().lower()
        if mode not in ("ask", "allow", "unreviewed"):
            return _err("mode 必须是 ask / allow / unreviewed")
        result = await send_and_wait(module, key, f"/perm {mode}", timeout=60)
        status = 200 if result["ok"] else 504
        return web.json_response(result, status=status)
    return handler


def _make_perm_get(module):
    async def handler(request):
        from gateway.agent_factory import _load_map

        key = request.match_info["key"]
        entry = module.session_mgr._sessions.get(key)
        if entry is None or entry.agent is None:
            meta = _load_map().get(key)
            mode = meta.get("permission_mode", "allow") \
                if isinstance(meta, dict) else "allow"
            return web.json_response(
                {"session_key": key, "mode": mode, "rules": None,
                 "note": "会话未加载，仅显示持久化档位"})
        agent = entry.agent
        meta = _load_map().get(key)
        mode = meta.get("permission_mode", "allow") \
            if isinstance(meta, dict) else "allow"
        return web.json_response({
            "session_key": key,
            "mode": mode,
            "rules": agent.permission.describe_rules(),
        })
    return handler


# ---------- plan 两阶段 ----------

def _make_modes(module):
    async def handler(request):
        return web.json_response({"modes": _MODES})
    return handler


def _make_plan(module):
    async def handler(request):
        body = await _body(request)
        text = (body.get("text") or "").strip()
        session_key = body.get("session_key") or "webui:default"
        if not text:
            return _err("text 不能为空")
        # 阶段一：合成 /plan-preview 走漏斗（只读预览）
        result = await send_and_wait(
            module, session_key, f"/plan-preview {text}",
            timeout=min(float(body.get("timeout", 300)), 600))
        if not result["ok"]:
            return web.json_response(result, status=504)
        try:
            preview = json.loads(result["reply"])
        except json.JSONDecodeError:
            return _err("预览结果解析失败", 500)
        if not preview.get("ok"):
            return _err(preview.get("error", "预览失败"), 422)
        plan_id = module.glue.create_plan(
            session_key, text, preview["plan_text"])
        return web.json_response({
            "plan_id": plan_id,
            "session_key": session_key,
            "plan_text": preview["plan_text"],
            "tasks": preview["tasks"],
        })
    return handler


def _make_plan_approve(module):
    async def handler(request):
        plan_id = request.match_info["plan_id"]
        plan = module.glue.take_plan(plan_id)
        if plan is None:
            return _err("方案不存在或已过期", 404)
        entry = module.session_mgr.get_or_create(plan["session_key"])
        if entry is None:
            # 放回暂存，避免丢失
            module.glue.create_plan(
                plan["session_key"], plan["text"], plan["plan_text"])
            return _err("会话池已满", 503)
        entry.pending_plan = {"text": plan["text"],
                              "plan_text": plan["plan_text"]}
        # 合成 /plan-apply 入漏斗，立即 202（结果经 SSE chat.done + history）
        msg = InboundMessage(
            channel="webui",
            session_key=plan["session_key"],
            user_id="webui", user_name="WebUI",
            text="/plan-apply",
            message_id=f"plan-{plan_id}",
        )
        await module.dispatcher.on_inbound(msg)
        return web.json_response(
            {"started": True, "plan_id": plan_id,
             "session_key": plan["session_key"]}, status=202)
    return handler


def _make_plan_reject(module):
    async def handler(request):
        plan_id = request.match_info["plan_id"]
        found = module.glue.reject_plan(plan_id)
        if not found:
            return _err("方案不存在或已过期", 404)
        return web.Response(status=204)
    return handler


# ---------- 审批 ----------

def _make_approvals(module):
    async def handler(request):
        return web.json_response(
            {"approvals": module.glue.bridge.list_pending()})
    return handler


def _make_approval_answer(module):
    async def handler(request):
        aid = request.match_info["aid"]
        body = await _body(request)
        answer = (body.get("answer") or "").strip().lower()
        if answer not in ("y", "n", "a", "s"):
            return _err("answer 必须是 y/n/a/s")
        ok = module.glue.bridge.resolve(aid, answer)
        if not ok:
            return _err("审批不存在或已处理", 404)
        return web.json_response({"ok": True, "id": aid, "answer": answer})
    return handler


# ---------- 命令补全 ----------

def _make_commands(module):
    async def handler(request):
        commands = module.dispatcher.commands_table()
        # /plan 由前端编排（两阶段 API），补一条 client_hint
        commands.append({"name": "/plan", "args": "[任务描述]",
                         "help": "/plan — 生成执行方案（两阶段确认）",
                         "client_hint": "plan-flow"})
        return web.json_response({"commands": commands})
    return handler
