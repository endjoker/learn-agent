# -*- coding: utf-8 -*-
"""Agent._light_compress 压力门控接线测试。

修复"工具输出被过早压缩 → 模型反复重读同一文件"：
- 历史预算占用 < LIGHT_RESULT_RATIO 时不压缩工具结果（compress_results=False）；
- 占用达标或预算未知时才压缩；
- 近端保护 keep_recent_results 恒为 LIGHT_KEEP_RECENT_RESULTS。
"""
import unittest
from unittest.mock import patch

from core.compressor import LIGHT_KEEP_RECENT_RESULTS, LIGHT_RESULT_RATIO


class _FakeStore:
    def __init__(self, live: int, max_tokens: int):
        self._live = live
        self.max_tokens = max_tokens

    def live_tokens(self) -> int:
        return self._live


class _FakeCompressor:
    def __init__(self):
        self.calls = []

    def light_compress(self, messages, *, compress_results=True, keep_recent_results=0):
        self.calls.append({"compress_results": compress_results,
                           "keep_recent_results": keep_recent_results})
        return (0, 0)


def _make_agent(live: int, max_tokens: int):
    from agent import Agent
    agent = object.__new__(Agent)
    agent.store = _FakeStore(live, max_tokens)
    agent.messages = []
    agent._compressor = _FakeCompressor()
    agent._cancel_checkpoint = lambda: None
    return agent


class LightCompressGateTests(unittest.TestCase):

    def _run(self, live, max_tokens):
        agent = _make_agent(live, max_tokens)
        with patch("agent.log_info"):
            agent._light_compress()
        return agent._compressor.calls

    def test_under_threshold_skips_tool_result_compression(self):
        calls = self._run(live=59, max_tokens=100)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["compress_results"])

    def test_at_threshold_compresses(self):
        calls = self._run(live=60, max_tokens=100)
        self.assertTrue(calls[0]["compress_results"])

    def test_unknown_budget_keeps_legacy_always_compress(self):
        calls = self._run(live=10, max_tokens=0)
        self.assertTrue(calls[0]["compress_results"])

    def test_recent_window_constant_passed(self):
        calls = self._run(live=95, max_tokens=100)
        self.assertEqual(calls[0]["keep_recent_results"], LIGHT_KEEP_RECENT_RESULTS)
        self.assertGreater(LIGHT_KEEP_RECENT_RESULTS, 0)
        self.assertTrue(0.0 < LIGHT_RESULT_RATIO < 1.0)


if __name__ == "__main__":
    unittest.main()
