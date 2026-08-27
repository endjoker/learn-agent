"""Gateway adapter that runs durable Plans through Dispatcher/TaskRuntime."""

from __future__ import annotations

from core.plan import PlanExecutor, PlanManager, PlanStatus
from core.runtime import ArtifactStore
from core.runtime.quality_gate import QualityGate


class PlanRuntime:
    def __init__(self, dispatcher, store, bus, artifact_store=None, on_terminal=None):
        self.dispatcher = dispatcher
        self.store = store
        self.bus = bus
        self.on_terminal = on_terminal
        self.quality_gate = QualityGate(artifact_store or ArtifactStore(store))
        self.manager = PlanManager(store)
        # P1：等待安全网对齐任务信封真实预算——信封用 default_timeout_seconds/
        # hard_timeout（默认 1200s），安全网此前写死 300s，合法长任务被提前置
        # blocked 而底层仍在跑，watchdog 随后反复空转重触发直到信封超时。
        budget_seconds = self._task_budget_seconds()
        self._executor = PlanExecutor(
            self.manager, submit_task=self._submit_task, wait_task=self.dispatcher.wait_runtime_task,
            publish=self._publish, artifact_store=artifact_store,
            wait_task_timeout_seconds=float(budget_seconds) + 30.0,
        )

    def _task_budget_seconds(self) -> int:
        """从 dispatcher 读取任务信封默认预算；拿不到时保守回落 1200s。"""
        getter = getattr(self.dispatcher, "runtime_task_budget_seconds", None)
        if callable(getter):
            try:
                return int(getter())
            except (TypeError, ValueError):
                pass
        return 1200

    def start(self, plan_id: str):
        return self._executor.start(plan_id)

    async def pause(self, plan_id: str):
        plan = self.manager.pause(plan_id)
        self._publish("paused", {"plan": plan.to_dict()})
        return plan

    async def resume(self, plan_id: str):
        plan = self.manager.activate(plan_id)
        self._publish("resumed", {"plan": plan.to_dict()})
        if not plan.is_terminal:
            self.start(plan.plan_id)
        return plan

    async def cancel(self, plan_id: str):
        before = self.manager.get(plan_id)
        if before is None:
            raise KeyError(plan_id)
        runtime_task_ids = [task.task_id for task in before.tasks if task.task_id and not task.is_terminal]
        plan = self.manager.cancel(plan_id)
        for task_id in runtime_task_ids:
            try:
                await self.dispatcher.cancel_runtime_task(task_id, reason="plan_cancelled")
            except (KeyError, RuntimeError):
                # The task may already have crossed its terminal boundary. The
                # durable Plan cancellation remains authoritative in either case.
                pass
        self._publish("cancelled", {"plan": plan.to_dict()})
        return plan

    async def _submit_task(self, *, plan, plan_task, task_id: str, prompt: str) -> str:
        # 空任务列表守卫：plan.tasks 为空时取不到 tasks[-1]，避免 IndexError
        last_task_id = plan.tasks[-1].plan_task_id if plan.tasks else ""
        return await self.dispatcher.submit_plan_task(
            session_key=self._session_key(plan.session_id), plan_id=plan.plan_id,
            plan_task_id=plan_task.plan_task_id, prompt=prompt,
            task_id=task_id,
            metadata={"final_response": bool(last_task_id)
                      and plan_task.plan_task_id == last_task_id},
        )

    def _publish(self, action: str, payload: dict) -> None:
        event = {"action": action, **payload}
        plan = payload.get("plan")
        if isinstance(plan, dict):
            try:
                session_key = self._session_key(plan.get("session_id", ""))
                event.setdefault("session_key", session_key)
                if session_key.startswith("workspace:"):
                    parts = session_key.split(":", 2)
                    if len(parts) == 3:
                        event.setdefault("workspace_id", parts[1])
                        event.setdefault("workspace_session_id", parts[2])
            except (RuntimeError, TypeError, ValueError):
                pass
        self.bus.publish("plan.changed", event)
        if isinstance(plan, dict) and plan.get("goal_id") and hasattr(self, "goal_runtime"):
            goal = self.goal_runtime.get(plan["goal_id"])
            if goal and not goal.is_terminal:
                self.goal_runtime.update_continuation(goal.goal_id, {
                    "plan_id": plan.get("plan_id"), "plan_status": plan.get("status"),
                    "progress": plan.get("progress", 0), "last_action": action,
                }, expected_version=goal.version)
        if self.on_terminal and isinstance(plan, dict) and plan.get("status") in {"completed", "failed", "cancelled"}:
            current = self.manager.get(plan.get("plan_id"))
            if current is not None:
                self.on_terminal(current)

    def _session_key(self, session_id: str) -> str:
        with self.store._connection() as connection:
            row = connection.execute("SELECT session_key FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise RuntimeError("Plan session is not registered in RuntimeStore")
        return row["session_key"]
