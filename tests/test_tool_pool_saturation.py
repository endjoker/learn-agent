# -*- coding: utf-8 -*-
"""P0-2：工具池饱和监控与降级保护。

超时后的底层线程无法被 kill（Python 线程），多个卡死工具会占满共享的
tool-timeout 执行池，后续工具排队后逐个超时。此处验证：一旦累计超时数达到
池容量，新的工具调用快速返回"池已饱和"说明，而不是再次排队。
"""
import unittest

from core.runtime.tool_runtime import ToolRuntime
from core.hook import HookResult
from core.permission import ALLOW


class FakeRegistry:
    def __init__(self, tool): self._tool = tool
    def get_tool(self, name): return self._tool if self._tool and self._tool.name == name else None
    def validate_arguments(self, name, args): return None
    def list_tools(self): return []


class FakeHooks:
    enabled = False
    @staticmethod
    def run_pre_tool(*a, **k): return HookResult()
    @staticmethod
    def run_post_tool(*a, **k): return HookResult()
    @staticmethod
    def run_denied(*a, **k): return None
    @staticmethod
    def run_notification(*a, **k): return HookResult()


class QuickTool:
    name = "read"
    parameters = {}
    def execute(self, **_kwargs): return "ok"


class ToolPoolSaturationTests(unittest.TestCase):
    def _agent(self, tool, timeout=5):
        from types import SimpleNamespace
        agent = SimpleNamespace(
            tool_registry=FakeRegistry(tool), hooks=FakeHooks(),
            _config={"agent_runtime": {"tool_timeout_seconds": timeout}})
        agent._gate_check = lambda n, a: (ALLOW, "")
        agent._ask_user = lambda n, a: "y"
        agent._emit_event = lambda *a, **k: None
        return agent

    def test_saturation_returns_fast_failure_instead_of_queueing(self):
        rt = ToolRuntime(tool_timeout_seconds=5)
        agent = self._agent(QuickTool())
        pool = rt._executor_pool()
        with rt._inflight_lock:
            rt._inflight_timeouts = pool._max_workers  # 模拟池被卡死工具占满
        obs = rt.execute_arguments(agent, "read", {})
        self.assertIn("饱和", obs)
        self.assertEqual(rt.in_flight_timeouts(), pool._max_workers)

    def test_timeout_increases_in_flight_counter(self):
        # 一个快速但超时阈值极小的工具 → 超时后累计数 +1
        from types import SimpleNamespace
        import time as _time
        class Hang:
            name = "hang"
            parameters = {}
            def execute(self, **_kwargs):
                _time.sleep(3)
                return "done"
        rt = ToolRuntime(tool_timeout_seconds=1)
        agent = self._agent(Hang(), timeout=1)
        obs = rt.execute_arguments(agent, "hang", {})
        self.assertIn("超时", obs)
        self.assertGreaterEqual(rt.in_flight_timeouts(), 1)


if __name__ == "__main__":
    unittest.main()
