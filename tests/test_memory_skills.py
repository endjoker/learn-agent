import tempfile
import unittest
from pathlib import Path

from memory.manager import MemoryManager
from skills.manager import SkillManager


class MemoryAndSkillTests(unittest.TestCase):
    def test_memory_merges_session_and_filters_tool_protocol_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryManager(directory)
            messages = [
                {"role": "system", "content": "hidden"},
                {"role": "user", "content": "Please remember project alpha"},
                {"role": "assistant", "content": "ACTION: read\nINPUT: {}\nFINAL_ANSWER: Alpha is ready"},
                {"role": "user", "name": "tool_result", "content": "hidden result"},
            ]
            first_id = memory.save_conversation("project alpha", messages, "session-a")
            second_id = memory.save_conversation("add deployment note", messages, "session-a")
            results = memory.search("alpha", limit=5)

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(results), 1)
        self.assertIn("deployment", results[0]["user_call"])
        self.assertIn("FINAL_ANSWER", results[0]["summary"])
        self.assertNotIn("ACTION:", results[0]["summary"])
        self.assertNotIn("hidden result", results[0]["summary"])

    def test_skill_lifecycle_persists_instruction_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SkillManager(directory)
            skill = manager.create_skill(
                "release-check", "Validate a release", "Run tests.",
                parameters={"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"]},
                tags=["release"],
            )
            changed = manager.update_skill("release-check", instruction="Run tests and publish notes.")
            reloaded = SkillManager(directory)
            loaded = reloaded.load_all()

        self.assertEqual(skill.name, "release-check")
        self.assertEqual(changed.version, 2)
        self.assertEqual([item.name for item in loaded], ["release-check"])
        self.assertEqual(loaded[0].instruction, "Run tests and publish notes.\n")
        self.assertIn("tag (string)", reloaded.get_skill_descriptions())

    def test_skill_rejects_path_traversal_name(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SkillManager(directory)
            with self.assertRaises(ValueError):
                manager.create_skill("../escape", "x", "x")
