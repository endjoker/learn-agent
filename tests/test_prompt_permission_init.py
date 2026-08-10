import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.init_wizard import _step_agent_runtime
from core.permission import ALLOW, ASK, DENY, PermissionChecker, classify_bash_command
from core.system_prompt import SystemPrompt


class PromptPermissionAndInitTests(unittest.TestCase):
    def test_prompt_contains_json_protocol_and_session_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_dir = root / "prompt"
            prompt_dir.mkdir()
            (prompt_dir / "SOUL.md").write_text("You are {name}.", encoding="utf-8")
            (prompt_dir / "TOOLS.md").write_text("Use tools carefully.", encoding="utf-8")
            (prompt_dir / "AGENT.md").write_text("Project rules.", encoding="utf-8")
            (prompt_dir / "MEMORY.md").write_text("Remember decisions.", encoding="utf-8")
            builder = SystemPrompt("Test Agent")
            builder.set_project_root(str(root))
            builder.set_workspace(str(root / "workspace"))
            builder.add_session_instruction("Reply with test evidence.")
            prompt = builder.build("- read", "- review", "- github")

        self.assertIn("<SYSTEM_STATIC_CONTEXT>", prompt)
        self.assertIn("<SYSTEM_DYNAMIC_CONTEXT>", prompt)
        self.assertIn('"version":"agent.turn.v1"', prompt)
        self.assertIn("Test Agent", prompt)
        self.assertIn("Reply with test evidence.", prompt)
        self.assertIn("- github", prompt)

    def test_permission_denies_dangerous_and_requires_confirmation_for_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            checker = PermissionChecker(workspace=str(workspace), config={"extra_workspaces": []})
            self.assertEqual(checker.check("read", {"file_path": "note.txt"}), ALLOW)
            self.assertEqual(checker.check("write", {"file_path": "note.txt"}), ASK)
            self.assertEqual(checker.check("bash", {"command": "rm -rf /"}), DENY)
            self.assertEqual(classify_bash_command("git status"), ALLOW)
            self.assertEqual(classify_bash_command("git commit -m test"), ASK)
            self.assertEqual(classify_bash_command("git status && git commit -m test"), ASK)

    def test_runtime_wizard_only_writes_changed_protocol_values(self):
        existing = {"agent_runtime": {
            "response_protocol": "auto", "legacy_execute": False, "protocol_retry_limit": 1,
        }}
        with patch("core.init_wizard._ask_choice", return_value="json_envelope"), \
             patch("core.init_wizard._ask_yes_no", return_value=True), \
             patch("core.init_wizard._ask_int", return_value=3), \
             patch("core.init_wizard.print"):
            changes = _step_agent_runtime(existing)
        self.assertEqual(changes, {"agent_runtime": {
            "response_protocol": "json_envelope", "legacy_execute": True, "protocol_retry_limit": 3,
        }})
