# -*- coding: utf-8 -*-
"""
api_chat.py —— 会话页全部 REST 端点（P3b）

chat / sessions / history / delete / clear / model / permission /
plan 两阶段 / approvals / commands / modes
"""

import json
import logging
from datetime import datetime

from aiohttp import web

from gateway.channels.base import InboundMessage
from core.plan import PlanStatus
from gateway.conversation.models import _loads_json
from gateway.dispatcher import Dispatcher, plan_approval_required
from gateway.webui.glue import clear_cached_agent_context, send_and_wait

logger = logging.getLogger("jk_agent.gateway")

_BASE_MODES = [
    {"id": "chat", "label": "会话", "available": True},
]

def register_routes(app: web.Application, module):
    app.router.add_get("/api/sessions", _make_sessions(module))
    app.router.add_delete("/api/sessions/{key}", _make_delete(module))
    app.router.add_post("/api/sessions/{key}/clear", _make_clear(module))
    app.router.add_post("/api/sessions/{key}/stop", _make_stop(module))
    app.router.add_post("/api/sessions/{key}/model", _make_model(module))
    app.router.add_get("/api/sessions/{key}/reasoning", _make_reasoning_get(module))
    app.router.add_post("/api/sessions/{key}/reasoning", _make_reasoning_set(module))
    app.router.add_post("/api/sessions/{key}/permission", _make_perm_set(module))
    app.router.add_get("/api/sessions/{key}/permission", _make_perm_get(module))
    app.router.add_get("/api/modes", _make_modes(module))
    app.router.add_post("/api/plan", _make_plan(module))
    app.router.add_get("/api/goals", _make_goals_list(module))
    app.router.add_post("/api/goals", _make_goal_create(module))
    app.router.add_post("/api/goals/{goal_id}/pause", _make_goal_pause(module))
    app.router.add_post("/api/goals/{goal_id}/resume", _make_goal_resume(module))
    app.router.add_post("/api/goals/{goal_id}/cancel", _make_goal_cancel(module))
    app.router.add_post("/api/goals/{goal_id}/archive", _make_goal_archive(module))
    app.router.add_post("/api/goals/{goal_id}/max-rounds", _make_goal_max_rounds(module))
    app.router.add_get("/api/subagents", _make_subagents_list(module))
    app.router.add_post("/api/subagents", _make_subagent_create(module))
    app.router.add_post("/api/subagents/{child_id}/cancel", _make_subagent_cancel(module))
    app.router.add_post("/api/subagents/{child_id}/continue", _make_subagent_continue(module))
    app.router.add_post("/api/subagents/{child_id}/archive", _make_subagent_archive(module))
    app.router.add_get("/api/subagents/{child_id}", _make_subagent_get(module))
    app.router.add_get("/api/sessions/{key}/children", _make_child_sessions(module))
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
    app.router.add_get("/api/questions", _make_questions(module))
    app.router.add_post("/api/questions/{qid}", _make_question_answer(module))
    app.router.add_post("/api/questions/{qid}/cancel", _make_question_cancel(module))
    app.router.add_get("/api/commands", _make_commands(module))
    app.router.add_get("/api/sessions/{key}/context", _make_context(module))


def _err(text, status=400):
    return web.json_response({"error": text}, status=status)


async def _body(request) -> dict:
    try:
        return await request.json()
    except (json.JSONDecodeError, Exception):
        return {}


def _parse_int(value, default=None):
    """安全解析整数参数；缺失/空返回 default，非法值抛 ValueError（调用方转 400）。"""
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError("参数必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise ValueError(f"参数必须是整数，收到 {value!r}")


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


def _iso_ts_to_epoch(value) -> float:
    """ISO 时间戳 → epoch 秒（统一会话列表真实时间；解析失败返回 0）。"""
    if not value:
        return 0
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


# ---------- 会话列表 / 历史 ----------

def _make_sessions(module):
    async def handler(request):
        # 统一模型：从 conversation_sessions 读 webui 会话（设计方案：会话管理统一化）
        svc = getattr(module, "conversation_service", None)
        if svc is not None:
            try:
                store = svc.store
                rows = []
                with store._db.connection() as conn:
                    rows = conn.execute(
                        "SELECT c.*, (SELECT COUNT(*) FROM turns t WHERE "
                        "t.conversation_id=c.conversation_id) AS turn_count "
                        "FROM conversation_sessions c "
                        "WHERE c.origin IN ('webui','channel') "
                        "AND c.session_key NOT LIKE 'sched:%' "
                        "AND c.session_key NOT LIKE 'heartbeat:%' "
                        "AND c.session_key NOT LIKE 'system:%' "
                        "ORDER BY c.updated_at DESC",
                    ).fetchall()
                memory = {e["session_key"]: e for e in module.session_mgr.list_entries()}
                out = []
                for row in rows:
                    key = row["session_key"]
                    entry = memory.get(key)
                    # list_entries 返回 dict（非 SessionEntry 对象），统一用字典访问
                    prefs = dict(_loads_json(row["route_metadata"]) or {}).get("prefs") or {}
                    out.append({
                        "session_key": key,
                        "session_id": row["conversation_id"],
                        "model": (entry or {}).get("model", "") or "",
                        "message_count": int(row["turn_count"] or 0),
                        "is_busy": bool(entry and entry.get("is_busy")),
                        # 统一会话行取真实创建/更新时间（ISO → epoch，兼容前端 number 类型）
                        "created_at": _iso_ts_to_epoch(row["created_at"]),
                        "last_active": _iso_ts_to_epoch(row["updated_at"]),
                        "loaded": bool(entry and entry.get("loaded")),
                        "source": "unified",
                    })
                return web.json_response({"sessions": out})
            except Exception:
                logger.exception("统一会话列表失败，回退旧路径")
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


def _normalize_context_for_model(stats: dict, model: str) -> dict:
    """把上下文统计向 model 的配置对齐（context_length 与历史预算）。

    即使 Agent 内部 store 因历史模型而保留旧值，只要展示的 model 在配置里有
    context_length，就用配置值覆盖 model_context_length 并重算预算，保证
    "模型上下文" 与 "模型" 一致（如 256K 模型不再显示 1M）。
    """
    if not model:
        return stats
    try:
        from core.config_loader import load_config
        from agent import _history_budget
        mcfg = load_config().get("llm", {}).get("models", {}).get(model, {})
        ctx_len = int(mcfg.get("context_length") or 0)
        if ctx_len > 0:
            budget = _history_budget(ctx_len)
            total = int(stats.get("total_tokens") or 0)
            stats = dict(stats)
            stats["model_context_length"] = ctx_len
            stats["max_tokens"] = budget
            stats["remaining_tokens"] = max(0, budget - total)
            stats["usage_ratio"] = total / budget if budget > 0 else 0
    except Exception:
        pass
    return stats


def _make_context(module):
    """会话上下文占用统计（WebUI 输入框上下文指示器数据源）。"""
    async def handler(request):
        key = request.match_info["key"]
        # 统一会话用 conversation_runner 的 Agent 执行（懒创建、跨 Turn 复用），
        # 运行期切换的模型/上下文长度以它为准；session_mgr 的 entry.agent 是
        # 另一实例，可能不反映切换。优先读 runner 的 agent。
        runner = getattr(module, "conversation_runner", None)
        agent = None
        if runner is not None:
            agent = runner.agent_for_session(key)
        if agent is None:
            entry = module.session_mgr._sessions.get(key)
            agent = entry.agent if entry else None
        # 已加载 Agent：以实际使用的模型为准（保证顶部显示 = agent.llm.model，
        # 彻底消除"顶部显示 ≠ 实际使用"的不一致）。运行中切模型会被挂起、运行结束
        # 后立即应用，因此空闲时 agent.llm.model 已等于用户切换的模型。
        svc = getattr(module, "conversation_service", None)
        prefs_model = ""
        if svc is not None:
            try:
                conv = svc.store.get_conversation_by_key(key)
                if conv is not None:
                    prefs_model = str((svc.conversation_prefs(conv.conversation_id) or {}).get("model") or "")
            except Exception:
                pass
        if agent is not None and hasattr(agent, "llm"):
            # 实际使用模型优先；缺省时回落持久化偏好。
            actual_model = getattr(agent.llm, "model", "") or prefs_model
        else:
            # 未加载：显示持久化偏好（下一次创建 Agent 将使用它）。
            actual_model = prefs_model
        effective_model = actual_model
        if agent is not None and hasattr(agent, "store"):
            try:
                stats = agent.store.stats()
                # 显示向模型配置对齐：上下文窗口/预算以 config.json 中该模型的
                # context_length 为准，避免 Agent 运行期因历史模型（如旧 sessions_map
                # 回填 deepseek 1M）导致"模型=256K 但上下文显示 1M"的不一致。
                stats = _normalize_context_for_model(stats, effective_model)
                return web.json_response({
                    "available": True,
                    "session_key": key,
                    "model": effective_model,
                    **stats,
                })
            except Exception:
                pass
        # agent 未加载：优先从统一会话 prefs 读运行期切换的模型，
        # 用 config.json 该模型的 context_length 计算上下文窗口/预算，
        # 避免显示旧模型或错误的上下文长度（如 256K 显示成 1M）。
        model = effective_model
        if not model:
            from gateway.agent_factory import _load_map
            meta = _load_map().get(key)
            model = meta.get("model") if isinstance(meta, dict) else ""
        if model:
            try:
                from core.config_loader import load_config
                from agent import _history_budget
                mcfg = load_config().get("llm", {}).get("models", {}).get(model, {})
                ctx_len = int(mcfg.get("context_length") or 0)
                if ctx_len > 0:
                    return web.json_response({
                        "available": True,
                        "session_key": key,
                        "model": model,
                        "total_tokens": 0,
                        "model_context_length": ctx_len,
                        "max_tokens": _history_budget(ctx_len),
                        "usage_ratio": 0,
                        "remaining_tokens": _history_budget(ctx_len),
                    })
            except Exception:
                pass
        return web.json_response({"available": False, "session_key": key})
    return handler


def _history_message_payload(messages) -> list[dict]:
    """Convert a sliced history page to a safe UI payload."""
    from core.message_store import _content_to_text

    out = []
    for source in messages:
        item = dict(source)
        content = item.get("content")
        if isinstance(content, list):
            item["content_text"] = _content_to_text(content)
        out.append(item)
    return out


# ---------- 删除 / 清空 ----------

def _make_delete(module):
    async def handler(request):
        key = request.match_info["key"]
        svc = getattr(module, "conversation_service", None)
        if module.session_mgr.is_busy(key):
            return _err("会话正忙，无法删除", 409)
        from gateway.agent_factory import _load_map, remove_map_entry
        from core.message_store import MessageStore
        # 真正删除：同时清理 统一会话行 + 旧 sessions_map/MessageStore 记录，
        # 否则会话仍留在 /api/sessions 列表（观感"删除没生效"）。
        # 先解析 MessageStore session_id（evict 会移除 entry，须在 evict 前取）：
        # ① 已加载 agent 的 store.session_id；② sessions_map；③ 工作区会话 key 第 3 段（wss_XXX）。
        sid = None
        entry = module.session_mgr._sessions.get(key)
        if entry is not None and getattr(entry, "agent", None) is not None:
            st = getattr(entry.agent, "store", None)
            if st is not None:
                sid = getattr(st, "session_id", None) or sid
        if not sid:
            meta = _load_map().get(key)
            sid = meta.get("session_id") if isinstance(meta, dict) else meta
        if not sid and key.startswith("workspace:"):
            parts = key.split(":", 2)
            if len(parts) == 3:
                sid = parts[2]
        removed = await module.session_mgr.evict(key, save=False)
        if sid:
            try:
                MessageStore.delete_session_file(sid)
            except ValueError as exc:
                # 路径穿越等非法 session_id：明确拒绝而非静默吞掉（P1 修复集成）
                return _err(f"非法的会话标识: {exc}", 400)
            except Exception:
                logger.debug("删除会话消息文件失败: %s", sid)
        remove_map_entry(key)
        deleted = False
        if svc is not None:
            try:
                deleted = svc.delete_conversation_by_key(key)
            except Exception:
                logger.exception("删除统一会话失败: %s", key)
        module.bus.publish("session.evicted",
                           {"session_key": key, "reason": "delete"})
        return web.json_response({"ok": True, "removed_from_memory": removed,
                                  "deleted": deleted})
    return handler


def _make_clear(module):
    async def handler(request):
        key = request.match_info["key"]
        # 统一模型：清空会话历史（设计方案：会话管理统一化）
        svc = getattr(module, "conversation_service", None)
        if svc is not None:
            conv = svc.store.get_conversation_by_key(key)
            if conv is not None:
                svc.clear_history(conv.conversation_id)
                # 清空会话时联动清理该会话的 Plan/Goal：停止运行中任务、
                # 删除业务记录与 system:plan/goal 系统会话，避免界面残留。
                await _clear_session_runtime(module, key)
                # 联动清空驻留 Agent 的内存上下文：统一模型与 Agent MessageStore
                # 是两份存储，只清模型不清 Agent，下一条消息仍带全部旧上下文。
                await clear_cached_agent_context(module, key)
                return web.json_response({"ok": True, "session_key": key,
                                          "reply": "✅ 会话已清空"})
        result = await send_and_wait(module, key, "/clear", timeout=60)
        status = 200 if result["ok"] else 504
        return web.json_response(result, status=status)
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


# ---------- 聊天 / 模型 / 权限 ----------

def _make_stop(module):
    async def handler(request):
        key = request.match_info["key"]
        entry = module.session_mgr._sessions.get(key)
        if entry is None or entry.agent is None:
            return _err("会话未加载或未在运行", 404)
        entry.agent.request_stop()
        # 停止会话 = 同时暂停该会话正在运行的 Plan/Goal 后台任务，
        # 否则 goal/plan 仍占用会话（entry.is_busy），用户无法重新输入。
        dispatcher = getattr(module, "dispatcher", None)
        if dispatcher is not None:
            try:
                await dispatcher.stop_session_runtime(key)
            except Exception:
                logger.exception("停止会话：联动取消 Plan/Goal 失败 %s", key)
        module.bus.publish("chat.stop_requested", {"session_key": key})
        bridge = getattr(module, "conversation_bridge", None)
        if bridge is not None:
            bridge.request_stop(key)
        return web.json_response({"ok": True, "session_key": key})
    return handler


def _make_model(module):
    async def handler(request):
        key = request.match_info["key"]
        body = await _body(request)
        model = (body.get("model") or "").strip()
        if not model:
            return _err("model 不能为空")
        # 统一模型：持久化会话偏好（设计方案：管理操作统一化）
        svc = getattr(module, "conversation_service", None)
        if svc is not None:
            conv = svc.store.get_conversation_by_key(key)
            if conv is not None:
                svc.update_prefs(conv.conversation_id, model=model)
                runner = getattr(module, "conversation_runner", None)
                if runner is not None:
                    runner.apply_prefs(conv.conversation_id, {"model": model})
                return web.json_response({"ok": True, "session_key": key,
                                          "reply": f"✅ 已切换到模型: {model}"})
        # 兜底：会话未接入统一模型时走旧命令路径
        result = await send_and_wait(module, key, f"/model {model}", timeout=90)
        status = 200 if result["ok"] else 504
        return web.json_response(result, status=status)
    return handler


def _make_perm_set(module):
    async def handler(request):
        key = request.match_info["key"]
        body = await _body(request)
        mode = (body.get("mode") or "").strip().lower()
        if mode not in ("readonly", "ask", "allow", "unreviewed"):
            return _err("mode 必须是 readonly / ask / allow / unreviewed")
        # 统一模型：持久化会话偏好（设计方案：管理操作统一化）
        svc = getattr(module, "conversation_service", None)
        if svc is not None:
            conv = svc.store.get_conversation_by_key(key)
            if conv is not None:
                svc.update_prefs(conv.conversation_id, permission_mode=mode)
                runner = getattr(module, "conversation_runner", None)
                if runner is not None:
                    runner.apply_prefs(conv.conversation_id,
                                       {"permission_mode": mode})
                return web.json_response({"ok": True, "session_key": key,
                                          "reply": f"✅ 权限档位：{mode}"})
        result = await send_and_wait(module, key, f"/perm {mode}", timeout=60)
        status = 200 if result["ok"] else 504
        return web.json_response(result, status=status)
    return handler


def _make_perm_get(module):
    async def handler(request):
        key = request.match_info["key"]
        # 统一会话用 conversation_runner 的 Agent（懒创建、跨 Turn 复用）；
        # 运行期切换的权限档位以它为准。优先读 runner agent。
        runner = getattr(module, "conversation_runner", None)
        agent = None
        if runner is not None:
            agent = runner.agent_for_session(key)
        if agent is None:
            entry = module.session_mgr._sessions.get(key)
            agent = entry.agent if entry else None
        # 统一模型优先：从 Conversation.route_metadata.prefs 读（设计方案：管理操作统一化）
        svc = getattr(module, "conversation_service", None)
        if svc is not None:
            conv = svc.store.get_conversation_by_key(key)
            if conv is not None:
                prefs = dict((conv.route_metadata or {}).get("prefs") or {})
                mode = prefs.get("permission_mode") or "allow"
                if agent is not None:
                    return web.json_response({
                        "session_key": key,
                        "mode": getattr(agent.permission, "_permission_mode", mode),
                        "rules": agent.permission.describe_rules(),
                    })
                return web.json_response(
                    {"session_key": key, "mode": mode, "rules": None,
                     "note": "会话未加载，仅显示持久化档位"})
        from gateway.agent_factory import _load_map

        if agent is None:
            meta = _load_map().get(key)
            mode = meta.get("permission_mode", "allow") \
                if isinstance(meta, dict) else "allow"
            return web.json_response(
                {"session_key": key, "mode": mode, "rules": None,
                 "note": "会话未加载，仅显示持久化档位"})
        meta = _load_map().get(key)
        mode = meta.get("permission_mode", "allow") \
            if isinstance(meta, dict) else "allow"
        return web.json_response({
            "session_key": key,
            "mode": mode,
            "rules": agent.permission.describe_rules(),
        })
    return handler





def _make_child_sessions(module):
    async def handler(request):
        parent_id = Dispatcher._runtime_session_id(request.match_info["key"])
        children = module.runtime_store.list_child_sessions(parent_id)
        return web.json_response({"parent_session_id": parent_id, "children": children})
    return handler

# ---------- Subagents: catalog + parent-controlled lifecycle ----------

def _make_subagents_list(module):
    async def handler(request):
        session_key = (request.query.get("session_key") or "").strip()
        if not session_key: return _err("session_key 为必填项")
        session_id = Dispatcher._runtime_session_id(session_key)
        reports = module.subagent_runtime.list_reports(session_id)
        return web.json_response({"subagents": [report.to_dict() for report in reports]})
    return handler

def _make_subagent_create(module):
    async def handler(request):
        body = await _body(request)
        session_key = (body.get("session_key") or "webui:default").strip()
        prompt = (body.get("prompt") or body.get("text") or "").strip()
        mode = body.get("mode") or "one-shot"
        if not prompt: return _err("prompt 不能为空")
        if not module.dispatcher.task_runtime_enabled: return _err("SessionRuntime 未启用", 409)
        try:
            report = await module.subagent_runtime.create(parent_session_id=Dispatcher._runtime_session_id(session_key), parent_session_key=session_key, prompt=prompt, mode=mode)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return _err(str(exc), 409)
        return web.json_response({"subagent": report.to_dict()}, status=202)
    return handler

def _make_subagent_cancel(module):
    async def handler(request):
        child_id = request.match_info["child_id"]
        if module.subagent_runtime.get_report(child_id) is None: return _err("Subagent 不存在", 404)
        await module.subagent_runtime.cancel_child(child_id)
        return web.json_response({"ok": True, "child_id": child_id})
    return handler


def _make_subagent_archive(module):
    async def handler(request):
        try:
            report = module.subagent_runtime.archive_child(request.match_info["child_id"])
        except KeyError: return _err("Subagent 不存在", 404)
        except ValueError as exc: return _err(str(exc), 409)
        return web.json_response({"subagent": report.to_dict()})
    return handler

def _make_subagent_continue(module):
    async def handler(request):
        child_id = request.match_info["child_id"]
        body = await _body(request)
        prompt = (body.get("prompt") or body.get("text") or "").strip()
        if not prompt: return _err("prompt 不能为空")
        try:
            report = await module.subagent_runtime.continue_child(child_id, prompt)
        except KeyError:
            return _err("Subagent 不存在", 404)
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc), 409)
        return web.json_response({"subagent": report.to_dict()}, status=202)
    return handler


def _make_subagent_get(module):
    async def handler(request):
        child_id = request.match_info["child_id"]
        report = module.subagent_runtime.get_report(child_id)
        if report is None: return _err("Subagent 不存在", 404)
        events = module.runtime_store.list_events(task_id=report.task_id, limit=200) if report.task_id else []
        return web.json_response({"subagent": report.to_dict(), "events": [event.to_dict() for event in events]})
    return handler

# ---------- Goals: structured long-running work, never a chat mode ----------

def _goal_payload(goal):
    return {"goal": goal.to_dict()}


def _runtime_session_key(module, session_id: str) -> str:
    """Resolve the WebUI session_key for a runtime session_id ('' if unknown)."""
    try:
        with module.runtime_store.connection() as connection:
            row = connection.execute(
                "SELECT session_key FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return row["session_key"] if row else ""
    except Exception:
        return ""

def _session_event_scope(session_key: str) -> dict:
    """Build the canonical SSE scope for a chat or workspace session key."""
    scope = {"session_key": session_key}
    if session_key.startswith("workspace:"):
        parts = session_key.split(":", 2)
        if len(parts) == 3:
            scope["workspace_id"], scope["workspace_session_id"] = parts[1], parts[2]
    return scope


def _runtime_event_scope(module, session_id: str) -> dict:
    return _session_event_scope(_runtime_session_key(module, session_id))


def _make_goals_list(module):
    async def handler(request):
        session_key = (request.query.get("session_key") or "").strip()
        if not session_key:
            return _err("session_key 为必填项")
        session_id = Dispatcher._runtime_session_id(session_key)
        return web.json_response({"goals": [goal.to_dict() for goal in module.goal_runtime.list(session_id)]})
    return handler

def _make_goal_create(module):
    async def handler(request):
        body = await _body(request)
        objective = (body.get("objective") or body.get("text") or "").strip()
        session_key = (body.get("session_key") or "webui:default").strip()
        if not objective:
            return _err("objective 不能为空")
        raw_rounds = body.get("max_rounds")
        # 显式轮次上限走直接 runtime 创建（/goal 命令语法不支持 max_rounds）；
        # 否则保持原有命令语义，与 REST/原生工具共享 active/armed Goal 语义。
        if raw_rounds is not None:
            try:
                max_rounds = max(1, int(raw_rounds))
            except (TypeError, ValueError):
                return _err("max_rounds 必须是正整数")
            session_id = Dispatcher._runtime_session_id(session_key)
            goal = module.goal_runtime.create(session_id, objective, max_rounds=max_rounds)
            module.goal_driver.trigger(goal.goal_id)
            module.bus.publish("goal.changed", {"action": "created", "goal": goal.to_dict(), **_session_event_scope(session_key)})
            return web.json_response(_goal_payload(goal), status=201)
        try:
            timeout = _parse_float(body.get("timeout"), default=300.0)
        except ValueError:
            return _err("timeout 必须是数字")
        result = await send_and_wait(
            module, session_key, f"/goal {objective}",
            timeout=min(timeout, 600))
        if not result.get("ok"):
            return web.json_response(result, status=504)
        session_id = Dispatcher._runtime_session_id(session_key)
        goals = module.goal_runtime.list(session_id)
        if not goals:
            return _err("Goal 创建失败", 500)
        goal = next((item for item in goals if item.objective == objective), goals[0])
        module.bus.publish("goal.changed", {"action": "created", "goal": goal.to_dict(), **_session_event_scope(session_key)})
        return web.json_response(_goal_payload(goal), status=201)
    return handler


def _make_goal_max_rounds(module):
    async def handler(request):
        body = await _body(request)
        try:
            max_rounds = max(1, int(body.get("max_rounds")))
        except (TypeError, ValueError):
            return _err("max_rounds 必须是正整数")
        try:
            goal = module.goal_runtime.update_max_rounds(request.match_info["goal_id"], max_rounds)
        except KeyError:
            return _err("Goal 不存在", 404)
        except ValueError as exc:
            return _err(str(exc), 409)
        module.bus.publish("goal.changed", {
            "action": "max_rounds_updated",
            **_runtime_event_scope(module, goal.session_id),
            "goal": goal.to_dict(),
        })
        return web.json_response(_goal_payload(goal))
    return handler

def _make_goal_pause(module):
    async def handler(request):
        try:
            current = module.goal_runtime.get(request.match_info["goal_id"])
            if current is None: raise KeyError(request.match_info["goal_id"])
            if current.plan_id:
                await module.plan_runtime.pause(current.plan_id)
            goal = await module.goal_runtime.pause_async(
                current.goal_id, cancel=module.dispatcher.cancel_runtime_task)
        except KeyError:
            return _err("Goal 不存在", 404)
        except ValueError as exc:
            return _err(str(exc), 409)
        module.bus.publish("goal.changed", {"action": "paused", "goal": goal.to_dict(), **_runtime_event_scope(module, goal.session_id)})
        return web.json_response(_goal_payload(goal))
    return handler

def _make_goal_resume(module):
    async def handler(request):
        try:
            current = module.goal_runtime.get(request.match_info["goal_id"])
            if current is None: raise KeyError(request.match_info["goal_id"])
            if current.plan_id:
                plan = module.plan_runtime.manager.get(current.plan_id)
                if plan and plan.status.value == "paused":
                    await module.plan_runtime.resume(current.plan_id)
            goal = module.goal_runtime.resume(current.goal_id)
            module.goal_driver.trigger(goal.goal_id)
        except KeyError:
            return _err("Goal 不存在", 404)
        except ValueError as exc:
            return _err(str(exc), 409)
        module.bus.publish("goal.changed", {"action": "resumed", "goal": goal.to_dict(), **_runtime_event_scope(module, goal.session_id)})
        return web.json_response(_goal_payload(goal))
    return handler

def _make_goal_cancel(module):
    async def handler(request):
        try:
            current = module.goal_runtime.get(request.match_info["goal_id"])
            if current is None: raise KeyError(request.match_info["goal_id"])
            if current.plan_id:
                plan = module.plan_runtime.manager.get(current.plan_id)
                if plan and not plan.is_terminal:
                    await module.plan_runtime.cancel(current.plan_id)
            goal = module.goal_runtime.cancel(current.goal_id)
        except KeyError:
            return _err("Goal 不存在", 404)
        except ValueError as exc:
            return _err(str(exc), 409)
        module.bus.publish("goal.changed", {"action": "cancelled", "goal": goal.to_dict(), **_runtime_event_scope(module, goal.session_id)})
        return web.json_response(_goal_payload(goal))
    return handler


def _make_goal_archive(module):
    async def handler(request):
        try:
            goal = module.goal_runtime.archive(request.match_info["goal_id"])
        except KeyError: return _err("Goal 不存在", 404)
        except ValueError as exc: return _err(str(exc), 409)
        module.bus.publish("goal.changed", {"action": "archived", "goal": goal.to_dict(), **_runtime_event_scope(module, goal.session_id)})
        return web.json_response(_goal_payload(goal))
    return handler

# ---------- plan 两阶段 ----------

def _make_modes(module):
    async def handler(request):
        return web.json_response({"modes": list(_BASE_MODES)})
    return handler


def _reasoning_payload(module, key: str) -> dict:
    """Return persisted selection and the effective value of a loaded session."""
    # 统一模型优先：从 Conversation.route_metadata.prefs 读（设计方案：管理操作统一化）
    svc = getattr(module, "conversation_service", None)
    if svc is not None:
        conv = svc.store.get_conversation_by_key(key)
        if conv is not None:
            prefs = dict((conv.route_metadata or {}).get("prefs") or {})
            override = prefs.get("reasoning_level") or None
            entry = module.session_mgr._sessions.get(key)
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
                "effective": override or "provider_default",
                "protocol": "openai",
                "loaded": False,
            }
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
        # 白名单校验：与 workspace _make_switch 对齐，非法值 400 不落库
        from gateway.webui.workspace_models import VALID_REASONING_LEVELS
        if level not in VALID_REASONING_LEVELS:
            return _err(f"level 必须是 {VALID_REASONING_LEVELS} 之一",
                        code="INVALID_REASONING_LEVEL")
        # 统一模型：持久化会话偏好（设计方案：管理操作统一化）
        svc = getattr(module, "conversation_service", None)
        if svc is not None:
            conv = svc.store.get_conversation_by_key(key)
            if conv is not None:
                svc.update_prefs(conv.conversation_id, reasoning_level=level)
                runner = getattr(module, "conversation_runner", None)
                if runner is not None:
                    runner.apply_prefs(conv.conversation_id,
                                       {"reasoning_level": level})
                return web.json_response({**_reasoning_payload(module, key),
                                          "ok": True, "reply": f"✅ 推理等级：{level}"})
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
        try:
            timeout = _parse_float(body.get("timeout"), default=300.0)
        except ValueError:
            return _err("timeout 必须是数字")
        result = await send_and_wait(
            module, session_key, f"/plan-preview {text}",
            timeout=min(timeout, 600))
        if not result["ok"]:
            return web.json_response(result, status=504)
        try:
            preview = json.loads(result["reply"])
        except json.JSONDecodeError:
            return _err("预览结果解析失败", 500)
        if not preview.get("ok"):
            return _err(preview.get("error", "预览失败"), 422)
        goal_id = (body.get("goal_id") or "").strip() or None
        if goal_id and module.goal_runtime.get(goal_id) is None:
            return _err("Goal 不存在", 404)
        if not module.dispatcher.task_runtime_enabled:
            return _err("TaskRuntime 未启用，无法执行 Plan", 409)
        plan = module.glue.create_plan(session_key, text, preview["plan"], goal_id=goal_id)
        if goal_id:
            module.goal_runtime.attach_plan(goal_id, plan.plan_id)
        if plan_approval_required():
            # 两阶段审批：create_preview 后不自动 approve，发
            # plan.changed(AWAITING_APPROVAL)，等待既有 approve/reject 端点。
            module.bus.publish("plan.changed", {
                "action": "awaiting_approval",
                **_session_event_scope(session_key),
                "plan": plan.to_dict(),
            })
            return web.json_response({
                "plan_id": plan.plan_id, "session_key": session_key, "started": False,
                "awaiting_approval": True, "status": plan.status.value,
                "plan": plan.to_dict(), "tasks": preview["tasks"],
            }, status=202)
        plan = module.glue.plan_manager.approve(plan.plan_id, actor="automatic")
        module.bus.publish("plan.changed", {
            "action": "approved",
            **_session_event_scope(session_key),
            "plan": plan.to_dict(),
        })
        module.plan_runtime.start(plan.plan_id)
        return web.json_response({
            "plan_id": plan.plan_id, "session_key": session_key, "started": True,
            "plan": plan.to_dict(), "tasks": preview["tasks"],
        }, status=202)
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
        module.bus.publish("plan.changed", {
            "action": "approved",
            **_runtime_event_scope(module, plan.session_id),
            "plan": plan.to_dict(),
        })
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
        module.bus.publish("plan.changed", {
            "action": "cancelled",
            **_runtime_event_scope(module, plan.session_id),
            "plan": plan.to_dict(),
        })
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
            "action": "terminal_cards_cleared", "count": archived,
            **_session_event_scope(session_key),
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
        module.bus.publish("plan.changed", {"action": "archived", "plan": plan.to_dict(), **_runtime_event_scope(module, plan.session_id)})
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
                module.bus.publish("plan.changed", {
                    "action": "cancelled",
                    **_runtime_event_scope(module, plan.session_id),
                    "plan": plan.to_dict(),
                })
        except ValueError as exc:
            return _err(str(exc), 409)
        return web.json_response(_plan_payload(plan))
    return handler


# ---------- 审批 ----------

def _make_approvals(module):
    async def handler(request):
        session_key = (request.query.get("session_key") or "").strip()
        return web.json_response(
            {"approvals": module.glue.bridge.list_pending(
                session_key=session_key)})
    return handler


def _make_approval_answer(module):
    async def handler(request):
        aid = request.match_info["aid"]
        body = await _body(request)
        answer = (body.get("answer") or "").strip().lower()
        if answer not in ("y", "n", "a", "s"):
            return _err("answer 必须是 y/n/a/s")
        # fail-closed：请求必须携带归属（session_key 或审批所属上下文），
        # 并与 pending 记录单边匹配，否则 403（防跨会话/跨页面答复）。
        context = {
            "session_key": str(body.get("session_key") or "").strip(),
            "workspace_id": str(body.get("workspace_id") or "").strip(),
            "workspace_session_id": str(body.get("workspace_session_id") or "").strip(),
            "snapshot_id": str(body.get("snapshot_id") or "").strip(),
            "message_id": str(body.get("message_id") or "").strip(),
        }
        if not any(context.values()):
            return _err(
                "请求必须携带归属信息（session_key 或工作区/消息上下文）", 403)
        status = module.glue.bridge.resolve(aid, answer, context=context)
        if status == "ok":
            return web.json_response({"ok": True, "id": aid, "answer": answer})
        if status == "context_mismatch":
            return _err("审批归属不匹配（会话/工作区/消息上下文不一致）", 403)
        return _err("审批不存在、已处理或已超时", 404)
    return handler


# ---------- 结构化问题（QuestionBridge，与审批协议分离） ----------

def _make_questions(module):
    """GET /api/questions —— 恢复待答问题（页面刷新不丢）。

    可选 query 过滤：session_key / workspace_id / workspace_session_id。
    """
    async def handler(request):
        questions = module.glue.question_bridge.list_pending(
            session_key=(request.query.get("session_key") or "").strip(),
            workspace_id=(request.query.get("workspace_id") or "").strip(),
            workspace_session_id=(request.query.get("workspace_session_id") or "").strip(),
        )
        return web.json_response({"questions": questions})
    return handler


def _make_question_answer(module):
    """POST /api/questions/{qid} —— 答复待答问题。

    body 支持 selected_option_ids（数组）与 custom_text（字符串），
    另可携带 session_key / workspace_id / workspace_session_id /
    snapshot_id / message_id 做归属校验，防止跨会话/跨页面答复。
    状态码：200 成功 / 400 答案不合约束 / 403 归属不匹配 /
    409 已答复（one-answer）/ 404 未知或已过期。
    """
    async def handler(request):
        qid = request.match_info["qid"]
        try:
            body = await request.json()
        except Exception:
            return _err("请求体必须是 JSON 对象")
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象")
        selected = body.get("selected_option_ids") or []
        if not isinstance(selected, list):
            return _err("selected_option_ids 必须是数组")
        selected = [str(item).strip() for item in selected if str(item).strip()]
        custom = body.get("custom_text")
        if custom is not None and not isinstance(custom, str):
            return _err("custom_text 必须是字符串")
        context = {
            "session_key": str(body.get("session_key") or "").strip(),
            "workspace_id": str(body.get("workspace_id") or "").strip(),
            "workspace_session_id": str(body.get("workspace_session_id") or "").strip(),
            "snapshot_id": str(body.get("snapshot_id") or "").strip(),
            "message_id": str(body.get("message_id") or "").strip(),
        }
        # fail-closed：必须携带归属（session_key 或 qid 所属上下文），
        # 由桥记录单边匹配校验，否则 403（防跨会话/跨页面答复）。
        if not any(context.values()):
            return _err(
                "请求必须携带归属信息（session_key 或工作区/消息上下文）", 403)
        answer = {"selected_option_ids": selected, "custom_text": custom or ""}
        status = module.glue.question_bridge.resolve(qid, answer, context=context)
        if status == "ok":
            # wire id 契约：id 主字段 + question_id 兼容别名（POST 路径不变）
            return web.json_response(
                {"ok": True, "id": qid, "question_id": qid, **answer})
        if status == "already_answered":
            return _err("该问题已答复，不能重复提交", 409)
        if status == "context_mismatch":
            return _err("问题归属不匹配（会话/工作区/消息上下文不一致）", 403)
        if status == "invalid":
            return _err("答案不符合问题约束（非法选项/多选越界/必答为空/未允许自定义）", 400)
        return _err("问题不存在或已过期", 404)
    return handler


def _make_question_cancel(module):
    """POST /api/questions/{qid}/cancel —— 用户取消待答问题（弹窗"取消"）。

    与前端本地移除不同：取消会唤醒 ask() 等待线程并以 status=cancelled
    返回给 ask_question 工具，LLM 明确知道用户取消了提问，不会在超时后
    原样重复弹窗（修复"取消后无限弹窗"）。
    可携带 session_key / workspace_id / workspace_session_id /
    snapshot_id / message_id 做归属校验（同答复接口）。
    状态码：200 成功 / 403 归属不匹配 / 409 已答复 / 404 未知或已过期。
    """
    async def handler(request):
        qid = request.match_info["qid"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        context = {
            "session_key": str(body.get("session_key") or "").strip(),
            "workspace_id": str(body.get("workspace_id") or "").strip(),
            "workspace_session_id": str(body.get("workspace_session_id") or "").strip(),
            "snapshot_id": str(body.get("snapshot_id") or "").strip(),
            "message_id": str(body.get("message_id") or "").strip(),
        }
        status = module.glue.question_bridge.cancel(
            qid, context=context if any(context.values()) else None)
        if status == "ok":
            return web.json_response(
                {"ok": True, "id": qid, "question_id": qid, "status": "cancelled"})
        if status == "already_answered":
            return _err("该问题已答复，无需取消", 409)
        if status == "context_mismatch":
            return _err("问题归属不匹配（会话/工作区/消息上下文不一致）", 403)
        return _err("问题不存在或已过期", 404)
    return handler


# ---------- 命令补全 ----------

def _skills_for_session(module, session_key: str, allowed_skills=None) -> list:
    """Return only skills available to this session's effective capability scope."""
    entry = module.session_mgr._sessions.get(session_key) if session_key else None
    agent = getattr(entry, "agent", None) if entry else None
    if agent is not None:
        names = set(getattr(agent.tool_registry, "_skill_tool_names", set()))
        manager = getattr(agent, "skill_manager", None)
        skills = manager.get_all_skills() if manager else []
        return [skill for skill in skills if skill.name in names]

    from gateway.webui.catalog_service import get_skills_catalog
    skills = get_skills_catalog()
    if allowed_skills is not None:
        allowed = set(allowed_skills)
        skills = [skill for skill in skills if skill.get("name") in allowed]
    return skills


def _skill_name(skill) -> str:
    """统一从 Skill 对象或 dict 提取名称（catalog 返回 dict，运行期返回 Skill 对象）。"""
    if isinstance(skill, dict):
        return skill.get("name") or ""
    return getattr(skill, "name", "") or ""


def _skill_description(skill) -> str:
    """统一从 Skill 对象或 dict 提取描述。"""
    if isinstance(skill, dict):
        return skill.get("description") or ""
    return getattr(skill, "description", "") or ""


def _skill_command_items(skills: list) -> list:
    return [{"name": f"/{_skill_name(skill)} ",
             "args": "[任务描述]",
             "help": "⚡ " + _skill_description(skill),
             "kind": "skill", "insert_text": f"/{_skill_name(skill)} "}
            for skill in skills]


def _make_commands(module):
    async def handler(request):
        session_key = (request.query.get("session_key") or "").strip()
        commands = module.dispatcher.commands_table()
        main_caps = module.dispatcher.agent_config.get("main_session_caps") or {}
        commands.extend(_skill_command_items(_skills_for_session(
            module, session_key, main_caps.get("skills"))))
        return web.json_response({"commands": commands})
    return handler
