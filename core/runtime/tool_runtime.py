"""The single, policy-aware entry point for executing Agent tools."""

from __future__ import annotations

import concurrent.futures
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from core.debug import logger
from core.hook import Decision, HookResult
from core.permission import ALLOW, ASK, DENY
from core import shell as _shell

# L2#10：池内线程标记（thread-local）。并行段把整条 native 调用链提交到
# 共享超时池后，内层 _execute_with_timeout 据此内联执行工具，避免同一池
# 嵌套提交导致全部工作线程互等 future、工具永远无法调度（线程饥饿）。
_POOL_LOCAL = threading.local()


class _ExecutionTracking:
    """在途工具摘要句柄（Fix-3 可观测性）：进入设置、退出清除，供
    ToolRuntime._track_execution 以 with 语法使用。"""

    __slots__ = ("_rt", "_summary")

    def __init__(self, rt: "ToolRuntime", summary: str):
        self._rt = rt
        self._summary = summary

    def __enter__(self) -> None:
        with self._rt._exec_lock:
            self._rt._exec_summary = self._summary

    def __exit__(self, *exc) -> None:
        with self._rt._exec_lock:
            self._rt._exec_summary = None


class ToolRuntime:
    """Validate, authorize, hook and execute every native tool call.

    The runtime deliberately depends on a narrow Agent-shaped object rather
    than importing ``Agent``. This keeps ordinary chats, durable tasks and
    Child sessions on the same tool path without creating an import cycle.
    """

    def __init__(self, *, max_result_chars: int = 10000,
                 tool_timeout_seconds: int = 60,
                 executor_max_workers: int = 4):
        try:
            self.max_result_chars = max(0, int(max_result_chars))
        except (TypeError, ValueError):
            self.max_result_chars = 10000
        try:
            # 工具执行超时（秒）；<=0 表示不强制超时（放行工具自身控制）。
            self.tool_timeout_seconds = max(0, int(tool_timeout_seconds))
        except (TypeError, ValueError):
            self.tool_timeout_seconds = 60
        self._executor: ThreadPoolExecutor | None = None
        # L2#10：唯一执行池容量（串行/并行共用）。建池前可经
        # ensure_executor_capacity 按 max_parallel_tools 合并容量。
        try:
            self._executor_max_workers = max(1, int(executor_max_workers))
        except (TypeError, ValueError):
            self._executor_max_workers = 4
        # P0-2：工具池饱和监控。记录当前仍在"跑"但已超时（底层线程未停）的数量。
        # 一旦达到池容量，后续工具调用快速失败（返回池饱和说明），避免全部排队
        # 等在挂起线程后逐个超时，也便于运维判断是否有工具泄漏线程。
        self._inflight_timeouts = 0
        self._inflight_lock = threading.Lock()
        # 资源清理防护：shutdown_pool() 后置位；再次提交时惰性重建新池，
        # 避免向已关闭池提交抛 RuntimeError（P-C1FULL 协调项）。
        self._pool_closed = False
        # Fix-3（stop_timeout 诊断·可观测性）：当前在途工具摘要。停止看门
        # stop_timeout 终态时读取并写入日志——此前排查只能靠翻 SQLite 还原
        # "看门到点时到底卡在哪个工具/命令"。
        self._exec_lock = threading.Lock()
        self._exec_summary: str | None = None

    # ---- 在途工具摘要（Fix-3 可观测性） --------------------------------

    @staticmethod
    def _summarize_execution(tool, arguments) -> str:
        name = getattr(tool, "name", None) or str(tool)
        try:
            args_text = json.dumps(arguments, ensure_ascii=False, default=str)
        except Exception:
            args_text = str(arguments)
        if len(args_text) > 200:
            args_text = args_text[:200] + "…"
        return f"{name} {args_text}"

    def _track_execution(self, tool, arguments):
        """contextmanager：标记在途工具摘要（进入设置 / 退出清除）。"""
        return _ExecutionTracking(self, self._summarize_execution(tool, arguments))

    def current_execution_summary(self) -> str | None:
        """当前正在执行的工具摘要；空闲返回 None（看门日志/诊断用）。"""
        with self._exec_lock:
            return self._exec_summary

    def in_flight_timeouts(self) -> int:
        """当前已超时但底层线程仍在运行的工具数（用于监控/降级判断）。"""
        with self._inflight_lock:
            return self._inflight_timeouts

    def _release_inflight_timeout(self, _future) -> None:
        """超时工具的底层 future 真正结束时递减计数（可能被同步回调，幂等保护）。"""
        with self._inflight_lock:
            if self._inflight_timeouts > 0:
                self._inflight_timeouts -= 1

    def _tool_timeout(self, agent, tool=None, arguments=None) -> int:
        """工具级超时：优先 agent 配置的 tool_timeout_seconds，否则运行时默认。

        二次放宽（取三者最大值）：
        1) 子进程类工具（bash/python 等 exec:shell / exec:code）允许更长的执行窗口，
           使用 agent_runtime.subprocess_timeout_seconds（长任务不被普通 120s 保护网误杀）。
        2) 调用方在 arguments 里显式传入的 ``timeout``（工具可自选更长窗口）。
        """
        cfg = getattr(agent, "_config", None) or {}
        ar = cfg.get("agent_runtime") or {}
        configured = int(ar.get("tool_timeout_seconds", self.tool_timeout_seconds) or 0)
        if tool is not None:
            caps = set(getattr(tool, "capabilities", ()) or ())
            if "exec:shell" in caps or "exec:code" in caps:
                sub = int(ar.get("subprocess_timeout_seconds", 0) or 0)
                if sub > configured:
                    configured = sub
        if isinstance(arguments, dict):
            try:
                declared = int(arguments.get("timeout") or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared > configured:
                configured = declared
        return max(0, configured)

    def _executor_pool(self) -> ThreadPoolExecutor:
        # 防护：shutdown_pool() 后惰性重建新池（旧线程对象由 GC 回收），
        # 保证被"清理后复用"的 Agent 工具调用不抛 RuntimeError。
        if self._pool_closed or self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._executor_max_workers,
                thread_name_prefix="tool-timeout")
            self._pool_closed = False
        return self._executor

    def shutdown_pool(self, wait: bool = False) -> None:
        """资源清理入口：关闭执行池并标记；后续提交将惰性重建新池。"""
        executor = getattr(self, "_executor", None)
        if executor is not None:
            executor.shutdown(wait=wait)
        self._executor = None
        self._pool_closed = True

    def executor_pool(self) -> ThreadPoolExecutor:
        """公开的超时池引用（并行段与 Agent 资源清理共用同一实例）。"""
        return self._executor_pool()

    def ensure_executor_capacity(self, max_workers: int) -> None:
        """合并并行容量到唯一池（L2#10）。

        并行段不再建外层 tool-batch 池，max_parallel_tools 直接作用到本池；
        仅在池尚未创建时生效（建池后容量固定，变更需重启或重新构造）。
        """
        try:
            workers = max(1, int(max_workers or self._executor_max_workers))
        except (TypeError, ValueError):
            return
        if self._executor is None:
            self._executor_max_workers = workers
        elif workers != self._executor_max_workers:
            logger.warning(
                "工具池已创建，无法把容量调整为 %d（当前 %d）；"
                "请在首次工具调用前配置 max_parallel_tools。",
                workers, self._executor_max_workers)

    def tool_timeout_for(self, agent, tool, arguments) -> int:
        """对外暴露的按工具超时计算（并行段逐调用复用串行同一语义）。"""
        return self._tool_timeout(agent, tool, arguments)

    def _run_in_pool(self, fn, *args, **kwargs):
        """在池线程内运行 fn，并标记该线程已处于运行时池中。

        L2#10：内层 _execute_with_timeout 检测到 in_pool 后内联执行工具，
        不再二次提交到同一池；超时由外层 future.result(timeout=...) 统一
        执行（并行段）或串行路径的原有提交（保持既有语义）。
        """
        _POOL_LOCAL.in_pool = True
        try:
            return fn(*args, **kwargs)
        finally:
            _POOL_LOCAL.in_pool = False

    def submit_native_call(self, agent, call_id: str, provider_name: str,
                           tool_name: str | None, arguments: Any,
                           raw_arguments: str | None = None):
        """把一条完整 native 调用链作为单个单元提交到共享超时池。

        供并行段（ToolBatchExecutor）使用：校验→授权→hook→执行整链在池
        线程内跑完，内层 _execute_with_timeout 检测 in_pool 后内联执行工具，
        因此全程只有一个池——容量语义与串行路径合并（L2#10）。
        调用方持有返回的 future，用 future.result(timeout=...) 收取。
        """
        return self._executor_pool().submit(
            self._run_in_pool, self.execute_native_call,
            agent, call_id, provider_name, tool_name, arguments, raw_arguments)

    def register_inflight_timeout(self, future) -> None:
        """并行段 future 超时后登记占用计数（与串行路径同一监控语义）。

        future 真正结束（done callback）时自动递减，避免池饱和误报。
        """
        with self._inflight_lock:
            self._inflight_timeouts += 1
        future.add_done_callback(self._release_inflight_timeout)

    def _execute_with_timeout(self, agent, tool, arguments):
        """执行工具并在超时后返回超时 observation，而非永久卡死 loop。

        P0-2 降级：若已超时的工具数占满整个池（底层线程仍未停），对新的工具调用
        快速返回设备饱和说明，而不是再次排队等在挂起线程后逐个超时。
        """
        timeout = self._tool_timeout(agent, tool, arguments)
        if timeout <= 0:
            with self._track_execution(tool, arguments):
                return tool.execute(**arguments), None
        if getattr(_POOL_LOCAL, "in_pool", False):
            # L2#10：已在本池线程内（并行段整链提交）——内联执行，超时由
            # 外层 future.result(timeout) 统一执行，避免同池嵌套饥饿。
            with self._track_execution(tool, arguments):
                return tool.execute(**arguments), None
        pool = self._executor_pool()
        pool_capacity = pool._max_workers
        if self._inflight_timeouts >= pool_capacity:
            return None, (
                f"❌ 工具执行池已饱和：已有 {self._inflight_timeouts} 个工具超时后仍占用执行线程。"
                f"请停止卡死的工具或增大 tool_timeout_seconds，稍后再试。"
            )

        # 停止直通修复（stop_timeout 诊断 Fix-1，方案 B）：stop_check 存储在
        # threading.local，只对写入线程可见。串行路径在 loop 线程 set
        # （execute_native_call），却在这里把 tool.execute 提交到池 worker
        # 执行 → run_killable / sandbox 轮询在 worker 线程读到 None，
        # "停止即杀进程组"对 bash 类工具完全失效（sleep 45 只能自然跑完，
        # 10s 停止看门必然先到 → stop_timeout）。把 set/清除包进 worker
        # 线程内执行，每次工具执行自带停止直通，与并行路径（整链提交，
        # set 与执行同线程）语义对齐。
        # 不采用模块级槽位（诊断方案 A）：多会话 Agent 并发时互相覆盖/清除
        # 会重新引入同类竞态；threading.local + 按执行安装是最小且无竞态的修复。
        def _execute_with_stop_check(**kwargs):
            _shell.set_stop_check(
                lambda: bool(getattr(agent, "_stop_requested", False)))
            _shell.set_stop_owner(id(agent))
            try:
                with self._track_execution(tool, kwargs):
                    return tool.execute(**kwargs)
            finally:
                _shell.set_stop_check(None)
                _shell.set_stop_owner(None)

        future = pool.submit(self._run_in_pool, _execute_with_stop_check, **arguments)
        try:
            return future.result(timeout=timeout), None
        except (TimeoutError, concurrent.futures.TimeoutError):
            with self._inflight_lock:
                self._inflight_timeouts += 1
            # 底层线程/future 真正结束时递减计数，保证“池饱和”是暂态而非永久。
            future.add_done_callback(self._release_inflight_timeout)
            logger.warning(
                "工具 '%s' 超时（>%ss），底层线程仍占用工具池（当前累计 %d 个）。",
                getattr(tool, "name", tool), timeout, self._inflight_timeouts)
            return None, (
                f"❌ 工具执行超时（>{timeout}s）。"
                f"请拆分命令、增大 tool_timeout_seconds，或检查是否网络/进程挂起。"
            )

    def execute_text(self, agent, tool_name: str, input_str: str | None = None) -> str:
        if not input_str:
            arguments: dict[str, Any] = {}
        else:
            try:
                arguments = json.loads(input_str)
            except json.JSONDecodeError as exc:
                return f"❌ 参数不是合法 JSON: {exc}\n收到: {input_str}"
        if not isinstance(arguments, dict):
            return "❌ 参数必须是 JSON 对象"
        return self.execute_arguments(agent, tool_name, arguments)

    def execute_arguments(self, agent, tool_name: str, arguments: dict[str, Any]) -> str:
        tool = agent.tool_registry.get_tool(tool_name)
        if tool is None:
            available = ", ".join(item.name for item in agent.tool_registry.list_tools())
            return f"❌ 未知工具 '{tool_name}'。可用: {available}"
        errors = agent.tool_registry.validate_arguments(tool_name, arguments)
        if errors:
            return "❌ 参数校验失败: " + "; ".join(errors)
        try:
            result, timeout_err = self._execute_with_timeout(agent, tool, arguments)
            if timeout_err is not None:
                return timeout_err
            return result
        except TypeError as exc:
            return (
                f"❌ 参数不匹配: {exc}\n"
                f"工具 '{tool_name}' 需要的参数:\n"
                f"{json.dumps(tool.parameters, ensure_ascii=False, indent=2)}"
            )
        except Exception as exc:
            logger.error("工具 '%s' 执行失败: %s", tool_name, exc, exc_info=True)
            return f"❌ 工具出错: {type(exc).__name__}: {exc}"

    def execute_native_call(self, agent, call_id: str, provider_name: str,
                            tool_name: str | None, arguments: Any,
                            raw_arguments: str | None = None) -> tuple[str, bool]:
        """Run one provider-native call and always return a typed observation.

        P2-5 兜底：判定/执行链任何未预期异常都转为 is_error 观察结果返回，
        不向调用方（串行批次/并行池线程）逃逸——逃逸会让该 call_id 没有
        tool 结果落账，provider 报 "No tool output found for function call"
        造成会话级 400。end 事件在任何路径上都恰好发一次。
        """
        display_name = tool_name or provider_name
        agent._emit_event("tool_execution_start", tool_call_id=call_id, tool=display_name,
                          arguments=arguments)
        # 停止直通（本线程）：把"agent 是否收到停止请求"暴露给子进程等待
        # 循环（run_killable / sandbox _execute 轮询）——停止请求立即杀进程
        # 组，不再等满 subprocess_timeout（默认 1200s）。只读标志不消费，
        # 消费仍由 AgentLoop 检查点负责。finally 保证清理，不污染池线程。
        # stop_timeout Fix-2：同步登记进程组归属（id(agent)），request_stop
        # 可对该 agent 名下的在途进程组直接强杀。
        _shell.set_stop_check(lambda: bool(getattr(agent, "_stop_requested", False)))
        _shell.set_stop_owner(id(agent))
        try:
            observation, is_error = self._dispatch_native_call(
                agent, call_id, provider_name, tool_name, arguments)
        finally:
            _shell.set_stop_check(None)
            _shell.set_stop_owner(None)
        observation = self._truncate_observation(observation)
        agent._emit_event("tool_execution_end", tool_call_id=call_id, tool=display_name,
                          result=observation, is_error=is_error)
        return observation, is_error

    def _dispatch_native_call(self, agent, call_id: str, provider_name: str,
                              tool_name: str | None, arguments: Any) -> tuple[str, bool]:
        """判定/授权/执行链（自 execute_native_call 拆出，便于包停止检查）。"""
        display_name = tool_name or provider_name
        try:
            if not tool_name:
                observation, is_error = f"❌ 未知工具: {provider_name}", True
            elif not isinstance(arguments, dict) or "__invalid_raw_arguments__" in arguments:
                observation, is_error = "❌ 工具参数不是完整 JSON 对象", True
            else:
                errors = agent.tool_registry.validate_arguments(tool_name, arguments)
                if errors:
                    observation, is_error = "❌ 参数校验失败: " + "; ".join(errors), True
                else:
                    level, reason = agent._gate_check(tool_name, arguments)
                    if level == DENY:
                        self._safe_hook(agent, "run_denied", tool_name,
                                        reason or "权限不足", level="gate")
                        observation, is_error = f"⛔ 已拒绝: {reason or '权限不足'}", True
                    elif level == ASK and not self._approved(agent, tool_name, arguments):
                        observation, is_error = "⏭️ 用户未批准工具调用", True
                    else:
                        # B4 判定链 memo：把首闸门 (level, reason) 与已校验标记传给
                        # execute_authorized；hook 未 MODIFY 时不再重跑第二闸门与
                        # 第二次参数校验（:135 出口校验保留）。
                        observation, is_error = self.execute_authorized(
                            agent, tool_name, arguments,
                            gate_level=level, gate_reason=reason, prevalidated=True)
        except Exception as exc:
            logger.error("工具 '%s' 执行链异常（已转为错误结果）: %s",
                         display_name, exc, exc_info=True)
            observation, is_error = f"❌ 工具执行异常: {type(exc).__name__}: {exc}", True
        return observation, is_error

    def _safe_hook(self, agent, fn: str, *args, default=None, **kwargs):
        """安全调用用户 hook：抛异常时退化为默认裁决并记录日志，不炸掉 loop。"""
        hook = getattr(getattr(agent, "hooks", None), fn, None)
        if not callable(hook):
            return default
        try:
            return hook(*args, **kwargs)
        except Exception as exc:
            logger.error("hook '%s' 执行异常（已忽略并放行）: %s", fn, exc, exc_info=True)
            return default

    @staticmethod
    def _approved(agent, tool_name: str, arguments: dict[str, Any]) -> bool:
        run_notification = getattr(agent.hooks, "run_notification", None)
        if callable(run_notification):
            try:
                notification = run_notification(
                    tool_name, arguments, message="tool approval requested")
                if notification.decision == Decision.BLOCK:
                    return False
            except Exception as exc:
                logger.error("hook 'run_notification' 异常（已忽略，放行）: %s", exc, exc_info=True)
        answer = agent._ask_user(tool_name, arguments)
        return answer in ("", "y", "yes", "a")

    def _truncate_observation(self, observation: Any) -> str:
        """Bound one tool observation before it is persisted into LLM context."""
        text = str(observation)
        limit = self.max_result_chars
        if limit <= 0 or len(text) <= limit:
            return text
        if limit < 80:
            return text[:limit]
        omitted = len(text) - limit
        marker = f"\n\n… [工具结果已截断，省略 {omitted} 个字符] …\n\n"
        payload_limit = max(1, limit - len(marker))
        head = max(1, int(payload_limit * 0.8))
        tail = max(0, payload_limit - head)
        return text[:head] + marker + (text[-tail:] if tail else "")

    def execute_authorized(self, agent, tool_name: str,
                           arguments: dict[str, Any], *,
                           gate_level: str | None = None,
                           gate_reason: str | None = None,
                           prevalidated: bool = False) -> tuple[str, bool]:
        original_arguments = copy.deepcopy(arguments)
        modified = False
        before = self._safe_hook(agent, "run_pre_tool", tool_name,
                                 copy.deepcopy(arguments), gate_level="allow",
                                 default=HookResult())
        if before.decision == Decision.BLOCK:
            return f"⛔ hook 拦截: {before.reason}", True
        if before.decision == Decision.MODIFY and before.data is not None:
            if not isinstance(before.data, dict):
                return "⛔ hook 修改后的参数必须是对象", True
            arguments = copy.deepcopy(before.data)
            modified = True
        else:
            # Python hooks receive mutable objects. Ignore in-place mutation
            # unless they explicitly return MODIFY + data.
            arguments = original_arguments

        # Bind authorization to the final payload, not merely the pre-hook one.
        # B4 判定链 memo：入口（execute_native_call）已校验且 hook 未 MODIFY
        # 时跳过第二次参数校验与第二闸门（复用传入的 level/reason）；
        # MODIFY 或直调（未预校验）时全部重跑——fail-closed 语义不变。
        if modified or not prevalidated:
            validation_error = agent.tool_registry.validate_arguments(tool_name, arguments)
            if validation_error:
                return f"⛔ 修改后的工具参数无效: {validation_error}", True
        if modified or gate_level is None:
            level, reason = agent._gate_check(tool_name, arguments)
        else:
            level, reason = gate_level, gate_reason
        if level == DENY:
            self._safe_hook(agent, "run_denied", tool_name, reason or "权限不足", level="gate")
            return f"⛔ SecurityGate 拒绝最终调用: {reason or '权限不足'}", True
        if level == ASK and arguments != original_arguments \
                and not self._approved(agent, tool_name, arguments):
            return "⏭️ 用户未批准最终工具调用", True
        observation = self.execute_arguments(agent, tool_name, arguments)
        is_error = observation.startswith(("❌", "⛔", "⏭️"))
        after = self._safe_hook(agent, "run_post_tool", tool_name, arguments,
                                observation, is_error, default=HookResult())
        if after.decision == Decision.MODIFY and after.data:
            observation = after.data.get("result", observation)
            is_error = observation.startswith(("❌", "⛔", "⏭️"))
        elif after.decision == Decision.BLOCK:
            observation, is_error = f"⛔ hook 拦截: {after.reason}", True
        return observation, is_error
