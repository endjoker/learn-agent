# -*- coding: utf-8 -*-
"""Regression tests for None content in tool-call messages.

An assistant tool-call message may legally use content=None. The old
MessageStore token cache called len(None) during persistence; in the
Plan/Goal finally path that exception escaped and left the run/UI in progress.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.message_store import MessageStore, _content_to_text, estimate_tokens


def _tool_calls_message():
    return {
        "role": "assistant",
        "content": None,
        "kind": "tool_calls",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "list_dir", "arguments": "{}"},
            }
        ],
    }


class NoneContentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_to_session_data_accepts_none_content(self):
        store = MessageStore()
        store.append({"role": "user", "content": "看看当前项目结构"})
        store.append(_tool_calls_message())

        data = store.to_session_data()

        assistant = [m for m in data["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertIsNone(assistant[0]["content"])
        self.assertEqual(assistant[0]["tokens"], 0)

    def test_save_and_load_accept_none_content(self):
        store = MessageStore()
        # 本测试验证序列化对 content=None 的兼容性，显式开启文件持久化
        # （2026-08-26 退役后 MessageStore 默认不写 sessions/*.json）。
        store.set_file_persistence(True)
        store.append({"role": "user", "content": "看看当前项目结构"})
        store.append(_tool_calls_message())

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "session.json")
            self.assertEqual(store.save_session(path), path)

        restored = MessageStore()
        restored.load_session_data(store.to_session_data())
        assistant = [m for m in restored.messages if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertIsNone(assistant[0]["content"])

    def test_none_content_helpers_are_empty_text(self):
        self.assertEqual(_content_to_text(None), "")
        self.assertEqual(estimate_tokens(None), 0)

    def test_clear_history_removes_none_content_messages(self):
        from agent import Agent

        store = MessageStore()
        store.append({"role": "user", "content": "old context"})
        store.append(_tool_calls_message())
        fake_agent = SimpleNamespace(store=store)

        with mock.patch("core.message_store.DEFAULT_SESSION_DIR",
                        Path(self._tmp.name)):
            Agent.clear_history(fake_agent)

        self.assertEqual(store.messages, [])
        self.assertEqual(store.events[-1]["type"], "history_cleared")


    def test_clear_history_skips_file_when_persistence_disabled(self):
        # 2026-08-26 退役后新契约：/clear 不再写任何会话文件（SQLite 统一
        # 会话是唯一权威，重建走 SQLite 回放；清空后回放自然为空，不存在
        # "旧上下文复活"）。file_persistence=False 时 save_session 直接返回。
        from agent import Agent

        store = MessageStore()
        store.set_file_persistence(False)
        store.append({"role": "user", "content": "old context"})
        store.append(_tool_calls_message())
        fake_agent = SimpleNamespace(store=store)

        target = Path(self._tmp.name) / (store.session_id + ".json")
        with mock.patch("core.message_store.DEFAULT_SESSION_DIR", Path(self._tmp.name)):
            Agent.clear_history(fake_agent)

        self.assertFalse(target.exists())
        self.assertEqual(store.messages, [])


class ClearCachedAgentContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_clears_resident_agent(self):
        from gateway.webui.glue import clear_cached_agent_context

        cleared = []

        class StubAgent:
            def clear_history(self):
                cleared.append(True)

        entry = SimpleNamespace(agent=StubAgent())

        class StubMgr:
            def get_or_create(self, key):
                self.key = key
                return entry

            def get_executor(self):
                return None

        module = SimpleNamespace(session_mgr=StubMgr())
        await clear_cached_agent_context(module, "webui:main")

        self.assertEqual(cleared, [True])
        self.assertEqual(module.session_mgr.key, "webui:main")

    async def test_without_agent_is_noop(self):
        from gateway.webui.glue import clear_cached_agent_context

        class StubMgr:
            def get_or_create(self, key):
                return SimpleNamespace(agent=None)

        module = SimpleNamespace(session_mgr=StubMgr())
        await clear_cached_agent_context(module, "webui:main")  # 不应抛错

    async def test_without_session_mgr_is_noop(self):
        from gateway.webui.glue import clear_cached_agent_context

        await clear_cached_agent_context(SimpleNamespace(), "webui:main")  # 不应抛错


class RuntimeActivityPersistenceTests(unittest.TestCase):
    def test_persistence_failure_in_finally_does_not_escape(self):
        from gateway.dispatcher import Dispatcher

        class FailingStore:
            def save_session(self):
                raise TypeError("object of type NoneType has no len()")

        class FakeAgent:
            def __init__(self):
                self.store = FailingStore()
                self.messages = [
                    {"role": "user", "content": "step prompt"},
                    _tool_calls_message(),
                    {"role": "tool", "tool_call_id": "call_1",
                     "name": "list_dir", "content": "file.txt",
                     "kind": "tool_result", "is_error": False},
                ]

        agent = FakeAgent()
        Dispatcher._retain_runtime_activity(
            agent, [], "plan", {"plan_id": "p1", "plan_task_id": "t1"}
        )

        retained = [m for m in agent.messages if m.get("runtime") == "plan"]
        self.assertEqual(len(retained), 2)
        self.assertTrue(any(m.get("content") is None for m in retained))


if __name__ == "__main__":
    unittest.main()
