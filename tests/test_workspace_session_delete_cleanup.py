# -*- coding: utf-8 -*-
"""工作区会话删除必须级联清理统一会话数据（回归测试）。

背景：api_workspace._make_delete 原先只 archive workspace_sessions 行并驱逐
内存 Agent，conversation_sessions / turns / turn_nodes / tool_results 以及该
会话派生的 subagent 子会话（键 ``subagent:workspace:{wid}:{sid}:{child_id}``）
全部永久残留。修复后：归档成功 → 先删子代再删主会话，单个删除失败仅告警不
让 DELETE 整体失败；响应带回 removed_conversations 统计；is_busy 409 早退
路径保持不变。

fixture 搭建与 tests/test_conversation_core.py 一致：
temp RuntimeStore + WorkspaceDatabase + ConversationStore + ConversationService，
另加真实的 WorkspaceStore / WorkspaceSessionStore 与最小化 SessionManager 替身。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.runtime import RuntimeStore
from gateway.conversation import ConversationService, ConversationStore
from gateway.webui.api_workspace import _make_delete
from gateway.webui.workspace_models import Workspace
from gateway.webui.workspace_store import (
    WorkspaceDatabase,
    WorkspaceSessionStore,
    WorkspaceStore,
)


class _FakeRequest:
    """_make_delete 只读 match_info，直接以桩对象调用 handler。"""

    def __init__(self, workspace_id: str, session_id: str):
        self.match_info = {"workspace_id": workspace_id,
                           "session_id": session_id}


class FakeSessionManager:
    """仅覆盖 _get_entry / evict 所需的最小表面。"""

    def __init__(self):
        self._sessions = {}
        self.evict_calls = []

    def add_entry(self, session_key):
        entry = SimpleNamespace(session_key=session_key, agent=None)
        self._sessions[session_key] = entry
        return entry

    async def evict(self, session_key, save=True):
        self.evict_calls.append((session_key, save))
        return self._sessions.pop(session_key, None)


class WorkspaceSessionDeleteCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = RuntimeStore(Path(self.tmp.name) / "runtime.db")
        self.db = WorkspaceDatabase(runtime_store=self.runtime)
        self.store = ConversationStore(self.db)
        self.events = []
        self.service = ConversationService(
            self.store, lambda t, p: self.events.append((t, p)),
            max_turns_per_scope=50, max_global_turns=100)
        self.workspace = Workspace("ws_del", "delete-cleanup", self.tmp.name)
        WorkspaceStore(self.db).create(self.workspace)
        self.session_store = WorkspaceSessionStore(self.db)
        self.session = self.session_store.create(self.workspace.workspace_id, {})
        self.wid = self.workspace.workspace_id
        self.sid = self.session.session_id
        self.main_key = f"workspace:{self.wid}:{self.sid}"

    async def asyncTearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------

    def _module(self, *, with_service=True):
        module = SimpleNamespace(
            session_store=self.session_store,
            workspace_store=WorkspaceStore(self.db),
            session_mgr=FakeSessionManager(),
        )
        if with_service:
            module.conversation_service = self.service
        return module

    def _request(self):
        return _FakeRequest(self.wid, self.sid)

    def _seed(self, session_key, origin="webui", subtype="workspace"):
        """建一条带 turn + node + tool_result 的统一会话记录。"""
        conv = self.service.get_or_create_conversation(
            session_key, origin=origin, subtype=subtype,
            workspace_id=self.wid)
        turn = self.service.start_turn(conv.conversation_id)
        self.service.upsert_node_delta(
            conv.conversation_id, turn.turn_id, "assistant",
            f"reply of {session_key}")
        self.service.save_tool_result(
            conv.conversation_id, turn.turn_id,
            result_ref=f"ref:{session_key}", kind="text")
        return conv

    def _counts(self, conversation_id):
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM conversation_sessions"
                "  WHERE conversation_id=?) AS conversations,"
                "(SELECT COUNT(*) FROM turns"
                "  WHERE conversation_id=?) AS turns,"
                "(SELECT COUNT(*) FROM turn_nodes"
                "  WHERE conversation_id=?) AS nodes,"
                "(SELECT COUNT(*) FROM tool_results"
                "  WHERE conversation_id=?) AS tool_results",
                (conversation_id,) * 4).fetchone()
        return {k: row[k] for k in row.keys()}

    # ------------------------------------------------------------
    # 用例
    # ------------------------------------------------------------

    async def test_delete_removes_main_and_subagent_conversation_data(self):
        main_conv = self._seed(self.main_key)
        child_a = self._seed(f"subagent:{self.main_key}:child_a",
                             origin="subagent")
        child_b = self._seed(f"subagent:{self.main_key}:child_b",
                             origin="subagent")
        # 无关会话不得被波及：另一主会话的子代理 + 独立 webui 会话
        other_sub = self._seed(
            f"subagent:workspace:{self.wid}:other_session:child",
            origin="subagent")
        unrelated = self._seed("webui:unrelated")

        module = self._module()
        module.session_mgr.add_entry(self.main_key)

        response = await _make_delete(module)(self._request())

        self.assertEqual(response.status, 200)
        payload = json.loads(response.text)
        self.assertTrue(payload["ok"])
        # 主 + 两个子代都被清理
        self.assertEqual(payload["removed_conversations"], 3)
        self.assertEqual(payload["session"]["status"], "archived")
        self.assertEqual(module.session_mgr.evict_calls,
                         [(self.main_key, True)])

        gone = {"conversations": 0, "turns": 0, "nodes": 0, "tool_results": 0}
        for conv in (main_conv, child_a, child_b):
            self.assertEqual(self._counts(conv.conversation_id), gone,
                             f"{conv.session_key} 应被整体清除")
        for keep in (other_sub, unrelated):
            counts = self._counts(keep.conversation_id)
            self.assertEqual(counts["conversations"], 1)
            self.assertEqual(counts["turns"], 1)
            self.assertGreaterEqual(counts["nodes"], 1)
            self.assertEqual(counts["tool_results"], 1)

    async def test_child_delete_failure_does_not_block_main_flow(self):
        main_conv = self._seed(self.main_key)
        bad_child = self._seed(f"subagent:{self.main_key}:child_boom",
                               origin="subagent")
        good_child = self._seed(f"subagent:{self.main_key}:child_ok",
                                origin="subagent")

        original = self.service.delete_conversation_by_key
        attempted = []

        def flaky(key):
            attempted.append(key)
            if key.endswith("child_boom"):
                raise RuntimeError("boom")
            return original(key)

        self.service.delete_conversation_by_key = flaky
        try:
            module = self._module()
            response = await _make_delete(module)(self._request())
        finally:
            del self.service.delete_conversation_by_key

        # 归档已成功：DELETE 不因子代清理异常而失败
        self.assertEqual(response.status, 200)
        payload = json.loads(response.text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["removed_conversations"], 2)

        # 先删子代、后删主会话；失败的子代被跳过后流程继续
        self.assertEqual(attempted[-1], self.main_key)
        self.assertEqual(set(attempted[:-1]),
                         {bad_child.session_key, good_child.session_key})
        gone = {"conversations": 0, "turns": 0, "nodes": 0, "tool_results": 0}
        self.assertEqual(self._counts(main_conv.conversation_id), gone)
        self.assertEqual(self._counts(good_child.conversation_id), gone)
        # 抛异常的子代保留残留（后续可由 retention 兜底），但主流程未受影响
        self.assertEqual(self._counts(bad_child.conversation_id)["conversations"],
                         1)

    async def test_busy_session_returns_409_without_any_cleanup(self):
        self._seed(self.main_key)
        child = self._seed(f"subagent:{self.main_key}:child_c",
                           origin="subagent")
        self.session_store.set_busy(self.wid, self.sid, True)

        module = self._module()
        module.session_mgr.add_entry(self.main_key)

        response = await _make_delete(module)(self._request())

        self.assertEqual(response.status, 409)
        payload = json.loads(response.text)
        self.assertEqual(payload.get("code"), "WORKSPACE_SESSION_BUSY")
        # 早退路径：不驱逐、不归档、不清理统一会话数据
        self.assertEqual(module.session_mgr.evict_calls, [])
        self.assertIsNotNone(self.store.get_conversation_by_key(self.main_key))
        self.assertIsNotNone(self.store.get_conversation_by_key(
            child.session_key))
        current = self.session_store.get_owned(self.wid, self.sid)
        self.assertEqual(current.status, "active")
        self.assertTrue(current.is_busy)

    async def test_missing_conversation_service_still_archives(self):
        conv = self._seed(self.main_key)
        module = self._module(with_service=False)

        response = await _make_delete(module)(self._request())

        self.assertEqual(response.status, 200)
        payload = json.loads(response.text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["removed_conversations"], 0)
        self.assertEqual(payload["session"]["status"], "archived")
        # 无统一会话服务可用时保持既有归档语义（数据不动）
        self.assertEqual(self._counts(conv.conversation_id)["conversations"], 1)

    def test_prefix_query_is_exact_and_escapes_like_wildcards(self):
        hit = self._seed("subagent:workspace:w1:s1:a", origin="subagent")
        self._seed("subagent:workspace:wX1:s1:b", origin="subagent")
        self._seed("subagent:workspace:w1:s10:c", origin="subagent")
        underscored = self._seed("subagent:workspace:w_1:s1:d", origin="subagent")
        percented = self._seed("subagent:workspace:w%:e", origin="subagent")

        got = self.service.list_conversation_keys_with_prefix(
            "subagent:workspace:w1:s1:")
        # s10 以 s1 开头但不带边界冒号，不属于该前缀
        self.assertEqual(got, [hit.session_key])

        # prefix 含字面量 _ / % 时按字面量匹配，不做 LIKE 通配
        self.assertEqual(
            self.service.list_conversation_keys_with_prefix(
                "subagent:workspace:w_1:s1:"),
            [underscored.session_key])
        self.assertEqual(
            self.service.list_conversation_keys_with_prefix(
                "subagent:workspace:w%:"),
            [percented.session_key])
        self.assertEqual(
            self.service.list_conversation_keys_with_prefix(""), [])


if __name__ == "__main__":
    unittest.main()
