"""SQLite-backed durable state for the unified runtime.

Transcript persistence intentionally stays in MessageStore during migration.
This store owns task/session runtime state and append-only domain events.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from core.debug import logger

from .models import RuntimeEvent, TaskEnvelope, TaskRecord, TaskResult, TaskStatus, utc_now

def _migration_script(version: int, body: str) -> str:
    """把迁移 DDL 包装进 BEGIN IMMEDIATE 事务，并把版本号写入与 DDL 同事务。

    executescript 会在执行前隐式 COMMIT 挂起事务，因此 BEGIN/COMMIT 必须
    放在脚本内部；版本号写入失败时整体回滚，下次启动重试保持幂等。
    """
    return (
        "BEGIN IMMEDIATE;\n"
        + body
        + f"INSERT INTO schema_migrations(version, applied_at) VALUES ({version}, '{utc_now()}');\n"
        + "COMMIT;\n"
    )



class RuntimeStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskSnapshot:
    envelope: TaskEnvelope
    record: TaskRecord
    result: Optional[TaskResult] = None


class RuntimeStore:
    """Small transactional store with thread-local pooled connections (B3).

    WAL 模式下读不阻塞写；每个线程持有自己的长连接，PRAGMA/busy_timeout
    在连接创建时初始化一次。每次 connection() 上下文仍保持"退出即
    提交/回滚"语义（嵌套上下文仅最外层提交），对外行为与逐操作短连接一致。
    """

    SCHEMA_VERSION = 16

    def __init__(self, path: str | Path, *, wal: bool = True,
                 busy_timeout_ms: int = 5000):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.wal = bool(wal)
        try:
            self.busy_timeout_ms = max(0, int(busy_timeout_ms))
        except (TypeError, ValueError):
            self.busy_timeout_ms = 5000
        # B3：thread-local 长连接池（每线程一条），close() 时统一释放。
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()
        self.initialize()

    def _get_thread_connection(self) -> sqlite3.Connection:
        """取（或创建）当前线程的长连接；PRAGMA/busy_timeout 只在创建时初始化一次。"""
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(
                self.path, timeout=self.busy_timeout_ms / 1000.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            # JKagent 收官 C-5：WAL 下 commit 不再强制 fsync，把增量写放大从
            # 每次 commit 一次 fsync 降为 checkpoint 一次。权衡：崩溃可能丢失
            # 最近一次 checkpoint 之后的少量提交——对话增量（node.delta/工具
            # 结果）可由客户端 version_gap + 快照自愈兜底，任务/权限等权威
            # 状态仍由上层 Outbox/幂等语义保证重放，因此可接受。
            connection.execute("PRAGMA synchronous=NORMAL")
            self._local.connection = connection
            with self._connections_lock:
                self._connections.add(connection)
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """thread-local 长连接上下文（B3）。

        与旧的"每操作 connect/close"语义一致：成功退出提交、异常回滚；
        嵌套上下文（同线程）共享同一连接且仅最外层提交，保证事务不被打断。
        """
        connection = self._get_thread_connection()
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        try:
            yield connection
            if depth == 0:
                connection.commit()
        except Exception:
            if depth == 0:
                connection.rollback()
            raise
        finally:
            self._local.depth = depth

    def close(self) -> None:
        """关闭全部线程本地长连接（优雅停机 / 测试清理）。"""
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass
        try:
            self._local.connection = None
            self._local.depth = 0
        except Exception:
            pass

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Public connection contextmanager (Phase 1).

        Lets WorkspaceStore share the same SQLite file/PRAGMA configuration
        without duplicating connection setup. Commits on success, rolls back
        on error.
        """
        with self._connection() as connection:
            yield connection

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                "PRAGMA journal_mode=" + ("WAL" if self.wal else "DELETE"))
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
            if current > self.SCHEMA_VERSION:
                raise RuntimeStoreError("runtime database is newer than this application")
            if current < self.SCHEMA_VERSION:
                # 迁移前做一次性 .bak 备份（已存在则跳过），失败不阻断启动。
                self._backup_once()
                migrations = [
                    (1, self._migrate_v1), (2, self._migrate_v2),
                    (3, self._migrate_v3), (4, self._migrate_v4),
                    (5, self._migrate_v5), (6, self._migrate_v6),
                    (7, self._migrate_v7), (8, self._migrate_v8),
                    (9, self._migrate_v9), (10, self._migrate_v10),
                    (11, self._migrate_v11), (12, self._migrate_v12),
                    (13, self._migrate_v13), (14, self._migrate_v14),
                    (15, self._migrate_v15),
                    (16, self._migrate_v16),
                ]
                for version, migrate in migrations:
                    # 只应用到目标版本为止：注册表可能包含超前条目，不得越级执行。
                    if current < version <= self.SCHEMA_VERSION:
                        # 每个迁移在 BEGIN IMMEDIATE 事务内执行 DDL 并写入版本号，
                        # 失败整体回滚（版本号不落库），下次启动重试保持幂等。
                        migrate(connection)
            # 兼容：已存在的 v9 库补 agent_profiles.is_system 列（幂等）
            self._ensure_profile_is_system_column(connection)
            # Plan 已从 Workspace/Profile chat_mode 退役。早期库中的
            # Profile 仍可能保留 chat_mode='plan'；这些行在读取时会被
            # 当前严格模型校验拒绝并让 /api/agents 返回 500，因此启动时
            # 幂等归一化为唯一支持的 chat 模式。
            self._normalize_retired_profile_chat_modes(connection)
            # 多实例就绪：leader_lease 选主表 + outbox 认领列（幂等，不提升
            # SCHEMA_VERSION——增量 DDL 独立于版本化迁移，避免扰动既有迁移测试）。
            self._ensure_multi_instance_schema(connection)

    def _backup_once(self) -> None:
        """迁移前对 runtime.db 做一次性 .bak 备份（已存在则跳过）。

        使用 sqlite backup API（包含 WAL 内容），失败只记日志不阻断启动，
        迁移本身仍由事务保证幂等。
        """
        backup = self.path.with_name(self.path.name + ".bak")
        if backup.exists() or not self.path.exists():
            return
        try:
            source = sqlite3.connect(str(self.path))
            try:
                target = sqlite3.connect(str(backup))
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
        except Exception:
            logger.warning("failed to back up runtime database to %s", backup, exc_info=True)

    # ============================================================
    # 磁盘空间回收（清理机制收尾，2026-08）
    # ============================================================

    def reclaim_if_bloated(self, *, free_ratio: float = 0.3,
                           min_free_mb: float = 50.0) -> dict:
        """空闲页超阈值时收缩数据库文件（checkpoint(TRUNCATE) + VACUUM）。

        背景：任务/会话/outbox 的各类保留期清理只把页标为 freelist，
        SQLite 永不自动把空间还给 OS——实测 110MB 库中 81.5MB（74%）是
        空闲页。此处按"空闲占比 > free_ratio 且空闲量 > min_free_mb"双
        条件触发收缩；条件不满足是常态路径（零成本）。

        VACUUM 需要临时空间且不可被打断：调用方须放在 executor 线程
        （retention loop 已经如此），不要在事件循环内直呼。
        """
        stats = {"triggered": False, "before_mb": 0.0, "after_mb": 0.0}
        if not self.path.exists():
            return stats

        def _size_mb() -> float:
            total = 0.0
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    total += p.stat().st_size
            return total / 1024 / 1024

        stats["before_mb"] = round(_size_mb(), 1)
        connection = self._get_thread_connection()
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]
        if page_count <= 0:
            return stats
        free_mb = freelist * page_size / 1024 / 1024
        ratio = freelist / page_count
        if not (ratio > free_ratio and free_mb > min_free_mb):
            return stats
        logger.warning(
            "runtime.db 空闲页超标（%.0f%%, %.1fMB），执行 VACUUM 收缩",
            ratio * 100, free_mb)
        # TRUNCATE 先把 WAL 归零（顺带治理 wal 膨胀），再重建主文件
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stats["triggered"] = True
        stats["after_mb"] = round(_size_mb(), 1)
        logger.warning("runtime.db 收缩完成: %.1fMB → %.1fMB",
                       stats["before_mb"], stats["after_mb"])
        return stats

    def rotate_backups(self, *, keep: int = 1, max_age_days: int = 30) -> int:
        """轮换迁移备份（runtime.db.bak*）：按 mtime 保留最新 keep 份，
        其余删除；任何保留份超过 max_age_days 也删除。返回删除数。

        背景：_backup_once 每次 schema 升级都生成整库备份（当前两份共
        ~106MB），此前永不回收。
        """
        removed = 0
        parent = self.path.parent
        cutoff = time.time() - max_age_days * 86400
        backups = sorted(
            (p for p in parent.glob(self.path.name + ".bak*") if p.is_file()),
            key=lambda p: p.stat().st_mtime, reverse=True)
        kept = 0
        for backup in backups:
            expired = backup.stat().st_mtime < cutoff
            # 超期优先于份数保留：旧备份即使"最新"也失去恢复价值
            # （实测曾出现 40 天前的 104MB .bak 因 keep=1 被保护）。
            if expired:
                reason = "超期"
            elif kept >= keep:
                reason = "超出保留份数"
            else:
                kept += 1
                continue
            try:
                backup.unlink()
                removed += 1
                logger.info("轮换迁移备份: 删除 %s（%s）", backup.name, reason)
            except OSError:
                logger.warning("轮换迁移备份失败: %s", backup, exc_info=True)
        return removed

    @staticmethod
    def _migrate_v1(connection: sqlite3.Connection) -> None:
        connection.executescript(_migration_script(1, """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    active_goal_id TEXT,
    active_plan_id TEXT,
    team_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    source TEXT NOT NULL,
    priority INTEGER NOT NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    result_json TEXT,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS tasks_session_status_idx ON tasks(session_id, status, priority DESC, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_idempotency_idx
    ON tasks(session_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS runtime_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT,
    task_id TEXT,
    run_id TEXT,
    sequence INTEGER,
    data_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
);
CREATE INDEX IF NOT EXISTS runtime_events_task_idx ON runtime_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS runtime_events_session_idx ON runtime_events(session_id, created_at);
"""))

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        """Add durable Goal state after the initial task-runtime schema."""
        connection.executescript(_migration_script(2, """
CREATE TABLE IF NOT EXISTS goals (
    goal_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    goal_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS goals_session_updated_idx ON goals(session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS goals_session_status_idx ON goals(session_id, status, updated_at DESC);
"""))

    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        """Add single-layer Team state, members, and append-only messages."""
        connection.executescript(_migration_script(3, """
CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    team_json TEXT NOT NULL,
    FOREIGN KEY(goal_id) REFERENCES goals(goal_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS teams_goal_idx ON teams(goal_id);
CREATE INDEX IF NOT EXISTS teams_session_updated_idx ON teams(session_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS team_members (
    agent_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    member_json TEXT NOT NULL,
    FOREIGN KEY(team_id) REFERENCES teams(team_id)
);
CREATE INDEX IF NOT EXISTS team_members_team_status_idx ON team_members(team_id, status, created_at);

CREATE TABLE IF NOT EXISTS team_messages (
    message_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    message_json TEXT NOT NULL,
    FOREIGN KEY(team_id) REFERENCES teams(team_id)
);
CREATE INDEX IF NOT EXISTS team_messages_team_created_idx ON team_messages(team_id, created_at);
"""))

    @staticmethod
    def _migrate_v4(connection: sqlite3.Connection) -> None:
        """Add durable Plan and PlanTask workflow state."""
        connection.executescript(_migration_script(4, """
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    goal_id TEXT,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    FOREIGN KEY(goal_id) REFERENCES goals(goal_id)
);
CREATE INDEX IF NOT EXISTS plans_session_updated_idx ON plans(session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS plans_goal_version_idx ON plans(goal_id, version DESC);

CREATE TABLE IF NOT EXISTS plan_tasks (
    plan_task_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    task_json TEXT NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
);
CREATE INDEX IF NOT EXISTS plan_tasks_plan_status_idx ON plan_tasks(plan_id, status, created_at);
"""))

    @staticmethod
    def _migrate_v5(connection: sqlite3.Connection) -> None:
        """Make PlanTask -> Task linkage optional until a runtime task is durable."""
        connection.executescript(_migration_script(5, """
ALTER TABLE plan_tasks RENAME TO plan_tasks_v4;
CREATE TABLE IF NOT EXISTS plan_tasks (
    plan_task_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    task_json TEXT NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);
INSERT INTO plan_tasks(plan_task_id, plan_id, status, task_id, created_at, updated_at, task_json)
    SELECT plan_task_id, plan_id, status, task_id, created_at, updated_at, task_json
    FROM plan_tasks_v4;
DROP TABLE IF EXISTS plan_tasks_v4;
CREATE INDEX IF NOT EXISTS plan_tasks_plan_status_idx ON plan_tasks(plan_id, status, created_at);
"""))

    @staticmethod
    def _migrate_v6(connection: sqlite3.Connection) -> None:
        """Scope PlanTask IDs by Plan so repeated step_1 IDs cannot overwrite history."""
        connection.executescript(_migration_script(6, """
ALTER TABLE plan_tasks RENAME TO plan_tasks_v5;
CREATE TABLE IF NOT EXISTS plan_tasks (
    plan_task_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    task_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, plan_task_id),
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);
INSERT INTO plan_tasks(plan_task_id, plan_id, status, task_id, created_at, updated_at, task_json)
    SELECT plan_task_id, plan_id, status, task_id, created_at, updated_at, task_json
    FROM plan_tasks_v5;
DROP TABLE IF EXISTS plan_tasks_v5;
CREATE INDEX IF NOT EXISTS plan_tasks_plan_status_idx ON plan_tasks(plan_id, status, created_at);
"""))

    @staticmethod
    def _migrate_v7(connection: sqlite3.Connection) -> None:
        """Add metadata index for immutable artifacts stored on the local filesystem."""
        connection.executescript(_migration_script(7, """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    goal_id TEXT,
    task_id TEXT,
    created_at TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS artifacts_session_created_idx ON artifacts(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS artifacts_goal_created_idx ON artifacts(goal_id, created_at DESC);
CREATE INDEX IF NOT EXISTS artifacts_task_created_idx ON artifacts(task_id, created_at DESC);
"""))

    @staticmethod
    def _migrate_v8(connection: sqlite3.Connection) -> None:
        """Decouple Teams from Goals while retaining existing Team snapshots."""
        connection.executescript(_migration_script(8, """
DROP INDEX IF EXISTS teams_goal_idx;
DROP INDEX IF EXISTS teams_session_updated_idx;
DROP INDEX IF EXISTS team_members_team_status_idx;
DROP INDEX IF EXISTS team_messages_team_created_idx;
ALTER TABLE team_members RENAME TO team_members_v7;
ALTER TABLE team_messages RENAME TO team_messages_v7;
ALTER TABLE teams RENAME TO teams_v7;
CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    goal_id TEXT,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    team_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_members (
    agent_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    member_json TEXT NOT NULL,
    FOREIGN KEY(team_id) REFERENCES teams(team_id)
);
CREATE INDEX IF NOT EXISTS team_members_team_status_idx ON team_members(team_id, status, created_at);
CREATE TABLE IF NOT EXISTS team_messages (
    message_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    message_json TEXT NOT NULL,
    FOREIGN KEY(team_id) REFERENCES teams(team_id)
);
CREATE INDEX IF NOT EXISTS team_messages_team_created_idx ON team_messages(team_id, created_at);
INSERT INTO teams(team_id, goal_id, session_id, status, created_at, updated_at, team_json)
    SELECT team_id, goal_id, session_id, status, created_at, updated_at, team_json
    FROM teams_v7;
INSERT INTO team_members(agent_id, team_id, status, parent_agent_id, created_at, updated_at, member_json)
    SELECT agent_id, team_id, status, parent_agent_id, created_at, updated_at, member_json
    FROM team_members_v7;
INSERT INTO team_messages(message_id, team_id, task_id, created_at, message_json)
    SELECT message_id, team_id, task_id, created_at, message_json
    FROM team_messages_v7;
DROP TABLE IF EXISTS team_members_v7;
DROP TABLE IF EXISTS team_messages_v7;
DROP TABLE IF EXISTS teams_v7;
CREATE INDEX IF NOT EXISTS teams_session_updated_idx ON teams(session_id, updated_at DESC);
"""))

    @staticmethod
    def _migrate_v9(connection: sqlite3.Connection) -> None:
        """Workspace module domain tables (Phase 1).

        Adds workspaces / agent_profiles / workspace_sessions /
        workspace_runtime_snapshots / agent_profile_versions.
        All JSON columns store UTF-8 JSON text via the workspace store.
        """
        connection.executescript(_migration_script(9, """
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_path TEXT NOT NULL,
    working_directory TEXT NOT NULL DEFAULT '',
    extra_workspace_roots TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    default_agent_profile_id TEXT NOT NULL DEFAULT '',
    default_model TEXT NOT NULL DEFAULT '',
    permission_mode TEXT NOT NULL DEFAULT 'ask',
    chat_mode TEXT NOT NULL DEFAULT 'chat',
    include_tools TEXT NOT NULL DEFAULT '[]',
    exclude_tools TEXT NOT NULL DEFAULT '[]',
    include_skills TEXT NOT NULL DEFAULT '[]',
    exclude_skills TEXT NOT NULL DEFAULT '[]',
    include_mcp_servers TEXT NOT NULL DEFAULT '[]',
    exclude_mcp_servers TEXT NOT NULL DEFAULT '[]',
    ui_preferences TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT NOT NULL DEFAULT '',
    path_risk_level TEXT NOT NULL DEFAULT 'none',
    path_warnings TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS workspaces_status_idx ON workspaces(status, updated_at);
CREATE INDEX IF NOT EXISTS workspaces_project_path_idx ON workspaces(project_path);

CREATE TABLE IF NOT EXISTS agent_profiles (
    profile_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '[]',
    skills TEXT NOT NULL DEFAULT '[]',
    mcp_servers TEXT NOT NULL DEFAULT '[]',
    default_model TEXT NOT NULL DEFAULT '',
    permission_mode TEXT NOT NULL DEFAULT 'ask',
    chat_mode TEXT NOT NULL DEFAULT 'chat',
    max_steps INTEGER NOT NULL DEFAULT 100,
    include_tools TEXT NOT NULL DEFAULT '[]',
    exclude_tools TEXT NOT NULL DEFAULT '[]',
    ui_preferences TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    is_system INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_profiles_name_idx ON agent_profiles(name);
CREATE INDEX IF NOT EXISTS agent_profiles_status_idx ON agent_profiles(status, updated_at);

CREATE TABLE IF NOT EXISTS workspace_sessions (
    session_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    agent_profile_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    permission_mode TEXT NOT NULL DEFAULT 'ask',
    chat_mode TEXT NOT NULL DEFAULT 'chat',
    reasoning_level TEXT NOT NULL DEFAULT 'inherit',
    status TEXT NOT NULL DEFAULT 'active',
    client_config_version INTEGER NOT NULL DEFAULT 0,
    last_snapshot_id TEXT NOT NULL DEFAULT '',
    last_active_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    is_busy INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS workspace_sessions_key_idx ON workspace_sessions(session_key);
CREATE INDEX IF NOT EXISTS workspace_sessions_workspace_idx
    ON workspace_sessions(workspace_id, status, updated_at);

CREATE TABLE IF NOT EXISTS workspace_runtime_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT '',
    workspace_session_id TEXT NOT NULL DEFAULT '',
    agent_profile_id TEXT NOT NULL DEFAULT '',
    agent_profile_version INTEGER NOT NULL DEFAULT 0,
    workspace_version INTEGER NOT NULL DEFAULT 0,
    session_client_config_version INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    permission_mode TEXT NOT NULL DEFAULT 'ask',
    chat_mode TEXT NOT NULL DEFAULT 'chat',
    reasoning_level TEXT NOT NULL DEFAULT 'inherit',
    working_directory TEXT NOT NULL DEFAULT '',
    project_root TEXT NOT NULL DEFAULT '',
    framework_root TEXT NOT NULL DEFAULT '',
    agent_data_root TEXT NOT NULL DEFAULT '',
    extra_workspace_roots TEXT NOT NULL DEFAULT '[]',
    tools TEXT NOT NULL DEFAULT '[]',
    skills TEXT NOT NULL DEFAULT '[]',
    mcp_servers TEXT NOT NULL DEFAULT '[]',
    system_prompt TEXT NOT NULL DEFAULT '',
    prompt_hash TEXT NOT NULL DEFAULT '',
    expected_prompt_hash TEXT NOT NULL DEFAULT '',
    capability_hash TEXT NOT NULL DEFAULT '',
    dedup_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS workspace_snapshots_dedup_idx
    ON workspace_runtime_snapshots(dedup_key);
CREATE INDEX IF NOT EXISTS workspace_snapshots_session_idx
    ON workspace_runtime_snapshots(workspace_session_id, created_at);

CREATE TABLE IF NOT EXISTS agent_profile_versions (
    version_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(profile_id) REFERENCES agent_profiles(profile_id)
);
CREATE INDEX IF NOT EXISTS agent_profile_versions_profile_idx
    ON agent_profile_versions(profile_id, version);
"""))

    @staticmethod
    def _migrate_v10(connection: sqlite3.Connection) -> None:
        """Persist per-workspace-session reasoning selections and snapshots."""
        connection.execute("BEGIN IMMEDIATE")
        RuntimeStore._ensure_column(
            connection, "workspace_sessions", "reasoning_level",
            "TEXT NOT NULL DEFAULT 'inherit'")
        RuntimeStore._ensure_column(
            connection, "workspace_runtime_snapshots", "reasoning_level",
            "TEXT NOT NULL DEFAULT 'inherit'")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (10, utc_now()))
        connection.execute("COMMIT")

    @staticmethod
    def _migrate_v11(connection: sqlite3.Connection) -> None:
        """Add unified-runtime lineage, delivery state and indexes."""
        def add_column(table: str, column: str, definition: str) -> None:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        # artifacts_plan_task_idx 依赖 artifacts.plan_id/plan_task_id 列，
        # 因此 add_column 必须先行（幂等，自动提交）；随后 executescript 用
        # 脚本内 BEGIN IMMEDIATE 开启事务，DDL/DELETE/版本号写入同事务，
        # 最后统一 COMMIT（executescript 会先隐式 COMMIT 挂起事务）。
        add_column("sessions", "parent_session_id", "TEXT")
        add_column("sessions", "origin", "TEXT NOT NULL DEFAULT 'gateway'")
        add_column("sessions", "subagent_mode", "TEXT")
        add_column("goals", "version", "INTEGER NOT NULL DEFAULT 1")
        add_column("artifacts", "plan_id", "TEXT")
        add_column("artifacts", "plan_task_id", "TEXT")
        add_column("artifacts", "team_id", "TEXT")
        add_column("artifacts", "child_session_id", "TEXT")
        connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS channel_delivery (
                delivery_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, channel TEXT NOT NULL,
                message_id TEXT, state TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
            CREATE INDEX IF NOT EXISTS runtime_events_domain_idx ON runtime_events(event_type, session_id, created_at);
            CREATE INDEX IF NOT EXISTS artifacts_plan_task_idx ON artifacts(plan_id, plan_task_id, created_at);
            CREATE INDEX IF NOT EXISTS channel_delivery_task_idx ON channel_delivery(task_id, state);
        """)
        # Retired plan-mode Workspace sessions cannot map to the unified chat
        # runtime. Plans/tasks/artifacts remain in their independent tables.
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_sessions'").fetchone():
            connection.execute("DELETE FROM workspace_runtime_snapshots WHERE workspace_session_id IN (SELECT session_id FROM workspace_sessions WHERE chat_mode='plan')")
            connection.execute("DELETE FROM workspace_sessions WHERE chat_mode='plan'")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (11, utc_now()))
        connection.execute("COMMIT")

    @staticmethod
    def _migrate_v12(connection: sqlite3.Connection) -> None:
        """Unified Conversation model: session / turn / node / queue / lease /
        idempotency / outbox / tool-results / projection / receipts / approvals.

        Implements the gateway-unified-conversation design baseline:
        - conversation_sessions is the single persisted fact source for all
          origins (webui / channel / system).
        - turns + turn_nodes persist every run as the display & recovery
          authority; run state and history share the same rows (no copy).
        - outbox_events records every broadcast before it is published so
          "database commit happens before event broadcast".
        - All writes are idempotent via idempotency_records.
        """
        connection.executescript(_migration_script(12, """
CREATE TABLE IF NOT EXISTS conversation_sessions (
    conversation_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL,
    subtype TEXT NOT NULL,
    workspace_id TEXT,
    execution_scope TEXT NOT NULL,
    route_metadata TEXT NOT NULL DEFAULT '{}',
    session_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversation_scope_idx
    ON conversation_sessions(execution_scope);
CREATE INDEX IF NOT EXISTS conversation_workspace_idx
    ON conversation_sessions(workspace_id);

CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    turn_version INTEGER NOT NULL DEFAULT 0,
    runtime_snapshot_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    final_assistant_node_id TEXT,
    error_code TEXT,
    parent_conversation_id TEXT,
    parent_turn_id TEXT,
    FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(conversation_id)
);
CREATE INDEX IF NOT EXISTS turns_conversation_idx
    ON turns(conversation_id, started_at);
CREATE INDEX IF NOT EXISTS turns_active_idx
    ON turns(conversation_id, status);

CREATE TABLE IF NOT EXISTS turn_nodes (
    node_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn_id TEXT,
    type TEXT NOT NULL,
    position INTEGER,
    status TEXT NOT NULL,
    text TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    source_channel TEXT,
    source_message_id TEXT,
    sender_id TEXT,
    sender_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(conversation_id),
    FOREIGN KEY(turn_id) REFERENCES turns(turn_id)
);
CREATE INDEX IF NOT EXISTS turn_nodes_turn_idx
    ON turn_nodes(turn_id, position);
CREATE INDEX IF NOT EXISTS turn_nodes_conversation_idx
    ON turn_nodes(conversation_id, position);

CREATE TABLE IF NOT EXISTS queue_items (
    queue_item_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    text TEXT NOT NULL,
    target_turn_id TEXT,
    created_turn_id TEXT,
    created_node_id TEXT,
    operation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(conversation_id)
);
CREATE INDEX IF NOT EXISTS queue_items_conversation_idx
    ON queue_items(conversation_id, position);

CREATE TABLE IF NOT EXISTS idempotency_records (
    operation_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(conversation_id)
);
CREATE INDEX IF NOT EXISTS idempotency_conv_idx
    ON idempotency_records(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS outbox_events (
    outbox_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    published_at TEXT,
    FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(conversation_id)
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx
    ON outbox_events(published_at, created_at);
CREATE INDEX IF NOT EXISTS outbox_conversation_idx
    ON outbox_events(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS tool_results (
    result_ref TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    node_id TEXT,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    lines INTEGER NOT NULL DEFAULT 0,
    content_type TEXT,
    summary TEXT NOT NULL DEFAULT '{}',
    truncation_reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(conversation_id)
);
CREATE INDEX IF NOT EXISTS tool_results_turn_idx
    ON tool_results(turn_id);

CREATE TABLE IF NOT EXISTS channel_message_receipts (
    channel TEXT NOT NULL,
    message_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, message_id),
    FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(conversation_id)
);
CREATE INDEX IF NOT EXISTS receipts_conversation_idx
    ON channel_message_receipts(conversation_id);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    node_id TEXT,
    tool_name TEXT NOT NULL,
    params_summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(conversation_id)
);
CREATE INDEX IF NOT EXISTS approvals_turn_idx
    ON approvals(turn_id, status);
"""))

    @staticmethod
    def _migrate_v13(connection: sqlite3.Connection) -> None:
        """B1 契约①：turn_nodes 增加 text_seq（节点内流式 delta 单调递增序号）。

        流式 node.delta 事件携带 (delta, seq)，前端按 (node_id, seq) 追加；
        seq 落库保证重启/快照恢复后序号连续可复算。"""
        connection.execute("BEGIN IMMEDIATE")
        RuntimeStore._ensure_column(
            connection, "turn_nodes", "text_seq",
            "INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (13, utc_now()))
        connection.execute("COMMIT")

    @staticmethod
    def _migrate_v14(connection: sqlite3.Connection) -> None:
        """P2-4/P3-5：回填 artifacts 关联列，并补齐缺失索引。

        - save_artifact 自 v14 起同步维护 goal/plan/team/task/child_session
          关联列（此前 plan/team/child_session 列恒为 NULL），本迁移把存量行
          从 artifact_json 回填到列上（仅当列为 NULL 且 JSON 含对应键），
          使 delete_plan/delete_goal 能按索引列解除引用，不再在 BEGIN
          IMMEDIATE 写锁内做全表扫描+逐行 JSON 解析。
        - 补 tasks(status)、sessions(parent_session_id) 索引（既有
          tasks_session_status_idx 以 session_id 为前导列，无法服务纯 status
          查询；parent_session_id 自 v11 加列后一直无索引），另补
          artifacts(team_id) / artifacts(child_session_id) 供 delete_goal 的
          team / child_session 维度同样走索引。
        """
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS sessions_parent_session_idx ON sessions(parent_session_id)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS artifacts_team_idx ON artifacts(team_id)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS artifacts_child_session_idx ON artifacts(child_session_id)")
        # 回填与版本号写入同事务：任一步失败整体回滚（版本号不落库），
        # 下次启动幂等重试。IS NULL 守卫保证重跑不覆盖已解除引用的行。
        rows = connection.execute(
            "SELECT artifact_id, artifact_json, goal_id, plan_id, team_id, task_id, child_session_id "
            "FROM artifacts WHERE goal_id IS NULL OR plan_id IS NULL OR team_id IS NULL "
            "OR task_id IS NULL OR child_session_id IS NULL").fetchall()
        relation_keys = ("goal_id", "plan_id", "team_id", "task_id", "child_session_id")
        for row in rows:
            payload = RuntimeStore._load(row["artifact_json"], {})
            updates = [(column, payload.get(column)) for column in relation_keys
                       if row[column] is None and payload.get(column)]
            if not updates:
                continue
            assignments = ", ".join(f"{column}=?" for column, _ in updates)
            connection.execute(
                f"UPDATE artifacts SET {assignments} WHERE artifact_id=?",
                (*[value for _, value in updates], row["artifact_id"]))
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (14, utc_now()))
        connection.execute("COMMIT")

    @staticmethod
    def _migrate_v15(connection: sqlite3.Connection) -> None:
        """移除会话控制租约（设计方案 10 多标签页控制权已废弃）。

        control_leases 仅在"插入消息（Steering）"一处真正强制（prepare_steering
        无条件校验），而停止（前端不传 holder_id 即跳过）与旧审批桥
        （require_lease=False）实则绕过，前端也从未 acquireLease，导致该租约
        恒为"半实现"——插入消息必 409 lease_not_held。经确认无多标签页同时
        控制同一会话的真实需求，整体删除该表（及全部租约方法/API/前端）。
        并发防护仍由 exec_lock / execution_scope 并发上限承担，不受影响。

        leader_lease（调度器多实例选主）为独立表，本迁移不触碰。
        """
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE IF EXISTS control_leases")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (15, utc_now()))
        connection.execute("COMMIT")

    @staticmethod
    def _migrate_v16(connection: sqlite3.Connection) -> None:
        """队列载荷支持图片信封（修正版方案 A，2026-08）。

        queue_items 增加 images_json 列（一次性信封：出队执行时消费，消费后
        由 runner 转存 artifacts 并建 image 引用节点，信封本身不参与历史
        回放）。NULL = 纯文本消息，旧行语义不变。原文不持久化进 SQLite——
        历史中的图片以 artifacts 文件 + turn_nodes 引用节点承载。
        """
        connection.execute("BEGIN IMMEDIATE")
        RuntimeStore._ensure_column(
            connection, "queue_items", "images_json", "TEXT")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (16, utc_now()))
        connection.execute("COMMIT")
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        try:
            cols = {row[1] for row in connection.execute(
                f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.OperationalError:
            return
        if column not in cols:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load(value: Optional[str], default: Any) -> Any:
        return default if not value else json.loads(value)

    def upsert_session(self, session_id: str, session_key: str, *, channel: str = "",
                       status: str = "active", active_goal_id: Optional[str] = None,
                       active_plan_id: Optional[str] = None, team_id: Optional[str] = None,
                       parent_session_id: Optional[str] = None, origin: str = "gateway",
                       subagent_mode: Optional[str] = None, metadata: Optional[dict[str, Any]] = None) -> None:
        now = utc_now()
        metadata_json = self._dump(metadata or {})
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions(session_id, session_key, channel, status, active_goal_id,
                                     active_plan_id, team_id, parent_session_id, origin, subagent_mode, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    session_key=excluded.session_key, channel=excluded.channel,
                    status=excluded.status, active_goal_id=excluded.active_goal_id,
                    active_plan_id=excluded.active_plan_id, team_id=excluded.team_id,
                    parent_session_id=COALESCE(excluded.parent_session_id, sessions.parent_session_id),
                    origin=excluded.origin, subagent_mode=COALESCE(excluded.subagent_mode, sessions.subagent_mode),
                    updated_at=excluded.updated_at, metadata_json=excluded.metadata_json
                """,
                (session_id, session_key, channel, status, active_goal_id, active_plan_id, team_id,
                 parent_session_id, origin, subagent_mode, now, now, metadata_json),
            )


    def create_task(self, envelope: TaskEnvelope) -> tuple[TaskRecord, bool]:
        """Persist a queued task, returning an existing task for duplicate keys."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if envelope.idempotency_key:
                existing = connection.execute(
                    "SELECT record_json FROM tasks WHERE session_id=? AND idempotency_key=?",
                    (envelope.session_id, envelope.idempotency_key),
                ).fetchone()
                if existing:
                    return TaskRecord.from_dict(self._load(existing["record_json"], {})), False

            session = connection.execute(
                "SELECT session_id FROM sessions WHERE session_id=?", (envelope.session_id,)
            ).fetchone()
            if session is None:
                self._upsert_session_in_connection(connection, envelope.session_id, envelope.session_key)

            record = TaskRecord(task_id=envelope.task_id)
            record.transition(TaskStatus.QUEUED)
            now = utc_now()
            try:
                connection.execute(
                    """
                    INSERT INTO tasks(task_id, session_id, session_key, source, priority,
                                      idempotency_key, status, created_at, updated_at,
                                      envelope_json, record_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (envelope.task_id, envelope.session_id, envelope.session_key, envelope.source,
                     envelope.priority, envelope.idempotency_key, record.status.value,
                     envelope.created_at, now, self._dump(envelope.to_dict()), self._dump(record.to_dict())),
                )
            except sqlite3.IntegrityError:
                # 相同 task_id 已存在（幂等键未命中）：转为可读错误而非原始 IntegrityError。
                raise RuntimeError("任务ID已存在") from None
            self._insert_event_in_connection(
                connection, RuntimeEvent.create("task.created", session_id=envelope.session_id,
                                                task_id=envelope.task_id,
                                                data={"source": envelope.source}),
            )
            self._insert_event_in_connection(
                connection, RuntimeEvent.create("task.queued", session_id=envelope.session_id,
                                                task_id=envelope.task_id),
            )
            return record, True

    def get_task(self, task_id: str) -> Optional[TaskSnapshot]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._snapshot_from_row(row) if row else None

    def list_tasks(self, *, session_id: Optional[str] = None,
                   statuses: Optional[set[TaskStatus]] = None) -> list[TaskSnapshot]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        clauses: list[str] = []
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(item.value for item in statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY priority DESC, created_at ASC"
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def has_active_work(self, *, session_id: str, sources: set, statuses: set) -> bool:
        """L4#10：EXISTS 探针——同会话是否存在指定来源/状态的活动任务。

        供 goal driver 轮询使用，避免 list_tasks 实体化整行列表。
        """
        source_ph = ",".join("?" for _ in sources)
        status_ph = ",".join("?" for _ in statuses)
        sql = (
            "SELECT 1 FROM tasks WHERE session_id=? "
            f"AND source IN ({source_ph}) AND status IN ({status_ph}) LIMIT 1"
        )
        params = [session_id, *[str(s) for s in sources], *[s.value for s in statuses]]
        with self._connection() as connection:
            return connection.execute(sql, params).fetchone() is not None

    def transition_task(self, task_id: str, target: TaskStatus, *, lease_owner: Optional[str] = None,
                        lease_expires_at: Optional[str] = None,
                        expected_status: Optional[TaskStatus] = None,
                        error_code: Optional[str] = None, error_message: Optional[str] = None,
                        result: Optional[TaskResult] = None) -> TaskRecord:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise RuntimeStoreError(f"task not found: {task_id}")
            snapshot = self._snapshot_from_row(row)
            record = snapshot.record
            if expected_status is not None and snapshot.record.status != expected_status:
                # 多实例防双跑：BEGIN IMMEDIATE 已拿到写锁，此处读到的就是
                # 当前权威状态；任务已被其他实例推进（如 QUEUED→LEASED）时
                # 放弃本次迁移（调用方 _transition 捕获 ValueError 后跳过）。
                raise ValueError(
                    f"task {task_id} status is {snapshot.record.status.value}, "
                    f"expected {expected_status.value} (concurrent instance)")
            if lease_owner:
                record.lease_owner = lease_owner
            if lease_expires_at:
                record.lease_expires_at = lease_expires_at
            summary = result.summary if result else None
            record.transition(target, error_code=error_code,
                              error_message=error_message, result_summary=summary)
            if result:
                record.artifact_ids = list(result.artifact_ids)
                record.usage = dict(result.usage)
            now = utc_now()
            sql = "UPDATE tasks SET status=?, updated_at=?, record_json=?, result_json=? WHERE task_id=?"
            params: list[Any] = [record.status.value, now, self._dump(record.to_dict()),
                                 self._dump(result.to_dict()) if result else row["result_json"], task_id]
            if expected_status is not None:
                sql += " AND status=?"
                params.append(expected_status.value)
            connection.execute(sql, params)
            self._insert_event_in_connection(
                connection,
                RuntimeEvent.create(f"task.{target.value}", session_id=snapshot.envelope.session_id,
                                    task_id=task_id, data={"error_code": error_code or ""}),
            )
            return record

    def requeue_missing_context(self, *, sources: Optional[set[str]] = None,
                                owner: str | None = None) -> list[TaskSnapshot]:
        """Reopen restart-blocked tasks whose source is safe to replay.

        Tasks blocked for permission, validation, or execution errors remain
        terminal. ``sources`` lets Gateway restrict recovery to producers
        with a durable delivery context (currently scheduler and plans).

        ``owner``（本实例 instance_id）用于多实例安全：跳过其他实例持有且
        租约未过期的任务（BLOCKED 任务通常无租约，此检查为未来流程兜底），
        认领时把 lease_owner 标记为自己（防双领 / 归属审计）。
        """
        recovered: list[TaskSnapshot] = []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status=?", (TaskStatus.BLOCKED.value,)
            ).fetchall()
            for row in rows:
                snapshot = self._snapshot_from_row(row)
                record = snapshot.record
                if record.error_code != "RUNTIME_CONTEXT_MISSING":
                    continue
                if sources is not None and snapshot.envelope.source not in sources:
                    continue
                if not self._recover_eligible(record, owner=owner, lease_grace_seconds=0.0):
                    continue
                record.transition(TaskStatus.QUEUED)
                if owner:
                    record.lease_owner = owner  # 认领标记：防双领 + 归属审计
                connection.execute(
                    "UPDATE tasks SET status=?, updated_at=?, record_json=?, result_json=NULL WHERE task_id=?",
                    (record.status.value, utc_now(), self._dump(record.to_dict()), record.task_id),
                )
                self._insert_event_in_connection(
                    connection,
                    RuntimeEvent.create("task.requeued", session_id=snapshot.envelope.session_id,
                                        task_id=record.task_id,
                                        data={"reason": "runtime_context_restore"}),
                )
                recovered.append(self._snapshot_from_row(
                    connection.execute("SELECT * FROM tasks WHERE task_id=?", (record.task_id,)).fetchone()))
        return recovered

    def increment_attempt(self, task_id: str) -> TaskRecord:
        """Persist one execution attempt before an executor is invoked."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise RuntimeStoreError(f"task not found: {task_id}")
            snapshot = self._snapshot_from_row(row)
            record = snapshot.record
            record.attempt += 1
            connection.execute(
                "UPDATE tasks SET updated_at=?, record_json=? WHERE task_id=?",
                (utc_now(), self._dump(record.to_dict()), task_id),
            )
            self._insert_event_in_connection(
                connection, RuntimeEvent.create("task.attempt_started", session_id=snapshot.envelope.session_id,
                                                task_id=task_id, data={"attempt": record.attempt}),
            )
            return record

    def request_cancel(self, task_id: str, reason: str = "user_requested") -> TaskRecord:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise RuntimeStoreError(f"task not found: {task_id}")
            snapshot = self._snapshot_from_row(row)
            record = snapshot.record
            record.cancel_requested = True
            if record.status in {TaskStatus.CREATED, TaskStatus.QUEUED, TaskStatus.WAITING_APPROVAL,
                                 TaskStatus.WAITING_DEPENDENCY, TaskStatus.RETRY_WAIT}:
                record.transition(TaskStatus.CANCELLED, error_code="TASK_CANCELLED", error_message=reason)
            connection.execute(
                "UPDATE tasks SET status=?, updated_at=?, record_json=? WHERE task_id=?",
                (record.status.value, utc_now(), self._dump(record.to_dict()), task_id),
            )
            self._insert_event_in_connection(
                connection, RuntimeEvent.create("task.cancel_requested", session_id=snapshot.envelope.session_id,
                                                task_id=task_id, data={"reason": reason}),
            )
            return record

    def recover_interrupted(self, *, requeue: bool = True, owner: str | None = None,
                            lease_grace_seconds: float = 0.0) -> list[str]:
        """Mark stale leased/running tasks interrupted; optionally requeue them.

        只恢复「租约已过期」或「属于本进程(owner 匹配)」的任务；历史数据
        未写租约字段时保持原恢复语义，避免重启后任务永久滞留。
        """
        recovered: list[str] = []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?)",
                (TaskStatus.LEASED.value, TaskStatus.RUNNING.value),
            ).fetchall()
            for row in rows:
                snapshot = self._snapshot_from_row(row)
                record = snapshot.record
                if not self._recover_eligible(record, owner=owner,
                                              lease_grace_seconds=lease_grace_seconds):
                    continue
                record.transition(TaskStatus.INTERRUPTED, error_code="RUNTIME_INTERRUPTED",
                                  error_message="runtime restarted before task completed")
                self._insert_event_in_connection(
                    connection, RuntimeEvent.create("task.interrupted", session_id=snapshot.envelope.session_id,
                                                    task_id=record.task_id),
                )
                if requeue:
                    record.transition(TaskStatus.QUEUED)
                    self._insert_event_in_connection(
                        connection, RuntimeEvent.create("task.queued", session_id=snapshot.envelope.session_id,
                                                        task_id=record.task_id,
                                                        data={"reason": "runtime_recovery"}),
                    )
                if owner:
                    # 认领标记：QUEUED 迁移会清空租约字段，这里在迁移后补写
                    # 自己的 lease_owner——防双领（其他实例对同一任务不再认领）
                    # + 归属审计。下次执行时 _run_queued 会写入新的租约。
                    record.lease_owner = owner
                connection.execute(
                    "UPDATE tasks SET status=?, updated_at=?, record_json=? WHERE task_id=?",
                    (record.status.value, utc_now(), self._dump(record.to_dict()), record.task_id),
                )
                recovered.append(record.task_id)
        return recovered

    @staticmethod
    def _recover_eligible(record: TaskRecord, *, owner: str | None,
                          lease_grace_seconds: float) -> bool:
        """判断一个 LEASED/RUNNING/RETRY_WAIT 任务是否应被本实例认领。

        多实例规则：
        - 本实例持有的租约（owner 匹配，含 owner-N 格式）→ 认领；
        - 其他实例持有且租约未过期 → 跳过（防双跑 / 防双领）；
        - 其他实例持有但租约已过期（含优雅停机 release_leases 置为过期）
          → 可认领；
        - 有 lease_owner 但无 lease_expires_at：旧版本遗留行（升级兼容），
          新代码写出的任务必定带租约到期时间，视为可认领；
        - 无租约字段的历史数据 → 保持原恢复语义。
        """
        if owner and record.lease_owner:
            if record.lease_owner == owner or record.lease_owner.startswith(f"{owner}-"):
                return True
            # 其他实例持有：仅当租约已过期才允许认领。
            if record.lease_expires_at:
                try:
                    expires = datetime.fromisoformat(record.lease_expires_at.replace("Z", "+00:00"))
                except ValueError:
                    return False  # 无法解析 → fail-closed 不抢占
                return expires < datetime.now(timezone.utc) - timedelta(
                    seconds=max(0.0, float(lease_grace_seconds)))
            # 有 owner 无租约到期时间：旧版本写出的行，升级兼容视为可认领。
            return True
        if record.lease_expires_at:
            try:
                expires = datetime.fromisoformat(record.lease_expires_at.replace("Z", "+00:00"))
            except ValueError:
                return False
            return expires < datetime.now(timezone.utc) - timedelta(
                seconds=max(0.0, float(lease_grace_seconds)))
        # 历史数据无租约字段：保持原恢复语义（单实例升级兼容）。
        return True

    def save_plan(self, plan: dict[str, Any], event: RuntimeEvent) -> None:
        """Persist a Plan snapshot, all current PlanTasks, and one event atomically."""
        required = {"plan_id", "session_id", "status", "version", "created_at", "updated_at", "tasks"}
        missing = required.difference(plan)
        if missing:
            raise ValueError(f"plan is missing required fields: {', '.join(sorted(missing))}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO plans(plan_id, session_id, goal_id, status, version, created_at, updated_at, plan_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at, plan_json=excluded.plan_json
                """,
                (plan["plan_id"], plan["session_id"], plan.get("goal_id"), plan["status"], plan["version"],
                 plan["created_at"], plan["updated_at"], self._dump(plan)),
            )
            for task in plan["tasks"]:
                connection.execute(
                    """
                    INSERT INTO plan_tasks(plan_task_id, plan_id, status, task_id, created_at, updated_at, task_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plan_id, plan_task_id) DO UPDATE SET
                        status=excluded.status, task_id=excluded.task_id,
                        updated_at=excluded.updated_at, task_json=excluded.task_json
                    """,
                    (task["plan_task_id"], plan["plan_id"], task["status"], task.get("task_id"),
                     task["created_at"], task["updated_at"], self._dump(task)),
                )
            self._insert_event_in_connection(connection, event)

    def get_plan(self, plan_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT plan_json FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return self._load(row["plan_json"], {}) if row else None

    def list_plans(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT plan_json FROM plans WHERE session_id=? ORDER BY updated_at DESC LIMIT ?",
                (session_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._load(row["plan_json"], {}) for row in rows]

    def list_plans_by_status(self, statuses: set[str], *, limit: int = 1000) -> list[dict[str, Any]]:
        """List durable Plans that need runtime recovery."""
        if not statuses:
            return []
        values = sorted(str(status) for status in statuses)
        placeholders = ",".join("?" for _ in values)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT plan_json FROM plans WHERE status IN ({placeholders}) "
                "ORDER BY updated_at ASC LIMIT ?",
                (*values, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._load(row["plan_json"], {}) for row in rows]

    def delete_plan(self, plan_id: str) -> bool:
        """Remove one Plan and its PlanTask rows.

        Called when the user clears a terminal Plan.  The conversation/memory
        history keeps the human-visible content; only the runtime business
        record (state machine and per-task linkage) is deleted.
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # v14 起 save_artifact 同步维护 plan_id 关联列（迁移已回填存量行），
            # 按其索引前导列定位关联行，替代旧的全表扫描+逐行 JSON 过滤。
            rows = connection.execute(
                "SELECT artifact_id, artifact_json FROM artifacts WHERE plan_id=?",
                (plan_id,)).fetchall()
            for row in rows:
                payload = self._load(row["artifact_json"], {})
                # 同步解除 plan_task_id 引用：父 Plan 已删除，_is_referenced
                # 不能继续 fail-closed 保护，否则回收永远不生效。
                payload.pop("plan_id", None)
                payload.pop("plan_task_id", None)
                connection.execute(
                    "UPDATE artifacts SET plan_id=NULL, plan_task_id=NULL, artifact_json=? WHERE artifact_id=?",
                    (self._dump(payload), row["artifact_id"]))
            connection.execute("DELETE FROM plan_tasks WHERE plan_id=?", (plan_id,))
            changed = connection.execute(
                "DELETE FROM plans WHERE plan_id=?", (plan_id,)).rowcount
            connection.execute(
                "UPDATE sessions SET active_plan_id=NULL, updated_at=? WHERE active_plan_id=?",
                (utc_now(), plan_id))
        return changed > 0
    def save_team(self, team: dict[str, Any], event: RuntimeEvent) -> None:
        required = {"team_id", "goal_id", "session_id", "status", "created_at", "updated_at"}
        missing = required.difference(team)
        if missing:
            raise ValueError(f"team is missing required fields: {', '.join(sorted(missing))}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO teams(team_id, goal_id, session_id, status, created_at, updated_at, team_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at, team_json=excluded.team_json
                """,
                (team["team_id"], team["goal_id"], team["session_id"], team["status"],
                 team["created_at"], team["updated_at"], self._dump(team)),
            )
            self._insert_event_in_connection(connection, event)

    def get_team(self, team_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT team_json FROM teams WHERE team_id=?", (team_id,)).fetchone()
        return self._load(row["team_json"], {}) if row else None

    def list_teams(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT team_json FROM teams WHERE session_id=? ORDER BY updated_at DESC LIMIT ?",
                (session_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._load(row["team_json"], {}) for row in rows]

    def save_team_member(self, member: dict[str, Any], event: RuntimeEvent) -> None:
        required = {"agent_id", "team_id", "status", "parent_agent_id", "created_at", "updated_at"}
        missing = required.difference(member)
        if missing:
            raise ValueError(f"team member is missing required fields: {', '.join(sorted(missing))}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO team_members(agent_id, team_id, status, parent_agent_id, created_at, updated_at, member_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at, member_json=excluded.member_json
                """,
                (member["agent_id"], member["team_id"], member["status"], member["parent_agent_id"],
                 member["created_at"], member["updated_at"], self._dump(member)),
            )
            self._insert_event_in_connection(connection, event)

    def create_team_member(self, member: dict[str, Any], event: RuntimeEvent,
                           *, max_children: int) -> None:
        """Insert a child once, enforcing the Team's single-layer capacity atomically."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) FROM team_members WHERE team_id=?", (member["team_id"],)
            ).fetchone()[0]
            if count >= max_children:
                raise RuntimeStoreError("team child limit has been reached")
            connection.execute(
                """INSERT INTO team_members(agent_id, team_id, status, parent_agent_id, created_at, updated_at, member_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (member["agent_id"], member["team_id"], member["status"], member["parent_agent_id"],
                 member["created_at"], member["updated_at"], self._dump(member)),
            )
            self._insert_event_in_connection(connection, event)
    def get_team_member(self, agent_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT member_json FROM team_members WHERE agent_id=?", (agent_id,)).fetchone()
        return self._load(row["member_json"], {}) if row else None

    def list_team_members(self, team_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT member_json FROM team_members WHERE team_id=? ORDER BY created_at ASC LIMIT ?",
                (team_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._load(row["member_json"], {}) for row in rows]

    def list_all_team_members(self, *, limit: int = 1000, offset: int = 0) -> list[dict[str, Any]]:
        """分页扫描全部 team_members（Subagent 重启对账用）。"""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT member_json FROM team_members ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (max(1, min(limit, 1000)), max(0, int(offset))),
            ).fetchall()
        return [self._load(row["member_json"], {}) for row in rows]

    def save_goal(self, goal: dict[str, Any], event: RuntimeEvent) -> None:
        """Persist one Goal snapshot and its state-change event atomically."""
        required = {"goal_id", "session_id", "status", "title", "created_at", "updated_at"}
        missing = required.difference(goal)
        if missing:
            raise ValueError(f"goal is missing required fields: {', '.join(sorted(missing))}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO goals(goal_id, session_id, status, title, created_at, updated_at, goal_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET
                    session_id=excluded.session_id, status=excluded.status, title=excluded.title,
                    updated_at=excluded.updated_at, goal_json=excluded.goal_json
                """,
                (goal["goal_id"], goal["session_id"], goal["status"], goal["title"],
                 goal["created_at"], goal["updated_at"], self._dump(goal)),
            )
            session = connection.execute(
                "SELECT active_goal_id FROM sessions WHERE session_id=?", (goal["session_id"],)
            ).fetchone()
            if session is not None:
                if goal["status"] == "active":
                    connection.execute(
                        "UPDATE sessions SET active_goal_id=?, updated_at=? WHERE session_id=?",
                        (goal["goal_id"], utc_now(), goal["session_id"]),
                    )
                elif session["active_goal_id"] == goal["goal_id"]:
                    connection.execute(
                        "UPDATE sessions SET active_goal_id=NULL, updated_at=? WHERE session_id=?",
                        (utc_now(), goal["session_id"]),
                    )
            self._insert_event_in_connection(connection, event)

    def get_goal(self, goal_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT goal_json FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
        return self._load(row["goal_json"], {}) if row else None

    def list_goals(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT goal_json FROM goals WHERE session_id=? ORDER BY updated_at DESC LIMIT ?",
                (session_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._load(row["goal_json"], {}) for row in rows]
    def list_goals_by_status(self, statuses: set[str], *, limit: int = 1000) -> list[dict[str, Any]]:
        if not statuses:
            return []
        values = sorted(str(status) for status in statuses)
        placeholders = ",".join("?" for _ in values)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT goal_json FROM goals WHERE status IN ({placeholders}) ORDER BY updated_at ASC LIMIT ?",
                (*values, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._load(row["goal_json"], {}) for row in rows]

    def delete_goal(self, goal_id: str) -> bool:
        """Remove one Goal and its dependent Plans/Teams.

        Called when the user clears a terminal Goal.  Conversation/memory keep
        the objective and history; the runtime business records (Goal state
        machine, linked Plans, single-layer Team) are deleted atomically.
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan_ids = [row[0] for row in connection.execute(
                "SELECT plan_id FROM plans WHERE goal_id=?", (goal_id,)).fetchall()]
            team_ids = [row[0] for row in connection.execute(
                "SELECT team_id FROM teams WHERE goal_id=?", (goal_id,)).fetchall()]
            member_ids: list[str] = []
            if team_ids:
                placeholders = ",".join("?" for _ in team_ids)
                member_ids = [row[0] for row in connection.execute(
                    f"SELECT agent_id FROM team_members WHERE team_id IN ({placeholders})", team_ids).fetchall()]
            # v14 起 save_artifact 同步维护关联列（迁移已回填存量行），按各
            # 索引列的并集定位候选行，替代旧的全表扫描+逐行 JSON 过滤；
            # 行内仍按 JSON 键逐一解除，保持与列更新一致。
            clauses = ["goal_id=?"]
            artifact_params: list[Any] = [goal_id]
            for column, values in (("plan_id", plan_ids), ("team_id", team_ids),
                                   ("child_session_id", member_ids)):
                if values:
                    placeholders = ",".join("?" for _ in values)
                    clauses.append(f"{column} IN ({placeholders})")
                    artifact_params.extend(values)
            rows = connection.execute(
                "SELECT artifact_id, artifact_json FROM artifacts WHERE " + " OR ".join(clauses),
                artifact_params).fetchall()
            for row in rows:
                payload = self._load(row["artifact_json"], {})
                changed = False
                if payload.get("goal_id") == goal_id:
                    payload.pop("goal_id", None); changed = True
                if payload.get("plan_id") in plan_ids:
                    payload.pop("plan_id", None); changed = True
                    # 父 Plan 随 Goal 删除：同步解除其 plan_task_id 引用。
                    payload.pop("plan_task_id", None)
                if payload.get("team_id") in team_ids:
                    payload.pop("team_id", None); changed = True
                if payload.get("child_session_id") in member_ids:
                    payload.pop("child_session_id", None); changed = True
                if changed:
                    connection.execute(
                        "UPDATE artifacts SET goal_id=?, plan_id=?, plan_task_id=?, team_id=?, child_session_id=?, artifact_json=? WHERE artifact_id=?",
                        (payload.get("goal_id"), payload.get("plan_id"), payload.get("plan_task_id"),
                         payload.get("team_id"), payload.get("child_session_id"),
                         self._dump(payload), row["artifact_id"]))
            for plan_id in plan_ids:
                connection.execute("DELETE FROM plan_tasks WHERE plan_id=?", (plan_id,))
                connection.execute("DELETE FROM plans WHERE plan_id=?", (plan_id,))
                connection.execute(
                    "UPDATE sessions SET active_plan_id=NULL, updated_at=? WHERE active_plan_id=?",
                    (utc_now(), plan_id))
            team_ids = [row[0] for row in connection.execute(
                "SELECT team_id FROM teams WHERE goal_id=?", (goal_id,)).fetchall()]
            for team_id in team_ids:
                connection.execute("DELETE FROM team_messages WHERE team_id=?", (team_id,))
                connection.execute("DELETE FROM team_members WHERE team_id=?", (team_id,))
                connection.execute("DELETE FROM teams WHERE team_id=?", (team_id,))
            changed = connection.execute(
                "DELETE FROM goals WHERE goal_id=?", (goal_id,)).rowcount
            connection.execute(
                "UPDATE sessions SET active_goal_id=NULL, updated_at=? WHERE active_goal_id=?",
                (utc_now(), goal_id))
        return changed > 0

    def save_artifact(self, artifact: dict[str, Any], event: RuntimeEvent) -> None:
        required = {"artifact_id", "session_id", "created_at", "path", "name", "sha256", "size"}
        missing = required.difference(artifact)
        if missing:
            raise ValueError(f"artifact is missing required fields: {', '.join(sorted(missing))}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_session_in_connection(connection, artifact["session_id"], artifact["session_id"])
            # v14 起同步维护关联列（此前 plan/team/child_session 列恒为 NULL，
            # 迫使 delete_plan/delete_goal 在写锁内全表扫描 JSON 过滤）；冲突
            # 分支一并刷新，保证关联列与 artifact_json 恒一致。
            connection.execute(
                """
                INSERT INTO artifacts(artifact_id, session_id, goal_id, plan_id, team_id,
                                      task_id, child_session_id, created_at, artifact_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    goal_id=excluded.goal_id, plan_id=excluded.plan_id,
                    team_id=excluded.team_id, task_id=excluded.task_id,
                    child_session_id=excluded.child_session_id,
                    artifact_json=excluded.artifact_json
                """,
                (artifact["artifact_id"], artifact["session_id"], artifact.get("goal_id"),
                 artifact.get("plan_id"), artifact.get("team_id"), artifact.get("task_id"),
                 artifact.get("child_session_id"), artifact["created_at"], self._dump(artifact)),
            )
            self._insert_event_in_connection(connection, event)

    def get_artifact(self, artifact_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT artifact_json FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return self._load(row["artifact_json"], {}) if row else None

    def list_artifacts(self, *, session_id: Optional[str] = None, goal_id: Optional[str] = None,
                       task_id: Optional[str] = None, limit: int = 100,
                       offset: int = 0) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if goal_id:
            clauses.append("goal_id=?")
            params.append(goal_id)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        query = "SELECT artifact_json FROM artifacts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(limit, 1000)), max(0, int(offset))])
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._load(row["artifact_json"], {}) for row in rows]

    def delete_artifact(self, artifact_id: str) -> bool:
        """Delete one Artifact metadata row after its file is safely removed."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return connection.execute("DELETE FROM artifacts WHERE artifact_id=?", (artifact_id,)).rowcount == 1

    def delete_terminal_task(self, task_id: str) -> bool:
        """Delete an already-terminal task and its dependent delivery/audit rows."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["status"] not in {item.value for item in TaskStatus.terminal()}:
                return False
            connection.execute("DELETE FROM channel_delivery WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM runtime_events WHERE task_id=?", (task_id,))
            return connection.execute("DELETE FROM tasks WHERE task_id=?", (task_id,)).rowcount == 1

    def delete_stale_sessions(self, cutoff_iso: str) -> int:
        """Delete sessions last updated before cutoff_iso that no longer have
        any referencing rows in the task domain. Returns the deleted row count.

        tasks.session_id 与 artifacts.session_id 均以 FOREIGN KEY 引用本表，
        且连接统一开启 PRAGMA foreign_keys=ON（默认 RESTRICT 语义）：直接删
        被引用行会抛 IntegrityError，故单条 SQL 用 NOT EXISTS 先确认无引用
        再删。goals/plans/teams 虽携带 session_id 但未声明 FK，不阻止清理。
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM sessions
                WHERE updated_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM tasks
                      WHERE tasks.session_id = sessions.session_id)
                  AND NOT EXISTS (
                      SELECT 1 FROM artifacts
                      WHERE artifacts.session_id = sessions.session_id)
                """,
                (cutoff_iso,),
            )
            return int(cursor.rowcount or 0)

    def save_channel_delivery(self, *, delivery_id: str, task_id: str, channel: str,
                              message_id: str | None, state: str, context: dict[str, Any]) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sql = ("INSERT INTO channel_delivery(delivery_id, task_id, channel, message_id, state, context_json, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                   "ON CONFLICT(delivery_id) DO UPDATE SET state=excluded.state, context_json=excluded.context_json, updated_at=excluded.updated_at")
            connection.execute(sql, (delivery_id, task_id, channel, message_id, state, self._dump(context), now, now))

    def list_channel_deliveries(self, *, states: set[str] | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        query = "SELECT * FROM channel_delivery"
        params: list[Any] = []
        if states:
            values = sorted(str(state) for state in states)
            query += " WHERE state IN (" + ",".join("?" for _ in values) + ")"
            params.extend(values)
        query += " ORDER BY updated_at ASC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [{"delivery_id": row["delivery_id"], "task_id": row["task_id"], "channel": row["channel"],
                 "message_id": row["message_id"], "state": row["state"],
                 "context": self._load(row["context_json"], {}), "created_at": row["created_at"],
                 "updated_at": row["updated_at"]} for row in rows]

    def append_event(self, event: RuntimeEvent) -> None:
        with self._connection() as connection:
            self._insert_event_in_connection(connection, event)

    def list_events(self, *, task_id: Optional[str] = None, session_id: Optional[str] = None,
                    after_event_id: Optional[str] = None, limit: int = 200) -> list[RuntimeEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if after_event_id:
            row_query = "SELECT rowid FROM runtime_events WHERE event_id=?"
            with self._connection() as connection:
                cursor = connection.execute(row_query, (after_event_id,)).fetchone()
            if cursor:
                clauses.append("rowid > ?")
                params.append(cursor["rowid"])
        query = "SELECT * FROM runtime_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY rowid ASC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [RuntimeEvent(event_id=row["event_id"], event_type=row["event_type"],
                             created_at=row["created_at"], session_id=row["session_id"],
                             task_id=row["task_id"], run_id=row["run_id"], sequence=row["sequence"],
                             data=self._load(row["data_json"], {})) for row in rows]

    def list_child_sessions(self, parent_session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT session_id, session_key, origin, subagent_mode, status, created_at, updated_at FROM sessions WHERE parent_session_id=? ORDER BY updated_at DESC LIMIT ?",
                (parent_session_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def _upsert_session_in_connection(self, connection: sqlite3.Connection,
                                      session_id: str, session_key: str) -> None:
        now = utc_now()
        connection.execute(
            "INSERT INTO sessions(session_id, session_key, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
            (session_id, session_key, now, now),
        )

    def _insert_event_in_connection(self, connection: sqlite3.Connection, event: RuntimeEvent) -> None:
        connection.execute(
            """INSERT INTO runtime_events(event_id, event_type, created_at, session_id,
                                            task_id, run_id, sequence, data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.event_id, event.event_type, event.created_at, event.session_id,
             event.task_id, event.run_id, event.sequence, self._dump(event.data)),
        )

    def _snapshot_from_row(self, row: sqlite3.Row) -> TaskSnapshot:
        envelope = TaskEnvelope.from_dict(self._load(row["envelope_json"], {}))
        record = TaskRecord.from_dict(self._load(row["record_json"], {}))
        result = TaskResult.from_dict(self._load(row["result_json"], {})) if row["result_json"] else None
        return TaskSnapshot(envelope=envelope, record=record, result=result)
    @staticmethod
    def _normalize_retired_profile_chat_modes(connection: sqlite3.Connection) -> None:
        """Normalize legacy AgentProfile chat modes after Plan-mode retirement."""
        try:
            now = utc_now()
            connection.execute(
                "UPDATE agent_profiles SET chat_mode='chat', version=version+1, "
                "updated_at=? WHERE chat_mode <> 'chat'",
                (now,),
            )
            connection.execute(
                "UPDATE workspaces SET chat_mode='chat', version=version+1, "
                "updated_at=? WHERE chat_mode <> 'chat'",
                (now,),
            )
        except sqlite3.OperationalError:
            # The table is created by the workspace migration; pre-workspace
            # databases legitimately do not have it yet.
            return

    @staticmethod
    def _ensure_profile_is_system_column(connection: sqlite3.Connection) -> None:
        """幂等补列：老 v9 库的 agent_profiles 没有 is_system（Phase 6 内置模板）。"""
        try:
            cols = {r[1] for r in connection.execute(
                "PRAGMA table_info(agent_profiles)").fetchall()}
        except sqlite3.OperationalError:
            return  # 表不存在（旧版本库尚未建表，后续 migration 会建）
        if "is_system" not in cols:
            connection.execute(
                "ALTER TABLE agent_profiles ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _ensure_multi_instance_schema(connection: sqlite3.Connection) -> None:
        """多实例就绪的共享 schema（幂等，不提升 SCHEMA_VERSION）。

        - leader_lease：scheduler/heartbeat 等单点执行者的 SQLite 租约表
          （INSERT ... ON CONFLICT 条件抢占，见 try_acquire_lease）。
        - outbox_events.claimed_by / claim_expires_at：Outbox 补发认领列，
          防止多实例对同一未发布事件重复广播（见 gateway/conversation/outbox.py）。
        """
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS leader_lease (
                name TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        RuntimeStore._ensure_column(connection, "outbox_events", "claimed_by", "TEXT")
        RuntimeStore._ensure_column(connection, "outbox_events", "claim_expires_at", "TEXT")

    def try_acquire_lease(self, name: str, holder: str, ttl_seconds: float) -> bool:
        """原子抢占/续租 leader_lease（多实例选主，防双跑）。

        语义（单条 SQL 条件 upsert，事务内完成）：
        - 无记录 → 插入并持有；
        - 持有者是自己 → 续租（刷新 expires_at）；
        - 持有者是别人且租约未过期 → 不抢占（返回 False，本轮跳过）；
        - 持有者是别人且租约已过期 → 抢占成功（返回 True）。

        expires_at 与 utc_now() 同为毫秒级 +00:00 ISO 文本，可直接字典序
        比较。返回 True 表示调用方在本租约期内是唯一执行者。
        """
        ttl = max(1.0, float(ttl_seconds))
        now = utc_now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl)
                   ).isoformat(timespec="milliseconds")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO leader_lease(name, holder, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    holder=excluded.holder, expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                WHERE leader_lease.holder = excluded.holder
                   OR leader_lease.expires_at < ?
                """,
                (name, holder, expires, now, now),
            )
            return cursor.rowcount == 1

    def get_lease(self, name: str) -> Optional[dict[str, Any]]:
        """读取当前租约（状态展示 / 运维诊断用）。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT name, holder, expires_at, updated_at FROM leader_lease WHERE name=?",
                (name,),
            ).fetchone()
        return dict(row) if row else None

    def release_leases(self, *, owner: str | None = None) -> int:
        """优雅停机：把本实例持有的 LEASED/RUNNING 任务租约置为立即过期。

        重启后 recover_interrupted 会把它们当作「过期租约」快速认领；
        崩溃（未调用本方法）的任务则要等租约自然过期（TTL）才会被认领——
        这是多实例防双跑的固有代价，见 docs/multi-instance.md。
        """
        count = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?)",
                (TaskStatus.LEASED.value, TaskStatus.RUNNING.value),
            ).fetchall()
            now = utc_now()
            for row in rows:
                record = self._snapshot_from_row(row).record
                if owner and record.lease_owner and not (
                        record.lease_owner == owner
                        or record.lease_owner.startswith(owner + "-")):
                    continue
                record.lease_expires_at = now  # 立即过期（保留 holder 供审计）
                connection.execute(
                    "UPDATE tasks SET record_json=?, updated_at=? WHERE task_id=?",
                    (self._dump(record.to_dict()), now, record.task_id),
                )
                count += 1
        return count

    def claim_retry_wait(self, *, owner: str | None = None,
                         lease_grace_seconds: float = 0.0) -> list[TaskSnapshot]:
        """原子认领 RETRY_WAIT 任务并置为 QUEUED（多实例防重复入队重试）。

        只认领「本实例所有」或「租约已过期」的重试任务；其他实例持有的
        活租约重试任务跳过（由持有实例自己的 _retry_after 定时器负责回队），
        避免两个实例把同一重试任务各入队一次造成双跑。
        """
        recovered: list[TaskSnapshot] = []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status=?",
                (TaskStatus.RETRY_WAIT.value,),
            ).fetchall()
            for row in rows:
                snapshot = self._snapshot_from_row(row)
                record = snapshot.record
                if not self._recover_eligible(record, owner=owner,
                                              lease_grace_seconds=lease_grace_seconds):
                    continue
                record.transition(TaskStatus.QUEUED)
                if owner:
                    record.lease_owner = owner  # 认领标记：防双领 + 归属审计
                connection.execute(
                    "UPDATE tasks SET status=?, updated_at=?, record_json=? WHERE task_id=?",
                    (record.status.value, utc_now(), self._dump(record.to_dict()),
                     record.task_id),
                )
                self._insert_event_in_connection(
                    connection,
                    RuntimeEvent.create("task.queued", session_id=snapshot.envelope.session_id,
                                        task_id=record.task_id,
                                        data={"reason": "retry_claim"}),
                )
                recovered.append(self._snapshot_from_row(
                    connection.execute(
                        "SELECT * FROM tasks WHERE task_id=?", (record.task_id,)).fetchone()))
        return recovered