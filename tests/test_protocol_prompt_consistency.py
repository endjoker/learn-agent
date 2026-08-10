import unittest
from pathlib import Path

from agent import Agent
from core.system_prompt import SystemPrompt


class ProtocolPromptConsistencyTests(unittest.TestCase):
    def test_composed_prompt_has_one_json_only_response_protocol(self):
        project_root = Path(__file__).resolve().parent.parent
        builder = SystemPrompt("test-agent")
        builder.set_project_root(str(project_root))
        prompt = builder.build("- read")

        self.assertIn("每一次回复都只能是一个完整 JSON 对象", prompt)
        self.assertIn('"version":"agent.turn.v1","type":"tool_calls"', prompt)
        self.assertIn('"version":"agent.turn.v1","type":"final"', prompt)
        self.assertNotIn("可以一次输出多个 ACTION", prompt)
        self.assertNotIn("复杂任务先说明思路再执行", prompt)

    def test_tool_result_directs_the_same_json_protocol(self):
        result = Agent._format_tool_result("read", '{"file_path":"a.txt"}', "ok")

        self.assertIn("agent.turn.v1/final JSON 信封", result)
        self.assertIn("agent.turn.v1/tool_calls JSON 信封", result)
        self.assertNotIn("FINAL_ANSWER", result)
        self.assertNotIn("ACTION + INPUT", result)
