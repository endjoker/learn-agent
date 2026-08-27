# -*- coding: utf-8 -*-
"""classify_runtime_failure 结构化失败判定单测（JKagent 收官）。

白名单：❌/⛔/⏰ 前缀 + insufficient_quota / 任务失败 子串 → 错误码；
白名单外的回复返回 None（成功/中性），保证 plan/goal 轮次不误判失败。
替代散落前缀判断（原 dispatcher._is_runtime_failure）后的单一判定入口。
"""

from __future__ import annotations

import unittest

from gateway.dispatcher import (
    RUNTIME_FAILURE_CODES,
    classify_runtime_failure,
)


class ClassifyRuntimeFailureTests(unittest.TestCase):
    def test_x_prefix_execution_failed(self):
        self.assertEqual(classify_runtime_failure("❌ 任务执行失败：xxx"),
                         "AGENT_EXECUTION_FAILED")

    def test_x_prefix_requires_leading_position(self):
        # 前缀标记只匹配回复开头；句中出现的 ❌ 不判失败
        self.assertIsNone(classify_runtime_failure("中途出现 ❌ 的普通文本"))

    def test_deny_prefix(self):
        self.assertEqual(classify_runtime_failure("⛔ 已拒绝: bash 需要审批"),
                         "AGENT_DENIED")

    def test_timeout_prefix(self):
        self.assertEqual(classify_runtime_failure("⏰ 处理超时，请简化问题后重试"),
                         "AGENT_TIMEOUT")

    def test_insufficient_quota_substring(self):
        self.assertEqual(classify_runtime_failure("insufficient_quota ..."),
                         "INSUFFICIENT_QUOTA")

    def test_insufficient_quota_case_insensitive(self):
        self.assertEqual(classify_runtime_failure("上游返回 Insufficient_Quota"),
                         "INSUFFICIENT_QUOTA")

    def test_task_failed_substring(self):
        self.assertEqual(classify_runtime_failure("任务失败：xxx"),
                         "TASK_FAILED")

    def test_success_reply_is_none(self):
        self.assertIsNone(classify_runtime_failure("✅ 已完成所有步骤"))

    def test_neutral_reply_is_none(self):
        self.assertIsNone(classify_runtime_failure("当前没有可执行的任务"))

    def test_empty_and_none_inputs(self):
        self.assertIsNone(classify_runtime_failure(None))
        self.assertIsNone(classify_runtime_failure(""))
        self.assertIsNone(classify_runtime_failure("   "))

    def test_whitelist_codes_complete(self):
        self.assertEqual(set(RUNTIME_FAILURE_CODES.values()), {
            "AGENT_EXECUTION_FAILED", "AGENT_DENIED", "AGENT_TIMEOUT",
            "AGENT_SESSION_BUSY", "INSUFFICIENT_QUOTA", "TASK_FAILED",
        })

    def test_session_busy_classified(self):
        # P1-2 exec_lock 忙拒绝：plan/goal 轮据此识别"未真正执行"
        self.assertEqual(
            classify_runtime_failure("⚠️ 会话正忙：上一条消息仍在处理中，请稍后再试"),
            "AGENT_SESSION_BUSY")


if __name__ == "__main__":
    unittest.main()
