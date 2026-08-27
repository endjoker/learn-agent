# -*- coding: utf-8 -*-
"""会话模板刷新测试：Plan/Goal 与普通轮次必须使用当前会话最新模板。

场景：会话绑定 AgentProfile（模板 = profile.system_prompt）。用户编辑该 profile 的
system_prompt（而非切换 profile）时不会驱逐缓存 Agent，导致新创建的 Plan/Goal 仍使用
"会话初始模板"。本测试验证 Dispatcher._refresh_session_template 会把缓存 Agent 的模板
对齐到 entry.runtime_profile_prompt 的最新值。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.system_prompt import SystemPrompt
from gateway.dispatcher import Dispatcher


class _FakeAgent:
    """轻量 Agent，仅提供 Dispatcher._refresh_session_template 依赖的成员。"""

    def __init__(self, builder: SystemPrompt):
        self.system_prompt_builder = builder
        self.system_prompt = builder.build(tool_descs="", skill_descs="", mcp_descs="")
        self.rebuild_calls = 0

    def _rebuild_system_prompt(self) -> None:
        self.rebuild_calls += 1
        self.system_prompt = self.system_prompt_builder.build(
            tool_descs="", skill_descs="", mcp_descs="")


def _entry(session_key: str = "webui:default", profile_prompt=None) -> SimpleNamespace:
    return SimpleNamespace(session_key=session_key, runtime_profile_prompt=profile_prompt)


def _agent_with(profile_prompt: str | None) -> _FakeAgent:
    builder = SystemPrompt(name="JKagent")
    builder.set_agent_profile_prompt(profile_prompt)
    return _FakeAgent(builder)


class SessionTemplateRefreshTests(unittest.TestCase):
    def test_refresh_updates_template_when_profile_edited(self):
        # Agent 创建时使用初始模板（session 初始 profile）。
        agent = _agent_with("【初始模板】你是一个旧版助手")
        entry = _entry(profile_prompt="【最新模板】你现在是专业文档助手")

        Dispatcher._refresh_session_template(entry, agent)

        self.assertEqual(agent.system_prompt_builder._agent_profile_prompt, "【最新模板】你现在是专业文档助手")
        self.assertEqual(agent.rebuild_calls, 1)
        self.assertIn("【最新模板】你现在是专业文档助手", agent.system_prompt)
        self.assertNotIn("【初始模板】你是一个旧版助手", agent.system_prompt)

    def test_no_rebuild_when_template_unchanged(self):
        agent = _agent_with("【初始模板】你是一个旧版助手")
        entry = _entry(profile_prompt="【初始模板】你是一个旧版助手")

        Dispatcher._refresh_session_template(entry, agent)

        self.assertEqual(agent.rebuild_calls, 0)
        self.assertEqual(agent.system_prompt_builder._agent_profile_prompt, "【初始模板】你是一个旧版助手")

    def test_detach_profile_prompt_when_target_none(self):
        agent = _agent_with("【初始模板】你是一个旧版助手")
        entry = _entry(profile_prompt=None)

        Dispatcher._refresh_session_template(entry, agent)

        self.assertIsNone(agent.system_prompt_builder._agent_profile_prompt)
        self.assertEqual(agent.rebuild_calls, 1)
        self.assertNotIn("【初始模板】你是一个旧版助手", agent.system_prompt)

    def test_non_profile_session_is_noop(self):
        # 非 profile 会话：entry.runtime_profile_prompt 与 Agent 都无 profile → 无操作。
        agent = _agent_with(None)
        entry = _entry(profile_prompt=None)

        Dispatcher._refresh_session_template(entry, agent)

        self.assertEqual(agent.rebuild_calls, 0)


if __name__ == "__main__":
    unittest.main()
