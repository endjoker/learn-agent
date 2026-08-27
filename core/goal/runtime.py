"""Durable Goal state with explicit activation and optimistic versioning."""
from __future__ import annotations
import asyncio
from typing import Any, Awaitable, Callable, Optional
from core.runtime.models import RuntimeEvent, utc_now
from core.runtime.quality_gate import QualityGate
from .models import Goal, GoalActivation, GoalStatus

class GoalRuntime:
    def __init__(self, store, *, plan_manager=None, plan_executor=None,
                 publish: Optional[Callable[[str, dict], None]] = None,
                 quality_gate: QualityGate | None = None,
                 cancel_task: Optional[Callable[[str, str], Awaitable[None]]] = None):
        self.store, self.plan_manager, self.plan_executor = store, plan_manager, plan_executor
        self.publish = publish or (lambda _event, _payload: None)
        self.quality_gate = quality_gate
        # 可选的任务取消钩子（async (task_id, reason) -> None），供
        # pause_async 联动取消 current_task_id 对应的运行时任务。未接线时
        # 退化为 store.request_cancel 的 durable 标记（TaskRuntime 在租约/
        # 重试调度时尊重 cancel_requested）。
        self._cancel_task = cancel_task

    def set_cancel_task(self, cb: Optional[Callable[[str, str], Awaitable[None]]]) -> None:
        """注入运行时任务取消回调（dispatcher.cancel_runtime_task 的适配层）。"""
        self._cancel_task = cb

    def create(self, session_id: str, objective: str, *, title: str = "",
               continuation: dict | None = None, max_rounds: int = 20,
               armed: bool = True) -> Goal:
        goal = Goal.create(session_id=session_id, objective=objective, title=title,
                           continuation=dict(continuation or {}), max_rounds=max_rounds,
                           activation=(GoalActivation.ARMED if armed else GoalActivation.DISARMED))
        self._save(goal, "goal.created")
        return goal

    def get(self, goal_id: str) -> Goal | None:
        data = self.store.get_goal(goal_id)
        return Goal.from_dict(data) if data else None

    def list(self, session_id: str, *, limit: int = 100):
        return [Goal.from_dict(item) for item in self.store.list_goals(session_id, limit=limit)]

    def update_continuation(self, goal_id: str, snapshot: dict[str, Any], *, expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if goal.is_terminal: raise ValueError("cannot update a terminal goal continuation")
        goal.continuation = dict(snapshot)
        self._touch(goal, "goal.continuation_updated")
        return goal

    def edit(self, goal_id: str, objective: str, *, expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        objective = str(objective or "").strip()
        if not objective: raise ValueError("objective cannot be empty")
        if goal.is_terminal: raise ValueError("cannot edit a terminal goal")
        goal.objective, goal.title = objective, objective[:120]
        self._touch(goal, "goal.edited")
        return goal

    def list_recoverable(self, *, limit: int = 1000) -> list[Goal]:
        statuses = {GoalStatus.ACTIVE.value, GoalStatus.PAUSED.value, GoalStatus.BLOCKED.value}
        return [Goal.from_dict(item) for item in self.store.list_goals_by_status(statuses, limit=limit)]

    def list_armed(self, *, limit: int = 1000) -> list[Goal]:
        return [goal for goal in self.list_recoverable(limit=limit) if goal.is_armed]

    def has_armed_for_session(self, session_id: str) -> bool:
        """该会话是否存在 armed 状态的 Goal（P2：janitor 驱逐豁免判定）。

        Goal 轮间隙/pause 期间会话无活动，janitor 按 idle_timeout 回收实例会
        丢失 proc 子进程会话等纯内存态；调用方应豁免此类会话。"""
        return any(
            goal.session_id == session_id
            for goal in self.list_armed(limit=100)
        )

    def attach_plan(self, goal_id: str, plan_id: str, *, expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if goal.is_terminal: raise ValueError("cannot attach plan to terminal goal")
        goal.plan_id = plan_id
        self._touch(goal, "goal.plan_attached")
        return goal

    def reserve_round(self, goal_id: str, task_id: str, *, expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if not goal.is_armed: raise ValueError("Goal is not armed")
        if goal.current_task_id: raise RuntimeError("Goal already has a current round")
        if goal.rounds_started >= goal.max_rounds:
            return self.block(goal_id, {"type": "max_rounds", "max_rounds": goal.max_rounds}, expected_version=goal.version)
        goal.rounds_started += 1
        goal.current_task_id = task_id
        goal.continuation = {**goal.continuation, "round": goal.rounds_started,
                             "revision": goal.version + 1, "task_id": task_id, "state": "queued"}
        self._touch(goal, "goal.round_reserved")
        return goal

    def finish_round(self, goal_id: str, task_id: str, *, summary: str = "",
                     expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if goal.current_task_id != task_id: raise RuntimeError("stale Goal round")
        goal.current_task_id = None
        history = list(goal.continuation.get("round_history") or [])
        if summary:
            history.append({"round": goal.rounds_started, "summary": summary[:300]})
        goal.continuation = {**goal.continuation, "task_id": task_id,
                             "state": "completed", "summary": summary[:2000],
                             "round_history": history[-8:]}
        self._touch(goal, "goal.round_completed")
        return goal

    def update_max_rounds(self, goal_id: str, max_rounds: int, *,
                          expected_version: int | None = None) -> Goal:
        """Adjust the round ceiling of a running (non-terminal) Goal."""
        goal = self._require_version(goal_id, expected_version)
        if goal.is_terminal: raise ValueError("terminal goal cannot change its round limit")
        max_rounds = max(1, int(max_rounds))
        if max_rounds < goal.rounds_started:
            raise ValueError(f"max_rounds must be >= started rounds ({goal.rounds_started})")
        goal.max_rounds = max_rounds
        self._touch(goal, "goal.max_rounds_updated")
        return goal

    def pause(self, goal_id: str, *, expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if goal.status is not GoalStatus.ACTIVE: raise ValueError("only active goals can pause")
        if goal.current_task_id:
            # 联动：把被暂停的轮次写入 continuation.dropped_rounds 审计，
            # 清除 current_task_id（该轮次的 runtime 任务由调用方经
            # pause_async(cancel=...) 取消；这里先落 durable cancel 标记，
            # 防止 TaskRuntime 在租约/重试调度时复活它）。
            self._record_dropped_round(
                goal, goal.current_task_id,
                round_no=goal.continuation.get("round") or goal.rounds_started,
                reason="paused")
            try:
                request_cancel = getattr(self.store, "request_cancel", None)
                if callable(request_cancel):
                    request_cancel(goal.current_task_id, "goal_paused")
            except Exception:
                pass
            goal.current_task_id = None
        goal.status, goal.activation = GoalStatus.PAUSED, GoalActivation.DISARMED
        self._touch(goal, "goal.paused")
        return goal

    async def pause_async(self, goal_id: str, *, expected_version: int | None = None,
                          cancel: Optional[Callable[[str, str], Awaitable[None]]] = None) -> Goal:
        """pause 的异步版本：先取消 current_task_id 对应的运行时任务再落 PAUSED。

        cancel 参数优先；未提供时使用 __init__/set_cancel_task 注入的钩子；
        都没有时 pause() 内的 store.request_cancel durable 标记兜底。
        """
        goal = self.get(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        task_id = getattr(goal, "current_task_id", None) or None
        canceller = cancel or self._cancel_task
        if task_id and canceller is not None:
            try:
                await canceller(task_id, "goal_paused")
            except (KeyError, RuntimeError, ValueError):
                pass
        return self.pause(goal_id, expected_version=expected_version)

    def record_dropped_round(self, goal_id: str, task_id: str, *, round_no: int,
                             reason: str, expected_version: int | None = None) -> Goal:
        """记录被替代/被丢弃的轮次（continuation.dropped_rounds 审计）。

        驱动在 wait 返回后发现 current_task_id 已不是本轮（被暂停/被新一轮
        替代）时调用，保留轮次轨迹，避免结果静默丢失。
        """
        goal = self._require_version(goal_id, expected_version)
        if goal.is_terminal:
            return goal
        self._record_dropped_round(goal, task_id, round_no=round_no, reason=reason)
        return goal

    def _record_dropped_round(self, goal: Goal, task_id: str, *, round_no: int,
                              reason: str) -> None:
        dropped = list(goal.continuation.get("dropped_rounds") or [])
        dropped.append({"round": int(round_no or 0), "task_id": str(task_id),
                        "reason": str(reason or ""), "at": utc_now()})
        goal.continuation = {**goal.continuation, "dropped_rounds": dropped[-20:]}
        self._touch(goal, "goal.round_dropped")

    def resume(self, goal_id: str, *, expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if goal.status not in {GoalStatus.PAUSED, GoalStatus.BLOCKED, GoalStatus.ACTIVE}:
            raise ValueError("only active, paused or blocked goals can resume")
        goal.status, goal.activation, goal.blocked_reason = GoalStatus.ACTIVE, GoalActivation.ARMED, None
        goal.current_task_id = None
        self._touch(goal, "goal.resumed")
        return goal

    def disarm(self, goal_id: str, *, expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if goal.is_terminal: return goal
        goal.activation, goal.current_task_id = GoalActivation.DISARMED, None
        self._touch(goal, "goal.disarmed")
        return goal

    def block(self, goal_id: str, reason: dict[str, Any], *, expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if goal.is_terminal: raise ValueError("terminal goal cannot be blocked")
        goal.status, goal.activation = GoalStatus.BLOCKED, GoalActivation.DISARMED
        goal.blocked_reason, goal.current_task_id = dict(reason), None
        self._touch(goal, "goal.blocked")
        return goal

    def cancel(self, goal_id: str, *, expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if goal.is_terminal: return goal
        goal.status, goal.activation, goal.current_task_id = GoalStatus.CANCELLED, GoalActivation.DISARMED, None
        self._touch(goal, "goal.cancelled")
        return goal

    def complete(self, goal_id: str, *, text: str = "", acceptance: list[dict] | None = None,
                 expected_version: int | None = None) -> Goal:
        goal = self._require_version(goal_id, expected_version)
        if goal.status is not GoalStatus.ACTIVE: raise ValueError("only active goals can complete")
        if acceptance and self.quality_gate:
            # 同步调用方（agent 工具线程 / 旧 reconcile 路径）直接求值；
            # 事件循环内请用 complete_async 避免 quality_gate 阻塞 loop。
            report = self.quality_gate.evaluate(acceptance, session_id=goal.session_id, text=text)
            if not report.passed:
                return self.block(goal_id, {"type": "quality_gate_failed", "checks": report.to_list()}, expected_version=goal.version)
        return self._finish_complete(goal)

    async def complete_async(self, goal_id: str, *, text: str = "", acceptance: list[dict] | None = None,
                             expected_version: int | None = None) -> Goal:
        """complete 的事件循环版本：quality_gate.evaluate 放入线程池（与 plan 对称）。

        quality_gate.evaluate 可能同步执行 subprocess，在事件循环内直接调用会
        阻塞其他协程；本方法供 api_chat/工具等 async 调用方使用。
        """
        goal = self._require_version(goal_id, expected_version)
        if goal.status is not GoalStatus.ACTIVE: raise ValueError("only active goals can complete")
        if acceptance and self.quality_gate:
            report = await asyncio.to_thread(
                self.quality_gate.evaluate, acceptance,
                session_id=goal.session_id, text=text)
            if not report.passed:
                return self.block(goal_id, {"type": "quality_gate_failed", "checks": report.to_list()}, expected_version=goal.version)
        return self._finish_complete(goal)

    def _finish_complete(self, goal: Goal) -> Goal:
        goal.status, goal.activation = GoalStatus.COMPLETED, GoalActivation.DISARMED
        goal.progress, goal.current_task_id = 1.0, None
        self._touch(goal, "goal.completed")
        return goal

    def archive(self, goal_id: str, *, expected_version: int | None = None) -> Goal:
        """Remove a Goal business record (terminal or not).

        A non-terminal Goal is cancelled first so its driver stops; then the
        Goal row, its linked Plans/Teams are deleted from the store.
        Conversation/memory keep the objective and round history.
        """
        goal = self._require_version(goal_id, expected_version)
        if not goal.is_terminal:
            goal.status, goal.activation = GoalStatus.CANCELLED, GoalActivation.DISARMED
            goal.current_task_id = None
        self.store.delete_goal(goal_id)
        return goal

    def reconcile_plan(self, plan) -> Goal | None:
        goal_id = getattr(plan, "goal_id", None)
        if not goal_id: return None
        goal = self.get(goal_id)
        if goal is None or goal.is_terminal: return goal
        if plan.status.value == "completed": return self.complete(goal_id, text=f"Plan {plan.plan_id} completed")
        if plan.status.value in {"failed", "cancelled"}:
            return self.block(goal_id, {"type": "plan_terminal", "plan_id": plan.plan_id, "status": plan.status.value})
        return goal

    def _require_version(self, goal_id, expected_version):
        goal = self.get(goal_id)
        if goal is None: raise KeyError(goal_id)
        if expected_version is not None and goal.version != expected_version: raise RuntimeError("goal version conflict")
        return goal

    def _touch(self, goal: Goal, event_type: str) -> None:
        goal.version += 1
        goal.updated_at = utc_now()
        self._save(goal, event_type)

    def _save(self, goal, event_type):
        payload = goal.to_dict()
        self.store.save_goal(payload, RuntimeEvent.create(event_type, session_id=goal.session_id,
            data={"goal_id": goal.goal_id, "status": goal.status.value,
                  "activation": goal.activation.value, "version": goal.version,
                  "rounds_started": goal.rounds_started, "max_rounds": goal.max_rounds,
                  "current_task_id": goal.current_task_id}))
        self.publish(event_type, payload)
