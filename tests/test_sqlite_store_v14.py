# -*- coding: utf-8 -*-
"""SCHEMA_VERSION=14 迁移与 artifacts 关联列修复的回归测试。

覆盖：
- v13→v14 迁移把存量行 artifact_json 中的 goal/plan/team/task/child_session
  关联键回填到列上（仅当列为 NULL 且 JSON 含对应键）；
- save_artifact 新写入的行同步携带关联列（含冲突重写分支）；
- delete_plan/delete_goal 解除引用走索引（EXPLAIN QUERY PLAN 不再 SCAN
  artifacts），行为与旧全表扫描版本一致；
- 迁移链 v1→v14 幂等，可重复打开同一数据库。
"""

import json
import tempfile
import unittest
from pathlib import Path

from core.runtime import RuntimeEvent, RuntimeStore

_NOW = "2026-01-01T00:00:00+00:00"


def _make_artifact(store, artifact_id, session_id, **relations):
    """按 ArtifactRef.to_dict 形状构造并持久化一个 artifact。"""
    payload = {
        "artifact_id": artifact_id, "session_id": session_id, "name": "x.md",
        "type": "report", "path": f"standalone/{artifact_id}-x.md",
        "media_type": "text/markdown", "size": 3, "sha256": "0" * 64,
        "summary": "", "created_by": "root", "created_at": _NOW,
    }
    payload.update(relations)
    store.save_artifact(payload, RuntimeEvent.create("artifact.created", session_id=session_id))
    return payload


def _artifact_row(store, artifact_id):
    with store.connection() as connection:
        row = connection.execute(
            "SELECT goal_id, plan_id, team_id, task_id, child_session_id, artifact_json "
            "FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    return row


class V14MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _open_legacy_v13_store(self):
        """以 v13 版本号打开库：得到尚未升级的存量 schema 与数据。"""
        original = RuntimeStore.SCHEMA_VERSION
        RuntimeStore.SCHEMA_VERSION = 13
        try:
            return RuntimeStore(self.path)
        finally:
            RuntimeStore.SCHEMA_VERSION = original

    def test_v13_to_v14_backfills_artifact_relation_columns_from_json(self):
        legacy = self._open_legacy_v13_store()
        with legacy.connection() as connection:
            connection.execute(
                "INSERT INTO sessions(session_id, session_key, created_at, updated_at) "
                "VALUES ('s1', 'k1', ?, ?)", (_NOW, _NOW))
            # 旧版 save_artifact 只填 goal/task 两列；这里直接按旧行为造数：
            # 行1 关联列全 NULL 但 JSON 携带全部关联键；
            # 行2 仅 plan 键待回填（goal 列已有值，不得被改写）；
            # 行3 JSON 无关联键 → 应保持原样。
            connection.execute(
                "INSERT INTO artifacts(artifact_id, session_id, goal_id, task_id, created_at, artifact_json) "
                "VALUES ('a_all', 's1', NULL, NULL, ?, ?)", (_NOW, json.dumps({
                    "artifact_id": "a_all", "goal_id": "g_old", "plan_id": "p_old",
                    "team_id": "t_old", "task_id": "task_old", "child_session_id": "c_old"})))
            connection.execute(
                "INSERT INTO artifacts(artifact_id, session_id, goal_id, task_id, created_at, artifact_json) "
                "VALUES ('a_partial', 's1', 'g_keep', NULL, ?, ?)", (_NOW, json.dumps({
                    "artifact_id": "a_partial", "goal_id": "g_keep", "plan_id": "p_old2"})))
            connection.execute(
                "INSERT INTO artifacts(artifact_id, session_id, goal_id, task_id, created_at, artifact_json) "
                "VALUES ('a_plain', 's1', NULL, NULL, ?, ?)", (_NOW, json.dumps({
                    "artifact_id": "a_plain"})))
        legacy.close()

        store = RuntimeStore(self.path)
        with store.connection() as connection:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        self.assertEqual(version, 16)  # v16：queue_items.images_json（图片信封）

        row = _artifact_row(store, "a_all")
        self.assertEqual(tuple(row)[:5], ("g_old", "p_old", "t_old", "task_old", "c_old"))
        row = _artifact_row(store, "a_partial")
        self.assertEqual(tuple(row)[:5], ("g_keep", "p_old2", None, None, None))
        row = _artifact_row(store, "a_plain")
        self.assertEqual(tuple(row)[:5], (None, None, None, None, None))

        # 幂等：再次打开不重复回填、不改写已解除引用（NULL 列）的行。
        store.close()
        reopened = RuntimeStore(self.path)
        try:
            self.assertEqual(tuple(_artifact_row(reopened, "a_partial"))[:5],
                             ("g_keep", "p_old2", None, None, None))
        finally:
            reopened.close()

    def test_save_artifact_persists_relation_columns(self):
        store = RuntimeStore(self.path)
        _make_artifact(store, "a_rel", "s_rel", goal_id="g1", plan_id="p1",
                       team_id="t1", task_id="tk1", child_session_id="c1",
                       plan_task_id="step_1")
        row = _artifact_row(store, "a_rel")
        self.assertEqual(tuple(row)[:5], ("g1", "p1", "t1", "tk1", "c1"))

        # 冲突重写分支：关联列与 JSON 一同刷新。
        _make_artifact(store, "a_rel", "s_rel", team_id="t2")
        row = _artifact_row(store, "a_rel")
        self.assertEqual(tuple(row)[:5], (None, None, "t2", None, None))
        store.close()

    def _seed_goal_workflow(self, store):
        """构造 Goal→Plan/Team/Member 及各维度关联 artifact 的完整数据。"""
        _make_artifact(store, "a_goal", "s_wf", goal_id="g_wf")
        _make_artifact(store, "a_plan", "s_wf", plan_id="p_wf", plan_task_id="step_2")
        _make_artifact(store, "a_team", "s_wf", team_id="t_wf")
        _make_artifact(store, "a_child", "s_wf", child_session_id="c_wf")
        _make_artifact(store, "a_keep", "s_wf", plan_id="p_other")

    def test_delete_plan_detaches_via_indexed_lookup(self):
        store = RuntimeStore(self.path)
        _make_artifact(store, "a_px", "s_px", plan_id="pX", plan_task_id="step_1")
        _make_artifact(store, "a_py", "s_px", plan_id="pY", plan_task_id="step_9")
        with store.connection() as connection:
            connection.execute(
                "INSERT INTO plans(plan_id, session_id, goal_id, status, version, created_at, updated_at, plan_json) "
                "VALUES ('pX', 's_px', NULL, 'completed', 1, ?, ?, '{}')", (_NOW, _NOW))
            connection.execute(
                "INSERT INTO plan_tasks(plan_task_id, plan_id, status, task_id, created_at, updated_at, task_json) "
                "VALUES ('step_1', 'pX', 'completed', NULL, ?, ?, '{}')", (_NOW, _NOW))

        # delete_plan 的候选查询必须按 plan_id 索引前导列检索，不再 SCAN。
        with store.connection() as connection:
            plan_rows = connection.execute(
                "EXPLAIN QUERY PLAN SELECT artifact_id, artifact_json FROM artifacts WHERE plan_id=?",
                ("pX",)).fetchall()
        detail = " ".join(row["detail"] for row in plan_rows)
        self.assertNotIn("SCAN artifacts", detail)
        self.assertIn("artifacts_plan_task_idx", detail)

        self.assertTrue(store.delete_plan("pX"))
        row = _artifact_row(store, "a_px")
        self.assertEqual(tuple(row)[:5], (None, None, None, None, None))
        payload = json.loads(row["artifact_json"])
        self.assertNotIn("plan_id", payload)
        self.assertNotIn("plan_task_id", payload)
        # 其他 Plan 的 artifact 不受影响
        row = _artifact_row(store, "a_py")
        self.assertEqual(tuple(row)[:5], (None, "pY", None, None, None))
        store.close()

    def test_delete_goal_detaches_all_dimensions_via_indexed_lookups(self):
        from core.goal import GoalRuntime
        store = RuntimeStore(self.path)
        runtime = GoalRuntime(store)
        goal = runtime.create("s_wf", "clear me")
        goal_id = goal.goal_id
        with store.connection() as connection:
            # 用固定 ID 的业务行替换随机 ID，便于断言各维度的级联解除。
            connection.execute("UPDATE goals SET goal_id='g_wf' WHERE goal_id=?", (goal_id,))
            connection.execute(
                "INSERT INTO teams(team_id, goal_id, session_id, status, created_at, updated_at, team_json) "
                "VALUES ('t_wf', 'g_wf', 's_wf', 'archived', ?, ?, '{}')", (_NOW, _NOW))
            connection.execute(
                "INSERT INTO team_members(agent_id, team_id, status, parent_agent_id, created_at, updated_at, member_json) "
                "VALUES ('c_wf', 't_wf', 'archived', 'root', ?, ?, '{}')", (_NOW, _NOW))
            connection.execute(
                "INSERT INTO plans(plan_id, session_id, goal_id, status, version, created_at, updated_at, plan_json) "
                "VALUES ('p_wf', 's_wf', 'g_wf', 'completed', 1, ?, ?, '{}')", (_NOW, _NOW))
            connection.execute(
                "INSERT INTO plan_tasks(plan_task_id, plan_id, status, task_id, created_at, updated_at, task_json) "
                "VALUES ('step_2', 'p_wf', 'completed', NULL, ?, ?, '{}')", (_NOW, _NOW))
        self._seed_goal_workflow(store)

        # delete_goal 的候选查询是四路索引列 OR 并集，任何一路都不允许 SCAN。
        with store.connection() as connection:
            goal_rows = connection.execute(
                "EXPLAIN QUERY PLAN SELECT artifact_id, artifact_json FROM artifacts "
                "WHERE goal_id=? OR plan_id IN (?,?) OR team_id IN (?) OR child_session_id IN (?)",
                ("g_wf", "p_wf", "p_other", "t_wf", "c_wf")).fetchall()
        detail = " ".join(row["detail"] for row in goal_rows)
        self.assertNotIn("SCAN artifacts", detail)
        for index_name in ("artifacts_goal_created_idx", "artifacts_plan_task_idx",
                           "artifacts_team_idx", "artifacts_child_session_idx"):
            self.assertIn(index_name, detail)

        self.assertTrue(store.delete_goal("g_wf"))
        expectations = {
            "a_goal": (None, None, None, None, None),
            "a_plan": (None, None, None, None, None),
            "a_team": (None, None, None, None, None),
            "a_child": (None, None, None, None, None),
        }
        for artifact_id, columns in expectations.items():
            row = _artifact_row(store, artifact_id)
            self.assertEqual(tuple(row)[:5], columns, artifact_id)
            payload = json.loads(row["artifact_json"])
            self.assertNotIn("goal_id", payload)
            self.assertNotIn("plan_id", payload)
            self.assertNotIn("plan_task_id", payload)
            self.assertNotIn("team_id", payload)
            self.assertNotIn("child_session_id", payload)
        # 非本 Goal 的关联保持不变
        row = _artifact_row(store, "a_keep")
        self.assertEqual(tuple(row)[:5], (None, "p_other", None, None, None))
        store.close()

    def test_full_migration_chain_v1_to_v14_is_idempotent(self):
        first = RuntimeStore(self.path)
        _make_artifact(first, "a_chain", "s_chain", plan_id="p_chain")
        first.close()

        for _ in range(2):
            reopened = RuntimeStore(self.path)
            try:
                with reopened.connection() as connection:
                    versions = [row[0] for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version")]
                    indexes = {row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertEqual(versions, list(range(1, 17)))
                for expected in ("tasks_status_idx", "sessions_parent_session_idx",
                                 "artifacts_team_idx", "artifacts_child_session_idx"):
                    self.assertIn(expected, indexes)
                # v15 移除了 control_leases 表
                with reopened.connection() as connection:
                    control_leases = {row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='control_leases'")}
                self.assertNotIn("control_leases", control_leases)
                row = _artifact_row(reopened, "a_chain")
                self.assertEqual(tuple(row)[:5], (None, "p_chain", None, None, None))
            finally:
                reopened.close()

    def test_v14_to_v15_drops_control_leases(self):
        """v14 存量库（含 control_leases）升级到 v15 后该表被移除。"""
        # 以 v14 打开，手动补建 control_leases（v15 之前 schema 有该表）
        original = RuntimeStore.SCHEMA_VERSION
        RuntimeStore.SCHEMA_VERSION = 14
        try:
            legacy = RuntimeStore(self.path)
            with legacy.connection() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS control_leases ("
                    "lease_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL UNIQUE, "
                    "holder_id TEXT NOT NULL, expires_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO control_leases(lease_id, conversation_id, holder_id, expires_at, updated_at) "
                    "VALUES ('l1','c1','tab-1','2999-01-01T00:00:00+00:00','')")
            legacy.close()
        finally:
            RuntimeStore.SCHEMA_VERSION = original

        store = RuntimeStore(self.path)
        try:
            with store.connection() as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            self.assertNotIn("control_leases", tables)
            self.assertEqual(version, 16)  # v16：queue_items.images_json（图片信封）
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
