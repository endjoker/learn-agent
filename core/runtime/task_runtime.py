"""Async task scheduler with durable lifecycle state and cooperative cancel."""

from __future__ import annotations

import asyncio
import inspect
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.debug import logger

from .cancellation import CancellationToken, TaskCancelled
from .models import RuntimeEvent, TaskEnvelope, TaskResult, TaskStatus, utc_now
from .sqlite_store import RuntimeStore


TaskExecutor = Callable[[TaskEnvelope, CancellationToken], TaskResult | Awaitable[TaskResult]]


@dataclass(order=True)
class _QueuedTask:
    sort_priority: int
    sequence: int
    envelope: TaskEnvelope


class TaskRuntime:
    """Durable global queue with one active task per session.

    The runtime owns task state transitions. Executors may mutate their local
    Agent session, but cannot write a second terminal task state after timeout.
    """

    def __init__(self, store: RuntimeStore, executor: TaskExecutor, *,
                 max_global_concurrency: int = 4, worker_id: str = "runtime",
                 max_attempts: int = 2, retry_backoff_seconds: float = 1.0,
                 cancel_grace_seconds: float = 10.0,
                 zombie_max_seconds: float = 300.0,
                 lease_ttl_seconds: float = 7200.0):
        # 任务租约 TTL（秒）：多实例下「崩溃恢复延迟」= lease TTL。必须大于
        # 最长任务执行时长（hard_timeout_seconds，默认 1200s，config 示例 6000s），
        # 否则存活实例的长时间任务会被新实例误认领造成双跑。默认 2h 保守安全。
        lease_ttl_seconds = max(1.0, float(lease_ttl_seconds))
        if max_global_concurrency <= 0:
            raise ValueError("max_global_concurrency must be positive")
        self.store = store
        self.executor = executor
        self.max_global_concurrency = max_global_concurrency
        self.worker_id = worker_id
        self.max_attempts = max(1, int(max_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.cancel_grace_seconds = max(0.0, float(cancel_grace_seconds))
        self.zombie_max_seconds = max(0.0, float(zombie_max_seconds))
        self.lease_ttl_seconds = lease_ttl_seconds
        self._queue: asyncio.PriorityQueue[_QueuedTask] = asyncio.PriorityQueue()
        self._workers: list[asyncio.Task] = []
        self._sequence = itertools.count()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_available: dict[str, asyncio.Event] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._completion: dict[str, asyncio.Future[TaskResult]] = {}
        self._quarantined_sessions: set[str] = set()
        self._zombies: dict[str, asyncio.Task] = {}
        self._retries: dict[str, asyncio.Task] = {}
        self._started = False
        self._stopping = False

    @property
    def quarantined_sessions(self) -> set[str]:
        return set(self._quarantined_sessions)

    async def start(self, *, recover_interrupted: bool = True,
                    enqueue_existing: bool = True) -> None:
        if self._started:
            return
        self._started = True
        self._stopping = False
        if recover_interrupted:
            # 只恢复租约过期或本进程（同一 worker_id 前缀）的滞留任务。
            self.store.recover_interrupted(requeue=True, owner=self.worker_id)
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"task-runtime-{index}")
            for index in range(self.max_global_concurrency)
        ]
        if enqueue_existing:
            await self.enqueue_persisted()

    async def enqueue_persisted(self) -> None:
        """Enqueue persisted work after the caller restores execution context."""
        if not self._started:
            raise RuntimeError("TaskRuntime.start() must be called before enqueue_persisted()")
        for snapshot in self.store.list_tasks(statuses={TaskStatus.QUEUED}):
            await self._enqueue(snapshot.envelope)
        # RETRY_WAIT 任务经原子认领（只认领本实例所有或租约已过期的行），
        # 防止多实例各自把同一重试任务入队一次造成双跑；其余由持有实例的
        # _retry_after 定时器负责回队。
        for snapshot in self.store.claim_retry_wait(owner=self.worker_id):
            await self._enqueue(snapshot.envelope)

    async def stop(self) -> None:
        if not self._started:
            return
        self._stopping = True
        for token in list(self._tokens.values()):
            token.cancel("runtime_stopping")
        for retry in list(self._retries.values()):
            retry.cancel()
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        # 优雅停机：把本实例仍在 LEASED（极短窗口）/未及收尾的任务租约置为
        # 立即过期，重启后 recover 可按「过期租约」快速认领（崩溃则等 TTL）。
        try:
            self.store.release_leases(owner=self.worker_id)
        except Exception:
            logger.debug("release leases on stop failed", exc_info=True)
        self._started = False

    async def submit(self, envelope: TaskEnvelope) -> str:
        if not self._started:
            raise RuntimeError("TaskRuntime.start() must be called before submit()")
        record, inserted = self.store.create_task(envelope)
        if inserted:
            await self._enqueue(envelope)
        return record.task_id

    async def cancel(self, task_id: str, reason: str = "user_requested") -> None:
        record = self.store.request_cancel(task_id, reason)
        token = self._tokens.get(task_id)
        if token:
            token.cancel(reason)
            if self.cancel_grace_seconds:
                try:
                    await self.wait(task_id, timeout=self.cancel_grace_seconds)
                except (asyncio.TimeoutError, KeyError):
                    pass
        if record.status == TaskStatus.CANCELLED:
            result = TaskResult(task_id=task_id, status=TaskStatus.CANCELLED,
                                summary=reason, error_code="TASK_CANCELLED",
                                error_message=reason, finished_at=utc_now())
            # 终态上的重复迁移幂等短路，不当作失败抛出。
            self._transition(task_id, TaskStatus.CANCELLED, result=result,
                             error_code=result.error_code, error_message=result.error_message)
            snapshot = self.store.get_task(task_id)
            self._complete(task_id, result,
                           session_id=snapshot.envelope.session_id if snapshot else None)

    async def wait(self, task_id: str, timeout: Optional[float] = None) -> TaskResult:
        snapshot = self.store.get_task(task_id)
        if snapshot is None:
            raise KeyError(task_id)
        if snapshot.result:
            return snapshot.result
        loop = asyncio.get_running_loop()
        future = self._completion.setdefault(task_id, loop.create_future())
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)

    async def _enqueue(self, envelope: TaskEnvelope) -> None:
        await self._queue.put(_QueuedTask(-envelope.priority, next(self._sequence), envelope))

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    def _available_for(self, session_id: str) -> asyncio.Event:
        event = self._session_available.get(session_id)
        if event is None:
            event = asyncio.Event()
            event.set()
            self._session_available[session_id] = event
        return event

    async def _worker(self, index: int) -> None:
        while True:
            queued = await self._queue.get()
            try:
                await self._run_queued(queued.envelope, index)
            except Exception as exc:
                # 单个任务上的异常绝不能杀死 worker 协程：记日志后继续下一轮。
                # asyncio.CancelledError 是 BaseException，不受此保护，会正常上抛。
                logger.error("task worker %d crashed while processing %s: %s",
                             index, queued.envelope.task_id, exc, exc_info=True)
            finally:
                self._queue.task_done()

    def _lease_expires_at(self) -> str:
        """当前租约到期时间（now + lease_ttl_seconds，毫秒 ISO UTC）。"""
        return (datetime.now(timezone.utc) + timedelta(seconds=self.lease_ttl_seconds)
                ).isoformat(timespec="milliseconds")

    async def _run_queued(self, envelope: TaskEnvelope, index: int) -> None:
        snapshot = self.store.get_task(envelope.task_id)
        if snapshot is None or snapshot.record.is_terminal:
            return
        available = self._available_for(envelope.session_id)
        await available.wait()
        async with self._lock_for(envelope.session_id):
            # A worker may have passed available.wait() immediately before a
            # sibling task timed out. Re-check after acquiring the serial lock.
            if not available.is_set():
                await self._enqueue(envelope)
                return
            snapshot = self.store.get_task(envelope.task_id)
            if snapshot is None or snapshot.record.is_terminal:
                return
            if snapshot.record.cancel_requested:
                # 并发 cancel 可能已把任务推到 CANCELLED：重复迁移幂等短路。
                self._transition(envelope.task_id, TaskStatus.CANCELLED,
                                 error_code="TASK_CANCELLED", error_message="cancel requested")
                return

            # 队列出队到加锁之间可能被并发 cancel 置为终态：非法迁移短路放弃本轮。
            # expected_status=QUEUED + 事务内状态校验：多实例下若任务已被其他
            # 实例租走（QUEUED→LEASED），本 worker 放弃执行，防双跑。
            if not self._transition(envelope.task_id, TaskStatus.LEASED,
                                    lease_owner=f"{self.worker_id}-{index}",
                                    lease_expires_at=self._lease_expires_at(),
                                    expected_status=TaskStatus.QUEUED):
                return
            if not self._transition(envelope.task_id, TaskStatus.RUNNING):
                return
            self.store.increment_attempt(envelope.task_id)
            token = CancellationToken()
            self._tokens[envelope.task_id] = token
            self.store.append_event(RuntimeEvent.create("task.started", session_id=envelope.session_id,
                                                         task_id=envelope.task_id))
            try:
                result = await self._execute(envelope, token)
                if result.task_id != envelope.task_id:
                    raise RuntimeError("executor returned a result for a different task")
                if result.status is TaskStatus.FAILED:
                    try:
                        if await self._schedule_retry(envelope, result):
                            return
                    except ValueError:
                        # 并发终态已推进：放弃重试，直接落终态。
                        pass
                self._transition(envelope.task_id, result.status, result=result,
                                 error_code=result.error_code,
                                 error_message=result.error_message)
                self._complete(envelope.task_id, result, session_id=envelope.session_id)
            except TaskCancelled as exc:
                result = TaskResult(task_id=envelope.task_id, status=TaskStatus.CANCELLED,
                                    summary=str(exc), error_code="TASK_CANCELLED",
                                    error_message=str(exc), finished_at=utc_now())
                self._transition(envelope.task_id, TaskStatus.CANCELLED, result=result,
                                 error_code=result.error_code, error_message=result.error_message)
                self._complete(envelope.task_id, result, session_id=envelope.session_id)
            except asyncio.TimeoutError:
                await self._handle_timeout(envelope, token)
            except Exception as exc:
                result = TaskResult(task_id=envelope.task_id, status=TaskStatus.FAILED,
                                     summary=str(exc), error_code="TASK_EXECUTION_ERROR",
                                     error_message=str(exc), finished_at=utc_now())
                try:
                    if await self._schedule_retry(envelope, result):
                        return
                except ValueError:
                    # 二次异常：并发终态已推进时放弃重试，不再向 worker 抛出。
                    pass
                self._transition(envelope.task_id, TaskStatus.FAILED, result=result,
                                 error_code=result.error_code, error_message=result.error_message)
                self._complete(envelope.task_id, result, session_id=envelope.session_id)
            finally:
                self._tokens.pop(envelope.task_id, None)

    async def _schedule_retry(self, envelope: TaskEnvelope, result: TaskResult) -> bool:
        snapshot = self.store.get_task(envelope.task_id)
        if snapshot is None or snapshot.record.cancel_requested or snapshot.record.attempt >= self.max_attempts:
            return False
        delay = self.retry_backoff_seconds * (2 ** max(0, snapshot.record.attempt - 1))
        self.store.transition_task(envelope.task_id, TaskStatus.RETRY_WAIT,
                                   error_code=result.error_code, error_message=result.error_message)
        self.store.append_event(RuntimeEvent.create(
            "task.retry_scheduled", session_id=envelope.session_id, task_id=envelope.task_id,
            data={"attempt": snapshot.record.attempt, "max_attempts": self.max_attempts, "delay_seconds": delay},
        ))
        retry = asyncio.create_task(self._retry_after(envelope, delay), name=f"task-retry-{envelope.task_id}")
        self._retries[envelope.task_id] = retry
        retry.add_done_callback(lambda _: self._retries.pop(envelope.task_id, None))
        return True

    async def _retry_after(self, envelope: TaskEnvelope, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            snapshot = self.store.get_task(envelope.task_id)
            if snapshot is None or snapshot.record.is_terminal or snapshot.record.cancel_requested:
                return
            try:
                # expected_status=RETRY_WAIT：若任务已被其他实例认领回队
                # （claim_retry_wait，仅当本实例租约过期时才会发生），放弃
                # 本次入队，防双实例把同一重试任务各入队一次造成双跑。
                self.store.transition_task(
                    envelope.task_id, TaskStatus.QUEUED,
                    expected_status=TaskStatus.RETRY_WAIT)
            except ValueError:
                return
            self.store.append_event(RuntimeEvent.create("task.queued", session_id=envelope.session_id,
                                                        task_id=envelope.task_id,
                                                        data={"reason": "retry"}))
            await self._enqueue(envelope)
        except asyncio.CancelledError:
            raise

    async def _execute(self, envelope: TaskEnvelope, token: CancellationToken) -> TaskResult:
        if inspect.iscoroutinefunction(self.executor):
            # 与线程 executor 一致：shield 保护协程不被 wait_for 取消，超时后
            # 登记为 zombie，交给隔离/回收流程处理，避免任务永久滞留。
            work = asyncio.create_task(self.executor(envelope, token))
            try:
                return await asyncio.wait_for(
                    asyncio.shield(work), timeout=envelope.timeout_seconds)
            except asyncio.TimeoutError:
                self._zombies[envelope.task_id] = work
                raise

        work = asyncio.create_task(asyncio.to_thread(self.executor, envelope, token))
        try:
            value = await asyncio.wait_for(asyncio.shield(work), timeout=envelope.timeout_seconds)
        except asyncio.TimeoutError:
            self._zombies[envelope.task_id] = work
            raise
        if inspect.isawaitable(value):
            return await asyncio.wait_for(value, timeout=envelope.timeout_seconds)
        return value

    async def _handle_timeout(self, envelope: TaskEnvelope, token: CancellationToken) -> None:
        token.cancel("task_timeout")
        result = TaskResult(task_id=envelope.task_id, status=TaskStatus.TIMED_OUT,
                            summary=f"task exceeded {envelope.timeout_seconds}s",
                            error_code="TASK_TIMEOUT", error_message="execution timeout",
                            finished_at=utc_now())
        self._transition(envelope.task_id, TaskStatus.TIMED_OUT, result=result,
                         error_code=result.error_code, error_message=result.error_message)
        self._complete(envelope.task_id, result, session_id=envelope.session_id)

        zombie = self._zombies.get(envelope.task_id)
        if zombie is not None:
            self._quarantined_sessions.add(envelope.session_id)
            self._available_for(envelope.session_id).clear()
            self.store.append_event(RuntimeEvent.create(
                "session.quarantined", session_id=envelope.session_id, task_id=envelope.task_id,
                data={"reason": "timed_out_worker_still_running"},
            ))
            asyncio.create_task(self._release_quarantine(envelope.session_id, envelope.task_id, zombie))

    async def _release_quarantine(self, session_id: str, task_id: str,
                                  zombie: asyncio.Task) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(zombie), timeout=self.zombie_max_seconds)
        except asyncio.TimeoutError:
            self.store.append_event(RuntimeEvent.create(
                "session.quarantine_expired", session_id=session_id, task_id=task_id,
                data={"zombie_max_seconds": self.zombie_max_seconds},
            ))
        except Exception:
            pass
        finally:
            self._zombies.pop(task_id, None)
            self._quarantined_sessions.discard(session_id)
            self._available_for(session_id).set()
            self.store.append_event(RuntimeEvent.create(
                "session.quarantine_released", session_id=session_id, task_id=task_id,
            ))

    def _transition(self, task_id: str, target: TaskStatus, *, lease_owner: Optional[str] = None,
                    lease_expires_at: Optional[str] = None,
                    expected_status: Optional[TaskStatus] = None,
                    error_code: Optional[str] = None, error_message: Optional[str] = None,
                    result: Optional[TaskResult] = None) -> bool:
        """执行状态迁移；终态上的非法迁移（并发 cancel 等已推进状态）幂等短路。"""
        try:
            self.store.transition_task(task_id, target, lease_owner=lease_owner,
                                       lease_expires_at=lease_expires_at,
                                       expected_status=expected_status,
                                       error_code=error_code, error_message=error_message,
                                       result=result)
            return True
        except ValueError:
            # 任务已被并发路径置为终态（或已被其他实例租走）：视为已满足，
            # 不当作失败抛出。
            return False

    def _prune_session_state(self, session_id: str) -> None:
        """任务终结后清理会话级状态，避免 _session_locks/_session_available 无限增长。"""
        active = {TaskStatus.CREATED, TaskStatus.QUEUED, TaskStatus.LEASED,
                  TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL,
                  TaskStatus.WAITING_DEPENDENCY, TaskStatus.RETRY_WAIT}
        if self.store.list_tasks(session_id=session_id, statuses=active):
            return
        self._session_locks.pop(session_id, None)
        self._session_available.pop(session_id, None)

    def _complete(self, task_id: str, result: TaskResult, *,
                  session_id: Optional[str] = None) -> None:
        # 完成后从 _completion 中清理，防止 per-task future 无限堆积；
        # 后续 wait() 会直接命中持久化的终态 result。
        future = self._completion.pop(task_id, None)
        if future and not future.done():
            future.set_result(result)
        if session_id:
            self._prune_session_state(session_id)
