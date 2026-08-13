"""Gateway adapter that runs durable Plans through Dispatcher/TaskRuntime."""

from __future__ import annotations

from core.plan import PlanExecutor, PlanManager, PlanStatus
from core.runtime import ArtifactStore
from core.runtime.quality_gate import QualityGate


class PlanRuntime:
    def __init__(self, dispatcher, store, bus, artifact_store=None):
        self.dispatcher = dispatcher
        self.store = store
        self.bus = bus
        self.quality_gate = QualityGate(artifact_store or ArtifactStore(store))
        self.manager = PlanManager(store)
        self._executor = PlanExecutor(
            self.manager, submit_task=self._submit_task, wait_task=self.dispatcher.wait_runtime_task,
            publish=self._publish, artifact_store=artifact_store,
        )

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
        return await self.dispatcher.submit_plan_task(
            session_key=self._session_key(plan.session_id), plan_id=plan.plan_id,
            plan_task_id=plan_task.plan_task_id, prompt=prompt,
            task_id=task_id,
        )

    def _publish(self, action: str, payload: dict) -> None:
        self.bus.publish("plan.changed", {"action": action, **payload})

    def _session_key(self, session_id: str) -> str:
        with self.store._connection() as connection:
            row = connection.execute("SELECT session_key FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise RuntimeError("Plan session is not registered in RuntimeStore")
        return row["session_key"]
