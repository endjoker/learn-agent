"""The single model/tool loop adapter."""
from __future__ import annotations
import asyncio
import logging
import threading
from typing import Any
from .models import AgentRunResult, RunStatus
from .control import BudgetExceeded, RunControl
from .adapters import ProviderTurnAdapter
from core.runtime import Budget, CancellationToken, TaskCancelled

logger = logging.getLogger("jk_agent")


class AgentLoop:
    """Adapter around the native provider-tool loop during the migration."""

    def __init__(self, agent: Any):
        self.agent = agent
        # P2: re-entrancy lock lives on the Agent - AgentLoop instances are
        # created per run, so the lock must be shared across instances to
        # block a concurrent second run() of the same Agent.
        lock = getattr(agent, "_agent_run_lock", None)
        if lock is None:
            lock = threading.Lock()
            agent._agent_run_lock = lock
        self._run_lock = lock

    @staticmethod
    def _int_or_none(value):
        """Coerce config values: string digits / empty / invalid -> int or None (P2)."""
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _build_budget(self) -> Budget:
        """Build this run Budget from config/Agent attributes (P1-4 / P2).

        max_tool_calls / max_wall_seconds / max_total_tokens are injected into
        RunControl so configured ceilings actually take effect (string digits
        are coerced to int). Sessions are never step-limited: max_steps stays
        None and unbounded loops are caught by max_tool_calls / wall clock."""
        cfg = getattr(self.agent, "_config", None) or {}
        ar = cfg.get("agent_runtime") or {}
        max_steps = None
        max_tool_calls = self._int_or_none(ar.get("max_tool_calls"))
        max_wall_seconds = self._int_or_none(ar.get("max_wall_seconds"))
        max_total_tokens = self._int_or_none(ar.get("max_total_tokens"))
        return Budget(max_steps=max_steps, max_tool_calls=max_tool_calls,
                      max_wall_seconds=max_wall_seconds,
                      max_total_tokens=max_total_tokens)

    def _run_token(self) -> CancellationToken:
        """Run-scoped cooperative-cancellation token: prefer the injected one.

        P1-5: thread the external (dispatcher) token through RunControl.
        checkpoint() instead of only checking _stop_requested at loop start."""
        token = getattr(self.agent, "_run_token", None)
        if isinstance(token, CancellationToken):
            return token
        return CancellationToken()

    def begin_run(self, event_sink=None) -> None:
        """Initialize the one authoritative run boundary."""
        self.agent._run_control = RunControl(
            budget=self._build_budget(), token=self._run_token())
        # 持久化退役（C2 契约②收尾）：PersistenceAdapter/save_session 调用
        # 网络已移除——文件转录停写后它们全是 no-op；会话权威在 gateway 的
        # SQLite 统一会话，独立 Agent 明确不持久化。
        self.agent._provider_turn = ProviderTurnAdapter(self.agent)
        self.agent._event_sink = event_sink

    def finish(self, reason: str) -> None:
        self.agent._last_run_reason = reason
        self.agent._emit_event("agent_end", reason=reason)

    def next_turn(self, step: int) -> str:
        self.agent._run_control.begin_step()
        turn_id = f"turn_{step}"
        self.agent._turn_id = turn_id
        return turn_id

    def _apply_pending_system_template(self) -> None:
        """A1：begin_run 后原子应用挂起的 system 模板（前缀稳定化）。

        技能刷新 / MCP reload / profile 更新只写入 _pending_system_template，
        不立即改写 messages[0]（避免打断 provider prompt 缓存）；此处若存在，
        在消息进入 provider 前原子替换 messages[0] 并清除。
        """
        agent = self.agent
        pending = getattr(agent, "_pending_system_template", None)
        if pending is None:
            return
        agent._pending_system_template = None
        try:
            messages = agent.messages
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = pending
                logger.info("已应用挂起的 system 模板（A1 前缀稳定化）")
            # 无 system 首条（新会话/已清空）时仅更新 agent.system_prompt，
            # 本轮 _run_native_loop 首次建消息时会带上最新模板。
            agent.system_prompt = pending
        except Exception as exc:
            logger.warning("应用挂起的 system 模板失败: %s", exc)

    def _maybe_spawn_incremental_compress(self) -> None:
        """A4：turn 边界后台增量压缩调度（非阻塞）。

        仅当 _check_context 置位 _compression_pending 时调度：优先
        asyncio.to_thread（运行在事件循环内时），否则退化为 daemon 线程
        （gateway 的 agent.run 位于 executor 线程，无运行中的事件循环）。
        """
        agent = self.agent
        if not getattr(agent, "_compression_pending", False):
            return
        compress = getattr(agent, "_incremental_compress", None)
        if not callable(compress):
            return
        agent._compression_pending = False  # 消费标记；失败由 Agent 视情况重新置位
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            threading.Thread(
                target=compress, daemon=True,
                name="jkagent-incremental-compress").start()
        else:
            asyncio.ensure_future(asyncio.to_thread(compress))

    def run(self, prompt: str, *, images: list | None = None, event_sink=None) -> AgentRunResult:
        """Run one authoritative loop with unified exception handling (P2-8).

        Success / cancel / error all guarantee a single agent_end event, a
        structured AgentRunResult, runtime-field cleanup and state persistence.

        P2 re-entrancy guard: non-blocking lock - a concurrent second entry
        raises RuntimeError("Agent 正在运行") immediately."""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Agent 正在运行")
        try:
            result = self._run_guarded(prompt, images=images, event_sink=event_sink)
        finally:
            self._run_lock.release()
        # A4：turn 边界——run 结束、运行锁释放后调度后台增量压缩（非阻塞）。
        # 调度失败不得把已完成的 run 变成异常（压缩只是尽力而为的后台优化）。
        try:
            self._maybe_spawn_incremental_compress()
        except Exception as exc:
            logger.warning("调度后台增量压缩失败: %s", exc)
        return result

    def _run_guarded(self, prompt: str, *, images: list | None = None,
                     event_sink=None) -> AgentRunResult:
        self.begin_run(event_sink)
        # A1：begin_run 后应用挂起的 system 模板（技能刷新/MCP reload/profile
        # 更新只写 _pending_system_template，此处原子替换 messages[0] 并清除）。
        self._apply_pending_system_template()
        self.agent._active_loop = self
        # P2-9: mark the run in progress so switch_llm defers model swaps.
        self.agent._is_running = True
        reason = "error"
        status = RunStatus.ERROR
        text = ""
        error_code = None
        error_message = None
        try:
            text = self.agent._run_native_loop(
                prompt, images=images, event_sink=event_sink)
            reason = getattr(self.agent, "_last_run_reason", "completed")
            status = {
                "completed": RunStatus.COMPLETED,
                "stopped": RunStatus.CANCELLED,
                "max_steps": RunStatus.MAX_STEPS,
                "max_tool_calls": RunStatus.MAX_TOOL_CALLS,
                "blocked": RunStatus.BLOCKED,  # P3-7：hook 拦截收口不再误记 ERROR
                "error": RunStatus.ERROR,
            }.get(reason, RunStatus.ERROR)
        except BudgetExceeded as exc:
            # P2: budget exceeded (wall clock / cumulative tokens) is a
            # TaskCancelled subclass handled before TaskCancelled: same
            # cleanup path (single agent_end) with a concrete budget reason.
            reason = "stopped"
            status = RunStatus.CANCELLED
            text = f"⏰ 运行预算超限：{exc}"
            self.finish("stopped")
        except TaskCancelled:
            reason = "stopped"
            status = RunStatus.CANCELLED
            text = text or "⏹️ 已停止"
            self.finish("stopped")
        except RuntimeError as exc:
            # max_steps / max_tool_calls raised by RunControl.
            msg = str(exc)
            if msg == "max_tool_calls":
                reason = "max_tool_calls"
                status = RunStatus.MAX_TOOL_CALLS
                text = f"⚠️ 已达最大工具调用数（{getattr(self.agent, '_run_control', None) and self.agent._run_control.budget.max_tool_calls or ''}），本轮已终止"
                self.finish("max_tool_calls")
            elif msg == "max_steps":
                reason = "max_steps"
                status = RunStatus.MAX_STEPS
                text = f"⚠️ 已达最大步骤数（{getattr(self.agent, '_run_control', None) and self.agent._run_control.budget.max_steps or ''}），本轮已终止"
                self.finish("max_steps")
            else:
                raise
        except Exception as exc:
            # MCP init / persistence / compression failures still close out
            # with a structured ERROR result and agent_end.
            reason = "error"
            status = RunStatus.ERROR
            error_code = type(exc).__name__
            error_message = str(exc)
            text = getattr(self.agent, "_runtime_failure_text", lambda e: str(e))(exc)
            self.finish("error")
        finally:
            # P2-9: clear the running marker.
            try:
                self.agent._is_running = False
            except Exception:
                pass
            # Apply a run-time pending model switch immediately after this
            # round instead of waiting for the next one.
            consume = getattr(self.agent, "_consume_pending_llm", None)
            if callable(consume):
                try:
                    consume()
                except Exception:
                    pass
            for attr in ("_event_seq", "_event_sink", "_active_loop"):
                if hasattr(self.agent, attr):
                    try:
                        setattr(self.agent, attr, None)
                    except Exception:
                        pass
        # P3: best-effort usage passthrough from llm.last_usage.
        usage = {}
        try:
            last_usage = getattr(getattr(self.agent, "llm", None), "last_usage", None)
            if isinstance(last_usage, dict):
                usage = dict(last_usage)
        except Exception:
            pass
        return AgentRunResult(
            run_id=getattr(self.agent, "_run_id", ""),
            status=status,
            visible_text=text or "",
            summary=(text or "")[:1000],
            error_code=error_code,
            error_message=error_message,
            usage=usage,
        )
