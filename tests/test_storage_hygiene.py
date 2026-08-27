# -*- coding: utf-8 -*-
"""存储卫生回归：outbox 死信终态 + 任务域 sessions 表保留回收。

背景（2026-08 批量修复）：
- outbox 此前"连败 3 次即 mark_published"，停机窗口事件被永久吞掉且无法
  与成功发布区分；现以保留认领哨兵 DEAD_LETTER_CLAIMANT 表达独立死信终态。
- runtime.db 任务域 sessions 表此前全仓无 DELETE，行数随 isolated 定时触发
  与 subagent child 单调增长；现由 RuntimeStore.delete_stale_sessions +
  RetentionManager.collect 按 30 天窗口回收孤儿行（无 tasks/artifacts 引用）。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.runtime import ArtifactStore, RetentionManager, RuntimeStore
from gateway.conversation.images import ImageStore
from gateway.conversation.service import ConversationService
from gateway.conversation.outbox import DEAD_LETTER_CLAIMANT, OutboxPublisher
from gateway.conversation.store import ConversationStore


# 1x1 合法 PNG（base64），供图片链路测试使用
_PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
            "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _ago(**kwargs) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kwargs)


class _TempDbBase(unittest.TestCase):
    class _Db:
        """ConversationStore 期望 WorkspaceDatabase 风格的 db 适配器
        （connection()/transaction() 双入口）；测试内联最小实现。"""

        def __init__(self, store):
            self._store = store

        def connection(self):
            return self._store.connection()

        def transaction(self):
            return self._store.connection()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = RuntimeStore(root / "runtime.db")
        self.store.initialize()
        self.cstore = ConversationStore(self._Db(self.store))
        artifact_root = root / "artifacts"
        artifact_root.mkdir(exist_ok=True)
        self.artifacts = ArtifactStore(self.store, str(artifact_root))

    # ---- 造数辅助 -------------------------------------------------------

    def _new_conversation(self, key_hint: str = "") -> str:
        conv, _created = self.cstore.create_conversation(
            f"webui:{key_hint or uuid.uuid4().hex[:8]}",
            origin="webui", subtype="main",
            execution_scope="gateway:default")
        return conv.conversation_id

    def _insert_outbox(self, conversation_id: str, outbox_id: str,
                       event_type: str = "node.delta", age_s: int = 120) -> None:
        """插入一条足够老（越过 _CLAIM_MIN_AGE_SECONDS）的未发布事件。"""
        with self.cstore.transaction() as conn:
            conn.execute(
                "INSERT INTO outbox_events(outbox_id, conversation_id, event_type,"
                " scope, version, payload, created_at)"
                " VALUES (?, ?, ?, 'turn', 1, '{}', ?)",
                (outbox_id, conversation_id, event_type, _iso(_ago(seconds=age_s))))

    def _outbox_row(self, outbox_id: str):
        with self.cstore.transaction() as conn:
            return conn.execute(
                "SELECT * FROM outbox_events WHERE outbox_id=?", (outbox_id,)
            ).fetchone()

    def _mk_session(self, sid: str, key: str, age_days: float = 0.0) -> None:
        self.store.upsert_session(sid, key)
        if age_days:
            stamp = _iso(_ago(days=age_days))
            with self.cstore.transaction() as conn:
                conn.execute(
                    "UPDATE sessions SET updated_at=? WHERE session_id=?",
                    (stamp, sid))

    def _link_task(self, sid: str, task_id: str) -> None:
        with self.cstore.transaction() as conn:
            conn.execute(
                "INSERT INTO tasks(task_id, session_id, session_key, source,"
                " priority, status, created_at, updated_at, envelope_json,"
                " record_json) VALUES (?, ?, ?, 'user', 0, 'running', ?, ?, '{}', '{}')",
                (task_id, sid, f"key-{task_id}", _iso(datetime.now(timezone.utc)),
                 _iso(datetime.now(timezone.utc))))


class OutboxDeadLetterTests(_TempDbBase):
    """连败达上限 → 独立死信终态：不再投递、不冒充 published、可辨识。"""

    def test_exhausted_retries_enter_dead_letter_and_never_republished(self):
        conv = self._new_conversation()
        oid = uuid.uuid4().hex
        self._insert_outbox(conv, oid)

        def _boom(event_type, payload):
            raise RuntimeError("bus down")

        publisher = OutboxPublisher(self.cstore, _boom, max_attempts=3)
        for _ in range(3):
            asyncio.run(publisher.flush_once())

        row = self._outbox_row(oid)
        self.assertIsNotNone(row)
        # 不冒充成功：published_at 保持 NULL；认领列为死信哨兵且租约清空
        self.assertIsNone(row["published_at"])
        self.assertEqual(row["claimed_by"], DEAD_LETTER_CLAIMANT)
        self.assertIsNone(row["claim_expires_at"])

        # 第 4 轮不再认领/投递：认领哨兵被显式跳过，事件计数也不再增长
        published_again = asyncio.run(publisher.flush_once())
        self.assertEqual(published_again, 0)
        row = self._outbox_row(oid)
        self.assertEqual(row["claimed_by"], DEAD_LETTER_CLAIMANT)

    def test_success_path_still_marks_published(self):
        conv = self._new_conversation()
        oid_ok = uuid.uuid4().hex
        self._insert_outbox(conv, oid_ok, event_type="queue.updated")
        publisher = OutboxPublisher(
            self.cstore, lambda t, p: None, max_attempts=3)
        asyncio.run(publisher.flush_once())
        row = self._outbox_row(oid_ok)
        self.assertIsNotNone(row["published_at"])
        self.assertNotEqual(row["claimed_by"] or "", DEAD_LETTER_CLAIMANT)


class SessionRetentionTests(_TempDbBase):
    """任务域 sessions 表：超窗且无引用才回收；任一引用存在即保留。"""

    def test_delete_stale_sessions_keeps_referenced_and_recent(self):
        self._mk_session("s-old-orphan", "webui:old-orphan", age_days=40)
        self._mk_session("s-old-with-task", "webui:old-task", age_days=40)
        self._link_task("s-old-with-task", "t-1")
        self._mk_session("s-recent", "webui:recent")

        cutoff = _iso(_ago(days=30))
        deleted = self.store.delete_stale_sessions(cutoff)

        self.assertEqual(deleted, 1)
        surviving = {r["session_id"] for r in self._all_sessions()}
        self.assertEqual(surviving, {"s-old-with-task", "s-recent"})

    def test_delete_stale_sessions_respects_artifact_reference(self):
        self._mk_session("s-old-artifact", "webui:old-art", age_days=40)
        with self.cstore.transaction() as conn:
            conn.execute(
                "INSERT INTO artifacts(artifact_id, session_id, goal_id, task_id,"
                " created_at, artifact_json) VALUES (?, ?, NULL, NULL, ?, '{}')",
                ("a-1", "s-old-artifact", _iso(datetime.now(timezone.utc))))
        deleted = self.store.delete_stale_sessions(_iso(_ago(days=30)))
        self.assertEqual(deleted, 0)
        ids = {r["session_id"] for r in self._all_sessions()}
        self.assertIn("s-old-artifact", ids)

    def test_retention_manager_collect_reports_deleted_sessions(self):
        self._mk_session("s-gone", "sched:j9:20260701T000000", age_days=60)
        manager = RetentionManager(self.store, self.artifacts)
        report = manager.collect(dry_run=False)
        self.assertGreaterEqual(report.get("deleted_sessions", 0), 1)
        ids = {r["session_id"] for r in self._all_sessions()}
        self.assertNotIn("s-gone", ids)

    # ---- 内部 -----------------------------------------------------------

    def _all_sessions(self):
        with self.cstore.transaction() as conn:
            return conn.execute("SELECT session_id FROM sessions").fetchall()


if __name__ == "__main__":
    unittest.main()


# ================================================================
# 磁盘空间回收（清理机制收尾 2026-08）
# ================================================================

class ReclaimBloatedTests(_TempDbBase):
    def _bloat(self) -> None:
        """造出大量 freelist：建表灌 3MB 数据后整表删除。"""
        with self.store.connection() as conn:
            conn.execute("CREATE TABLE bloat (id INTEGER PRIMARY KEY, blob TEXT)")
            conn.executemany(
                "INSERT INTO bloat(blob) VALUES (?)",
                [("x" * 4096,) for _ in range(768)])
        with self.store.connection() as conn:
            conn.execute("DROP TABLE bloat")

    def _freelist_mb(self) -> float:
        with self.store.connection() as conn:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        return freelist * page_size / 1024 / 1024

    def test_reclaim_triggers_on_bloat_and_shrinks_file(self):
        import os
        self._bloat()
        before_freelist = self._freelist_mb()
        size_before = os.path.getsize(self.store.path)
        stats = self.store.reclaim_if_bloated(min_free_mb=0.5)
        self.assertTrue(stats["triggered"])
        self.assertGreater(before_freelist, 0.5)
        size_after = os.path.getsize(self.store.path)
        self.assertLess(size_after, size_before * 0.6)
        # 库仍可用
        with self.store.connection() as conn:
            conn.execute("CREATE TABLE t(x)").fetchall() if False else conn.execute("SELECT 1").fetchall()

    def test_reclaim_skips_small_freelist(self):
        stats = self.store.reclaim_if_bloated(min_free_mb=0.5)
        self.assertFalse(stats["triggered"])

    def test_reclaim_ratio_guard(self):
        # 空闲量达标但占比低：3MB freelist / 总量撑大 → 不触发
        with self.store.connection() as conn:
            conn.execute("CREATE TABLE keep (id INTEGER PRIMARY KEY, blob TEXT)")
            conn.executemany("INSERT INTO keep(blob) VALUES (?)",
                             [("y" * 4096,) for _ in range(2000)])
            conn.execute("CREATE TABLE bloat2 (id INTEGER PRIMARY KEY, blob TEXT)")
            conn.executemany("INSERT INTO bloat2(blob) VALUES (?)",
                             [("z" * 4096,) for _ in range(800)])
        with self.store.connection() as conn:
            conn.execute("DROP TABLE bloat2")
        # freelist ≈ 3.2MB > min_free_mb,但占比 ~25% < 0.3 → 不触发
        stats = self.store.reclaim_if_bloated(min_free_mb=1.0, free_ratio=0.3)
        self.assertFalse(stats["triggered"])


class BackupRotationTests(_TempDbBase):
    def test_rotate_keeps_newest_and_drops_expired(self):
        import os
        import time as _time
        db = self.store.path
        old = db.with_name(db.name + ".bak")
        new = db.with_name(db.name + ".bak-before-profiles")
        for p, age_days in ((old, 40), (new, 1)):
            p.write_bytes(b"backup")
            stamp = _time.time() - age_days * 86400
            os.utime(p, (stamp, stamp))
        removed = self.store.rotate_backups(keep=1, max_age_days=30)
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(old.exists())   # 超期删除
        self.assertTrue(new.exists())    # 最新且未超期保留

    def test_rotate_keeps_cap_when_all_fresh(self):
        import os
        import time as _time
        db = self.store.path
        b1 = db.with_name(db.name + ".bak")
        b2 = db.with_name(db.name + ".bak-x")
        for p in (b1, b2):
            p.write_bytes(b"b")
            os.utime(p, (_time.time() - 3600,) * 2)
        removed = self.store.rotate_backups(keep=1, max_age_days=30)
        self.assertEqual(removed, 1)
        self.assertTrue(b2.exists() or b1.exists())
        self.assertFalse(b1.exists() and b2.exists())


class ImageCascadeTests(_TempDbBase):
    def setUp(self):
        super().setUp()
        self.image_store = ImageStore(self.store.path.parent / "images")
        self.service = ConversationService(
            ConversationStore(self._Db(self.store)), lambda t, p: None,
            max_global_turns=100, image_store=self.image_store)

    def test_delete_conversation_removes_image_dir(self):
        conv = self.service.get_or_create_conversation(
            "webui:cascade", origin="webui", subtype="main",
            execution_scope="gateway:default")
        self.service.enqueue(
            conv.conversation_id, "带图",
            images=[{"data": _PNG_1PX, "media_type": "image/png"}])
        image_store = self.service.image_store
        turn_id = self.service.send_next(conv.conversation_id)[0].turn_id
        img_node = next(n for n in self.service.store.get_turn_nodes(turn_id)
                        if n.type == "image")
        ref = img_node.metadata["ref"]
        img_dir = image_store._root / conv.conversation_id
        self.assertTrue(img_dir.exists())
        # 会话删除 → 图片目录级联清理，且 ref 不再可解析
        self.assertTrue(self.service.delete_conversation_by_key("webui:cascade"))
        self.assertFalse(img_dir.exists())
        with self.assertRaises(Exception):
            image_store.resolve(conv.conversation_id, ref)

    def test_delete_without_images_is_noop(self):
        conv = self.service.get_or_create_conversation(
            "webui:noimg", origin="webui", subtype="main",
            execution_scope="gateway:default")
        self.assertTrue(self.service.delete_conversation_by_key("webui:noimg"))
        self.assertFalse((self.service.image_store._root / conv.conversation_id).exists())


class BackupExpiryPriorityTests(_TempDbBase):
    def test_expiry_beats_keep_slot(self):
        """回归：40 天前的 .bak 即使是"最新一份"也必须删除（超期优先于
        份数保留）。此前 keep=1 会保护它——实测放过 104MB 旧备份。"""
        import os
        import time as _time
        db = self.store.path
        old_newest = db.with_name(db.name + ".bak")            # 唯一一份,但 40 天前
        old_newest.write_bytes(b"old")
        stamp = _time.time() - 40 * 86400
        os.utime(old_newest, (stamp, stamp))
        removed = self.store.rotate_backups(keep=1, max_age_days=30)
        self.assertEqual(removed, 1)
        self.assertFalse(old_newest.exists())
