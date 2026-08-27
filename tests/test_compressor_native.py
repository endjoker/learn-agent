# -*- coding: utf-8 -*-
"""轻量压缩对原生 role=tool 结果的识别（P1-6）+ 压力门控/近端保护。

原生 tool-call loop 追加的是一条 ``{"role": "tool", "kind": "tool_result",
"tool_call_id": ..., "name": ...}`` 消息，而非旧版 ``{"role": "user",
"name": "tool_result"}``。轻量压缩必须同时识别两种格式，否则长工具输出
永远不会被压缩，持续占用上下文并反复触发昂贵的全量摘要。

压力门控/近端保护（修复"工具输出被过早压缩 → 模型反复重读同一文件"）：
- compress_results=False 时跳过工具结果压缩（图片降级仍执行）；
- keep_recent_results=N 时最近 N 条仍有内容的工具结果永不压缩。
"""
import unittest

from core.compressor import (
    _compress_tool_result,
    light_compress_tool_results,
    tool_compress_needed,
)


def _native_tool_result(call_id, name, content, is_error=False):
    return {"role": "tool", "tool_call_id": call_id, "name": name,
            "content": content, "kind": "tool_result", "is_error": is_error}


class NativeToolResultCompressionTests(unittest.TestCase):

    def test_native_tool_result_is_compressed_when_consumed(self):
        # assistant(tool_calls) → tool(result) → assistant(final) 的三段式
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "query"},
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}]},
            _native_tool_result("call_1", "bash", "A" * 5000),
            {"role": "assistant", "content": "final answer", "kind": "final"},
        ]
        light_compress_tool_results(messages)
        # 原生 tool 结果被就地压缩为短摘要，且保留了消息类型字段
        result = messages[3]
        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "call_1")
        self.assertEqual(result["name"], "bash")
        self.assertEqual(result["kind"], "tool_result")
        self.assertIn("已压缩", result["content"])
        self.assertIn("工具: bash", result["content"])
        self.assertLess(len(result["content"]), 200)
        # 工具名回填自消息字段（原生内容不含"工具:"行）
        self.assertIn("工具: grep", _compress_tool_result(
            _native_tool_result("c2", "grep", "some output"))["content"])

    def test_legacy_tool_result_still_compressed(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "query"},
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "call_1"}]},
            {"role": "user", "name": "tool_result", "content": "X" * 5000},
            {"role": "assistant", "content": "done", "kind": "final"},
        ]
        light_compress_tool_results(messages)
        self.assertIn("已压缩", messages[3]["content"])
        self.assertEqual(messages[3]["role"], "user")
        self.assertEqual(messages[3]["name"], "tool_result")

    def test_unconsumed_tool_result_not_compressed(self):
        # 最后一个消息是 tool 结果，其后没有 assistant —— 模型尚未消费，不压缩
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "query"},
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "call_1"}]},
            _native_tool_result("call_1", "bash", "B" * 5000),
        ]
        light_compress_tool_results(messages)
        self.assertEqual(messages[3]["content"], "B" * 5000)

    def test_mixed_history_multiple_tool_results(self):
        # 多轮混合历史：native + legacy 都压缩，未消费的最后一条 tool 不压缩
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c1"}]},
            _native_tool_result("c1", "read", "R" * 4000),
            {"role": "assistant", "content": "a1", "kind": "final"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c2"}]},
            {"role": "user", "name": "tool_result", "content": "L" * 4000},
            {"role": "assistant", "content": "a2", "kind": "final"},
            _native_tool_result("c3", "grep", "unconsumed" * 500),
        ]
        light_compress_tool_results(messages)
        self.assertIn("已压缩", messages[3]["content"])     # native consumed
        self.assertIn("已压缩", messages[7]["content"])     # legacy consumed
        self.assertEqual(messages[9]["content"], "unconsumed" * 500)  # not consumed


class PressureGateTests(unittest.TestCase):
    """压力门控 + 近端保护（修复反复重读文件）。"""

    def test_compress_results_false_keeps_all_tool_results(self):
        messages = [
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c1"}]},
            _native_tool_result("c1", "read", "R" * 4000),
            {"role": "assistant", "content": "a1", "kind": "final"},
        ]
        old_total, new_total = light_compress_tool_results(
            messages, compress_results=False)
        self.assertEqual(messages[1]["content"], "R" * 4000)  # 原样保留
        self.assertEqual(old_total, new_total)                # 无节省

    def test_keep_recent_results_protects_tail_window(self):
        # 3 条已消费工具结果，保护最近 2 条 → 最旧 1 条被压缩
        messages = [
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c1"}]},
            _native_tool_result("c1", "read", "A" * 4000),
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c2"}]},
            _native_tool_result("c2", "read", "B" * 4000),
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c3"}]},
            _native_tool_result("c3", "grep", "C" * 4000),
            {"role": "assistant", "content": "final", "kind": "final"},
        ]
        light_compress_tool_results(messages, keep_recent_results=2)
        self.assertIn("已压缩", messages[1]["content"])          # 最旧 → 压缩
        self.assertEqual(messages[3]["content"], "B" * 4000)     # 近端保护
        self.assertEqual(messages[5]["content"], "C" * 4000)     # 近端保护
        # 压缩 stub 带重读提示
        self.assertIn("重新调用该工具", messages[1]["content"])

    def test_keep_recent_counts_only_uncompressed_results(self):
        # 最近窗口内已有 1 条 stub：保护名额应让给仍有内容的结果
        stub = _compress_tool_result(_native_tool_result("c0", "bash", "Z" * 4000))
        messages = [
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c0"}]},
            stub,
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c1"}]},
            _native_tool_result("c1", "read", "A" * 4000),
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c2"}]},
            _native_tool_result("c2", "read", "B" * 4000),
            {"role": "assistant", "content": "final", "kind": "final"},
        ]
        light_compress_tool_results(messages, keep_recent_results=2)
        self.assertEqual(messages[3]["content"], "A" * 4000)  # 内容结果受保护
        self.assertEqual(messages[5]["content"], "B" * 4000)  # 内容结果受保护
        self.assertIn("已压缩", messages[1]["content"])        # stub 保持 stub

    def test_keep_recent_zero_preserves_legacy_behavior(self):
        messages = [
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c1"}]},
            _native_tool_result("c1", "read", "A" * 4000),
            {"role": "assistant", "content": "final", "kind": "final"},
        ]
        light_compress_tool_results(messages, keep_recent_results=0)
        self.assertIn("已压缩", messages[1]["content"])

    def test_tool_compress_needed_thresholds(self):
        # 预算未知 → 保守压缩（旧行为）
        self.assertTrue(tool_compress_needed(1000, 0))
        self.assertTrue(tool_compress_needed(1000, -1))
        # 有预算：占用 < 阈值不压缩；达到阈值才压缩
        self.assertFalse(tool_compress_needed(59, 100, ratio=0.60))
        self.assertTrue(tool_compress_needed(60, 100, ratio=0.60))
        self.assertTrue(tool_compress_needed(95, 100, ratio=0.60))


if __name__ == "__main__":
    unittest.main()
