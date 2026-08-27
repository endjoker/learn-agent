# -*- coding: utf-8 -*-
"""并行工具批处理超时不得被 ThreadPoolExecutor 隐式等待抵消（P1-3）。

旧实现用 ``with ThreadPoolExecutor(...) as pool:``，退出会 shutdown(wait=True)，
只要一个工具超时后底层线程仍在跑，整批就会隐式等待它，AgentLoop 被卡住。
验证：改用 Agent 级持久化池后，调度的并行段在超时边界立即返回，不被挂起线程拖住。
"""
import time
import unittest

from core.agent_runtime.tools import ToolBatchExecutor, PreparedToolCall


def _tool(name, parallel_safe):
    return type(f"Tool_{name}", (), {"name": name, "parallel_safe": parallel_safe})()


class FakeRegistry:
    def __init__(self, tool): self._tool = tool
    def get_tool(self, name): return self._tool if self._tool and self._tool.name == name else None


class FakeAgent:
    def __init__(self, timeout=1, hang=3):
        self._config = {"agent_runtime": {"tool_timeout_seconds": timeout, "max_parallel_tools": 4}}
        self.tool_registry = FakeRegistry(_tool("hang", True))
        self._hang = hang

    def _execute_native_tool_call(self, call_id, provider_name, tool_name, arguments, raw_arguments):
        if tool_name == "hang":
            time.sleep(self._hang)  # 模拟一个不协作取消、实际挂起的工具（远超 timeout）
            return "finally done", False
        return "ok", False


class ToolBatchTimeoutTests(unittest.TestCase):
    def test_timed_out_parallel_segment_returns_promptly(self):
        agent = FakeAgent(timeout=1, hang=3)
        batch = ToolBatchExecutor(agent)
        calls = [PreparedToolCall("call_1", "hang", "hang", {}, None, 0)]
        start = time.time()
        results = batch.execute(calls)
        elapsed = time.time() - start
        # 批次必须在超时边界附近返回，而不是等挂起工具跑完（3s）。
        self.assertLess(elapsed, 2.5)
        self.assertEqual(len(results), 1)
        call, observation, is_error = results[0]
        self.assertIn("超时", observation)
        self.assertTrue(is_error)
        agent._parallel_tool_executor.shutdown(wait=False)

    def test_serial_non_parallel_safe_call_still_runs(self):
        # 非 parallel_safe 工具走串行路径，不触发并行池，也不受超时影响。
        agent = FakeAgent(timeout=0.5)
        agent.tool_registry = FakeRegistry(_tool("seq", False))
        batch = ToolBatchExecutor(agent)
        result = batch.execute([PreparedToolCall("c2", "seq", "seq", {}, None, 0)])
        self.assertEqual(result[0][1], "ok")
        self.assertFalse(result[0][2])
        agent._parallel_tool_executor.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
