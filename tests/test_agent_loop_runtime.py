# -*- coding: utf-8 -*-
"""AgentLoop 运行期加固单测（P1-4 / P1-5 / P2-8）。

用轻量 fake Agent 驱动的 AgentLoop：
- P1-4：配置的 max_tool_calls / max_steps 进入 RunControl Budget；超限返回
  MAX_TOOL_CALLS / MAX_STEPS，而不是无效配置。
- P1-5：外部 CancellationToken 贯通到 RunControl.checkpoint，取消即 CANCELLED。
- P2-8：外围步骤（MCP/持久化/压缩）抛异常时收口为结构化 ERROR 结果 +
  唯一 agent_end，不再跳过。
"""
import unittest

from core.agent_runtime.control import RunControl
from core.agent_runtime.loop import AgentLoop
from core.agent_runtime.models import AgentRunResult, RunStatus
from core.runtime import Budget, CancellationToken, TaskCancelled


class _FakeStore:
    def __init__(self): self.save_calls = 0
    def save_session(self): self.save_calls += 1


class _FakeAgent:
    """A minimal Agent-shaped object that AgentLoop depends on."""

    def __init__(self, *, max_tool_calls=2, max_steps=3, behaviour="ok"):
        self._config = {"agent_runtime": {"max_tool_calls": max_tool_calls}}
        self.max_steps = max_steps
        self._run_id = "run_1"
        self._last_run_reason = "completed"
        self._events = []
        self._event_seq = 0
        self.store = _FakeStore()
        self._run_token = None
        self._behaviour = behaviour
        self._active_loop = None
        self._turn_id = ""
        self._pending_llm_config = None
        self._consume_calls = 0

    def _consume_pending_llm(self):
        # 模拟 Agent._consume_pending_llm：运行结束后应用挂起切换。
        self._consume_calls += 1
        self._pending_llm_config = None

    def _emit_event(self, event_type, **payload):
        self._event_seq += 1
        self._events.append((event_type, payload))

    def _runtime_failure_text(self, exc):
        return f"❌ LLM 调用失败：{type(exc).__name__}: {exc}"

    def _run_native_loop(self, prompt, *, images=None, event_sink=None):
        # 模拟 _run_native_loop 的内部关键调用，用于触发各分支。
        self._active_loop.next_turn(1)          # → begin_step → checkpoint
        b = getattr(self, "_behaviour", "ok")
        if b == "raise_outer":
            raise ValueError("mcp init burst")
        if b == "over_tool_calls":
            self._run_control.add_tool_calls(99)   # 超过 budget.max_tool_calls
        if b == "cancelled":
            self._run_control.checkpoint()          # token 已取消 → TaskCancelled
        return "final answer"


class AgentLoopRuntimeTests(unittest.TestCase):

    def _loop(self, agent):
        return AgentLoop(agent)

    # ---- P1-4：budget 注入 ----
    def test_budget_is_injected_from_config(self):
        agent = _FakeAgent(max_tool_calls=2, max_steps=3)
        loop = self._loop(agent)
        loop.begin_run()
        rc = agent._run_control
        self.assertEqual(rc.budget.max_tool_calls, 2)
        # 会话不限制最大步骤数：max_steps 不再注入 budget（None = 不截断）。
        self.assertIsNone(rc.budget.max_steps)

    def test_max_tool_calls_returns_max_tool_calls_status(self):
        agent = _FakeAgent(max_tool_calls=2, behaviour="over_tool_calls")
        result = self._loop(agent).run("hi")
        self.assertEqual(result.status, RunStatus.MAX_TOOL_CALLS)
        self.assertIn("最大工具调用", result.visible_text)
        # 唯一 agent_end 事件已发且 reason 正确
        ends = [p for t, p in agent._events if t == "agent_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].get("reason"), "max_tool_calls")

    # ---- P2-8：外层异常收口 ----
    def test_outer_exception_returns_structured_error(self):
        agent = _FakeAgent(behaviour="raise_outer")
        result = self._loop(agent).run("hi")
        self.assertIsInstance(result, AgentRunResult)
        self.assertEqual(result.status, RunStatus.ERROR)
        self.assertEqual(result.error_code, "ValueError")
        self.assertIn("mcp init burst", result.error_message or "")
        # 异常路径也发唯一 agent_end（持久化已退役：不再有落盘动作，
        # save_calls 保持 0——独立 Agent 明确不持久化，见 persistence.py）。
        ends = [p for t, p in agent._events if t == "agent_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].get("reason"), "error")
        self.assertEqual(agent.store.save_calls, 0)

    # ---- P1-5：token 贯通 ----
    def test_external_cancellation_token_yields_cancelled(self):
        agent = _FakeAgent(behaviour="cancelled")
        token = CancellationToken()
        token.cancel("user_requested")
        agent._run_token = token
        result = self._loop(agent).run("hi")
        self.assertEqual(result.status, RunStatus.CANCELLED)
        ends = [p for t, p in agent._events if t == "agent_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].get("reason"), "stopped")

    def test_normal_completion(self):
        agent = _FakeAgent(max_tool_calls=100, behaviour="ok")
        result = self._loop(agent).run("hi")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.visible_text, "final answer")

    def test_pending_llm_switch_applied_after_run_completes(self):
        # 运行中切模型被挂起（_pending_llm_config 已设）；run 结束后必须立即应用，
        # 否则 agent.llm 停留旧模型 → 顶部显示与实际使用不一致（issue 3）。
        agent = _FakeAgent(max_tool_calls=100, behaviour="ok")
        agent._pending_llm_config = {"model": "gpt-5.6-luna"}
        result = self._loop(agent).run("hi")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        # run 结束后（finally）已消费挂起切换
        self.assertGreaterEqual(agent._consume_calls, 1)
        self.assertIsNone(agent._pending_llm_config)


if __name__ == "__main__":
    unittest.main()
