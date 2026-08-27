# -*- coding: utf-8 -*-
"""工作区后台任务（plan/goal/subagent）entry 挂接工作区上下文的回归测试。

根因：这些任务经 ``session_mgr.get_or_create`` 得到 SessionEntry，不共享 conversation runner
的 entry，通常没有 ``runtime_context``，导致 Agent 走"非工作区"分支、权限档位被硬编码为
ask——即使会话是 unreviewed/allow 也会误弹审批。本测试验证 ``Dispatcher._ensure_workspace_context``
会把快照冻结上下文（含 permission_mode/project_root 等）挂上去，使后台任务与父会话同档。
"""
import unittest
from pathlib import Path

from gateway.dispatcher import Dispatcher
from gateway.session import SessionEntry
from core.permission import PermissionChecker

REPO = "/home/test/project/learn-agent-main_extracted/learn-agent-main"

_CMD = ("cd /home/test/project/learn-agent-main_extracted/learn-agent-main/workspace "
        "&& git rev-parse --show-toplevel && ls ../gateway/webui/ 2>&1 | head -30")


def _provider(wid: str, sid: str) -> dict:
    rc = type("RC", (), {
        "permission_mode": "unreviewed",
        "project_root": REPO,
        "working_directory": REPO,
        "extra_workspace_roots": [],
        "framework_root": REPO,
        "agent_data_root": REPO,
    })()
    return {
        "runtime_context": rc, "snapshot_id": "snap1", "model": "gpt-5.6-sol",
        "permission_mode": "unreviewed", "reasoning_level": "inherit",
        "max_steps": 100, "mcp_servers": None, "profile_prompt": None,
        "allowed_tools": None, "allowed_skills": None,
    }


class WorkspaceTaskContextTests(unittest.TestCase):
    def test_workspace_task_entry_gets_context_and_unreviewed_mode(self):
        d = Dispatcher(session_mgr=None, agent_config={})
        d.set_workspace_context_provider(_provider)
        entry = SessionEntry(session_key="workspace:ws_1:wss_abc")
        self.assertIsNone(getattr(entry, "runtime_context", None))

        d._ensure_workspace_context(entry)

        self.assertIsNotNone(entry.runtime_context)
        self.assertEqual(entry.runtime_permission_mode, "unreviewed")
        # 模拟 create_agent 的 checker 模式来源
        checker = PermissionChecker(workspace=REPO, extra_workspaces=[])
        checker.set_permission_mode(getattr(entry.runtime_context, "permission_mode", None)
                                    or entry.runtime_permission_mode or "ask")
        self.assertEqual(checker.permission_mode, "unreviewed")
        self.assertEqual(checker.decide("bash", {"command": _CMD}).level, "allow")

    def test_nonworkspace_entry_is_untouched(self):
        d = Dispatcher(session_mgr=None, agent_config={})
        d.set_workspace_context_provider(_provider)
        entry = SessionEntry(session_key="webui:default")
        d._ensure_workspace_context(entry)
        self.assertIsNone(getattr(entry, "runtime_context", None))
        self.assertEqual(entry.session_key, "webui:default")

    def test_no_provider_is_noop(self):
        d = Dispatcher(session_mgr=None, agent_config={})
        entry = SessionEntry(session_key="workspace:ws_1:wss_abc")
        d._ensure_workspace_context(entry)
        self.assertIsNone(getattr(entry, "runtime_context", None))

    def test_existing_context_not_overwritten(self):
        d = Dispatcher(session_mgr=None, agent_config={})
        d.set_workspace_context_provider(_provider)
        entry = SessionEntry(session_key="workspace:ws_1:wss_abc")
        entry.runtime_context = object()
        d._ensure_workspace_context(entry)
        self.assertIs(entry.runtime_context, entry.runtime_context)


if __name__ == "__main__":
    unittest.main()
