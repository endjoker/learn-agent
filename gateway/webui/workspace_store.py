# -*- coding: utf-8 -*-
"""
Workspace 存储层 —— 聚合 Workspace / AgentProfile / WorkspaceSession /
RuntimeSnapshot 的 SQLite CRUD（Phase 1）。

- 复用现有 RuntimeStore 的 SQLite 文件与 PRAGMA（WAL / busy_timeout / FK）。
- 工作区会话唯一事实源为 SQLite，绝不写 sessions_map.json。
- 统一 JSON 编解码、时间戳、ID 生成、异常映射。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

from gateway.webui.workspace_models import (
    AgentProfile,
    RuntimeSnapshot,
    Workspace,
    WorkspaceSession,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _gen_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(8)}"


# ============================================================
# 异常与错误码（API 层映射）
# ============================================================


class WorkspaceStoreError(RuntimeError):
    """内部存储异常基类。"""

    code = "WORKSPACE_STORE_ERROR"
    http_status = 500

    def __init__(self, message: str = "", *, code: str = ""):
        super().__init__(message)
        if code:
            self.code = code


class WorkspaceNotFound(WorkspaceStoreError):
    code = "WORKSPACE_NOT_FOUND"
    http_status = 404


class AgentProfileNotFound(WorkspaceStoreError):
    code = "AGENT_PROFILE_NOT_FOUND"
    http_status = 404


class WorkspaceSessionNotFound(WorkspaceStoreError):
    code = "WORKSPACE_SESSION_NOT_FOUND"
    http_status = 404


class VersionConflict(WorkspaceStoreError):
    code = "WORKSPACE_VERSION_CONFLICT"
    http_status = 409


class ReferenceConflict(WorkspaceStoreError):
    code = "AGENT_PROFILE_IN_USE"
    http_status = 409


class ValidationError(WorkspaceStoreError):
    code = "WORKSPACE_VALIDATION_ERROR"
    http_status = 400


class StoreBusy(WorkspaceStoreError):
    code = "WORKSPACE_STORE_BUSY"
    http_status = 503


def map_store_error(exc: Exception) -> WorkspaceStoreError:
    """将 sqlite 异常映射为稳定内部异常。"""
    if isinstance(exc, WorkspaceStoreError):
        return exc
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return StoreBusy(str(exc))
        return WorkspaceStoreError(str(exc))
    return WorkspaceStoreError(str(exc))


# ============================================================
# 数据库协作句柄
# ============================================================


class WorkspaceDatabase:
    """共享 SQLite 连接句柄（同一 runtime.db，不新建第二个文件）。"""

    def __init__(self, runtime_store=None, db_path=None):
        if runtime_store is not None:
            self._store = runtime_store
            self._db_path = Path(runtime_store.path)
        elif db_path is not None:
            self._store = None
            self._db_path = Path(db_path).expanduser().resolve()
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            from core.runtime import RuntimeStore
            self._store = RuntimeStore(self._db_path)
        else:
            raise ValueError("需要 runtime_store 或 db_path 之一")

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._store.connection() as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务上下文（复用 connection 的 commit/rollback 语义）。"""
        with self.connection() as connection:
            yield connection


# ============================================================
# 通用 JSON 编解码
# ============================================================


def _dumps_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads_json(raw: str, default=None):
    if not raw:
        return default if default is not None else []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else []

# ============================================================
# WorkspaceStore
# ============================================================


class WorkspaceStore:
    """Workspace CRUD + 组合事务（创建 Workspace + 首会话）。"""

    def __init__(self, db: WorkspaceDatabase):
        self._db = db

    # ---------- 查询 ----------

    def get(self, workspace_id: str, include_deleted: bool = False) -> Workspace:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        if row is None or (row["status"] == "deleted" and not include_deleted):
            raise WorkspaceNotFound(f"工作区不存在: {workspace_id}")
        return _workspace_from_row(row)

    def find_by_project_path(self, project_path: str,
                             include_archived: bool = False) -> Optional[Workspace]:
        """按解析后的绝对路径查找（容忍短名/长名与分隔符差异）。"""
        target = Path(project_path).resolve()
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM workspaces WHERE status IN ('active', 'archived') "
                "ORDER BY updated_at DESC").fetchall()
        for row in rows:
            w = _workspace_from_row(row)
            try:
                if Path(w.project_path).resolve() == target:
                    return w
            except (OSError, ValueError):
                continue
        return None

    def list(self, *, status: str = "active", q: str = "",
             limit: int = 50, offset: int = 0) -> list[Workspace]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        clauses = ["status = ?"]
        params: list = [status]
        if q:
            clauses.append("(name LIKE ? OR project_path LIKE ? OR description LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        sql = (f"SELECT * FROM workspaces WHERE {' AND '.join(clauses)} "
               f"ORDER BY updated_at DESC LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_workspace_from_row(r) for r in rows]

    def count(self, *, status: str = "active", q: str = "") -> int:
        clauses = ["status = ?"]
        params: list = [status]
        if q:
            clauses.append("(name LIKE ? OR project_path LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM workspaces WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return int(row["c"]) if row else 0

    # ---------- 写入 ----------

    def _insert(self, conn, workspace: Workspace) -> None:
        conn.execute(
            """INSERT INTO workspaces (
                workspace_id, name, project_path, working_directory,
                extra_workspace_roots, description, default_agent_profile_id,
                default_model, permission_mode, chat_mode,
                include_tools, exclude_tools, include_skills, exclude_skills,
                include_mcp_servers, exclude_mcp_servers, ui_preferences,
                status, version, created_at, updated_at, archived_at,
                path_risk_level, path_warnings
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                workspace.workspace_id, workspace.name, workspace.project_path,
                workspace.working_directory,
                _dumps_json(workspace.extra_workspace_roots), workspace.description,
                workspace.default_agent_profile_id, workspace.default_model,
                workspace.permission_mode, workspace.chat_mode,
                _dumps_json(workspace.include_tools), _dumps_json(workspace.exclude_tools),
                _dumps_json(workspace.include_skills), _dumps_json(workspace.exclude_skills),
                _dumps_json(workspace.include_mcp_servers),
                _dumps_json(workspace.exclude_mcp_servers),
                _dumps_json(workspace.ui_preferences),
                workspace.status, workspace.version,
                workspace.created_at or utc_now(),
                workspace.updated_at or utc_now(),
                workspace.archived_at,
                workspace.path_risk_level, _dumps_json(workspace.path_warnings),
            ),
        )

    def create(self, workspace: Workspace,
               first_session: Optional[WorkspaceSession] = None
               ) -> Workspace:
        """创建 Workspace；可选原子创建首会话（P1-S-03 组合事务）。"""
        now = utc_now()
        if not workspace.created_at:
            workspace.created_at = now
        workspace.updated_at = now
        try:
            with self._db.transaction() as conn:
                self._insert(conn, workspace)
                if first_session is not None:
                    if not first_session.session_id:
                        first_session.session_id = _gen_id("wss_")
                    first_session.session_key = (
                        f"workspace:{workspace.workspace_id}:{first_session.session_id}")
                    if not first_session.created_at:
                        first_session.created_at = now
                    first_session.updated_at = now
                    _insert_session(conn, first_session)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"工作区创建失败: {exc}") from exc
        except sqlite3.OperationalError as exc:
            raise map_store_error(exc) from exc
        return workspace

    def create_with_first_session(self, workspace: Workspace,
                                  session_payload: dict) -> tuple[Workspace, WorkspaceSession]:
        """创建 Workspace + 首会话（原子）。"""
        session = WorkspaceSession(
            session_id=_gen_id("wss_"),
            workspace_id=workspace.workspace_id,
            name=str(session_payload.get("name") or "默认会话"),
            agent_profile_id=str(session_payload.get("agent_profile_id") or ""),
            model=str(session_payload.get("model") or workspace.default_model or ""),
            permission_mode=str(session_payload.get("permission_mode")
                               or workspace.permission_mode or "ask"),
            chat_mode=str(session_payload.get("chat_mode") or workspace.chat_mode or "chat"),
            reasoning_level=str(session_payload.get("reasoning_level") or "inherit"),
        )
        self.create(workspace, first_session=session)
        return workspace, session

    def update(self, workspace_id: str, patch: dict,
               expected_version: Optional[int] = None) -> Workspace:
        current = self.get(workspace_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(
                f"工作区版本冲突: 当前 {current.version}，期望 {expected_version}",
                code="WORKSPACE_VERSION_CONFLICT")
        updated = Workspace.from_dict({**current.to_dict(), **patch})
        updated.workspace_id = workspace_id
        updated.version = current.version + 1
        updated.updated_at = utc_now()
        with self._db.transaction() as conn:
            conn.execute(
                """UPDATE workspaces SET name=?, project_path=?, working_directory=?,
                   extra_workspace_roots=?, description=?, default_agent_profile_id=?,
                   default_model=?, permission_mode=?, chat_mode=?,
                   include_tools=?, exclude_tools=?, include_skills=?, exclude_skills=?,
                   include_mcp_servers=?, exclude_mcp_servers=?, ui_preferences=?,
                   status=?, version=?, updated_at=?, archived_at=?,
                   path_risk_level=?, path_warnings=?
                   WHERE workspace_id=?""",
                (
                    updated.name, updated.project_path, updated.working_directory,
                    _dumps_json(updated.extra_workspace_roots), updated.description,
                    updated.default_agent_profile_id, updated.default_model,
                    updated.permission_mode, updated.chat_mode,
                    _dumps_json(updated.include_tools), _dumps_json(updated.exclude_tools),
                    _dumps_json(updated.include_skills), _dumps_json(updated.exclude_skills),
                    _dumps_json(updated.include_mcp_servers),
                    _dumps_json(updated.exclude_mcp_servers),
                    _dumps_json(updated.ui_preferences),
                    updated.status, updated.version, updated.updated_at,
                    updated.archived_at, updated.path_risk_level,
                    _dumps_json(updated.path_warnings),
                    workspace_id,
                ),
            )
        return updated

    def archive(self, workspace_id: str,
                expected_version: Optional[int] = None) -> Workspace:
        """Archive a workspace and all of its active sessions atomically."""
        current = self.get(workspace_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(
                f"workspace version conflict: current {current.version}, expected {expected_version}",
                code="WORKSPACE_VERSION_CONFLICT")
        now = utc_now()
        current.status = "archived"
        current.archived_at = now
        current.version += 1
        current.updated_at = now
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE workspaces SET status='archived', version=?, updated_at=?, archived_at=? "
                "WHERE workspace_id=?",
                (current.version, current.updated_at, current.archived_at, workspace_id),
            )
            conn.execute(
                "UPDATE workspace_sessions SET status='archived', updated_at=? "
                "WHERE workspace_id=? AND status='active'",
                (now, workspace_id),
            )
        return current


# ============================================================
# AgentProfileStore
# ============================================================


    def delete(self, workspace_id: str) -> Workspace:
        """彻底删除工作区：标记 deleted，删除其会话与运行快照。"""
        current = self.get(workspace_id)
        now = utc_now()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE workspaces SET status='deleted', version=version+1, "
                "updated_at=?, archived_at=? WHERE workspace_id=?",
                (now, now, workspace_id),
            )
            conn.execute(
                "UPDATE workspace_sessions SET status='deleted', updated_at=? "
                "WHERE workspace_id=?",
                (now, workspace_id),
            )
            conn.execute(
                "DELETE FROM workspace_runtime_snapshots WHERE workspace_id=?",
                (workspace_id,),
            )
        current.status = "deleted"
        current.archived_at = now
        current.updated_at = now
        current.version += 1
        return current


class AgentProfileStore:
    """AgentProfile CRUD, version snapshots, and workspace reference checks."""

    def __init__(self, db: WorkspaceDatabase):
        self._db = db

    def get(self, profile_id: str, include_deleted: bool = False) -> AgentProfile:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_profiles WHERE profile_id=?", (profile_id,),
            ).fetchone()
        if row is None or (row["status"] == "deleted" and not include_deleted):
            raise AgentProfileNotFound(f"agent profile not found: {profile_id}")
        return _profile_from_row(row)

    def get_by_name(self, name: str) -> Optional[AgentProfile]:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_profiles WHERE name=?", (name,),
            ).fetchone()
        return _profile_from_row(row) if row else None

    def list(self, *, status: str = "active", q: str = "",
             limit: int = 50, offset: int = 0) -> list[AgentProfile]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        clauses = ["status = ?"]
        params: list = [status]
        if q:
            clauses.append("(name LIKE ? OR description LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])
        sql = (f"SELECT * FROM agent_profiles WHERE {' AND '.join(clauses)} "
               "ORDER BY updated_at DESC LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_profile_from_row(row) for row in rows]

    def count(self, *, status: str = "active", q: str = "") -> int:
        clauses = ["status = ?"]
        params: list = [status]
        if q:
            clauses.append("(name LIKE ? OR description LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM agent_profiles WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return int(row["c"]) if row else 0

    def create(self, profile: AgentProfile) -> AgentProfile:
        now = utc_now()
        profile.created_at = profile.created_at or now
        profile.updated_at = now
        try:
            with self._db.transaction() as conn:
                self._insert(conn, profile)
                _insert_profile_version(conn, profile, now)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"agent profile create failed: {exc}") from exc
        except sqlite3.OperationalError as exc:
            raise map_store_error(exc) from exc
        return profile

    def _insert(self, conn, profile: AgentProfile) -> None:
        conn.execute(
            """INSERT INTO agent_profiles (
                profile_id, name, description, system_prompt, tools, skills,
                mcp_servers, default_model, permission_mode, chat_mode, max_steps,
                include_tools, exclude_tools, ui_preferences, status, version,
                is_system, created_at, updated_at, archived_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                profile.profile_id, profile.name, profile.description,
                profile.system_prompt, _dumps_json(profile.tools),
                _dumps_json(profile.skills), _dumps_json(profile.mcp_servers),
                profile.default_model, profile.permission_mode, profile.chat_mode,
                profile.max_steps, _dumps_json(profile.include_tools),
                _dumps_json(profile.exclude_tools), _dumps_json(profile.ui_preferences),
                profile.status, profile.version, 1 if profile.is_system else 0,
                profile.created_at, profile.updated_at, profile.archived_at,
            ),
        )

    def update(self, profile_id: str, patch: dict,
               expected_version: Optional[int] = None) -> AgentProfile:
        current = self.get(profile_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(
                f"agent profile version conflict: current {current.version}, expected {expected_version}",
                code="AGENT_VERSION_CONFLICT")
        updated = AgentProfile.from_dict({**current.to_dict(), **patch})
        updated.profile_id = profile_id
        updated.version = current.version + 1
        updated.updated_at = utc_now()
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    """UPDATE agent_profiles SET name=?, description=?, system_prompt=?,
                       tools=?, skills=?, mcp_servers=?, default_model=?,
                       permission_mode=?, chat_mode=?, max_steps=?,
                       include_tools=?, exclude_tools=?, ui_preferences=?,
                       status=?, version=?, is_system=?, updated_at=?, archived_at=?
                       WHERE profile_id=?""",
                    (
                        updated.name, updated.description, updated.system_prompt,
                        _dumps_json(updated.tools), _dumps_json(updated.skills),
                        _dumps_json(updated.mcp_servers), updated.default_model,
                        updated.permission_mode, updated.chat_mode, updated.max_steps,
                        _dumps_json(updated.include_tools), _dumps_json(updated.exclude_tools),
                        _dumps_json(updated.ui_preferences), updated.status,
                        updated.version, 1 if updated.is_system else 0,
                        updated.updated_at, updated.archived_at, profile_id,
                    ),
                )
                _insert_profile_version(conn, updated, updated.updated_at)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"agent profile update failed: {exc}") from exc
        except sqlite3.OperationalError as exc:
            raise map_store_error(exc) from exc
        return updated

    def duplicate(self, profile_id: str, *, name: Optional[str] = None) -> AgentProfile:
        current = self.get(profile_id)
        dup = AgentProfile.from_dict(current.to_dict())
        dup.profile_id = _gen_id("agent_")
        dup.name = name or f"{current.name} (copy)"
        dup.version = 1
        dup.created_at = ""
        dup.updated_at = ""
        dup.status = "active"
        dup.archived_at = ""
        dup.is_system = False
        return self.create(dup)

    def references(self, profile_id: str) -> list[Workspace]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM workspaces WHERE default_agent_profile_id=? "
                "AND status IN ('active','archived')",
                (profile_id,),
            ).fetchall()
        return [_workspace_from_row(row) for row in rows]

    def archive(self, profile_id: str,
                expected_version: Optional[int] = None) -> AgentProfile:
        current = self.get(profile_id)
        if current.is_system:
            raise ValidationError("system agent profiles cannot be disabled",
                                  code="SYSTEM_AGENT_IMMUTABLE")
        if current.status != "active":
            raise ValidationError("only active agent profiles can be disabled",
                                  code="AGENT_PROFILE_NOT_ACTIVE")
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(
                f"agent profile version conflict: current {current.version}, expected {expected_version}",
                code="AGENT_VERSION_CONFLICT")
        active_refs = [item for item in self.references(profile_id) if item.status == "active"]
        if active_refs:
            raise ReferenceConflict(
                f"agent profile is referenced by {len(active_refs)} active workspace(s)",
                code="AGENT_PROFILE_IN_USE")
        current.status = "archived"
        current.archived_at = utc_now()
        current.version += 1
        current.updated_at = current.archived_at
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE agent_profiles SET status='archived', version=?, updated_at=?, archived_at=? "
                "WHERE profile_id=?",
                (current.version, current.updated_at, current.archived_at, profile_id),
            )
            _insert_profile_version(conn, current, current.updated_at)
        return current

    def activate(self, profile_id: str,
                 expected_version: Optional[int] = None) -> AgentProfile:
        """Re-enable an archived user profile without losing version history."""
        current = self.get(profile_id)
        if current.is_system:
            raise ValidationError("system agent profiles cannot be enabled or disabled",
                                  code="SYSTEM_AGENT_IMMUTABLE")
        if current.status != "archived":
            raise ValidationError("only archived agent profiles can be enabled",
                                  code="AGENT_PROFILE_NOT_ARCHIVED")
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(
                f"agent profile version conflict: current {current.version}, expected {expected_version}",
                code="AGENT_VERSION_CONFLICT")
        current.status = "active"
        current.archived_at = ""
        current.version += 1
        current.updated_at = utc_now()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE agent_profiles SET status='active', version=?, updated_at=?, archived_at='' "
                "WHERE profile_id=?",
                (current.version, current.updated_at, profile_id),
            )
            _insert_profile_version(conn, current, current.updated_at)
        return current

    def delete(self, profile_id: str,
               expected_version: Optional[int] = None) -> AgentProfile:
        """Logically delete a normal profile while retaining audit/snapshot history."""
        current = self.get(profile_id)
        if current.is_system:
            raise ValidationError("system agent profiles cannot be deleted",
                                  code="SYSTEM_AGENT_IMMUTABLE")
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(
                f"agent profile version conflict: current {current.version}, expected {expected_version}",
                code="AGENT_VERSION_CONFLICT")
        active_refs = [item for item in self.references(profile_id) if item.status == "active"]
        if active_refs:
            raise ReferenceConflict(
                f"agent profile is referenced by {len(active_refs)} active workspace(s)",
                code="AGENT_PROFILE_IN_USE")
        current.status = "deleted"
        current.archived_at = utc_now()
        current.version += 1
        current.updated_at = current.archived_at
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE agent_profiles SET status='deleted', version=?, updated_at=?, archived_at=? "
                "WHERE profile_id=?",
                (current.version, current.updated_at, current.archived_at, profile_id),
            )
            _insert_profile_version(conn, current, current.updated_at)
        return current

    def seed_system_profiles(self) -> int:
        """Create missing package-owned default agent templates.

        Built-in profiles remain recognizable as system defaults and cannot be
        deleted or archived, but administrators may edit and save them in the UI.
        Therefore an existing system profile is never overwritten at Gateway
        start-up; its saved configuration is the source of truth.
        """
        templates = [
            AgentProfile(
                profile_id="agent_coder",
                name="\u7f16\u7a0b\u667a\u80fd\u4f53",
                description="在现有项目中分析、实现、调试并验证改动。",
                system_prompt="""# 角色
你是一名资深软件工程智能体。在当前工作区内分析、实现、调试并验证改动，交付可运行、可复现、可审查的结果，而不是猜测或零散片段。

# 方法
1. 明确目标、约束和验收标准。只提出解决真正阻塞所必需的最少问题。
2. 改动前，先用 read、grep、glob 检查相关实现、配置、测试和项目约定。不要仅凭文件名推断行为。
3. 定位根因，再做最小且连贯的改动。保持现有架构、命名和格式；避免无关重构。
4. 在写入前说明预期影响。优先精确修改；仅在必要时创建或覆盖文件。
5. 改动后运行最相关的测试、lint、类型检查、构建或聚焦复现。若无法验证，说明原因、仍未验证的内容以及确切的下一条命令。

# 技能使用规则（绑定以下 skill，触发场景必须使用）
- 改动完成后必须使用 `code-review` 技能自审一遍改动（逻辑、性能、安全、可维护性），再交付结论。
- 排查疑难 bug 时使用 `systematic-debugging` 技能：先找根因再修，禁止症状修补。
- 复现/定位缺陷时使用 `diagnosing-bugs` 技能：复现→定位→假设→验证。
- 长任务（多步骤/多文件）必须使用 `executing-plans` 技能：先制定计划，再逐步执行并验证；执行前先用 `writing-plans` 技能把计划写成带完成定义的任务清单。
- 涉及测试先行时使用 `test-driven-development` 技能：先写失败测试，再最小实现使其通过。
- 完成任何任务前使用 `verification-before-completion` 技能：用真实测试/运行证据验证，而非口头声称完成。
- 遇到 git 合并/变基冲突时使用 `resolving-merge-conflicts` 技能：理解双方意图、保留双意、跑自动化检查。

# 工具与安全策略
- 检查优先用 read/grep/glob，文件改动使用专门的 edit/write 工具。
- 仅在确能推进诊断、构建或验证时才使用 shell 或 Python 执行；命令要保持范围小且可解释。
- 遵守工作区边界、权限和审批门禁。除非用户明确要求，不得修改工作区之外的文件，不得 commit 或 push 到远端。

# 输出
先给结论。然后报告改动文件、关键决策和验证证据。明确区分已确认事实、假设、风险和阻塞项。""",
                tools=["read", "write", "edit", "grep", "glob", "bash", "python"],
                skills=["code-review", "systematic-debugging", "diagnosing-bugs",
                        "verification-before-completion", "executing-plans",
                        "writing-plans", "test-driven-development",
                        "resolving-merge-conflicts"],
                mcp_servers=["web-search"],
                permission_mode="ask", chat_mode="chat", max_steps=100, is_system=True,
            ),
            AgentProfile(
                profile_id="agent_product",
                name="\u4ea7\u54c1\u667a\u80fd\u4f53",
                description="把模糊需求转化为可实施、可测试的产品方案与 PRD。",
                system_prompt="""# 角色
你是产品策略与需求分析智能体。把模糊需求转化为可实施、可测试的产品方案。事实、假设和待确认问题要明确分开。

# 方法
1. 澄清问题、目标用户、场景、期望结果、约束和成功指标。
2. 阅读现有需求、代码、数据和文档以理解现状。仅在需要核验公开信息时使用 search 或 web_fetch。
3. 产出的内容包括：问题定义、范围与非目标、用户流程、功能需求、边界情况、优先级、依赖、风险和验收标准。
4. 存在权衡时，给出带成本、风险、预期收益的选项，并给出推荐。优先选择可小步验证的方案。

# 技能使用规则（绑定以下 skill，触发场景必须使用）
- 用户给出模糊需求或方案时，使用 `grill-me` 技能反复追问：走完决策树每个分支，一次一问，直到达成共识后再动笔。
- 梳理业务概念与数据关系时使用 `domain-modeling` 技能：主动打磨领域模型、写清术语表和关键决策。
- 方案成型后使用 `writing-plans` 技能把方案写成可执行计划（任务粒度、每步完成定义）。
- 长方案/多阶段交付使用 `executing-plans` 技能逐步执行并验证。

# 输出
先给结论。PRD 需包含优先级、依赖、异常路径、可量化的验收标准，以及上线/验证计划。外部信息需注明来源与检索日期。除非明确要求，不修改项目代码。""",
                tools=["read", "write", "grep", "glob", "search", "web_fetch"],
                skills=["grill-me", "domain-modeling", "writing-plans", "executing-plans"],
                mcp_servers=["web-search"],
                permission_mode="ask", chat_mode="plan", max_steps=60, is_system=True,
            ),
            AgentProfile(
                profile_id="agent_tester",
                name="\u6d4b\u8bd5\u667a\u80fd\u4f53",
                description="设计测试、执行验证、隔离回归并输出可复现缺陷。",
                system_prompt="""# 角色
你是软件测试与质量保障智能体。把需求和实现变更转化为可执行的证据，并准确报告质量风险。

# 方法
1. 检查需求、变更代码、现有测试和运行配置。覆盖正常路径、边界、失败、兼容性和回归风险。
2. 尽可能复用项目现有的测试命令和框架。执行前说明范围，并保留关键结果证据。
3. 缺陷报告必须包含环境/前置条件、精确复现步骤、期望结果、实际结果、影响、严重度和证据。
4. 明确区分已确认缺陷、风险观察和疑问。不要把未运行的测试或偶发症状当作已确认结论。

# 技能使用规则（绑定以下 skill，触发场景必须使用）
- 编写测试用例时使用 `test-driven-development` 技能：先写失败测试，再最小实现使其通过。
- 回归排查时使用 `diagnosing-bugs` 技能：复现→定位→假设→验证，输出可复现步骤。
- 完成测试/验证后使用 `verification-before-completion` 技能：以真实运行证据为准，不凭猜测下结论。
- 以测试视角审查代码时使用 `code-review` 技能：检查测试覆盖缺口、可测性和边界。

# 工具与输出策略
分析用 read/grep/glob；仅在需要做聚焦验证时使用 bash 或 python。未经明确授权不得修改产品代码。先给质量结论和阻塞项，再给覆盖范围、执行结果、失败项和推荐后续动作。""",
                tools=["read", "grep", "glob", "bash", "python"],
                skills=["code-review", "test-driven-development",
                        "verification-before-completion", "diagnosing-bugs"],
                mcp_servers=[],
                permission_mode="ask", chat_mode="chat", max_steps=80, is_system=True,
            ),
            AgentProfile(
                profile_id="agent_reviewer",
                name="\u53ea\u8bfb\u5ba1\u67e5\u667a\u80fd\u4f53",
                description="仅使用只读与检索能力的严格审查，覆盖正确性、安全、性能和可维护性。",
                system_prompt="""# 角色
你是严格的只读代码审查智能体。只分析并报告发现；不写入、不编辑、不执行任何会改变项目状态的操作。

# 审查方法
1. 在判断前，先理解变更目标、调用链路、数据流、错误处理和现有测试。
2. 检查正确性、边界条件、并发与资源生命周期、安全与授权、性能、可维护性和测试缺口。
3. 只报告有代码或行为证据支撑的发现。不确定的标注为问题或假设，而不是制造噪音。
4. 每个发现给出严重度、文件/位置、理由、影响和最小可行修复。若没有发现，说明残余测试盲区。

# 技能使用规则（绑定以下 skill，触发场景必须使用）
- 审查代码改动时使用 `code-review` 技能：按 4 阶段流程（上下文→宏观→逐行→总结）系统审查，输出严重度分级发现。
- 审查外部 skill / 插件 / 下载内容时使用 `skill-inspector` 技能：静态扫描 + 源码语义审查，输出 APPROVE / CAUTION / REJECT 判定。

# 权限边界（不可协商）
你只能使用 read、grep、glob、search 和 web_fetch。这些能力仅用于检查和收集证据。
绝不调用 write、edit、file_mgr、bash、python、process、调度或管理类工具。即使命令看起来是只读的，也不执行。不得修改源文件、配置、生成文件、依赖、Git 状态或远端服务。
如果仅凭 read/search 能力无法获得证据，说明限制并建议后续跟进，而不是尝试执行操作。

# 输出
按严重度排序：阻断、重要、建议。每个发现都要给出证据、影响和最小修复。最后给出整体评估、残余盲区和建议验证项。""",
                tools=["read", "grep", "glob", "search", "web_fetch"],
                skills=["code-review", "skill-inspector"],
                mcp_servers=["web-search"],
                permission_mode="readonly", chat_mode="chat", max_steps=50, is_system=True,
            ),
        ]
        added = 0
        for template in templates:
            try:
                current = self.get(template.profile_id)
            except AgentProfileNotFound:
                # Do not overwrite a user profile that happens to own a template name.
                if self.get_by_name(template.name) is not None:
                    logger.warning("Skipping system profile %s: name is occupied", template.name)
                    continue
                self.create(template)
                added += 1
                continue
            if not current.is_system:
                logger.warning("Skipping system profile %s: ID is occupied by a user profile", template.profile_id)
                continue
            # Existing built-ins may contain administrator edits. Do not reset
            # their prompt, capabilities, or permissions at normal startup.
            continue
        return added


def _insert_profile_version(conn, profile: AgentProfile, now: str) -> None:
    conn.execute(
        """INSERT INTO agent_profile_versions (
            version_id, profile_id, version, snapshot_json, created_at
        ) VALUES (?,?,?,?,?)""",
        (_gen_id("ver_"), profile.profile_id, profile.version,
         _dumps_json(profile.to_dict()), now),
    )

# ============================================================
# WorkspaceSessionStore
# ============================================================


class WorkspaceSessionStore:
    """WorkspaceSession CRUD + 归属校验（P1-S-05 外键归属）。"""

    def __init__(self, db: WorkspaceDatabase):
        self._db = db

    def create(self, workspace_id: str, payload: dict) -> WorkspaceSession:
        session = WorkspaceSession(
            session_id=_gen_id("wss_"),
            workspace_id=workspace_id,
            name=str(payload.get("name") or "新会话"),
            agent_profile_id=str(payload.get("agent_profile_id") or ""),
            model=str(payload.get("model") or ""),
            permission_mode=str(payload.get("permission_mode") or "ask"),
            chat_mode=str(payload.get("chat_mode") or "chat"),
            reasoning_level=str(payload.get("reasoning_level") or "inherit"),
        )
        session.session_key = f"workspace:{workspace_id}:{session.session_id}"
        now = utc_now()
        session.created_at = now
        session.updated_at = now
        try:
            with self._db.transaction() as conn:
                # 校验 workspace 存在且 active
                ws = conn.execute(
                    "SELECT workspace_id FROM workspaces WHERE workspace_id=? "
                    "AND status='active'",
                    (workspace_id,),
                ).fetchone()
                if ws is None:
                    raise WorkspaceNotFound(f"工作区不存在或未激活: {workspace_id}")
                _insert_session(conn, session)
        except WorkspaceStoreError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"会话创建失败: {exc}") from exc
        except sqlite3.OperationalError as exc:
            raise map_store_error(exc) from exc
        return session

    def get_owned(self, workspace_id: str, session_id: str) -> WorkspaceSession:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM workspace_sessions WHERE session_id=? AND workspace_id=?",
                (session_id, workspace_id),
            ).fetchone()
        if row is None or row["status"] == "deleted":
            raise WorkspaceSessionNotFound(
                f"工作区会话不存在: {workspace_id}/{session_id}")
        return _session_from_row(row)

    def list_for_workspace(self, workspace_id: str, *, status: str = "active",
                           limit: int = 100, offset: int = 0) -> list[WorkspaceSession]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM workspace_sessions WHERE workspace_id=? AND status=? "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (workspace_id, status, limit, offset),
            ).fetchall()
        return [_session_from_row(r) for r in rows]

    def update_runtime_overrides(self, workspace_id: str, session_id: str,
                                 *, model: str = None, permission_mode: str = None,
                                 chat_mode: str = None, reasoning_level: str = None, name: str = None,
                                 expected_busy: bool = False) -> WorkspaceSession:
        """更新运行期覆盖（模型/权限/chat mode/名称）。busy 时拒绝。"""
        current = self.get_owned(workspace_id, session_id)
        if expected_busy and current.is_busy:
            raise StoreBusy("会话正在运行，不能切换配置", code="WORKSPACE_SESSION_BUSY")
        fields = []
        params: list = []
        if model is not None:
            fields.append("model=?")
            params.append(str(model))
        if permission_mode is not None:
            fields.append("permission_mode=?")
            params.append(str(permission_mode))
        if chat_mode is not None:
            fields.append("chat_mode=?")
            params.append(str(chat_mode))
        if reasoning_level is not None:
            fields.append("reasoning_level=?")
            params.append(str(reasoning_level))
        if name is not None:
            fields.append("name=?")
            params.append(str(name))
        if not fields:
            return current
        now = utc_now()
        fields.append("updated_at=?")
        params.append(now)
        params.extend([workspace_id, session_id])
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE workspace_sessions SET {', '.join(fields)} "
                "WHERE workspace_id=? AND session_id=?",
                params,
            )
        return self.get_owned(workspace_id, session_id)

    def set_busy(self, workspace_id: str, session_id: str, busy: bool) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE workspace_sessions SET is_busy=?, updated_at=? "
                "WHERE workspace_id=? AND session_id=?",
                (1 if busy else 0, utc_now(), workspace_id, session_id),
            )

    def touch_snapshot(self, workspace_id: str, session_id: str,
                       snapshot_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE workspace_sessions SET last_snapshot_id=?, last_active_at=?, "
                "updated_at=? WHERE workspace_id=? AND session_id=?",
                (snapshot_id, utc_now(), utc_now(), workspace_id, session_id),
            )

    def archive(self, workspace_id: str, session_id: str) -> WorkspaceSession:
        current = self.get_owned(workspace_id, session_id)
        if current.is_busy:
            raise StoreBusy("会话正在运行，不能归档", code="WORKSPACE_SESSION_BUSY")
        now = utc_now()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE workspace_sessions SET status='archived', updated_at=?, "
                "archived_at=? WHERE workspace_id=? AND session_id=?",
                (now, now, workspace_id, session_id),
            )
        return self.get_owned(workspace_id, session_id)


def _insert_session(conn, session: WorkspaceSession) -> None:
    conn.execute(
        """INSERT INTO workspace_sessions (
            session_id, workspace_id, session_key, name, agent_profile_id, model,
            permission_mode, chat_mode, reasoning_level, status, client_config_version,
            last_snapshot_id, last_active_at, created_at, updated_at, archived_at,
            error_message, is_busy
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            session.session_id, session.workspace_id, session.session_key, session.name,
            session.agent_profile_id, session.model, session.permission_mode,
            session.chat_mode, session.reasoning_level, session.status, session.client_config_version,
            session.last_snapshot_id, session.last_active_at, session.created_at,
            session.updated_at, session.archived_at, session.error_message,
            1 if session.is_busy else 0,
        ),
    )

# ============================================================
# RuntimeSnapshotStore
# ============================================================


class RuntimeSnapshotStore:
    """RuntimeSnapshot 去重 / 读取 / retention。"""

    def __init__(self, db: WorkspaceDatabase):
        self._db = db

    def get(self, snapshot_id: str) -> Optional[RuntimeSnapshot]:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM workspace_runtime_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        return _snapshot_from_row(row) if row else None

    def get_or_create(self, snapshot: RuntimeSnapshot) -> RuntimeSnapshot:
        """按 dedup_key 复用（P1-S-08）；不同配置产生新行。"""
        if not snapshot.dedup_key:
            snapshot.dedup_key = snapshot.capability_hash or snapshot.snapshot_id
        existing = self.get_by_dedup_key(snapshot.dedup_key)
        if existing is not None:
            return existing
        if not snapshot.snapshot_id:
            snapshot.snapshot_id = _gen_id("snap_")
        if not snapshot.created_at:
            snapshot.created_at = utc_now()
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    """INSERT INTO workspace_runtime_snapshots (
                        snapshot_id, workspace_id, workspace_session_id,
                        agent_profile_id, agent_profile_version, workspace_version,
                        session_client_config_version, model, permission_mode, chat_mode, reasoning_level,
                        working_directory, project_root, framework_root, agent_data_root,
                        extra_workspace_roots, tools, skills, mcp_servers, system_prompt,
                        prompt_hash, expected_prompt_hash, capability_hash, dedup_key,
                        created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        snapshot.snapshot_id, snapshot.workspace_id,
                        snapshot.workspace_session_id, snapshot.agent_profile_id,
                        snapshot.agent_profile_version, snapshot.workspace_version,
                        snapshot.session_client_config_version, snapshot.model,
                        snapshot.permission_mode, snapshot.chat_mode, snapshot.reasoning_level,
                        snapshot.working_directory, snapshot.project_root,
                        snapshot.framework_root, snapshot.agent_data_root,
                        _dumps_json(snapshot.extra_workspace_roots),
                        _dumps_json(snapshot.tools), _dumps_json(snapshot.skills),
                        _dumps_json(snapshot.mcp_servers), snapshot.system_prompt,
                        snapshot.prompt_hash, snapshot.expected_prompt_hash,
                        snapshot.capability_hash, snapshot.dedup_key,
                        snapshot.created_at,
                    ),
                )
        except sqlite3.IntegrityError:
            # 并发去重：另一连接已插入相同 dedup_key
            existing = self.get_by_dedup_key(snapshot.dedup_key)
            if existing is not None:
                return existing
            raise
        except sqlite3.OperationalError as exc:
            raise map_store_error(exc) from exc
        return snapshot

    def get_by_dedup_key(self, dedup_key: str) -> Optional[RuntimeSnapshot]:
        if not dedup_key:
            return None
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM workspace_runtime_snapshots WHERE dedup_key=?",
                (dedup_key,),
            ).fetchone()
        return _snapshot_from_row(row) if row else None

    def list_for_session(self, workspace_session_id: str,
                         limit: int = 50) -> list[RuntimeSnapshot]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM workspace_runtime_snapshots "
                "WHERE workspace_session_id=? ORDER BY created_at DESC LIMIT ?",
                (workspace_session_id, limit),
            ).fetchall()
        return [_snapshot_from_row(r) for r in rows]

    def retain(self, *, days: int = 30, per_workspace: int = 10) -> int:
        """只删除超期/超量且未被当前 Session 引用的旧快照（P1-S-09）。"""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = 0
        with self._db.transaction() as conn:
            referenced = set()
            for r in conn.execute(
                    "SELECT DISTINCT last_snapshot_id FROM workspace_sessions "
                    "WHERE last_snapshot_id != ''").fetchall():
                referenced.add(r["last_snapshot_id"])
            rows = conn.execute(
                "SELECT snapshot_id FROM workspace_runtime_snapshots "
                "WHERE created_at < ?", (cutoff,),
            ).fetchall()
            for r in rows:
                if r["snapshot_id"] in referenced:
                    continue
                conn.execute(
                    "DELETE FROM workspace_runtime_snapshots WHERE snapshot_id=?",
                    (r["snapshot_id"],),
                )
                deleted += 1
            ws_rows = conn.execute(
                "SELECT DISTINCT workspace_session_id FROM workspace_runtime_snapshots"
            ).fetchall()
            for w in ws_rows:
                sid = w["workspace_session_id"]
                keep = conn.execute(
                    "SELECT snapshot_id FROM workspace_runtime_snapshots "
                    "WHERE workspace_session_id=? ORDER BY created_at DESC LIMIT ?",
                    (sid, per_workspace),
                ).fetchall()
                keep_ids = {r["snapshot_id"] for r in keep}
                old = conn.execute(
                    "SELECT snapshot_id FROM workspace_runtime_snapshots "
                    "WHERE workspace_session_id=?", (sid,),
                ).fetchall()
                for r in old:
                    if r["snapshot_id"] in keep_ids or r["snapshot_id"] in referenced:
                        continue
                    conn.execute(
                        "DELETE FROM workspace_runtime_snapshots WHERE snapshot_id=?",
                        (r["snapshot_id"],),
                    )
                    deleted += 1
        return deleted

# ============================================================
# 行 → 模型
# ============================================================


def _workspace_from_row(row) -> Workspace:
    return Workspace(
        workspace_id=row["workspace_id"],
        name=row["name"],
        project_path=row["project_path"],
        working_directory=row["working_directory"] or row["project_path"],
        extra_workspace_roots=_loads_json(row["extra_workspace_roots"]),
        description=row["description"],
        default_agent_profile_id=row["default_agent_profile_id"],
        default_model=row["default_model"],
        permission_mode=row["permission_mode"],
        chat_mode=row["chat_mode"],
        include_tools=_loads_json(row["include_tools"]),
        exclude_tools=_loads_json(row["exclude_tools"]),
        include_skills=_loads_json(row["include_skills"]),
        exclude_skills=_loads_json(row["exclude_skills"]),
        include_mcp_servers=_loads_json(row["include_mcp_servers"]),
        exclude_mcp_servers=_loads_json(row["exclude_mcp_servers"]),
        ui_preferences=_loads_json(row["ui_preferences"], default={}),
        status=row["status"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
        path_risk_level=row["path_risk_level"],
        path_warnings=_loads_json(row["path_warnings"]),
    )


def _profile_from_row(row) -> AgentProfile:
    return AgentProfile(
        profile_id=row["profile_id"],
        name=row["name"],
        description=row["description"],
        system_prompt=row["system_prompt"],
        tools=_loads_json(row["tools"]),
        skills=_loads_json(row["skills"]),
        mcp_servers=_loads_json(row["mcp_servers"]),
        default_model=row["default_model"],
        permission_mode=row["permission_mode"],
        chat_mode=row["chat_mode"],
        max_steps=row["max_steps"],
        include_tools=_loads_json(row["include_tools"]),
        exclude_tools=_loads_json(row["exclude_tools"]),
        ui_preferences=_loads_json(row["ui_preferences"], default={}),
        status=row["status"],
        version=row["version"],
        is_system=bool(row["is_system"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _session_from_row(row) -> WorkspaceSession:
    return WorkspaceSession(
        session_id=row["session_id"],
        workspace_id=row["workspace_id"],
        session_key=row["session_key"],
        name=row["name"],
        agent_profile_id=row["agent_profile_id"],
        model=row["model"],
        permission_mode=row["permission_mode"],
        chat_mode=row["chat_mode"],
        reasoning_level=row["reasoning_level"],
        status=row["status"],
        client_config_version=row["client_config_version"],
        last_snapshot_id=row["last_snapshot_id"],
        last_active_at=row["last_active_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
        error_message=row["error_message"],
        is_busy=bool(row["is_busy"]),
    )


def _snapshot_from_row(row) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        snapshot_id=row["snapshot_id"],
        workspace_id=row["workspace_id"],
        workspace_session_id=row["workspace_session_id"],
        agent_profile_id=row["agent_profile_id"],
        agent_profile_version=row["agent_profile_version"],
        workspace_version=row["workspace_version"],
        session_client_config_version=row["session_client_config_version"],
        model=row["model"],
        permission_mode=row["permission_mode"],
        chat_mode=row["chat_mode"],
        reasoning_level=row["reasoning_level"],
        working_directory=row["working_directory"],
        project_root=row["project_root"],
        framework_root=row["framework_root"],
        agent_data_root=row["agent_data_root"],
        extra_workspace_roots=_loads_json(row["extra_workspace_roots"]),
        tools=_loads_json(row["tools"]),
        skills=_loads_json(row["skills"]),
        mcp_servers=_loads_json(row["mcp_servers"]),
        system_prompt=row["system_prompt"],
        prompt_hash=row["prompt_hash"],
        expected_prompt_hash=row["expected_prompt_hash"],
        capability_hash=row["capability_hash"],
        dedup_key=row["dedup_key"],
        created_at=row["created_at"],
    )
