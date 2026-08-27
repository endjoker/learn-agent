# -*- coding: utf-8 -*-
"""system 会话保留窗口（方案A）回归：定时任务/心跳会话超期自动回收。

覆盖：
- origin=system 且超过保留窗口的会话连同 turns 一并删除；
- 窗口内的 system 会话、webui 会话不受影响；
- 历史上被误标为 webui 的 sched:/heartbeat: 键会话按 key 前缀兜底回收；
- cleanup 返回 system_conversations 删除清单。
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.runtime import RuntimeStore
from gateway.conversation import ConversationService, ConversationStore
from gateway.webui.workspace_store import WorkspaceDatabase


def _iso(dt):
    return dt.isoformat(timespec="milliseconds")


class SystemConversationRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = RuntimeStore(Path(self.tmp.name) / "runtime.db")
        self.db = WorkspaceDatabase(runtime_store=self.runtime)
        self.store = ConversationStore(self.db)
        self.service = ConversationService(
            self.store, lambda t, p: None, max_global_turns=100)

    def _backdate(self, conversation_id, days):
        stale = _iso(datetime.now(timezone.utc) - timedelta(days=days))
        with self.store._db.connection() as conn:
            conn.execute(
                "UPDATE conversation_sessions SET updated_at=? "
                "WHERE conversation_id=?", (stale, conversation_id))

    def _turn_count(self, conversation_id):
        with self.store._db.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE conversation_id=?",
                (conversation_id,)).fetchone()
        return int(row["c"] or 0)

    def test_stale_system_conversations_are_recycled(self):
        # 8 天前的 system 会话 → 删；7 天窗口内的 system 会话 → 留
        # 误标为 webui 的 sched:/heartbeat: 旧会话 → 按 key 前缀兜底删
        # 老 webui 会话 → 永不回收
        stale_system = self.service.get_or_create_conversation(
            "sched:nightly:20260801-000000", origin="system", subtype="scheduler")
        fresh_system = self.service.get_or_create_conversation(
            "sched:nightly:20990101-000000", origin="system", subtype="scheduler")
        legacy_sched = self.service.get_or_create_conversation(
            "sched:legacy:20260802-000000",  # 早期 bug：origin 误标为 webui
            origin="webui", subtype="main")
        legacy_heartbeat = self.service.get_or_create_conversation(
            "heartbeat:legacy", origin="webui", subtype="main")
        old_webui = self.service.get_or_create_conversation(
            "webui:main-old", origin="webui", subtype="main")
        self.service.start_turn(stale_system.conversation_id)  # 级联验证数据源

        self._backdate(stale_system.conversation_id, days=8)
        self._backdate(legacy_sched.conversation_id, days=8)
        self._backdate(legacy_heartbeat.conversation_id, days=8)
        self._backdate(old_webui.conversation_id, days=30)

        result = self.service.cleanup(system_retention_days=7)

        deleted = set(result["system_conversations"])
        self.assertIn(stale_system.conversation_id, deleted)
        self.assertIn(legacy_sched.conversation_id, deleted)
        self.assertIn(legacy_heartbeat.conversation_id, deleted)
        self.assertNotIn(fresh_system.conversation_id, deleted)
        self.assertNotIn(old_webui.conversation_id, deleted)
        # 级联：被回收会话的 turns 一并删除
        self.assertEqual(self._turn_count(stale_system.conversation_id), 0)
        # 存活会话数据完好
        self.assertIsNotNone(self.store.get_conversation(
            fresh_system.conversation_id))
        self.assertIsNotNone(self.store.get_conversation(
            old_webui.conversation_id))


if __name__ == "__main__":
    unittest.main()
