"""SQLite-backed durable state for the unified runtime.

Transcript persistence intentionally stays in MessageStore during migration.
This store owns task/session runtime state and append-only domain events.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import RuntimeEvent, TaskEnvelope, TaskRecord, TaskResult, TaskStatus, utc_now


class RuntimeStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskSnapshot:
    envelope: TaskEnvelope
    record: TaskRecord
    result: Optional[TaskResult] = None


class RuntimeStore:
    """Small transactional store with one connection per operation."""

    SCHEMA_VERSION = 8

    def __init__(self, path: str | Path, *, wal: bool = True,
                 busy_timeout_ms: int = 5000):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.wal = bool(wal)
        try:
            self.busy_timeout_ms = max(0, int(busy_timeout_ms))
        except (TypeError, ValueError):
            self.busy_timeout_ms = 5000
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1000.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
            if current < 1:
                self._migrate_v1(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, utc_now()),
                )
            if current < 2:
                self._migrate_v2(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, utc_now()),
                )
            if current < 3:
                self._migrate_v3(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, utc_now()),
                )
            if current < 4:
                self._migrate_v4(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, utc_now()),
                )
            if current < 5:
                self._migrate_v5(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (5, utc_now()),
                )
            if current < 6:
                self._migrate_v6(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (6, utc_now()),
                )
            if current < 7:
                self._migrate_v7(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (7, utc_now()),
                )
            if current < 8:
                self._migrate_v8(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (8, utc_now()),
                )

    @staticmethod
    def _migrate_v1(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE sessions (
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

            CREATE TABLE tasks (
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
            CREATE INDEX tasks_session_status_idx ON tasks(session_id, status, priority DESC, created_at);
            CREATE UNIQUE INDEX tasks_idempotency_idx
                ON tasks(session_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

            CREATE TABLE runtime_events (
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
            CREATE INDEX runtime_events_task_idx ON runtime_events(task_id, created_at);
            CREATE INDEX runtime_events_session_idx ON runtime_events(session_id, created_at);
            """
        )

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        """Add durable Goal state after the initial task-runtime schema."""
        connection.executescript(
            """
            CREATE TABLE goals (
                goal_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                goal_json TEXT NOT NULL
            );
            CREATE INDEX goals_session_updated_idx ON goals(session_id, updated_at DESC);
            CREATE INDEX goals_session_status_idx ON goals(session_id, status, updated_at DESC);
            """
        )
    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        """Add single-layer Team state, members, and append-only messages."""
        connection.executescript(
            """
            CREATE TABLE teams (
                team_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                team_json TEXT NOT NULL,
                FOREIGN KEY(goal_id) REFERENCES goals(goal_id)
            );
            CREATE UNIQUE INDEX teams_goal_idx ON teams(goal_id);
            CREATE INDEX teams_session_updated_idx ON teams(session_id, updated_at DESC);

            CREATE TABLE team_members (
                agent_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                status TEXT NOT NULL,
                parent_agent_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                member_json TEXT NOT NULL,
                FOREIGN KEY(team_id) REFERENCES teams(team_id)
            );
            CREATE INDEX team_members_team_status_idx ON team_members(team_id, status, created_at);

            CREATE TABLE team_messages (
                message_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                task_id TEXT,
                created_at TEXT NOT NULL,
                message_json TEXT NOT NULL,
                FOREIGN KEY(team_id) REFERENCES teams(team_id)
            );
            CREATE INDEX team_messages_team_created_idx ON team_messages(team_id, created_at);
            """
        )
    @staticmethod
    def _migrate_v4(connection: sqlite3.Connection) -> None:
        """Add durable Plan and PlanTask workflow state."""
        connection.executescript(
            """
            CREATE TABLE plans (
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
            CREATE INDEX plans_session_updated_idx ON plans(session_id, updated_at DESC);
            CREATE INDEX plans_goal_version_idx ON plans(goal_id, version DESC);

            CREATE TABLE plan_tasks (
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
            CREATE INDEX plan_tasks_plan_status_idx ON plan_tasks(plan_id, status, created_at);
            """
        )
    @staticmethod
    def _migrate_v5(connection: sqlite3.Connection) -> None:
        """Make PlanTask -> Task linkage optional until a runtime task is durable."""
        connection.executescript(
            """
            ALTER TABLE plan_tasks RENAME TO plan_tasks_v4;
            CREATE TABLE plan_tasks (
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
            DROP TABLE plan_tasks_v4;
            CREATE INDEX plan_tasks_plan_status_idx ON plan_tasks(plan_id, status, created_at);
            """
        )
    @staticmethod
    def _migrate_v7(connection: sqlite3.Connection) -> None:
        """Add metadata index for immutable artifacts stored on the local filesystem."""
        connection.executescript(
            """
            CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                goal_id TEXT,
                task_id TEXT,
                created_at TEXT NOT NULL,
                artifact_json TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id)
            );
            CREATE INDEX artifacts_session_created_idx ON artifacts(session_id, created_at DESC);
            CREATE INDEX artifacts_goal_created_idx ON artifacts(goal_id, created_at DESC);
            CREATE INDEX artifacts_task_created_idx ON artifacts(task_id, created_at DESC);
            """
        )

    @staticmethod
    def _migrate_v8(connection: sqlite3.Connection) -> None:
        """Decouple Teams from Goals while retaining existing Team snapshots."""
        connection.executescript(
            """
            DROP INDEX IF EXISTS teams_goal_idx;
            DROP INDEX IF EXISTS teams_session_updated_idx;
            DROP INDEX IF EXISTS team_members_team_status_idx;
            DROP INDEX IF EXISTS team_messages_team_created_idx;
            ALTER TABLE team_members RENAME TO team_members_v7;
            ALTER TABLE team_messages RENAME TO team_messages_v7;
            ALTER TABLE teams RENAME TO teams_v7;
            CREATE TABLE teams (
                team_id TEXT PRIMARY KEY,
                goal_id TEXT,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                team_json TEXT NOT NULL
            );
            CREATE TABLE team_members (
                agent_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                status TEXT NOT NULL,
                parent_agent_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                member_json TEXT NOT NULL,
                FOREIGN KEY(team_id) REFERENCES teams(team_id)
            );
            CREATE INDEX team_members_team_status_idx ON team_members(team_id, status, created_at);
            CREATE TABLE team_messages (
                message_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                task_id TEXT,
                created_at TEXT NOT NULL,
                message_json TEXT NOT NULL,
                FOREIGN KEY(team_id) REFERENCES teams(team_id)
            );
            CREATE INDEX team_messages_team_created_idx ON team_messages(team_id, created_at);
            INSERT INTO teams(team_id, goal_id, session_id, status, created_at, updated_at, team_json)
                SELECT team_id, goal_id, session_id, status, created_at, updated_at, team_json
                FROM teams_v7;
            INSERT INTO team_members(agent_id, team_id, status, parent_agent_id, created_at, updated_at, member_json)
                SELECT agent_id, team_id, status, parent_agent_id, created_at, updated_at, member_json
                FROM team_members_v7;
            INSERT INTO team_messages(message_id, team_id, task_id, created_at, message_json)
                SELECT message_id, team_id, task_id, created_at, message_json
                FROM team_messages_v7;
            DROP TABLE team_members_v7;
            DROP TABLE team_messages_v7;
            DROP TABLE teams_v7;
            CREATE INDEX teams_session_updated_idx ON teams(session_id, updated_at DESC);
            """
        )

    @staticmethod
    def _migrate_v6(connection: sqlite3.Connection) -> None:
        """Scope PlanTask IDs by Plan so repeated step_1 IDs cannot overwrite history."""
        connection.executescript(
            """
            ALTER TABLE plan_tasks RENAME TO plan_tasks_v5;
            CREATE TABLE plan_tasks (
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
            DROP TABLE plan_tasks_v5;
            CREATE INDEX plan_tasks_plan_status_idx ON plan_tasks(plan_id, status, created_at);
            """
        )

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load(value: Optional[str], default: Any) -> Any:
        return default if not value else json.loads(value)

    def upsert_session(self, session_id: str, session_key: str, *, channel: str = "",
                       status: str = "active", active_goal_id: Optional[str] = None,
                       active_plan_id: Optional[str] = None, team_id: Optional[str] = None,
                       metadata: Optional[dict[str, Any]] = None) -> None:
        now = utc_now()
        metadata_json = self._dump(metadata or {})
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions(session_id, session_key, channel, status, active_goal_id,
                                     active_plan_id, team_id, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    session_key=excluded.session_key, channel=excluded.channel,
                    status=excluded.status, active_goal_id=excluded.active_goal_id,
                    active_plan_id=excluded.active_plan_id, team_id=excluded.team_id,
                    updated_at=excluded.updated_at, metadata_json=excluded.metadata_json
                """,
                (session_id, session_key, channel, status, active_goal_id, active_plan_id,
                 team_id, now, now, metadata_json),
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

    def transition_task(self, task_id: str, target: TaskStatus, *, lease_owner: Optional[str] = None,
                        error_code: Optional[str] = None, error_message: Optional[str] = None,
                        result: Optional[TaskResult] = None) -> TaskRecord:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise RuntimeStoreError(f"task not found: {task_id}")
            snapshot = self._snapshot_from_row(row)
            record = snapshot.record
            if lease_owner:
                record.lease_owner = lease_owner
            summary = result.summary if result else None
            record.transition(target, error_code=error_code,
                              error_message=error_message, result_summary=summary)
            if result:
                record.artifact_ids = list(result.artifact_ids)
                record.usage = dict(result.usage)
            now = utc_now()
            connection.execute(
                "UPDATE tasks SET status=?, updated_at=?, record_json=?, result_json=? WHERE task_id=?",
                (record.status.value, now, self._dump(record.to_dict()),
                 self._dump(result.to_dict()) if result else row["result_json"], task_id),
            )
            self._insert_event_in_connection(
                connection,
                RuntimeEvent.create(f"task.{target.value}", session_id=snapshot.envelope.session_id,
                                    task_id=task_id, data={"error_code": error_code or ""}),
            )
            return record

    def requeue_missing_context(self, *, sources: Optional[set[str]] = None) -> list[TaskSnapshot]:
        """Reopen restart-blocked tasks whose source is safe to replay.

        Tasks blocked for permission, validation, or execution errors remain
        terminal. ``sources`` lets Gateway restrict recovery to producers
        with a durable delivery context (currently scheduler and plans).
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
                record.transition(TaskStatus.QUEUED)
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

    def recover_interrupted(self, *, requeue: bool = True) -> list[str]:
        """Mark stale leased/running tasks interrupted; optionally requeue them."""
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
                connection.execute(
                    "UPDATE tasks SET status=?, updated_at=?, record_json=? WHERE task_id=?",
                    (record.status.value, utc_now(), self._dump(record.to_dict()), record.task_id),
                )
                recovered.append(record.task_id)
        return recovered

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

    def create_team_for_goal(self, team: dict[str, Any], goal: dict[str, Any],
                             team_event: RuntimeEvent, goal_event: RuntimeEvent) -> None:
        """Create one Team and attach it to its Goal in one transaction."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT goal_id FROM teams WHERE goal_id=?", (team["goal_id"],)
            ).fetchone()
            if current is not None:
                raise RuntimeStoreError("a team already exists for this goal")
            changed = connection.execute(
                "UPDATE goals SET updated_at=?, goal_json=? WHERE goal_id=?",
                (goal["updated_at"], self._dump(goal), goal["goal_id"]),
            )
            if changed.rowcount != 1:
                raise KeyError(goal["goal_id"])
            connection.execute(
                """INSERT INTO teams(team_id, goal_id, session_id, status, created_at, updated_at, team_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (team["team_id"], team["goal_id"], team["session_id"], team["status"],
                 team["created_at"], team["updated_at"], self._dump(team)),
            )
            self._insert_event_in_connection(connection, goal_event)
            self._insert_event_in_connection(connection, team_event)
    def get_team(self, team_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT team_json FROM teams WHERE team_id=?", (team_id,)).fetchone()
        return self._load(row["team_json"], {}) if row else None

    def get_team_by_goal(self, goal_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT team_json FROM teams WHERE goal_id=?", (goal_id,)).fetchone()
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

    def save_team_message(self, message: dict[str, Any], event: RuntimeEvent) -> None:
        required = {"message_id", "team_id", "created_at"}
        missing = required.difference(message)
        if missing:
            raise ValueError(f"team message is missing required fields: {', '.join(sorted(missing))}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO team_messages(message_id, team_id, task_id, created_at, message_json) VALUES (?, ?, ?, ?, ?)",
                (message["message_id"], message["team_id"], message.get("task_id"),
                 message["created_at"], self._dump(message)),
            )
            self._insert_event_in_connection(connection, event)

    def list_team_messages(self, team_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT message_json FROM team_messages WHERE team_id=? ORDER BY created_at ASC LIMIT ?",
                (team_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._load(row["message_json"], {}) for row in rows]
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
    def save_artifact(self, artifact: dict[str, Any], event: RuntimeEvent) -> None:
        required = {"artifact_id", "session_id", "created_at", "path", "name", "sha256", "size"}
        missing = required.difference(artifact)
        if missing:
            raise ValueError(f"artifact is missing required fields: {', '.join(sorted(missing))}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_session_in_connection(connection, artifact["session_id"], artifact["session_id"])
            connection.execute(
                """
                INSERT INTO artifacts(artifact_id, session_id, goal_id, task_id, created_at, artifact_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET artifact_json=excluded.artifact_json
                """,
                (artifact["artifact_id"], artifact["session_id"], artifact.get("goal_id"), artifact.get("task_id"),
                 artifact["created_at"], self._dump(artifact)),
            )
            self._insert_event_in_connection(connection, event)

    def get_artifact(self, artifact_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute("SELECT artifact_json FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return self._load(row["artifact_json"], {}) if row else None

    def list_artifacts(self, *, session_id: Optional[str] = None, goal_id: Optional[str] = None,
                       task_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
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
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._load(row["artifact_json"], {}) for row in rows]

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





