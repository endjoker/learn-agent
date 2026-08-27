# -*- coding: utf-8 -*-
"""ToolRuntime robustness tests: 工具执行超时 + hook 异常兜底（防炸 loop）。"""
import time
import unittest

from core.runtime.tool_runtime import ToolRuntime
from core.hook import HookResult
from core.permission import ALLOW


class FakeRegistry:
    def __init__(self, tool=None):
        self._tool = tool
    def get_tool(self, name):
        return self._tool if self._tool and self._tool.name == name else None
    def validate_arguments(self, name, args):
        return None
    def list_tools(self):
        return []


class FakeHooks:
    enabled = False
    @staticmethod
    def run_pre_tool(*a, **k):
        return HookResult()
    @staticmethod
    def run_post_tool(*a, **k):
        return HookResult()
    @staticmethod
    def run_denied(*a, **k):
        return None
    @staticmethod
    def run_notification(*a, **k):
        return HookResult()


class HangingTool:
    name = "hang"
    parameters = {}
    def execute(self, **_kwargs):
        time.sleep(10)
        return "done"


class ThrowingHookTool:
    name = "boom"
    parameters = {}
    def execute(self, **_kwargs):
        return "ok"


class HooksThatThrow:
    """run_pre_tool/run_post_tool/run_denied 都抛异常，不得炸掉执行。"""
    enabled = True
    @staticmethod
    def run_pre_tool(*a, **k):
        raise RuntimeError("pre_tool boom")
    @staticmethod
    def run_post_tool(*a, **k):
        raise RuntimeError("post_tool boom")
    @staticmethod
    def run_denied(*a, **k):
        raise RuntimeError("denied boom")


class ToolRuntimeRobustnessTests(unittest.TestCase):
    def _agent(self, tool, hooks=None, timeout=60):
        from types import SimpleNamespace
        agent = SimpleNamespace(
            tool_registry=FakeRegistry(tool),
            hooks=hooks or FakeHooks(),
            _config={"agent_runtime": {"tool_timeout_seconds": timeout}},
        )
        agent._gate_check = lambda n, a: (ALLOW, "")
        agent._ask_user = lambda n, a: "y"
        agent._emit_event = lambda *a, **k: None
        return agent

    def test_tool_hang_returns_timeout_without_blocking(self):
        rt = ToolRuntime(tool_timeout_seconds=1)
        agent = self._agent(HangingTool(), timeout=1)
        start = time.time()
        obs = rt.execute_arguments(agent, "hang", {})
        elapsed = time.time() - start
        self.assertIn("超时", obs)
        self.assertLess(elapsed, 5)

    def test_throwing_hooks_do_not_break_execution(self):
        rt = ToolRuntime(tool_timeout_seconds=5)
        # execute_authorized 路径会调用 run_pre_tool/run_post_tool（都抛异常）
        agent = self._agent(ThrowingHookTool(), hooks=HooksThatThrow(), timeout=5)
        obs, is_error = rt.execute_authorized(agent, "boom", {})
        # hook 异常被吞掉并放行，工具正常返回 "ok"
        self.assertEqual(obs, "ok")
        self.assertFalse(is_error)


class _ExecTool:
    name = "bash"
    capabilities = ("exec:shell",)
    def execute(self, command, timeout=None):
        return command


class _FsTool:
    name = "read"
    capabilities = ("fs:read",)
    def execute(self, file_path):
        return file_path


class ToolTimeoutWideningTests(unittest.TestCase):
    """子进程类工具应获得更宽的执行窗口，约束由 subprocess_timeout_seconds 决定。"""

    def _agent(self, tool_timeout=120, subprocess_timeout=1200):
        from types import SimpleNamespace
        return SimpleNamespace(
            _config={"agent_runtime": {
                "tool_timeout_seconds": tool_timeout,
                "subprocess_timeout_seconds": subprocess_timeout,
            }})

    def test_subprocess_tool_uses_wider_timeout(self):
        rt = ToolRuntime(tool_timeout_seconds=120)
        agent = self._agent()
        # exec:shell 工具 → 放宽到 subprocess_timeout_seconds (1200)
        self.assertEqual(rt._tool_timeout(agent, _ExecTool(), {}), 1200)

    def test_non_subprocess_tool_keeps_general_timeout(self):
        rt = ToolRuntime(tool_timeout_seconds=120)
        agent = self._agent()
        # fs:read 无 exec 能力 → 保持通用 120s
        self.assertEqual(rt._tool_timeout(agent, _FsTool(), {}), 120)

    def test_per_call_timeout_overrides_both(self):
        rt = ToolRuntime(tool_timeout_seconds=120)
        agent = self._agent()
        # 调用方显式传入更大的 timeout 参数 → 以它为准
        self.assertEqual(rt._tool_timeout(agent, _FsTool(), {"timeout": 600}), 600)

    def test_lower_configured_tool_timeout_still_bounded(self):
        rt = ToolRuntime(tool_timeout_seconds=120)
        agent = self._agent(tool_timeout=30, subprocess_timeout=1200)
        # exec 工具仍以 subprocess 宽限为准（1200），不受通用 30s 压制
        self.assertEqual(rt._tool_timeout(agent, _ExecTool(), {}), 1200)


if __name__ == "__main__":
    unittest.main()
