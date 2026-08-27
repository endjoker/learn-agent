"""Reference-aware retention for terminal runtime data and artifacts."""
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .models import TaskStatus

# core.goal.models.GoalStatus / core.plan.models.PlanStatus 的全量 value 集合。
# 刻意本地固化而非 import 枚举：core.runtime.retention -> core.goal/plan.models
# -> core.runtime.models 在 goal/plan 先被导入时会构成包初始化环；冒烟测试会
# 断言两处集合与真实枚举一致，防止漂移。
_ALL_GOAL_STATUSES = frozenset({"active", "paused", "blocked", "completed", "cancelled"})
_ALL_PLAN_STATUSES = frozenset({"draft", "awaiting_approval", "approved", "active", "paused",
                                "completed", "failed", "cancelled", "superseded"})
_MEMBER_BATCH = 1000
# runtime.db 任务域 sessions 表的保留窗口（天）。sessions 行此前全仓无
# DELETE、永不清理；默认与 tasks/artifacts 一致取 30 天。仅回收 updated_at
# 超窗且无任何 tasks/artifacts 行引用的 session（引用判定由
# RuntimeStore.delete_stale_sessions 在单条 SQL 内完成，FK RESTRICT 安全）。
_DEFAULT_SESSION_DAYS = 30


@dataclass(frozen=True)
class ReferenceSets:
    """Workflow reference state snapshot, loaded once per collect() run."""
    archived_goal_ids: frozenset[str]
    archived_plan_ids: frozenset[str]
    active_member_ids: frozenset[str]


class RetentionManager:
    """Archive first; never delete active work or referenced artifacts."""
    def __init__(self, store, artifact_store, *, terminal_days: int = 30, artifact_days: int = 30,
                 session_days: int = _DEFAULT_SESSION_DAYS):
        self.store, self.artifact_store = store, artifact_store
        self.terminal_days, self.artifact_days = max(1, int(terminal_days)), max(1, int(artifact_days))
        self.session_days = max(1, int(session_days))

    @staticmethod
    def _expired(value: str | None, days: int) -> bool:
        if not value: return False
        try: when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError: return False
        return when < datetime.now(timezone.utc) - timedelta(days=days)

    def _load_reference_sets(self) -> ReferenceSets:
        """L4#8: collect() 开始时一次性把引用状态加载进内存 set。

        Goals/Plans 经全状态枚举（每行都携带枚举内 status）；team_members 经
        分页跨会话扫描。缺失/未知 id 一律 fail closed（见 _is_referenced）：
        宁可多保护也不误删被引用数据。
        """
        archived_goal_ids = {
            goal["goal_id"] for goal in self.store.list_goals_by_status(
                _ALL_GOAL_STATUSES, limit=1000) if goal.get("archived_at")
        }
        archived_plan_ids = {
            plan["plan_id"] for plan in self.store.list_plans_by_status(
                _ALL_PLAN_STATUSES, limit=1000) if plan.get("archived_at")
        }
        active_member_ids: set[str] = set()
        offset = 0
        while True:
            batch = self.store.list_all_team_members(limit=_MEMBER_BATCH, offset=offset)
            if not batch:
                break
            active_member_ids.update(
                member["agent_id"] for member in batch
                if member.get("status") != "archived")
            if len(batch) < _MEMBER_BATCH:
                break
            offset += len(batch)
        return ReferenceSets(
            archived_goal_ids=frozenset(archived_goal_ids),
            archived_plan_ids=frozenset(archived_plan_ids),
            active_member_ids=frozenset(active_member_ids),
        )

    def _is_referenced(self, artifact, refs: ReferenceSets) -> bool:
        """Only active/unarchived workflow relations protect an Artifact.

        O(1) in-memory set lookups against the snapshot loaded once at collect
        start (zero DB round-trips in the loop).  The legacy team relation has
        no cross-session enumeration in the store, so it keeps a single indexed
        point query; goal/plan/member relations never touch the database here.
        """
        if artifact.goal_id and artifact.goal_id not in refs.archived_goal_ids:
            # Dangling legacy relations fail closed: never auto-delete data
            # whose referenced workflow can no longer be inspected.
            return True
        if artifact.plan_id and artifact.plan_id not in refs.archived_plan_ids:
            return True
        if artifact.team_id:
            team = self.store.get_team(artifact.team_id)
            if team and team.get("status") != "archived":
                return True
        if artifact.child_session_id and artifact.child_session_id in refs.active_member_ids:
            return True
        # A plan task relation is protected by its parent plan. If the legacy
        # artifact lacks plan_id, fail closed rather than deleting it blindly.
        return bool(artifact.plan_task_id)

    def collect(self, *, dry_run: bool = True) -> dict:
        candidates = {"tasks": [], "artifacts": [], "protected": []}
        # L4#8: 引用判定集合一次性加载（相对每 artifact 最多 4 次连接查询）。
        refs = self._load_reference_sets()
        for snapshot in self.store.list_tasks():
            if not snapshot.record.is_terminal or not self._expired(snapshot.record.finished_at, self.terminal_days): continue
            candidates["tasks"].append(snapshot.envelope.task_id)
        # 循环分页扫全量（单次 limit 上限 1000），确保 >1000 条时回收仍然生效。
        offset = 0
        while True:
            batch = self.artifact_store.list(limit=1000, offset=offset)
            if not batch:
                break
            for artifact in batch:
                if self._is_referenced(artifact, refs):
                    candidates["protected"].append(artifact.artifact_id); continue
                if self._expired(artifact.created_at, self.artifact_days): candidates["artifacts"].append(artifact.artifact_id)
            if len(batch) < 1000:
                break
            offset += len(batch)
        if dry_run: return candidates
        deleted_tasks, deleted_artifacts = [], []
        # ArtifactStore deletion removes metadata only after the local path is
        # successfully unlinked (or already absent), maintaining two-stage safety.
        for artifact_id in candidates["artifacts"]:
            if self.artifact_store.delete(artifact_id):
                deleted_artifacts.append(artifact_id)
        for task_id in candidates["tasks"]:
            if self.store.delete_terminal_task(task_id):
                deleted_tasks.append(task_id)
        candidates["deleted_tasks"] = deleted_tasks
        candidates["deleted_artifacts"] = deleted_artifacts
        # 任务域 sessions 表回收（此前全仓无 DELETE，行数随 isolated 定时触发
        # 与 subagent child 单调增长）。放在 tasks 删除之后：本轮刚删的 task
        # 立即解除对 session 的引用，同一轮即可回收其孤儿 session。cutoff 与
        # utc_now() 同格式（+00:00 毫秒精度），保证 TEXT 比较语义正确。
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=self.session_days)).isoformat(timespec="milliseconds")
        candidates["deleted_sessions"] = self.store.delete_stale_sessions(cutoff)
        return candidates