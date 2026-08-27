# -*- coding: utf-8 -*-
"""Model-callable adapters for root-session Plan and Goal capabilities."""
from __future__ import annotations
import asyncio
import json
from datetime import datetime
from gateway.dispatcher import plan_approval_required
from tools.base_tool import BaseTool

def _log_cancel_plan_failure(future) -> None:
    """记录关联 Plan 异步取消的结果：异常不静默吞掉（防 Task exception never retrieved）。"""
    try:
        exc = future.exception()
    except Exception:
        return
    if exc is not None and not isinstance(exc, asyncio.CancelledError):
        import logging
        logging.getLogger("jk_agent.gateway").warning(
            "取消关联 Plan 失败: %s", exc)


class _StructuredCapabilityTool(BaseTool):
    def __init__(self, module, entry, agent):
        self.module, self.entry, self.agent = module, entry, agent
    @staticmethod
    def _require_text(value: str, field: str) -> str:
        value = str(value or "").strip()
        if not value: raise ValueError(f"{field} 不能为空")
        return value
    def _loop(self):
        loop = self.module.bus._loop
        if loop is None: raise RuntimeError("WebUI 事件循环未就绪")
        return loop
    def _scoped(self, payload):
        event = {**payload, "session_key": self.entry.session_key}
        if self.entry.session_key.startswith("workspace:"):
            parts = self.entry.session_key.split(":", 2)
            if len(parts) == 3:
                event["workspace_id"], event["workspace_session_id"] = parts[1], parts[2]
        return event

class CreatePlanTool(_StructuredCapabilityTool):
    name = "create_plan"
    description = ("为复杂但边界明确的任务生成结构化 Plan，并立即批准和执行。"
                   "无需用户再次审核；需求或关键决策不明确时必须先询问用户。")
    parameters = {"type":"object","properties":{"objective":{"type":"string"}},"required":["objective"]}
    def execute(self, objective: str, **_kwargs) -> str:
        objective = self._require_text(objective, "objective")
        preview = self.module.glue.plan_preview_sync(self.agent, objective)
        plan = self.module.glue.create_plan(self.entry.session_key, objective, preview["plan"])
        if plan_approval_required():
            # 两阶段审批：create_preview 后不自动 approve，进入 AWAITING_APPROVAL，
            # 等待既有 /api/plan/{plan_id}/approve|reject 端点确认。
            self.module.bus.publish("plan.changed", self._scoped(
                {"action":"awaiting_approval", "plan":plan.to_dict()}))
            return json.dumps({"plan_id":plan.plan_id,"status":plan.status.value,
                               "started":False,"awaiting_approval":True}, ensure_ascii=False)
        plan = self.module.glue.plan_manager.approve(plan.plan_id, actor="agent")
        self.module.bus.publish("plan.changed", self._scoped({"action":"approved", "plan":plan.to_dict()}))
        self._loop().call_soon_threadsafe(self.module.plan_runtime.start, plan.plan_id)
        return json.dumps({"plan_id":plan.plan_id,"status":plan.status.value,"started":True}, ensure_ascii=False)

class CreateGoalTool(_StructuredCapabilityTool):
    name = "create_goal"
    description = ("创建 active/armed 的长期 Goal，并由同会话 GoalRoundDriver 自动多轮推进。"
                   "仅用于目标和完成标准明确的长期工作；模糊时先询问用户。")
    parameters = {"type":"object","properties":{"objective":{"type":"string"},
        "max_rounds":{"type":"integer","description":"自动轮次上限，默认20"}},"required":["objective"]}
    def execute(self, objective: str, max_rounds: int = 20, **_kwargs) -> str:
        objective = self._require_text(objective, "objective")
        session_id = self.module.dispatcher._runtime_session_id(self.entry.session_key)
        self.module.runtime_store.upsert_session(session_id, self.entry.session_key, channel="webui", status="active")
        goal = self.module.goal_runtime.create(session_id, objective, max_rounds=max_rounds)
        self._loop().call_soon_threadsafe(self.module.goal_driver.trigger, goal.goal_id)
        self.module.bus.publish("goal.changed", self._scoped({"action":"created", "goal":goal.to_dict()}))
        return json.dumps(goal.to_dict(), ensure_ascii=False)

class _GoalActionTool(_StructuredCapabilityTool):
    action = ""
    parameters = {"type":"object","properties":{"goal_id":{"type":"string"}},"required":["goal_id"]}
    def execute(self, goal_id: str, **_kwargs) -> str:
        goal_id = self._require_text(goal_id, "goal_id")
        goal = getattr(self.module.goal_runtime, self.action)(goal_id)
        if self.action == "resume": self._loop().call_soon_threadsafe(self.module.goal_driver.trigger, goal.goal_id)
        self.module.bus.publish("goal.changed", self._scoped({"action":self.action,"goal":goal.to_dict()}))
        return json.dumps(goal.to_dict(), ensure_ascii=False)

class PauseGoalTool(_GoalActionTool):
    name="pause_goal"; action="pause"; description="暂停当前 Goal 并停止自动续跑。"
    def execute(self, goal_id: str, **_kwargs) -> str:
        goal_id = self._require_text(goal_id, "goal_id")
        # 联动取消 current_task_id 对应的运行时任务（async 路径经事件循环）。
        future = asyncio.run_coroutine_threadsafe(
            self.module.goal_runtime.pause_async(
                goal_id, cancel=self.module.dispatcher.cancel_runtime_task),
            self._loop())
        goal = future.result(timeout=60)
        self.module.bus.publish("goal.changed", self._scoped({"action":"paused","goal":goal.to_dict()}))
        return json.dumps(goal.to_dict(), ensure_ascii=False)
class ResumeGoalTool(_GoalActionTool):
    name="resume_goal"; action="resume"; description="恢复 Goal 并重新启动自动续跑。"
class CompleteGoalTool(_GoalActionTool):
    name="complete_goal"; action="complete"; description="仅在已有明确完成证据时将 Goal 标记完成。"
    def execute(self, goal_id: str, **_kwargs) -> str:
        goal_id = self._require_text(goal_id, "goal_id")
        # quality_gate.evaluate 走线程池（complete_async，与 plan 对称），
        # 避免在 agent 工具线程内同步执行 subprocess。
        future = asyncio.run_coroutine_threadsafe(
            self.module.goal_runtime.complete_async(goal_id), self._loop())
        goal = future.result(timeout=300)
        self.module.bus.publish("goal.changed", self._scoped({"action":"completed","goal":goal.to_dict()}))
        return json.dumps(goal.to_dict(), ensure_ascii=False)
class CancelGoalTool(_StructuredCapabilityTool):
    name="cancel_goal"
    description=("终止当前 Goal 并停止自动续跑；目标确定无法完成或用户要求停止时调用。\n"
                 "关联的 Plan（如有）会一并取消。")
    parameters={"type":"object","properties":{"goal_id":{"type":"string"}},"required":["goal_id"]}
    def execute(self, goal_id: str, **_kwargs) -> str:
        goal_id = self._require_text(goal_id, "goal_id")
        current = self.module.goal_runtime.get(goal_id)
        if current is None:
            raise KeyError(goal_id)
        # 与 REST cancel 一致：取消关联的 Plan（fire-and-forget 提交到事件循环）
        if getattr(current, "plan_id", None):
            try:
                plan = self.module.plan_runtime.manager.get(current.plan_id)
                if plan is not None and not getattr(plan, "is_terminal", True):
                    future = asyncio.run_coroutine_threadsafe(
                        self.module.plan_runtime.cancel(current.plan_id), self._loop())
                    # 登记回调记录异常：run_coroutine_threadsafe 的 Future 不取
                    # result() 时异常会被吞掉（"exception was never retrieved"）
                    future.add_done_callback(_log_cancel_plan_failure)
            except Exception:
                pass
        goal = self.module.goal_runtime.cancel(goal_id)
        self.module.bus.publish("goal.changed", self._scoped({"action":"cancelled","goal":goal.to_dict()}))
        return json.dumps(goal.to_dict(), ensure_ascii=False)

def _goal_view(goal) -> dict:
    """Compact model-visible Goal projection (dsh get_goal canonical shape)."""
    return {
        "id": goal.goal_id,
        "revision": goal.version,
        "objective": goal.objective,
        "phase": goal.status.value,
        "activation": goal.activation.value,
        "roundsStarted": goal.rounds_started,
        "maxGoalRounds": goal.max_rounds,
        "progress": goal.progress,
        "blockedReason": goal.blocked_reason,
        "planId": goal.plan_id,
        "updatedAt": goal.updated_at,
    }


def _plan_view(plan) -> dict:
    """Compact model-visible Plan projection with per-task execution state."""
    tasks = [{
        "id": task.plan_task_id,
        # 运行时任务 id（PlanExecutor.assign_task 在提交时写回），用于关联实时状态。
        "taskId": task.task_id or None,
        "description": task.description,
        "status": task.status.value,
        "dependsOn": list(task.depends_on),
        "resultSummary": (task.result_summary or "")[:500],
        "blockedReason": task.blocked_reason,
        "attempts": task.attempts,
    } for task in plan.tasks]
    return {
        "id": plan.plan_id,
        "revision": plan.version,
        "title": plan.title,
        "status": plan.status.value,
        "progress": round(plan.progress, 3),
        "goalId": plan.goal_id,
        "tasks": tasks,
        "updatedAt": plan.updated_at,
    }


class GetGoalTool(_StructuredCapabilityTool):
    name = "get_goal"
    description = ("查询当前会话的长期 Goal 状态：目标、阶段、已消耗轮次/上限、是否自动续跑、阻塞原因、"
                   "关联 Plan。不传 goal_id 时返回当前会话的活动 Goal。调用 update_goal 前必须先 get_goal 取 id 与 revision。")
    parameters = {"type":"object","properties":{"goal_id":{"type":"string","description":"Goal id，可省略"}}}
    def execute(self, goal_id: str | None = None, **_kwargs) -> str:
        goal_id = str(goal_id or "").strip() or None
        session_id = self.module.dispatcher._runtime_session_id(self.entry.session_key)
        goal = None
        if goal_id:
            goal = self.module.goal_runtime.get(goal_id)
        else:
            goals = self.module.goal_runtime.list(session_id, limit=100)
            current = [g for g in goals if g.status.value in {"active", "paused", "blocked"}]
            goal = current[0] if current else (goals[0] if goals else None)
        if goal is None:
            return json.dumps({"goal": None}, ensure_ascii=False)
        return json.dumps({"goal": _goal_view(goal)}, ensure_ascii=False)


class ListGoalsTool(_StructuredCapabilityTool):
    name = "list_goals"
    description = "列出当前会话的全部 Goal 及其阶段/轮次，按最近更新排序。"
    parameters = {"type":"object","properties":{"limit":{"type":"integer"}}}
    def execute(self, limit: int = 20, **_kwargs) -> str:
        session_id = self.module.dispatcher._runtime_session_id(self.entry.session_key)
        goals = self.module.goal_runtime.list(session_id, limit=max(1, min(int(limit or 20), 100)))
        return json.dumps({"goals": [_goal_view(g) for g in goals]}, ensure_ascii=False)


class GetPlanTool(_StructuredCapabilityTool):
    name = "get_plan"
    description = ("查询当前会话的 Plan 状态：标题、阶段、总体进度、以及每个 step 的状态/结果摘要/阻塞原因。"
                   "不传 plan_id 时返回当前会话最近的非终态 Plan。")
    parameters = {"type":"object","properties":{"plan_id":{"type":"string","description":"Plan id，可省略"}}}
    def execute(self, plan_id: str | None = None, **_kwargs) -> str:
        plan_id = str(plan_id or "").strip() or None
        session_id = self.module.dispatcher._runtime_session_id(self.entry.session_key)
        manager = self.module.plan_runtime.manager
        plan = None
        if plan_id:
            plan = manager.get(plan_id)
        else:
            plans = manager.list(session_id, limit=100)
            current = [p for p in plans if p.status.value in {"approved", "active", "paused"}]
            plan = current[0] if current else (plans[0] if plans else None)
        if plan is None:
            return json.dumps({"plan": None}, ensure_ascii=False)
        return json.dumps({"plan": _plan_view(plan)}, ensure_ascii=False)


class ListPlansTool(_StructuredCapabilityTool):
    name = "list_plans"
    description = "列出当前会话的全部 Plan 及阶段/进度。"
    parameters = {"type":"object","properties":{"limit":{"type":"integer"}}}
    def execute(self, limit: int = 20, **_kwargs) -> str:
        session_id = self.module.dispatcher._runtime_session_id(self.entry.session_key)
        plans = self.module.plan_runtime.manager.list(session_id, limit=max(1, min(int(limit or 20), 100)))
        return json.dumps({"plans": [_plan_view(p) for p in plans]}, ensure_ascii=False)


class GetRuntimeStatusTool(_StructuredCapabilityTool):
    """实时聚合 Plan/Goal 子任务运行状态（持久进度 + 正在执行的活动）。

    数据源全部为同步只读 SQLite（RuntimeStore / ConversationStore），可在 Agent
    同步线程内直接调用；不触碰 TaskRuntime 的 async 内存态。
    """

    name = "get_runtime_status"
    description = ("实时查询当前会话 Plan/Goal 子任务的运行状态：持久进度（阶段/步骤/轮次）"
                   "加上正在执行子任务的实时活动（最近工具调用/推理、已耗时）。"
                   "需要掌握后台任务当前进展、判断是否卡住或已完成时调用。")
    parameters = {"type": "object", "properties": {
        "plan_id": {"type": "string", "description": "Plan id，可省略"},
        "goal_id": {"type": "string", "description": "Goal id，可省略"},
        "activity_limit": {"type": "integer", "description": "最近活动条数上限，默认 10"},
    }}

    _RUNNING_TASK_STATUSES = {"queued", "leased", "running", "waiting_approval", "retry_wait"}
    _SUMMARY_MAX = 200

    def execute(self, plan_id: str | None = None, goal_id: str | None = None,
                activity_limit: int = 10, **_kwargs) -> str:
        session_id = self.module.dispatcher._runtime_session_id(self.entry.session_key)
        plan_view = self._plan_section(str(plan_id or "").strip() or None, session_id)
        goal_view = self._goal_section(str(goal_id or "").strip() or None, session_id)
        live = self._live_section(session_id, plan_view, goal_view, activity_limit)
        return json.dumps({"plan": plan_view, "goal": goal_view, "live": live},
                          ensure_ascii=False)

    # ---- 持久态（复用既有查询语义） ----

    def _plan_section(self, plan_id: str | None, session_id: str) -> dict | None:
        manager = self.module.plan_runtime.manager
        if plan_id:
            plan = manager.get(plan_id)
        else:
            plans = manager.list(session_id, limit=100)
            current = [p for p in plans if p.status.value in {"approved", "active", "paused"}]
            plan = current[0] if current else (plans[0] if plans else None)
        return _plan_view(plan) if plan else None

    def _goal_section(self, goal_id: str | None, session_id: str) -> dict | None:
        runtime = self.module.goal_runtime
        if goal_id:
            goal = runtime.get(goal_id)
        else:
            goals = runtime.list(session_id, limit=100)
            current = [g for g in goals if g.status.value in {"active", "paused", "blocked"}]
            goal = current[0] if current else (goals[0] if goals else None)
        return _goal_view(goal) if goal else None

    # ---- 实时段（运行任务 + 最近活动） ----

    def _live_section(self, session_id: str, plan_view: dict | None,
                      goal_view: dict | None, activity_limit) -> dict:
        runtime_ids = {str(v) for v in ((plan_view or {}).get("id"),
                                        (goal_view or {}).get("id")) if v}
        live: dict = {"running_task_id": None, "stage": "idle", "recent_activity": []}
        if not plan_view and not goal_view:
            return live
        running = self._running_tasks(session_id, runtime_ids)
        if running:
            snapshot = running[0]
            record = snapshot.record
            live["running_task_id"] = record.task_id
            # 子任务对应的 plan step / goal 轮次 id，便于模型定位到具体步骤。
            live["current_task_id"] = snapshot.envelope.plan_task_id or None
            live["current_task_status"] = record.status.value
            live["started_at"] = record.started_at
            live["elapsed_seconds"] = self._elapsed_seconds(record.started_at)
        recent = self._recent_activity(runtime_ids, activity_limit)
        live["recent_activity"] = recent
        if any(a.get("type") == "tool" and a.get("status") == "running" for a in recent):
            live["stage"] = "tool"
        elif recent and recent[0].get("type") == "reasoning":
            live["stage"] = "reasoning"
        elif running:
            live["stage"] = "llm"
        # P1：stall 检测——运行中但最近活动时间距今超过阈值（无节点时退化为 started_at）。
        if running:
            last_activity_at = recent[0].get("at") if recent else None
            stall_after = last_activity_at or live.get("started_at")
            elapsed_since_activity = self._elapsed_seconds(stall_after)
            if elapsed_since_activity is not None:
                live["last_activity_at"] = last_activity_at or live.get("started_at")
                live["seconds_since_activity"] = elapsed_since_activity
                if elapsed_since_activity > 120:
                    live["stage"] = "stalled"
        return live

    def _running_tasks(self, session_id: str, runtime_ids: set[str]) -> list:
        try:
            tasks = self.module.runtime_store.list_tasks(session_id=session_id)
        except Exception:
            return []
        running = []
        for snapshot in tasks:
            status = str(snapshot.record.status.value)
            if status not in self._RUNNING_TASK_STATUSES:
                continue
            envelope = snapshot.envelope
            markers = {str(envelope.plan_id or ""),
                       str((envelope.metadata or {}).get("goal_id") or "")}
            if runtime_ids and not (markers & runtime_ids):
                continue
            running.append(snapshot)
        return running

    @staticmethod
    def _elapsed_seconds(started_at: str | None) -> float | None:
        if not started_at:
            return None
        try:
            started = datetime.fromisoformat(started_at)
            return round((datetime.now(started.tzinfo) - started).total_seconds(), 1)
        except Exception:
            return None

    def _recent_activity(self, runtime_ids: set[str], limit) -> list[dict]:
        try:
            count = max(1, min(int(limit or 10), 50))
        except (TypeError, ValueError):
            count = 10
        # P2 快速路径：优先读 bridge 内存环形缓冲（未打 SQLite）；为空回退 SQLite。
        bridge = getattr(self.module, "conversation_bridge", None)
        read_buffer = getattr(bridge, "recent_runtime_activity", None)
        if callable(read_buffer) and runtime_ids:
            buffered: list[dict] = []
            for runtime_id in runtime_ids:
                try:
                    buffered.extend(read_buffer(runtime_id, count))
                except Exception:
                    pass
            if buffered:
                buffered.sort(key=lambda a: str(a.get("at") or ""))
                buffered = buffered[-count:]
                return [{k: v for k, v in item.items() if k in {
                    "type", "tool", "status", "params_summary",
                    "result_summary", "text", "at"}} for item in buffered]
        try:
            conversation = self.module.conversation_bridge.resolve(self.entry.session_key)
            page = self.module.conversation_service.history(conversation.conversation_id,
                                                            limit=30)
        except Exception:
            return []
        nodes = [node for item in (page.get("items") or [])
                 for node in (item.get("nodes") or [])]
        activity = []
        for node in nodes:
            meta = node.get("metadata") or {}
            if meta.get("runtime_type") not in {"plan", "goal"}:
                continue
            if runtime_ids and meta.get("runtime_id") not in runtime_ids:
                continue
            node_type = node.get("type")
            if node_type not in {"tool", "reasoning"}:
                continue
            at = node.get("updated_at") or node.get("created_at") or ""
            activity.append({
                "type": node_type,
                "tool": str(meta.get("tool") or meta.get("call_id") or "") or None,
                "status": node.get("status"),
                "params_summary": str(meta.get("params_summary") or "")[:self._SUMMARY_MAX],
                "result_summary": str(meta.get("result_summary") or "")[:self._SUMMARY_MAX],
                "text": str(node.get("text") or "")[:self._SUMMARY_MAX] if node_type == "reasoning" else None,
                "at": at,
                "_sort": at,
            })
        activity.sort(key=lambda a: a["_sort"])
        activity = activity[-count:]
        for item in activity:
            item.pop("_sort", None)
        return activity


class UpdateGoalTool(_StructuredCapabilityTool):
    name = "update_goal"
    description = ("管理当前会话的 Goal：edit(改目标)/pause(暂停)/resume(续跑)/complete(完成)/cancel(终止)/blocked(阻塞)。"
                   "需先 get_goal 取 goal_id 与 revision（revision 不匹配会报错）；只有证据确凿时才能 complete。")
    parameters = {"type":"object","properties":{
        "goal_id":{"type":"string"},"revision":{"type":"integer"},
        "action":{"type":"string","enum":["edit","pause","resume","complete","cancel","blocked"]},
        "objective":{"type":"string"},"blocked_reason":{"type":"string"}},
        "required":["goal_id","action"]}
    def execute(self, goal_id: str, action: str, *, objective: str | None = None,
                revision: int | None = None, blocked_reason: str | None = None, **_kwargs) -> str:
        goal_id = self._require_text(goal_id, "goal_id")
        action = self._require_text(action, "action")
        rt = self.module.goal_runtime
        expected_version = int(revision) if revision is not None else None
        if action == "edit":
            goal = rt.edit(goal_id, self._require_text(objective, "objective"), expected_version=expected_version)
        elif action == "pause":
            # 联动取消 current_task_id 任务（async 路径经事件循环）。
            goal = asyncio.run_coroutine_threadsafe(
                rt.pause_async(goal_id, expected_version=expected_version,
                               cancel=self.module.dispatcher.cancel_runtime_task),
                self._loop()).result(timeout=60)
        elif action == "resume":
            goal = rt.resume(goal_id, expected_version=expected_version)
        elif action == "complete":
            # quality_gate.evaluate 走线程池（complete_async，与 plan 对称）。
            goal = asyncio.run_coroutine_threadsafe(
                rt.complete_async(goal_id, expected_version=expected_version),
                self._loop()).result(timeout=300)
        elif action == "cancel":
            goal = rt.cancel(goal_id, expected_version=expected_version)
        elif action == "blocked":
            goal = rt.block(goal_id, {"type": "model-reported", "message": str(blocked_reason or "")},
                            expected_version=expected_version)
        else:
            raise ValueError(f"unknown goal action: {action}")
        if action == "resume":
            self._loop().call_soon_threadsafe(self.module.goal_driver.trigger, goal.goal_id)
        self.module.bus.publish("goal.changed", self._scoped({"action": action, "goal": goal.to_dict()}))
        return json.dumps(_goal_view(goal), ensure_ascii=False)


class UpdatePlanTool(_StructuredCapabilityTool):
    name = "update_plan"
    description = ("管理当前会话的 Plan：pause(暂停)/resume(续跑)/cancel(终止)。"
                   "需先 get_plan 取 plan_id；只有非终态 Plan 才能变更。")
    parameters = {"type":"object","properties":{
        "plan_id":{"type":"string"},"action":{"type":"string","enum":["pause","resume","cancel"]}},
        "required":["plan_id","action"]}
    def execute(self, plan_id: str, action: str, **_kwargs) -> str:
        plan_id = self._require_text(plan_id, "plan_id")
        action = self._require_text(action, "action")
        rt = self.module.plan_runtime
        if action == "pause":
            plan = rt.pause(plan_id)
        elif action == "resume":
            plan = rt.resume(plan_id)
        elif action == "cancel":
            plan = rt.cancel(plan_id)
        else:
            raise ValueError(f"unknown plan action: {action}")
        self.module.bus.publish("plan.changed", self._scoped({"action": action, "plan": plan.to_dict()}))
        return json.dumps(_plan_view(plan), ensure_ascii=False)


class CreateSubagentTool(_StructuredCapabilityTool):
    name="create_subagent"
    description="将边界清晰、可独立完成的子任务委派给一个直接子 Agent。子 Agent 不可继续派生。"
    parameters={"type":"object","properties":{"task":{"type":"string"}},"required":["task"]}
    # 等待子 Agent 完成的上限：子任务有自己的 soft/hard 超时，
    # 父工具最多等这么久再返回，避免父轮次无限挂起。
    WAIT_TIMEOUT_S = 300

    def execute(self, task: str, **_kwargs) -> str:
        task=self._require_text(task,"task")
        if not self.module.dispatcher.task_runtime_enabled: raise RuntimeError("SessionRuntime 未启用")
        session_id=self.module.dispatcher._runtime_session_id(self.entry.session_key)
        future=asyncio.run_coroutine_threadsafe(self.module.subagent_runtime.create(
            parent_session_id=session_id,parent_session_key=self.entry.session_key,
            prompt=task,mode="one-shot",parent_is_root=True),self._loop())
        # 创建失败（如 SessionRuntime 拒绝）直接抛给调用方，不再静默吞掉。
        report = future.result(timeout=60)
        child_id = report.child_id
        try:
            completed = asyncio.run_coroutine_threadsafe(
                self.module.subagent_runtime.wait_report(child_id, timeout=self.WAIT_TIMEOUT_S),
                self._loop()).result(timeout=self.WAIT_TIMEOUT_S + 30)
        except (asyncio.TimeoutError, TimeoutError):
            return json.dumps({"status": "running", "child_id": child_id,
                               "message": "子 Agent 仍在运行，结果稍后通过事件可见"}, ensure_ascii=False)
        return json.dumps({
            "status": completed.status, "child_id": child_id,
            "summary": completed.summary, "artifact_ids": completed.artifact_ids,
        }, ensure_ascii=False)

class AskQuestionTool(_StructuredCapabilityTool):
    name = "ask_question"
    description = ("向用户发起结构化提问（候选项 + 自定义输入），WebUI 将弹出选择框。"
                   "当需要用户做产品决策、补充参数或在候选方案中选择时必须使用本工具，"
                   "而不是在普通文本中罗列问题。返回用户的实际选择；"
                   "若返回 status 为 cancelled，表示用户明确取消了该问题，应跳过提问"
                   "按当前信息继续或改用文字询问，不要原样重复发起同一问题；"
                   "若返回 status 为 timeout / unavailable / fail_closed，表示用户未作答，"
                   "不得假设任何默认选项（recommended 仅展示、不会自动选中），必须基于该状态继续。")
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "向用户提出的问题（必填）"},
            "options": {
                "type": "array",
                "description": "候选项列表（推荐 2-5 项）",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "选项唯一标识"},
                        "label": {"type": "string", "description": "选项展示文本"},
                        "description": {"type": "string", "description": "选项补充说明（可选）"},
                        "recommended": {"type": "boolean", "description": "是否推荐（仅展示提示，不会自动选中）"},
                    },
                    "required": ["id", "label"],
                },
            },
            "title": {"type": "string", "description": "弹窗标题（可选）"},
            "allow_custom": {"type": "boolean", "description": "是否允许用户自定义输入，默认 true"},
            "custom_placeholder": {"type": "string", "description": "自定义输入框占位提示"},
            "multiple": {"type": "boolean", "description": "是否允许多选，默认 false"},
            "required": {"type": "boolean", "description": "是否必答，默认 false"},
            "timeout_s": {"type": "integer", "description": "等待秒数（默认 300，超时 fail-closed）"},
        },
        "required": ["question"],
    }

    def execute(self, question: str, options=None, title=None, allow_custom=True,
                custom_placeholder=None, multiple=False, required=False,
                timeout_s=None, **_kwargs) -> str:
        module = self.module
        # WebUI 不可用：立即返回明确状态，不阻塞、不替用户选择
        if (not getattr(module, "_started", False)
                or getattr(getattr(module, "bus", None), "_loop", None) is None):
            return json.dumps({
                "status": "unavailable",
                "reason": "WebUI 不可用，当前通道不支持结构化弹窗；需要用户输入",
            }, ensure_ascii=False)
        try:
            question = self._require_text(question, "question")
            context = self._question_context()
            result = module.glue.question_bridge.ask(
                self.entry.session_key, question, options or [],
                title=title, allow_custom=allow_custom,
                custom_placeholder=custom_placeholder, multiple=multiple,
                required=required, timeout_s=timeout_s, context=context)
        except ValueError as exc:
            return json.dumps({"status": "invalid", "error": str(exc)},
                              ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

    def _question_context(self) -> dict:
        """从当前轮次归属元数据（agent._webui_metadata，由 Dispatcher 在每次
        agent.run 前写入）推导归属上下文，供答复校验（session/workspace/message
        上下文必须一致，防止跨会话/跨消息答复）。"""
        meta = dict(getattr(self.agent, "_webui_metadata", None) or {})
        ctx = {
            "message_id": str(meta.get("message_id") or ""),
            "snapshot_id": str(meta.get("snapshot_id")
                              or getattr(self.entry, "runtime_snapshot_id", "") or ""),
            "workspace_id": str(meta.get("workspace_id") or ""),
            "workspace_session_id": str(meta.get("workspace_session_id") or ""),
        }
        key = self.entry.session_key
        if key.startswith("workspace:"):
            parts = key.split(":", 2)
            if len(parts) == 3:
                if not ctx["workspace_id"]:
                    ctx["workspace_id"] = parts[1]
                if not ctx["workspace_session_id"]:
                    ctx["workspace_session_id"] = parts[2]
        return ctx

def register_structured_capability_tools(agent, module, entry) -> None:
    registry=agent.tool_registry
    for tool_type in (GetPlanTool,ListPlansTool,UpdatePlanTool,
                      GetGoalTool,ListGoalsTool,UpdateGoalTool,
                      GetRuntimeStatusTool,
                      PauseGoalTool,ResumeGoalTool,CompleteGoalTool,CancelGoalTool,
                      CreatePlanTool,CreateGoalTool,CreateSubagentTool,AskQuestionTool):
        if registry.get_tool(tool_type.name) is None: registry.register_tool(tool_type(module,entry,agent))
    agent._rebuild_system_prompt()
