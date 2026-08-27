# -*- coding: utf-8 -*-
"""sandbox.python 黑名单合并语义回归测试。

修复：_get_python_rules 原为"硬编码兜底 ∪ config"只增不减——配置放宽的项
（如移除 open）会被默认集悄悄加回，模型调用 open 仍被拦（残留）。
新语义：config.json 提供某键 → 以配置为准（整体替换）；缺失该键 → 硬编码兜底。
"""

from __future__ import annotations

import unittest
from unittest import mock

from core.sandbox import guard
from core.sandbox.guard import _merge_python_rules, check_python_code

# 用户实际 config（config.json / config.example.json 一致）：不含 open
USER_RULES = {
    "forbidden_imports": ["ctypes", "socket"],
    "forbidden_calls": ["eval", "exec", "__import__"],
    "forbidden_qualified_calls": [
        "os.system", "os.popen", "os.execv", "os.execve", "os.execvp",
        "os.execvpe", "os.remove", "os.unlink", "os.rmdir", "os.kill",
        "os.killpg", "shutil.rmtree",
    ],
}


class MergeSemanticsTests(unittest.TestCase):
    def test_config_provided_key_replaces_default(self):
        merged = _merge_python_rules(USER_RULES)
        self.assertNotIn("open", merged["forbidden_calls"])
        self.assertNotIn("getattr", merged["forbidden_calls"])
        self.assertNotIn("setattr", merged["forbidden_calls"])
        self.assertIn("eval", merged["forbidden_calls"])
        self.assertIn("__import__", merged["forbidden_calls"])

    def test_missing_key_falls_back_to_default(self):
        merged = _merge_python_rules({"forbidden_calls": ["eval"]})
        self.assertEqual(merged["forbidden_calls"], {"eval"})
        # 未提供的键用硬编码兜底
        self.assertIn("subprocess", merged["forbidden_imports"])
        self.assertIn("builtins.open", merged["forbidden_qualified"])

    def test_empty_config_uses_full_defaults(self):
        merged = _merge_python_rules({})
        self.assertIn("open", merged["forbidden_calls"])
        self.assertIn("socket", merged["forbidden_imports"])

    def test_explicit_empty_list_respected(self):
        # 显式空列表 = 用户明确全放行，不用默认集覆盖
        merged = _merge_python_rules({"forbidden_calls": []})
        self.assertEqual(merged["forbidden_calls"], set())


class CheckPythonCodeBehaviorTests(unittest.TestCase):
    def _with_rules(self, rules):
        return mock.patch.object(guard, "_get_python_rules",
                                 lambda: _merge_python_rules(rules))

    def test_open_allowed_with_user_config(self):
        # 用户 config 已移除 open → open 调用放行（修复残留拦截）
        with self._with_rules(USER_RULES):
            is_safe, reason = check_python_code("open('a.txt', 'w').close()")
        self.assertTrue(is_safe, reason)

    def test_eval_still_blocked_with_user_config(self):
        with self._with_rules(USER_RULES):
            is_safe, reason = check_python_code("eval('1+1')")
        self.assertFalse(is_safe)
        self.assertIn("eval", reason)

    def test_os_system_still_blocked_with_user_config(self):
        with self._with_rules(USER_RULES):
            is_safe, _ = check_python_code("import os\nos.system('ls')")
        self.assertFalse(is_safe)

    def test_default_rules_still_block_open(self):
        # 无 sandbox.python 段时用硬编码兜底（open 拦截）。
        with self._with_rules({}):
            is_safe, reason = check_python_code("open('a.txt')")
        self.assertFalse(is_safe)
        self.assertIn("open", reason)


if __name__ == "__main__":
    unittest.main()
