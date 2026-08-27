import tempfile
import unittest
from pathlib import Path

from core.hook import Decision, HookResult
from core.permission import ASK, DENY, PermissionChecker
from core.policy_engine import PolicyEngine
from core.runtime.tool_runtime import ToolRuntime
from core.sandbox.executor import SandboxExecutor
from core.sandbox.guard import check_command_safety


class PermissionOptimizationTests(unittest.TestCase):
    def test_policy_modes_have_stable_core_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = PolicyEngine(project_root=tmp, working_directory=tmp)
            cases = {
                "readonly": (DENY, DENY),
                "ask": (ASK, ASK),
                "allow": ("allow", "allow"),
                "unreviewed": ("allow", "allow"),
            }
            for mode, expected in cases.items():
                engine.set_mode(mode)
                self.assertEqual(engine.decide("write", {"file_path": "x"}).level, expected[0])
                self.assertEqual(engine.decide("bash", {"command": "echo ok"}).level, expected[1])
                self.assertEqual(engine.decide("read", {"file_path": "x"}).level, "allow")
            engine.set_mode("allow")
            self.assertEqual(engine.decide("write", {"file_path": "/tmp/outside"}).level, ASK)

    def test_command_hard_checks_can_delegate_policy_paths(self):
        self.assertFalse(check_command_safety("cat /etc/passwd")[0])
        self.assertFalse(check_command_safety(
            "cat /etc/passwd", check_policy_paths=False)[0])
        self.assertFalse(check_command_safety(
            "rm -rf /", check_policy_paths=False)[0])

    def test_sandbox_l2_gate_defaults_off_but_still_blocks_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = SandboxExecutor(workspace=tmp)
            self.assertFalse(sandbox.enabled, "L2 沙箱硬闸门默认关闭")
            # 默认关闭：L2-A 检查跳过，命令直接执行
            result = sandbox.run("echo", ["l2-off-ok"], tool_name="bash")
            self.assertFalse(result.blocked)
            self.assertEqual(result.exit_code, 0)
            # 显式开启后：unreviewed 仍不绕过 L2 硬检查（危险命令照拦）
            sandbox.enabled = True
            sandbox.set_unreviewed_mode(True)
            self.assertTrue(sandbox._unreviewed_mode)
            result = sandbox.run("rm", ["-rf", "/"], tool_name="bash")
            self.assertTrue(result.blocked)

    def test_noninteractive_ask_without_bridge_is_fail_closed(self):
        from agent import Agent
        agent = object.__new__(Agent)
        agent.non_interactive = True
        agent.ask_callback = None
        self.assertEqual(agent._ask_user("write", {"file_path": "x"}), "n")

    def test_pre_hook_in_place_mutation_is_ignored(self):
        class Tool:
            def execute(self, **kwargs):
                return kwargs["value"]

        class Registry:
            def get_tool(self, name):
                return Tool()
            def list_tools(self):
                return []
            def validate_arguments(self, name, arguments):
                return []

        class Hooks:
            def run_pre_tool(self, name, arguments, gate_level="allow"):
                arguments["value"] = "mutated"
                return HookResult(Decision.CONTINUE)
            def run_post_tool(self, *args, **kwargs):
                return HookResult(Decision.CONTINUE)
            def run_denied(self, *args, **kwargs):
                return None

        class Agent:
            tool_registry = Registry()
            hooks = Hooks()
            def _gate_check(self, name, arguments):
                return "allow", ""

        result, is_error = ToolRuntime().execute_authorized(
            Agent(), "demo", {"value": "original"})
        self.assertEqual(result, "original")
        self.assertFalse(is_error)

    def test_pre_hook_explicit_modify_is_rechecked(self):
        class Registry:
            def validate_arguments(self, name, arguments):
                return []

        class Hooks:
            def run_pre_tool(self, name, arguments, gate_level="allow"):
                return HookResult(Decision.MODIFY, data={"value": "blocked"})
            def run_denied(self, *args, **kwargs):
                return None

        class Agent:
            tool_registry = Registry()
            hooks = Hooks()
            def _gate_check(self, name, arguments):
                return DENY, "final payload denied"

        result, is_error = ToolRuntime().execute_authorized(
            Agent(), "demo", {"value": "original"})
        self.assertIn("final payload denied", result)
        self.assertTrue(is_error)


    def test_clear_history_skips_file_persistence(self):
        # 2026-08-26 退役后新契约：/clear 不再写会话文件（SQLite 统一会话
        # 为唯一权威，重建走 SQLite 回放）。仅验证内存清空 + record_event。
        from agent import Agent
        from core.message_store import MessageStore
        with tempfile.TemporaryDirectory() as tmp:
            agent = object.__new__(Agent)
            agent.store = MessageStore(session_id="clear_test")
            agent.store._messages.extend([
                {"role": "system", "content": "system"},
                {"role": "user", "content": "old"},
            ])
            agent._memory_clear_count = 0
            agent.clear_history()
            self.assertEqual(agent.store.messages, [])
            self.assertFalse(
                (Path(tmp) / "clear_test.json").exists())


if __name__ == "__main__":
    unittest.main()
