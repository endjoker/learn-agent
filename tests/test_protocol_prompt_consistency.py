import unittest
from pathlib import Path

from agent import Agent
from core.system_prompt import SystemPrompt
from skills import SkillTool
from skills.skill import Skill


class ProtocolPromptConsistencyTests(unittest.TestCase):
    def test_composed_prompt_uses_native_tool_protocol(self):
        project_root = Path(__file__).resolve().parent.parent
        builder = SystemPrompt("test-agent")
        builder.set_project_root(str(project_root))
        prompt = builder.build("- read")

        self.assertIn("原生 function calling", prompt)
        self.assertIn("不要把调用内容写进回复文本", prompt)
        self.assertNotIn("agent.turn.v1", prompt)
        self.assertNotIn("JSON 工具调用信封", prompt)

    def test_skill_instruction_uses_native_tool_protocol(self):
        skill = Skill(
            name="review", description="Review code", instruction="Inspect the files.",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        instruction = SkillTool(skill).execute()
        self.assertIn("原生 function calling", instruction)
        self.assertNotIn("agent.turn.v1", instruction)

    def test_native_tool_result_is_a_typed_message(self):
        agent = object.__new__(Agent)
        result = {"role": "tool", "tool_call_id": "call-1", "name": "read", "content": "ok"}
        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "call-1")
