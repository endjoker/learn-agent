"""Generic durable Plan executor independent of any presentation channel."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable
from uuid import uuid4

from core.runtime import ArtifactStore, TaskStatus
from core.runtime.quality_gate import QualityGate

from .manager import PlanManager
from .models import PlanStatus, PlanTaskStatus


class PlanExecutor:
    """Execute ready PlanTasks through injected TaskRuntime submission callbacks."""

    def __init__(self, manager: PlanManager, *, submit_task: Callable[..., Awaitable[str]],
                 wait_task: Callable[..., Awaitable[Any]], publish: Callable[[str, dict[str, Any]], None],
                 artifact_store: ArtifactStore | None = None):
        self.manager = manager
        self.artifact_store = artifact_store or ArtifactStore(manager.store)
        self.quality_gate = QualityGate(self.artifact_store)
        self.submit_task = submit_task
        self.wait_task = wait_task
        self.publish = publish
        self._running: dict[str, asyncio.Task] = {}

    def start(self, plan_id: str) -> asyncio.Task:
        existing = self._running.get(plan_id)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(self.run(plan_id), name=f"plan-{plan_id}")
        self._running[plan_id] = task
        task.add_done_callback(lambda _: self._running.pop(plan_id, None))
        return task

    async def run(self, plan_id: str) -> None:
        try:
            plan = self.manager.get(plan_id)
            if plan is None:
                return
            if plan.status is PlanStatus.APPROVED:
                plan = self.manager.activate(plan_id)
            if plan.status is not PlanStatus.ACTIVE:
                return
            self._publish(plan, "active")
            while True:
                plan = self.manager.get(plan_id)
                if plan is None or plan.status is not PlanStatus.ACTIVE:
                    return
                # After a gateway restart, a PlanTask can already be assigned
                # to a durable runtime task. Wait for that task instead of
                # creating a duplicate execution.
                in_flight = next(
                    (item for item in plan.tasks
                     if item.status in {
                         PlanTaskStatus.ASSIGNED,
                         PlanTaskStatus.IN_PROGRESS,
                         PlanTaskStatus.WAITING,
                     } and item.task_id),
                    None,
                )
                if in_flight is not None:
                    plan_task = in_flight
                    runtime_task_id = plan_task.task_id
                    prompt = None
                else:
                    ready = self.manager.ready_tasks(plan_id)
                    if not ready:
                        self._publish(plan, "idle")
                        return
                    plan_task = ready[0]
                    runtime_task_id = f"task_{uuid4().hex}"
                    self.manager.assign_task(plan_id, plan_task.plan_task_id, runtime_task_id)
                    self.manager.start_task(plan_id, plan_task.plan_task_id)
                    prompt = self._task_prompt(plan, plan_task)
                try:
                    if prompt is not None:
                        submitted_id = await self.submit_task(
                            plan=plan, plan_task=plan_task, task_id=runtime_task_id, prompt=prompt)
                        if submitted_id != runtime_task_id:
                            raise RuntimeError("Plan Task idempotency returned a different task")
                    result = await self.wait_task(runtime_task_id)
                except Exception as exc:
                    plan = self.manager.finish_task(
                        plan_id, plan_task.plan_task_id, success=False, summary=str(exc),
                        blocked_reason={"type": "runtime_unhealthy", "message": str(exc)},
                    )
                    self._publish(plan, "task_failed")
                    return
                current = self.manager.get(plan_id)
                if current is None:
                    return
                if current.status in PlanStatus.terminal():
                    self._publish(current, current.status.value)
                    return
                success = result.status is TaskStatus.COMPLETED
                blocked_reason = None if success else {
                    "type": "runtime_task_failed",
                    "message": result.error_message or result.summary or result.status.value,
                }
                artifact_ids = list(result.artifact_ids)
                result_text = result.visible_text or result.summary
                if result_text:
                    artifact = self.artifact_store.create_text(
                        session_id=plan.session_id, plan_id=plan.plan_id,
                        plan_task_id=plan_task.plan_task_id, task_id=runtime_task_id,
                        name=f"{plan_task.plan_task_id}-result.md", type="plan-task-result",
                        content=result_text, summary=(result.summary or result_text)[:1000], created_by="root",
                    )
                    artifact_ids.append(artifact.artifact_id)
                quality_report = []
                if success and plan_task.acceptance:
                    report = self.quality_gate.evaluate(
                        plan_task.acceptance, session_id=plan.session_id,
                        text=result.visible_text or result.summary,
                    )
                    quality_report = report.to_list()
                    if not report.passed:
                        success = False
                        blocked_reason = {
                            "type": "quality_gate_failed", "message": "PlanTask 验收条件未通过。",
                            "checks": quality_report,
                        }
                plan = self.manager.finish_task(
                    plan_id, plan_task.plan_task_id, success=success,
                    summary=result.summary or result.visible_text, blocked_reason=blocked_reason,
                    artifact_ids=artifact_ids, quality_report=quality_report,
                )
                self._publish(plan, "task_completed" if success else "task_failed",
                              task_id=runtime_task_id, plan_task_id=plan_task.plan_task_id)
                if plan.is_terminal:
                    self._publish(plan, plan.status.value)
                    return
                if plan.status is PlanStatus.PAUSED:
                    self._publish(plan, "paused")
                    return
        except Exception as exc:
            self.publish("runtime_error", {"plan_id": plan_id, "error": str(exc)})

    def _publish(self, plan, action: str, **extra: Any) -> None:
        self.publish(action, {"plan": plan.to_dict(), **extra})

    @staticmethod
    def _task_prompt(plan, task) -> str:
        acceptance = ""
        if task.acceptance:
            acceptance = "\n验收条件：" + "; ".join(
                str(item.get("description") or item.get("type") or item) for item in task.acceptance
            )
        return (
            f"你正在执行已批准 Plan {plan.plan_id} 的任务 {task.plan_task_id}。\n"
            f"任务：{task.description}\n"
            "请使用可用的原生工具完成该任务，并在最终回复中给出实际修改、验证命令和结果。"
            "不要输出文本控制协议，也不要擅自执行其他 PlanTask。" + acceptance
        )
