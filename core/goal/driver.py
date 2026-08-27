"""Serialized same-session autonomous Goal continuation driver."""
from __future__ import annotations
import asyncio
import logging
from uuid import uuid4
from core.runtime import TaskStatus

logger = logging.getLogger("jk_agent.gateway")

# goal 轮次等待运行时结果的安全网：略大于 TaskRuntime envelope 超时
# （default_timeout_seconds 默认 1200s），保证 envelope 超时先生效、这里
# 只在运行时异常/挂死时兜底，超时按 round_failed block。
_DEFAULT_ROUND_WAIT_SECONDS = 1300.0


def _round_wait_seconds() -> float:
    """goal.round_wait_seconds 配置读取（gateway.goal.round_wait_seconds 优先，
    兼容顶层 goal.round_wait_seconds），默认 1300s，非法值回落默认。"""
    try:
        from core.config_loader import load_config
        cfg = load_config()
        section = ((cfg.get("gateway", {}) or {}).get("goal") or {})             or (cfg.get("goal") or {})
        value = section.get("round_wait_seconds", _DEFAULT_ROUND_WAIT_SECONDS)
        return max(60.0, float(value))
    except Exception:
        return _DEFAULT_ROUND_WAIT_SECONDS

def _goal_driver_enabled() -> bool:
    """gateway.goal.driver_enabled（兼容顶层 goal.driver_enabled），默认 True。

    多实例部署下 goal 自动轮次推进依赖进程内 priority queue 与同会话仲裁，
    双实例同 goal 不会双跑（TaskRuntime.submit 幂等键 + reserve_round 乐观版本
    冲突），但两实例会各自驱动同一 goal 造成重复轮次——因此多实例下要么
    粘性路由（同一 session 永远落在同一实例），要么只在 leader 实例启用本
    驱动（见 docs/multi-instance.md）。设为 False 时 trigger() 静默不推进。
    """
    try:
        from core.config_loader import load_config
        cfg = load_config()
        section = ((cfg.get("gateway", {}) or {}).get("goal") or {})             or (cfg.get("goal") or {})
        value = section.get("driver_enabled", True)
        return bool(value)
    except Exception:
        return True


_GOAL_PROMPT = """[GOAL CONTINUATION ROUND]
You are continuing a durable long-running objective in the same session.

Goal id: {goal_id}
Objective:
{objective}

Round: {round}/{max_rounds}

Work done so far:
{work_summary}

{convergence_note}You control this Goal with these tools (always pass the exact goal_id above):
- complete_goal: the objective is fully met with concrete evidence.
- pause_goal: user input or a decision is required, or you must stop.
- resume_goal: continue a paused Goal.
- cancel_goal: the objective cannot be met, or the user wants to stop; it terminates the Goal.

Inspect the actual current state and perform the next most important bounded step.
Do not repeat completed work or expand scope. If the objective is complete, call
complete_goal with concrete evidence. If user input or a decision is required,
call pause_goal and state exactly what is needed. Otherwise finish this round
with a concise progress summary; the runtime may schedule another round.
"""


def _build_work_summary(goal) -> str:
    """Summarize completed rounds for the next continuation prompt."""
    continuation = getattr(goal, "continuation", None) or {}
    history = continuation.get("round_history") or []
    lines = []
    for item in history[-6:]:
        if not isinstance(item, dict):
            continue
        round_no = item.get("round")
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        lines.append(f"- Round {round_no}: {summary[:300]}")
    if not lines:
        return "(no completed rounds yet)"
    return "\n".join(lines)


def _convergence_note(goal) -> str:
    """High-round goals must converge: stop expanding and call complete_goal."""
    started = getattr(goal, "rounds_started", 0) or 0
    max_rounds = max(1, getattr(goal, "max_rounds", 20) or 20)
    threshold = max(3, min(max_rounds - 1, int(max_rounds * 0.6)))
    if started < threshold:
        return ""
    return (
        f"This is round {started}/{max_rounds}, approaching the round limit. "  # noqa: E501
        f"If the current state is already deliverable, you MUST call complete_goal "  # noqa: E501
        "with concrete evidence now; do not keep doing extra work or expanding scope.\n\n"
    )

class GoalRoundDriver:
    """Coalesces triggers and keeps at most one autonomous round per session."""
    def __init__(self, goal_runtime, store, *, submit, wait, session_key,
                 publish=None, idle_delay: float = 0.05, cancel=None):
        self.goal_runtime, self.store = goal_runtime, store
        self.submit, self.wait, self.session_key = submit, wait, session_key
        self.publish = publish or (lambda _event, _payload: None)
        self.idle_delay = max(0.0, float(idle_delay))
        # 可选的任务取消钩子（async (task_id, reason) -> None）：轮次等待超时
        # 时联动取消滞留的 runtime 任务。由装配方注入
        # （dispatcher.cancel_runtime_task 适配层）。
        self.cancel = cancel
        self._jobs: dict[str, asyncio.Task] = {}
        # 同会话仲裁：session_key -> asyncio.Lock，同一会话同时只跑一个
        # goal round，其余 goal 的 _drive 在锁上排队（pending 并记日志）。
        self._session_gates: dict[str, asyncio.Lock] = {}
        # 多实例门禁：gateway.goal.driver_enabled 默认 True（单实例行为不变）；
        # 多实例下建议仅 leader 实例启用或依赖粘性路由（见 docs/multi-instance.md）。
        self._enabled = _goal_driver_enabled()
        if not self._enabled:
            logger.warning(
                "GoalRoundDriver 已禁用（gateway.goal.driver_enabled=false）："
                "goal 自动轮次不再推进，需人工 resume 或由 leader 实例启用")

    def trigger(self, goal_id: str) -> None:
        if not self._enabled:
            # 多实例下驱动被禁用：不启动任何 goal 轮次（静默 no-op）。
            return
        current = self._jobs.get(goal_id)
        if current and not current.done():
            return
        job = asyncio.create_task(self._drive(goal_id), name=f"goal-round-{goal_id}")
        self._jobs[goal_id] = job
        job.add_done_callback(lambda done, gid=goal_id: self._job_done(gid, done))

    def _job_done(self, goal_id: str, job: asyncio.Task) -> None:
        self._jobs.pop(goal_id, None)
        if job.cancelled():
            return
        error = job.exception()
        if error is None:
            goal = self.goal_runtime.get(goal_id)
            if (goal is not None and goal.is_armed and not goal.current_task_id
                    and not self._user_work_waiting(goal.session_id)):
                self.trigger(goal_id)
            return
        goal = self.goal_runtime.get(goal_id)
        if goal is None or goal.is_terminal:
            return
        try:
            blocked = self.goal_runtime.block(
                goal_id, {"type": "driver_error", "message": str(error)},
                expected_version=goal.version)
        except RuntimeError:
            # pause/cancel 等并发操作导致的版本冲突：放弃本轮并重读最新状态，
            # 不能把瞬时竞态固化为 driver_error 封禁。
            self.goal_runtime.get(goal_id)
            return
        except Exception:
            return
        self._publish_scoped("goal.changed", blocked, action="blocked",
                             error=str(error))

    async def stop(self) -> None:
        jobs = list(self._jobs.values())
        for job in jobs: job.cancel()
        if jobs: await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()
        self._session_gates.clear()

    def prune_session_gates(self, session_keys) -> int:
        """P4 资源卫生：移除指定会话的空闲仲裁锁（未被持有才移除）。

        会话删除/终态回收时由装配方调用，防止 _session_gates 随历史会话数
        无限累积（每把 Lock 量级小，但长生命周期网关缓慢增长）。"""
        removed = 0
        for key in session_keys:
            gate = self._session_gates.get(key)
            if gate is not None and not gate.locked():
                self._session_gates.pop(key, None)
                removed += 1
        return removed

    async def _drive(self, goal_id: str) -> None:
        await asyncio.sleep(self.idle_delay)
        goal = self.goal_runtime.get(goal_id)
        if goal is None or not goal.is_armed or goal.current_task_id:
            return
        # Goal creation may occur inside a user command/tool turn. Wait until
        # higher-priority same-session work drains, but keep publishing a
        # visible scheduling state so WebUI never looks inert.
        self._publish_scoped("goal.changed", goal, action="scheduled")
        # 等待用户工作排空：DB 轮询用自适应退避（0.05s 起，翻倍至最大 1s），
        # 避免高频空轮询数据库。
        poll_delay = self.idle_delay or 0.05
        while self._user_work_waiting(goal.session_id):
            await asyncio.sleep(poll_delay)
            poll_delay = min(1.0, poll_delay * 2)
            goal = self.goal_runtime.get(goal_id)
            if goal is None or not goal.is_armed or goal.current_task_id:
                return
        # 同会话仲裁：同一会话同时只跑一个 goal round；其余 pending 排队
        # （避免多 Goal 并发轮次互相抢占会话 Agent / TaskRuntime 并发槽）。
        session_key = self.session_key(goal.session_id)
        gate = self._session_gates.setdefault(session_key, asyncio.Lock())
        if gate.locked():
            logger.info("同会话 Goal 轮次排队等待: session=%s goal=%s",
                        session_key, goal.goal_id)
        await gate.acquire()
        try:
            # 排队期间 Goal 可能被暂停/取消/新一轮推进：持锁后重读最新状态。
            goal = self.goal_runtime.get(goal_id)
            if goal is None or not goal.is_armed or goal.current_task_id:
                return
            if goal.rounds_started >= goal.max_rounds:
                blocked = self.goal_runtime.block(goal.goal_id,
                    {"type": "max_rounds", "max_rounds": goal.max_rounds}, expected_version=goal.version)
                self._publish_scoped("goal.changed", blocked, action="blocked")
                return
            task_id = f"goalround_{uuid4().hex}"
            reserved = self.goal_runtime.reserve_round(goal.goal_id, task_id, expected_version=goal.version)
            prompt = _GOAL_PROMPT.format(
                goal_id=reserved.goal_id,
                objective=reserved.objective,
                round=reserved.rounds_started,
                max_rounds=reserved.max_rounds,
                work_summary=_build_work_summary(reserved),
                convergence_note=_convergence_note(reserved),
            )
            submitted = await self.submit(session_id=reserved.session_id,
                session_key=self.session_key(reserved.session_id), prompt=prompt,
                goal_id=reserved.goal_id, goal_revision=reserved.version,
                goal_round=reserved.rounds_started, task_id=task_id)
            self._publish_scoped("goal.round.queued", reserved, task_id=submitted)
            # 等待运行时结果的安全网：goal.round_wait_seconds（默认 1300s，
            # 略大于 envelope 1200s）——超时按 round_failed block，防止驱动
            # 无限挂起；运行中的滞留任务由 cancel 钩子联动取消。
            round_wait = _round_wait_seconds()
            try:
                result = await asyncio.wait_for(self.wait(submitted), timeout=round_wait)
            except asyncio.TimeoutError:
                current = self.goal_runtime.get(goal_id)
                if current is None or current.current_task_id != submitted:
                    return
                blocked = self.goal_runtime.block(goal_id,
                    {"type": "round_failed", "task_id": submitted, "status": "timeout",
                     "message": f"goal round {reserved.rounds_started} did not finish "
                                f"within {round_wait:.0f}s"},
                    expected_version=current.version)
                self._publish_scoped("goal.changed", blocked, action="blocked")
                if self.cancel is not None:
                    try:
                        await self.cancel(submitted, "goal_round_timeout")
                    except Exception:
                        logger.debug("goal round timeout cancel failed: %s",
                                     submitted, exc_info=True)
                return
            current = self.goal_runtime.get(goal_id)
            if current is None:
                return
            if current.current_task_id != submitted:
                # 被替代轮次（pause/resume 或新一轮已推进）：结果不再归属当前
                # Goal 状态，写 continuation.dropped_rounds 审计后放弃；
                # current_task_id 为空（pause 已清）时不重复审计。
                if current.current_task_id:
                    try:
                        self.goal_runtime.record_dropped_round(
                            goal_id, task_id=submitted, round_no=reserved.rounds_started,
                            reason="superseded", expected_version=current.version)
                    except (KeyError, RuntimeError, ValueError):
                        pass
                return
            if result.status is TaskStatus.COMPLETED:
                current = self.goal_runtime.finish_round(goal_id, submitted,
                    summary=result.summary or result.visible_text, expected_version=current.version)
                self._publish_scoped("goal.round.completed", current, task_id=submitted)
                if current.is_armed and not self._user_work_waiting(current.session_id):
                    # Schedule after this job leaves _jobs; trigger() would otherwise
                    # coalesce against the currently running round.
                    asyncio.get_running_loop().call_soon(self.trigger, goal_id)
            elif result.status is TaskStatus.CANCELLED:
                current = self.goal_runtime.disarm(goal_id, expected_version=current.version)
                self._publish_scoped("goal.changed", current, action="disarmed")
            else:
                current = self.goal_runtime.block(goal_id,
                    {"type": "round_failed", "task_id": submitted,
                     "status": result.status.value, "message": result.error_message or result.summary},
                    expected_version=current.version)
                self._publish_scoped("goal.changed", current, action="blocked")
        finally:
            gate.release()

    def _publish_scoped(self, event: str, goal, **payload) -> None:
        session_key = self.session_key(goal.session_id)
        data = {**payload, "goal": goal.to_dict(), "session_key": session_key}
        if session_key.startswith("workspace:"):
            parts = session_key.split(":", 2)
            if len(parts) == 3:
                data["workspace_id"], data["workspace_session_id"] = parts[1], parts[2]
        self.publish(event, data)
        if event == "goal.round.completed":
            self.publish("chat.runtime_update", {
                **{key: value for key, value in data.items()
                   if key in {"session_key", "workspace_id", "workspace_session_id"}},
                "runtime_source": "goal", "action": "round_completed",
                "goal_id": goal.goal_id,
                "goal_round": goal.rounds_started,
                "status": goal.status.value,
                "summary": str(goal.continuation.get("summary") or ""),
            })

    def _user_work_waiting(self, session_id: str) -> bool:
        """True while higher-priority same-session work is still active.

        L4#10: 优先走 store 的 EXISTS 探针（``SELECT 1 ... LIMIT 1``，不实体化
        行列表）；store 尚未提供该方法时回退到行级列出，语义完全一致。退避
        参数（0.05s 起、翻倍至 1s）由 _drive 的轮询循环持有，此处不涉及。
        """
        active = {TaskStatus.CREATED, TaskStatus.QUEUED, TaskStatus.LEASED,
                  TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL,
                  TaskStatus.RETRY_WAIT}
        probe = getattr(self.store, "has_active_work", None)
        if callable(probe):
            return probe(session_id=session_id,
                         sources={"user", "system", "plan"}, statuses=active)
        return any(item.envelope.source in {"user", "system", "plan"}
                   for item in self.store.list_tasks(session_id=session_id, statuses=active))
