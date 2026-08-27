"""Single async Gateway/WebUI submit/wait/subscribe facade."""
from __future__ import annotations
import asyncio
from typing import Awaitable, Callable, Optional
from core.runtime import CancellationToken, TaskEnvelope, TaskResult, TaskRuntime
from .events import AgentEventBus

Executor = Callable[[TaskEnvelope, CancellationToken, Callable[[str, dict], None]], Awaitable[TaskResult]]

class SessionRuntime:
    """One durable execution path: TaskRuntime invokes this bridge only."""
    def __init__(self, task_runtime: TaskRuntime, executor: Executor, *, events: AgentEventBus | None = None):
        self.task_runtime, self.executor = task_runtime, executor
        self.events = events or AgentEventBus()
        self._started = False
        self._start_lock = asyncio.Lock()  # P3：start 幂等互斥
        self.task_runtime.executor = self._execute

    async def start(self, *, recover_interrupted: bool = True,
                    enqueue_existing: bool = True) -> None:
        # P3：asyncio.Lock 幂等——并发/重复 start 只触发一次底层初始化。
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self.task_runtime.start(recover_interrupted=recover_interrupted,
                                          enqueue_existing=enqueue_existing)
            self._started = True

    async def stop(self) -> None:
        if self._started:
            await self.task_runtime.stop()
            self._started = False

    async def submit(self, *, session_id: str, session_key: str, prompt: str, source: str = "user", **kwargs) -> str:
        envelope = TaskEnvelope.create(session_id=session_id, session_key=session_key, prompt=prompt, source=source, **kwargs)
        return await self.task_runtime.submit(envelope)

    async def submit_envelope(self, envelope: TaskEnvelope) -> str:
        """Submit a prebuilt durable envelope without reserializing it."""
        return await self.task_runtime.submit(envelope)

    async def wait(self, task_id: str, timeout: Optional[float] = None) -> TaskResult:
        return await self.task_runtime.wait(task_id, timeout)

    async def cancel(self, task_id: str, reason: str = "user_requested") -> None:
        await self.task_runtime.cancel(task_id, reason)

    def subscribe(self, session_id: str):
        return self.events.subscribe(session_id)

    def unsubscribe(self, session_id: str, queue) -> None:
        self.events.unsubscribe(session_id, queue)

    async def _execute(self, envelope: TaskEnvelope, token: CancellationToken) -> TaskResult:
        run_id = envelope.task_id
        def emit(kind: str, payload: dict | None = None) -> None:
            self.events.publish(kind, run_id=run_id, session_id=envelope.session_id, task_id=envelope.task_id, **(payload or {}))
        emit("run.started", {"source": envelope.source})
        try:
            result = await self.executor(envelope, token, emit)
        except Exception as exc:
            emit("run.ended", {"status": "error", "error": str(exc)})
            # P2：run 结束清理 per-run 序号状态，避免 _sequence 无限增长。
            self.events.forget(run_id)
            raise
        emit("run.ended", {"status": result.status.value})
        self.events.forget(run_id)
        return result
