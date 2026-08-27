# -*- coding: utf-8 -*-
"""P3 安全修复回归测试：CommandHook 元字符感知执行（shell=True 收敛）。

两个分支：
  - 无 shell 元字符 → shlex.split 后列表执行（stdin JSON 仍可达子进程）
  - 含 shell 操作符（| & ; < > $ ` ( ) 换行）→ 保留 shell=True
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from core.hook.events import Decision, HookContext, HookEvent
from core.hook.hooks import CommandHook, _contains_shell_metachars


def _ctx():
    return HookContext(event=HookEvent.PRE_TOOL, agent_name="t",
                       session_id="s",
                       payload={"tool_name": "bash", "params": {}})


class MetacharClassificationTests(unittest.TestCase):
    def test_shell_operators_detected(self):
        for cmd in ("a | b", "a & b", "a;b", "a < in", "a > out",
                    "$HOME/bin/tool", "`cmd`", "(sub)", "line1\nline2",
                    "echo $(date)"):
            self.assertTrue(_contains_shell_metachars(cmd), repr(cmd))

    def test_plain_commands_not_flagged(self):
        for cmd in ("/usr/bin/env python3 script.py --flag value",
                    "./tools/run.sh --x=1 -y 2",
                    'python3 "/tmp/some dir/s.py" --name "含 空格"',
                    "git status --porcelain"):
            self.assertFalse(_contains_shell_metachars(cmd), repr(cmd))


class CommandHookExecTests(unittest.TestCase):
    def test_plain_command_runs_as_argv_list(self):
        """无元字符：argv 列表执行，且 stdin 的 HookContext JSON 可读。"""
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "probe.py"
            script.write_text(
                "import sys, json\n"
                "ctx = json.load(sys.stdin)\n"
                "print(json.dumps({'decision': 'block',\n"
                "                  'reason': 'argv:' + ctx['payload']['tool_name']}))\n",
                encoding="utf-8")
            command = f"{sys.executable} \"{script}\""
            self.assertFalse(_contains_shell_metachars(command))
            hook = CommandHook(command, timeout=30)
            result = hook.run(_ctx())
            self.assertEqual(result.decision, Decision.BLOCK)
            self.assertEqual(result.reason, "argv:bash")

    def test_pipeline_keeps_shell_semantics(self):
        """含管道符：保留 shell=True，既有 hooks 配置行为不变。"""
        command = (
            "printf '%s' '{\"decision\":\"block\",\"reason\":\"shell-ok\"}' | cat")
        self.assertTrue(_contains_shell_metachars(command))
        hook = CommandHook(command, timeout=30)
        result = hook.run(_ctx())
        self.assertEqual(result.decision, Decision.BLOCK)
        self.assertEqual(result.reason, "shell-ok")

    def test_variable_expansion_still_shell(self):
        command = 'sh -c "exit 0" && echo done'
        self.assertTrue(_contains_shell_metachars(command))
        result = CommandHook(command, timeout=30).run(_ctx())
        # exit 0 && echo done → stdout 非 JSON → CONTINUE（与旧行为一致）
        self.assertEqual(result.decision, Decision.CONTINUE)

    def test_unbalanced_quote_falls_back_to_shell_without_crash(self):
        """shlex 解析失败回退 shell=True：不崩溃、行为退化为旧路径。"""
        hook = CommandHook("echo 'unbalanced", timeout=30)
        result = hook.run(_ctx())
        self.assertIsInstance(result.decision, Decision)

    def test_exit_code_two_blocks_with_stderr_reason(self):
        """协议回归：exit code 2 → BLOCK(stderr)。"""
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "deny.py"
            script.write_text(
                "import sys\n"
                "sys.stdin.read()\n"
                "print('denied by policy', file=sys.stderr)\n"
                "sys.exit(2)\n", encoding="utf-8")
            hook = CommandHook(f"{sys.executable} \"{script}\"", timeout=30)
            result = hook.run(_ctx())
            self.assertEqual(result.decision, Decision.BLOCK)
            self.assertIn("denied by policy", result.reason)


if __name__ == "__main__":
    unittest.main()
