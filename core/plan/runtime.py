"""Generic durable Plan executor independent of any presentation channel."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable
from uuid import uuid4

from core.runtime import ArtifactStore, TaskStatus
from core.runtime.quality_gate import QualityGate

from .manager import PlanManager
from .models import PlanStatus, PlanTaskStatus

logger = logging.getLogger("jk_agent.gateway")


class PlanExecutor:
    """Execute ready PlanTasks through injected TaskRuntime submission callbacks."""

    def __init__(self, manager: PlanManager, *, submit_task: Callable[..., Awaitable[str]],
                 wait_task: Callable[..., Awaitable[Any]], publish: Callable[[str, dict[str, Any]], None],
                 artifact_store: ArtifactStore | None = None,
                 wait_task_timeout_seconds: float = 300.0,
                 watchdog_interval_seconds: float = 30.0):
        self.manager = manager
        self.artifact_store = artifact_store or ArtifactStore(manager.store)
        self.quality_gate = QualityGate(self.artifact_store)
        self.submit_task = submit_task
        self.wait_task = wait_task
        self.publish = publish
        # 等待运行时任务结果的安全网：超时把 PlanTask 置 blocked 而非无限挂起。
        self.wait_task_timeout_seconds = max(1.0, float(wait_task_timeout_seconds))
        # 孤儿任务 watchdog：executor 协程意外退出后，ACTIVE 计划里仍处于
        # ASSIGNED/IN_PROGRESS/WAITING 的任务需要被回收（blocked）或重新触发
        # executor 接管（run() 幂等：发现 in-flight 任务会继续等待其结果）。
        self.watchdog_interval_seconds = max(5.0, float(watchdog_interval_seconds))
        self._running: dict[str, asyncio.Task] = {}
        self._watchdog: asyncio.Task | None = None

    def start(self, plan_id: str) -> asyncio.Task:
        existing = self._running.get(plan_id)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(self.run(plan_id), name=f"plan-{plan_id}")
        self._running[plan_id] = task
        task.add_done_callback(lambda _: self._running.pop(plan_id, None))
        self._ensure_watchdog()
        return task

    def _ensure_watchdog(self) -> None:
        """首次执行 Plan 时启动孤儿任务 watchdog（幂等，进程内只跑一个）。"""
        if self._watchdog is not None and not self._watchdog.done():
            return
        self._watchdog = asyncio.create_task(
            self._watchdog_loop(), name="plan-executor-watchdog")

    def stop_watchdog(self) -> None:
        """停止 watchdog 协程（进程/测试收尾用；run 任务不受影响）。"""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    async def _watchdog_loop(self) -> None:
        """周期扫描 ACTIVE 计划的孤儿任务：blocked 或重触发 executor。

        孤儿任务 = executor 协程已退出（崩溃/被取消）但计划仍 ACTIVE，且存在
        ASSIGNED/IN_PROGRESS/WAITING 或 READY 任务无人驱动。扫描间隔默认 30s。
        """
        while True:
            try:
                await asyncio.sleep(self.watchdog_interval_seconds)
                await self._watchdog_scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("plan watchdog scan failed")

    async def _watchdog_scan(self) -> None:
        try:
            plans = self.manager.list_recoverable(limit=500)
        except Exception:
            logger.debug("plan watchdog: list_recoverable failed", exc_info=True)
            return
        for plan in plans:
            if plan.status is not PlanStatus.ACTIVE:
                continue
            live = self._running.get(plan.plan_id)
            if live is not None and not live.done():
                continue
            try:
                active_tasks = [
                    task for task in plan.tasks
                    if task.status in {PlanTaskStatus.ASSIGNED,
                                       PlanTaskStatus.IN_PROGRESS,
                                       PlanTaskStatus.WAITING}]
                ready = self.manager.ready_tasks(plan.plan_id)
            except Exception:
                logger.debug("plan watchdog: probe failed %s", plan.plan_id, exc_info=True)
                continue
            if not active_tasks and not ready:
                continue
            # 已分配但没有 runtime task_id 的任务无法被 run() 接住（in-flight
            # 探测依赖 task_id）：直接 blocked，避免永久悬挂。
            for task in active_tasks:
                if task.task_id:
                    continue
                try:
                    self.manager.finish_task(
                        plan.plan_id, task.plan_task_id, success=False,
                        summary="watchdog: orphaned PlanTask without a runtime task id",
                        blocked_reason={"type": "watchdog_orphan",
                                        "message": "PlanTask assigned but never submitted; "
                                                   "executor recovered the Plan"})
                    logger.info("plan watchdog blocked orphaned task: plan=%s task=%s",
                                plan.plan_id, task.plan_task_id)
                except (KeyError, ValueError, RuntimeError):
                    pass
            # 其余孤儿（有 task_id 的 in-flight / READY）由 run() 幂等接管：
            # 重触发 executor 后 run() 会找到 in-flight 任务继续等待其结果。
            try:
                self.start(plan.plan_id)
                logger.info("plan watchdog re-triggered executor for orphaned Plan: %s",
                            plan.plan_id)
            except Exception:
                logger.debug("plan watchdog: re-trigger failed %s", plan.plan_id, exc_info=True)

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
            plan_task = None
            runtime_task_id = None
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
                    result = await asyncio.wait_for(
                        self.wait_task(runtime_task_id), timeout=self.wait_task_timeout_seconds)
                except asyncio.TimeoutError:
                    plan = self.manager.finish_task(
                        plan_id, plan_task.plan_task_id, success=False,
                        summary=f"waiting for task {runtime_task_id} timed out",
                        blocked_reason={"type": "wait_task_timeout",
                                        "message": f"task {runtime_task_id} did not finish within "
                                                   f"{self.wait_task_timeout_seconds:.0f}s"},
                    )
                    self._publish(plan, "task_failed")
                    return
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
                    # quality_gate.evaluate 可能同步执行 subprocess，放到线程池避免阻塞事件循环。
                    report = await asyncio.to_thread(
                        self.quality_gate.evaluate, plan_task.acceptance,
                        session_id=plan.session_id, text=result.visible_text or result.summary,
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 外层兜底：executor 生命周期/资源写路径（artifact/quality_gate/
            # 状态机保存等）异常。先把当前正在执行的任务按 runtime_unhealthy
            # 落终态（blocked），再广播 runtime_error，避免计划悬在 ACTIVE
            # 且任务停留在 IN_PROGRESS 成为孤儿。
            if plan_task is not None:
                try:
                    self.manager.finish_task(
                        plan_id, plan_task.plan_task_id, success=False,
                        summary=f"plan executor error: {exc}",
                        blocked_reason={"type": "runtime_unhealthy", "message": str(exc)})
                except (KeyError, ValueError, RuntimeError):
                    # 任务可能已终态（如 finish_task 之后的 publish 抛错）：
                    # 已落库状态为准，不重复改写。
                    pass
            try:
                self.publish("runtime_error", {"plan_id": plan_id, "error": str(exc)})
            except Exception:
                logger.exception("plan runtime_error publish failed: %s", plan_id)

    def _publish(self, plan, action: str, **extra: Any) -> None:
        self.publish(action, {"plan": plan.to_dict(), **extra})

    @staticmethod
    def _task_prompt(plan, task) -> str:
        acceptance = ""
        if task.acceptance:
            acceptance = "\n验收条件：" + "; ".join(
                str(item.get("description") or item.get("type") or item) for item in task.acceptance
            )
        # 上下文接力：前序已完成 step 的结果摘要注入当前 step 的 prompt；
        # L4#12：只取最近 K 条（默认 5），避免超长 plan 的 prompt 线性膨胀。
        try:
            from core.plan.manager import PlanManager
            prior_block = PlanManager.task_prior_summaries_block(plan, task)
        except Exception:
            prior_block = ""
        return (
            f"你正在执行已批准 Plan {plan.plan_id} 的任务 {task.plan_task_id}。\n"
            f"任务：{task.description}\n"
            f"{prior_block}"
            "请使用可用的原生工具完成该任务，并在最终回复中给出实际修改、验证命令和结果。"
            "不要输出文本控制协议，也不要擅自执行其他 PlanTask。" + acceptance
        )
