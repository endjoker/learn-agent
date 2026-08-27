"""Single tool-batch lifecycle: prepare, execute, finalize in source order."""
from __future__ import annotations
import concurrent.futures
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("jk_agent")

@dataclass(frozen=True)
class PreparedToolCall:
    call_id: str
    provider_name: str
    tool_name: str | None
    arguments: Any
    raw_arguments: str | None
    order: int

class ToolBatchExecutor:
    def __init__(self, agent): self.agent = agent

    def _runtime_pool(self) -> ThreadPoolExecutor:
        """L2#10：唯一执行池 = ToolRuntime 的共享超时池（不随批次 shut down）。

        并行段不再建外层 tool-batch 池：整条 native 调用链直接提交运行时池，
        容量语义合并（max_parallel_tools 经 ensure_executor_capacity 作用到该池），
        池内线程标记 in_pool 后工具内联执行，不存在双池嵌套。
        无 ToolRuntime 注入（测试/旧路径）时退化为 Agent 级持久化池，保持
        兼容；两种来源都挂在 agent._parallel_tool_executor 别名上，
        runner 资源清理与旧测试 shutdown 的是同一个池。
        """
        rt = getattr(self.agent, "_tool_runtime", None)
        if rt is not None:
            max_workers = int(getattr(self.agent, "_config", {}).get(
                "agent_runtime", {}).get("max_parallel_tools", 4) or 4)
            rt.ensure_executor_capacity(max_workers)
            pool = rt.executor_pool()
            if getattr(self.agent, "_parallel_tool_executor", None) is not pool:
                self.agent._parallel_tool_executor = pool
            return pool
        pool = getattr(self.agent, "_parallel_tool_executor", None)
        if pool is None:
            max_workers = int(getattr(self.agent, "_config", {}).get(
                "agent_runtime", {}).get("max_parallel_tools", 4))
            pool = ThreadPoolExecutor(
                max_workers=max(1, max_workers), thread_name_prefix="tool-batch")
            self.agent._parallel_tool_executor = pool
        return pool

    def prepare(self, calls, name_map: dict[str, str]) -> list[PreparedToolCall]:
        prepared = []
        for call in sorted(calls, key=lambda item: item.order):
            provider_name = call.name
            internal = name_map.get(provider_name)
            if internal is None and self.agent.tool_registry.get_tool(provider_name): internal = provider_name
            prepared.append(PreparedToolCall(call.call_id or uuid.uuid4().hex, provider_name, internal, call.arguments, call.raw_arguments, call.order))
        return prepared

    def _is_parallel_safe(self, call: PreparedToolCall) -> bool:
        tool = self.agent.tool_registry.get_tool(call.tool_name) if call.tool_name else None
        # Default deny: only explicitly audited tools may overlap.
        return bool(getattr(tool, "parallel_safe", False))

    def _call_timeout(self, item: PreparedToolCall) -> int:
        """单条调用的并行等待超时（P3-6）：与串行路径同一口径。

        有 ToolRuntime 时走 tool_timeout_for（agent 配置 tool_timeout_seconds
        + 子进程类工具 subprocess_timeout_seconds 放宽 + arguments['timeout']
        显式放宽），不再读裸配置导致并行/串行口径分叉；无 ToolRuntime 注入的
        兼容路径退回读裸配置。
        """
        rt = getattr(self.agent, "_tool_runtime", None)
        if rt is not None:
            tool = (self.agent.tool_registry.get_tool(item.tool_name)
                    if item.tool_name else None)
            try:
                return max(0, int(rt.tool_timeout_for(self.agent, tool, item.arguments)))
            except (TypeError, ValueError):
                pass
        cfg = getattr(self.agent, "_config", None) or {}
        ar = cfg.get("agent_runtime") or {}
        try:
            return max(0, int(ar.get("tool_timeout_seconds", 60) or 0))
        except (TypeError, ValueError):
            return 0

    def execute(self, calls: list[PreparedToolCall]):
        """Execute an audited parallel segment while returning source order.

        Authorization/preparation stays sequential. Calls without explicit
        ``parallel_safe`` are deliberately serialized; this avoids races in
        write tools, approval hooks, and mutable process/session tools.

        L2#10：并行段直接把整条 native 调用链提交到 ToolRuntime 共享超时池
        （不再嵌套外层 tool-batch 池）。内层 _execute_with_timeout 检测到
        已在池内后内联执行工具，超时由本层 future.result(timeout) 统一执行；
        超时后登记 inflight 占用（与串行路径同一池饱和监控）。
        """
        results = []
        index = 0
        rt = getattr(self.agent, "_tool_runtime", None)
        pool = self._runtime_pool()
        while index < len(calls):
            call = calls[index]
            if not self._is_parallel_safe(call):
                # P2-5：串行分支单工具异常不得逃逸——异常逃逸会跳过本批剩余
                # 工具结果的落账，留下"声明了 N 个 call_id 却只有 M 个结果"
                # 的转录，provider 直接会话级 400 卡死。这里把任何异常转成
                # is_error 结果，保证批次继续、每个声明都有配对结果。
                try:
                    observation, is_error = self.agent._execute_native_tool_call(
                        call.call_id, call.provider_name, call.tool_name,
                        call.arguments, call.raw_arguments)
                except Exception as exc:
                    logger.error("串行工具 '%s' 执行异常（已转为错误结果）: %s",
                                 call.tool_name or call.provider_name, exc,
                                 exc_info=True)
                    observation = f"❌ 工具执行异常: {type(exc).__name__}: {exc}"
                    is_error = True
                results.append((call, observation, is_error))
                index += 1
                continue
            end = index
            while end < len(calls) and self._is_parallel_safe(calls[end]):
                end += 1
            segment = calls[index:end]
            if rt is not None:
                futures = [rt.submit_native_call(
                    self.agent, item.call_id, item.provider_name, item.tool_name,
                    item.arguments, item.raw_arguments) for item in segment]
            else:
                futures = [pool.submit(self.agent._execute_native_tool_call,
                    item.call_id, item.provider_name, item.tool_name,
                    item.arguments, item.raw_arguments) for item in segment]
            # futures are collected in source order, regardless of completion order.
            # 并行段同样受工具超时约束，避免单个挂起工具拖死整段；超时后批次
            # 立即返回（不 shutdown 等待底层线程），挂起线程仅占用池槽位，不阻塞 loop。
            # P3-6：逐 future 用 tool_timeout_for 计算超时（与串行同一口径），
            # 不再整段共用裸 tool_timeout_seconds。
            for item, future in zip(segment, futures):
                timeout = self._call_timeout(item)
                try:
                    result = future.result(timeout=timeout) if timeout > 0 else future.result()
                except (TimeoutError, concurrent.futures.TimeoutError):
                    # Py3.10 下 future.result(timeout=...) 抛的是
                    # concurrent.futures.TimeoutError（3.11+ 起与内置 TimeoutError
                    # 同义），两者都要捕获，否则超时被当普通异常吞成"执行失败"。
                    if rt is not None:
                        rt.register_inflight_timeout(future)
                    result = ("❌ 并行工具执行超时（>%ss）：%s" % (timeout, item.tool_name or item.provider_name), True)
                except Exception as exc:
                    result = ("❌ 并行工具执行失败: %s: %s" % (type(exc).__name__, exc), True)
                results.append((item, *result))
            index = end
        return results
