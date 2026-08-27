# -*- coding: utf-8 -*-
"""PolicyEngine 四档权限语义单测（readonly / ask / allow / unreviewed）。

项目整体权限完全由 PolicyEngine 四档驱动；本文件只验证四档对"系统路径"与
"工作区外路径"的处理，不引入额外的敏感区判定层。
"""
import tempfile
import unittest
from pathlib import Path

from core.policy_engine import ASK, DENY, PolicyEngine


def _make_tree(root: Path) -> None:
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text("{}")
    (root / "agent.py").write_text("# code")
    (root / "src").mkdir(parents=True, exist_ok=True)


class PolicyFourTierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _make_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _engine(self, mode="ask"):
        return PolicyEngine(project_root=self.root, working_directory=self.root, mode=mode)

    def _rp(self, rel):
        return str(self.root / rel)

    # ---- 四档核心语义 ----

    def test_readonly_denies_mutation_and_exec(self):
        e = self._engine("readonly")
        self.assertEqual(e.decide("write", {"file_path": self._rp("agent.py"), "content": "x"}).level, DENY)
        self.assertEqual(e.decide("bash", {"command": "echo hi"}).level, DENY)
        self.assertEqual(e.decide("read", {"file_path": self._rp("agent.py")}).level, "allow")

    def test_ask_asks_on_mutation_and_exec(self):
        e = self._engine("ask")
        self.assertEqual(e.decide("write", {"file_path": self._rp("agent.py"), "content": "x"}).level, ASK)
        self.assertEqual(e.decide("bash", {"command": "echo hi"}).level, ASK)
        # 读不确认
        self.assertEqual(e.decide("read", {"file_path": self._rp("agent.py")}).level, "allow")

    def test_allow_auto_allows_in_workspace_writes(self):
        e = self._engine("allow")
        self.assertEqual(e.decide("write", {"file_path": self._rp("agent.py"), "content": "x"}).level, "allow")
        self.assertEqual(e.decide("write", {"file_path": self._rp("src/main.py"), "content": "x"}).level, "allow")
        self.assertEqual(e.decide("read", {"file_path": self._rp("config.json")}).level, "allow")

    def test_unreviewed_allows_everything_including_system(self):
        e = self._engine("unreviewed")
        self.assertEqual(e.decide("write", {"file_path": self._rp("agent.py"), "content": "x"}).level, "allow")
        self.assertEqual(e.decide("write", {"file_path": "/etc/foo", "content": "x"}).level, "allow")

    # ---- 系统路径：非 unreviewed 需确认 ----

    def test_system_path_asks_in_allow_for_read_and_write(self):
        e = self._engine("allow")
        self.assertEqual(e.decide("read", {"file_path": "/etc/hosts"}).level, ASK)
        self.assertEqual(e.decide("write", {"file_path": "/etc/foo", "content": "x"}).level, ASK)

    def test_system_path_shell_asks_in_allow(self):
        e = self._engine("allow")
        self.assertEqual(e.decide("bash", {"command": "cat /etc/passwd"}).level, ASK)

    def test_dev_redirection_is_not_treated_as_outside_path(self):
        # `cmd 2>/dev/null` 的 /dev/null 是重定向/设备目标，不应判为工作区外 → allow 模式不应弹确认。
        e = self._engine("allow")
        self.assertEqual(e.decide("bash", {"command": "echo hi && find . -name '*.py' 2>/dev/null"}).level, "allow")
        self.assertEqual(e.decide("bash", {"command": "cd /tmp && ls 2>&1 | head"}).level, ASK)
        # 真实的系统路径仍确认、工作区外仍确认（不受此修复影响）
        self.assertEqual(e.decide("bash", {"command": "cat /etc/hosts"}).level, ASK)

    # ---- 工作区外：allow 模式需确认 ----

    def test_outside_workspace_write_asks_in_allow(self):
        e = self._engine("allow")
        self.assertEqual(e.decide("write", {"file_path": "/tmp/outside", "content": "x"}).level, ASK)

    def test_read_workspace_file_allowed_in_ask_but_system_asks(self):
        # 读取：工作区文件始终允许；系统路径在非 unreviewed 下需确认。
        e = self._engine("ask")
        self.assertEqual(e.decide("read", {"file_path": self._rp("agent.py")}).level, "allow")
        self.assertEqual(e.decide("read", {"file_path": "/etc/hosts"}).level, ASK)

    # ---- 沙箱工作区边界（L2）与 allowed_roots 一致性 ----

    def test_sandbox_writes_in_extra_root_allowed(self):
        from core.sandbox.executor import SandboxExecutor
        with tempfile.TemporaryDirectory() as a_, tempfile.TemporaryDirectory() as b_:
            a, b = Path(a_), Path(b_)
            (b / "file.txt").write_text("x")
            sb = SandboxExecutor(workspace=str(a), extra_workspace_roots=[str(b)])
            sb.enabled = True
            ok, reason = sb.check_write_file(str(b / "file.txt"), "new", check_policy_paths=True)
            self.assertTrue(ok, reason)

    def test_sandbox_writes_outside_all_roots_denied(self):
        from core.sandbox.executor import SandboxExecutor
        with tempfile.TemporaryDirectory() as a_, tempfile.TemporaryDirectory() as b_:
            a, b = Path(a_), Path(b_)
            (a / "file.txt").write_text("x")
            sb = SandboxExecutor(workspace=str(a), extra_workspace_roots=[str(b)])
            sb.enabled = True
            ok, reason = sb.check_write_file("/tmp/foo-outside", "new", check_policy_paths=True)
            self.assertFalse(ok, reason)
            self.assertIn("工作区", reason)

    def test_sandbox_checks_defer_when_disabled(self):
        """沙箱未启用时，内置工具的自我判定不拦截（整体遵循四档）。"""
        from core.sandbox.executor import SandboxExecutor
        with tempfile.TemporaryDirectory() as ws:
            sb = SandboxExecutor(workspace=str(ws))
            sb.enabled = False
            # 即便目标是系统路径 / 工作区外，未启用时不拦截
            self.assertEqual(sb.check_write_file("/etc/foo", "x", check_policy_paths=True), (True, ""))
            self.assertEqual(sb.check_write_file("/tmp/out", "x", check_policy_paths=True), (True, ""))
            self.assertEqual(sb.check_python("import socket"), (True, ""))
            self.assertEqual(sb.check_egress("https://example.com"), (True, ""))
            # 启用后才拦截（用配置禁用的 import socket 作为可拦截用例）
            sb.enabled = True
            ok, _ = sb.check_write_file("/etc/foo", "x", check_policy_paths=True)
            self.assertFalse(ok)
            ok2, _ = sb.check_python("import socket")
            self.assertFalse(ok2)


if __name__ == "__main__":
    unittest.main()
