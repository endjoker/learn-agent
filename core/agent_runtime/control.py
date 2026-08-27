"""Shared cancellation and budget accounting for an Agent run."""
from __future__ import annotations
import time
from core.runtime import Budget, CancellationToken, TaskCancelled


class BudgetExceeded(TaskCancelled):
    """预算超限（墙钟超时 / 累计 token 超限）。

    继承 TaskCancelled：由 AgentLoop.run 的取消收口路径统一处理，保证
    唯一的 agent_end 事件与结构化 AgentRunResult，而不是裸 RuntimeError
    打断收尾。
    """


class RunControl:
    def __init__(self, budget: Budget | None = None, token: CancellationToken | None = None):
        self.budget = budget or Budget()
        self.token = token or CancellationToken()
        self.steps = 0
        self.tool_calls = 0
        # P2：墙钟预算起点 = run 开始（begin_run 创建 RunControl 的时刻）。
        self._started_at = time.monotonic()
        self._total_tokens = 0

    def checkpoint(self) -> None:
        self.token.checkpoint()
        # P2：预算落地——配置存在才生效；超限抛 BudgetExceeded（TaskCancelled
        # 子类）走正常取消收口。
        if self.budget.max_wall_seconds is not None:
            if time.monotonic() - self._started_at > self.budget.max_wall_seconds:
                raise BudgetExceeded(
                    f"运行墙钟超时（>{self.budget.max_wall_seconds}s）")
        if (self.budget.max_total_tokens is not None
                and self._total_tokens > self.budget.max_total_tokens):
            raise BudgetExceeded(
                f"累计 token 超限（>{self.budget.max_total_tokens}）")

    def begin_step(self) -> None:
        self.checkpoint()
        self.steps += 1
        if self.budget.max_steps is not None and self.steps > self.budget.max_steps:
            raise RuntimeError("max_steps")

    def add_tool_calls(self, count: int = 1) -> None:
        self.tool_calls += count
        if self.budget.max_tool_calls is not None and self.tool_calls > self.budget.max_tool_calls:
            raise RuntimeError("max_tool_calls")

    def add_tokens(self, usage) -> None:
        """累计一次 provider 调用的 token（usage 为 None/缺字段时尽力而为）。"""
        if not isinstance(usage, dict):
            return
        total = usage.get("total_tokens")
        if not total:
            total = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        if total:
            try:
                self._total_tokens += int(total)
            except (TypeError, ValueError):
                pass
