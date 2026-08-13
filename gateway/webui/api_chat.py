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
from core.plan import PlanStatus
from gateway.dispatcher import Dispatcher
from gateway.webui.glue import send_and_wait

logger = logging.getLogger("hello_agent.gateway")

_BASE_MODES = [
    {"id": "chat", "label": "会话", "available": True},
    {"id": "plan", "label": "方案", "available": True},
]

def register_routes(app: web.Application, module):
    app.router.add_get("/api/sessions", _make_sessions(module))
    app.router.add_get("/api/sessions/{key}/history", _make_history(module))
    app.router.add_delete("/api/sessions/{key}", _make_delete(module))
    app.router.add_post("/api/sessions/{key}/clear", _make_clear(module))
    app.router.add_post("/api/chat", _make_chat(module))
    app.router.add_post("/api/sessions/{key}/stop", _make_stop(module))
    app.router.add_post("/api/sessions/{key}/model", _make_model(module))
    app.router.add_get("/api/sessions/{key}/reasoning", _make_reasoning_get(module))
    app.router.add_post("/api/sessions/{key}/reasoning", _make_reasoning_set(module))
    app.router.add_post("/api/sessions/{key}/permission", _make_perm_set(module))
    app.router.add_get("/api/sessions/{key}/permission", _make_perm_get(module))
    app.router.add_get("/api/modes", _make_modes(module))
    app.router.add_post("/api/plan", _make_plan(module))
    app.router.add_post("/api/plan/{plan_id}/approve", _make_plan_approve(module))
    app.router.add_post("/api/plan/{plan_id}/reject", _make_plan_reject(module))
    app.router.add_get("/api/plans", _make_plans_list(module))
    app.router.add_post("/api/plans/clear", _make_plans_clear(module))
    app.router.add_post("/api/plans/{plan_id}/archive", _make_plan_archive(module))
    app.router.add_get("/api/plans/{plan_id}", _make_plan_get(module))
    app.router.add_get("/api/plans/{plan_id}/tasks", _make_plan_tasks(module))
    app.router.add_post("/api/plans/{plan_id}/pause", _make_plan_pause(module))
    app.router.add_post("/api/plans/{plan_id}/resume", _make_plan_resume(module))
    app.router.add_post("/api/plans/{plan_id}/cancel", _make_plan_cancel(module))
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
        return web.json_response({"modes": list(_BASE_MODES)})
    return handler


def _reasoning_payload(module, key: str) -> dict:
    """Return persisted selection and the effective value of a loaded session."""
    from gateway.agent_factory import _load_map

    entry = module.session_mgr._sessions.get(key)
    meta = _load_map().get(key)
    override = meta.get("reasoning_level") if isinstance(meta, dict) else None
    if not isinstance(override, str):
        override = None
    if entry is not None and entry.agent is not None:
        agent = entry.agent
        return {
            "session_key": key,
            "selected": override or "inherit",
            "effective": getattr(agent.llm, "reasoning_level", "provider_default"),
            "protocol": getattr(agent.llm, "_protocol", "openai"),
            "loaded": True,
        }
    return {
        "session_key": key,
        "selected": override or "inherit",
        "effective": None,
        "protocol": None,
        "loaded": False,
    }


def _make_reasoning_get(module):
    async def handler(request):
        return web.json_response(_reasoning_payload(module, request.match_info["key"]))
    return handler


def _make_reasoning_set(module):
    async def handler(request):
        key = request.match_info["key"]
        body = await _body(request)
        level = (body.get("level") or "").strip().lower()
        if not level:
            return _err("level 不能为空")
        result = await send_and_wait(module, key, f"/reasoning {level}", timeout=90)
        if not result["ok"]:
            return web.json_response(result, status=504)
        return web.json_response({**result, **_reasoning_payload(module, key)})
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
        plan = module.glue.create_plan(session_key, text, preview["plan"])
        return web.json_response({
            "plan_id": plan.plan_id,
            "session_key": session_key,
            "plan": plan.to_dict(),
            "tasks": preview["tasks"],
        })
    return handler

def _make_plan_approve(module):
    async def handler(request):
        plan_id = request.match_info["plan_id"]
        plan = module.glue.plan_manager.get(plan_id)
        if plan is None:
            return _err("方案不存在", 404)
        if not module.dispatcher.task_runtime_enabled:
            return _err("TaskRuntime 未启用，无法执行已批准方案；请通过初始化向导启用 task_runtime", 409)
        try:
            plan = module.glue.plan_manager.approve(plan_id, actor="webui")
        except ValueError as exc:
            return _err(str(exc), 409)
        module.bus.publish("plan.changed", {"action": "approved", "plan": plan.to_dict()})
        module.plan_runtime.start(plan.plan_id)
        return web.json_response({"started": True, "plan": plan.to_dict()}, status=202)
    return handler

def _make_plan_reject(module):
    async def handler(request):
        plan_id = request.match_info["plan_id"]
        plan = module.glue.plan_manager.get(plan_id)
        if plan is None:
            return _err("方案不存在", 404)
        if plan.status is not PlanStatus.AWAITING_APPROVAL:
            return _err("只有待确认方案可以拒绝", 409)
        plan = module.glue.plan_manager.cancel(plan_id)
        module.bus.publish("plan.changed", {"action": "cancelled", "plan": plan.to_dict()})
        return web.json_response({"plan": plan.to_dict()})
    return handler



def _plan_payload(plan):
    return {"plan": plan.to_dict(), "tasks": [task.to_dict() for task in plan.tasks]}


def _make_plans_list(module):
    async def handler(request):
        session_key = (request.query.get("session_key") or "").strip()
        if not session_key:
            return _err("session_key 为必填项")
        try:
            limit = max(1, min(int(request.query.get("limit", 100)), 1000))
        except ValueError:
            return _err("limit 必须为整数")
        session_id = Dispatcher._runtime_session_id(session_key)
        plans = module.glue.plan_manager.list(session_id, limit=limit)
        return web.json_response({"plans": [plan.to_dict() for plan in plans]})
    return handler


def _make_plans_clear(module):
    async def handler(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        session_key = str((body or {}).get("session_key") or "").strip()
        if not session_key:
            return _err("session_key 为必填项")
        session_id = Dispatcher._runtime_session_id(session_key)
        archived = module.glue.plan_manager.archive_terminal_for_session(session_id)
        module.bus.publish("plan.changed", {
            "action": "terminal_cards_cleared", "session_key": session_key, "count": archived,
        })
        return web.json_response({"ok": True, "archived": archived})
    return handler


def _make_plan_archive(module):
    async def handler(request):
        plan_id = request.match_info["plan_id"]
        try:
            plan = module.glue.plan_manager.archive_terminal(plan_id)
        except KeyError:
            return _err("方案不存在", 404)
        except ValueError as exc:
            return _err(str(exc), 409)
        module.bus.publish("plan.changed", {"action": "archived", "plan": plan.to_dict()})
        return web.json_response(_plan_payload(plan))
    return handler


def _make_plan_get(module):
    async def handler(request):
        plan = module.glue.plan_manager.get(request.match_info["plan_id"])
        if plan is None:
            return _err("方案不存在", 404)
        return web.json_response(_plan_payload(plan))
    return handler


def _make_plan_tasks(module):
    async def handler(request):
        plan = module.glue.plan_manager.get(request.match_info["plan_id"])
        if plan is None:
            return _err("方案不存在", 404)
        return web.json_response({"tasks": [task.to_dict() for task in plan.tasks]})
    return handler


def _make_plan_pause(module):
    async def handler(request):
        plan_id = request.match_info["plan_id"]
        try:
            plan = await module.plan_runtime.pause(plan_id)
        except KeyError:
            return _err("方案不存在", 404)
        except ValueError as exc:
            return _err(str(exc), 409)
        return web.json_response(_plan_payload(plan))
    return handler


def _make_plan_resume(module):
    async def handler(request):
        if not module.dispatcher.task_runtime_enabled:
            return _err("TaskRuntime 未启用，无法恢复方案", 409)
        plan_id = request.match_info["plan_id"]
        try:
            plan = await module.plan_runtime.resume(plan_id)
        except KeyError:
            return _err("方案不存在", 404)
        except ValueError as exc:
            return _err(str(exc), 409)
        return web.json_response(_plan_payload(plan), status=202)
    return handler


def _make_plan_cancel(module):
    async def handler(request):
        plan_id = request.match_info["plan_id"]
        plan = module.glue.plan_manager.get(plan_id)
        if plan is None:
            return _err("方案不存在", 404)
        try:
            if plan.status in {PlanStatus.ACTIVE, PlanStatus.PAUSED}:
                plan = await module.plan_runtime.cancel(plan_id)
            else:
                plan = module.glue.plan_manager.cancel(plan_id)
                module.bus.publish("plan.changed", {"action": "cancelled", "plan": plan.to_dict()})
        except ValueError as exc:
            return _err(str(exc), 409)
        return web.json_response(_plan_payload(plan))
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
