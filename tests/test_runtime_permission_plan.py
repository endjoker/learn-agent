import tempfile
import unittest
from pathlib import Path

from core.policy_engine import ASK, ALLOW, PolicyEngine
from core.sandbox.executor import SandboxExecutor
from core.sandbox.guard import check_command_safety, check_write_content, apply_guard_config


class RuntimePermissionAndPlanTests(unittest.TestCase):
    def test_unreviewed_allows_policy_sensitive_project_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = PolicyEngine(project_root=tmp, working_directory=tmp, mode="unreviewed")
            self.assertEqual(engine.decide("bash", {"command": f"git diff {tmp}/agent.py"}).level, ALLOW)
            self.assertEqual(engine.decide("write", {"file_path": f"{tmp}/agent.py"}).level, ALLOW)
            sandbox = SandboxExecutor(workspace=tmp)
            sandbox.set_unreviewed_mode(True)
            ok, _ = check_command_safety(f"git diff {tmp}/agent.py", check_policy_paths=False)
            self.assertTrue(ok)

    def test_unreviewed_keeps_hard_content_checks(self):
        self.assertFalse(check_command_safety("rm -rf /", check_policy_paths=False)[0])
        self.assertTrue(check_write_content("agent.py", "os.system('rm -rf /')", check_policy_paths=False)[0])


if __name__ == "__main__":
    unittest.main()
