# -*- coding: utf-8 -*-
"""
Workspace 领域模型 —— Phase 0 最小运行时上下文 + Phase 1 完整领域模型

本模块分层：
  - Phase 0：WorkspaceRuntimeContext（运行上下文 dataclass，无数据库依赖）
  - Phase 1：Workspace / AgentProfile / WorkspaceSession / RuntimeSnapshot /
             PathValidationResult（强类型领域模型，供 SQLite Store / API 使用）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 常量与枚举
# ============================================================

VALID_PERMISSION_MODES = ("readonly", "ask", "allow", "unreviewed")
VALID_CHAT_MODES = ("chat",)
# ``inherit`` means: follow the selected model / provider reasoning configuration.
VALID_REASONING_LEVELS = ("inherit", "provider_default", "none", "minimal", "low", "medium", "high", "xhigh", "max")
VALID_WORKSPACE_STATUSES = ("active", "archived", "deleted")
VALID_SESSION_STATUSES = ("active", "archived", "deleted", "error", "stale")
VALID_PROFILE_STATUSES = ("active", "archived", "deleted")

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _validate_id(value: str, label: str, max_len: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 不能为空")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"{label} 长度不能超过 {max_len}")
    if not _ID_RE.match(value):
        raise ValueError(f"{label} 只能包含字母/数字/下划线/短横线")
    return value


def _validate_enum(value: str, choices: Tuple[str, ...], label: str, default: str) -> str:
    if value is None or value == "":
        return default
    if value not in choices:
        raise ValueError(f"{label} 必须是 {choices} 之一，收到 {value!r}")
    return value


def _norm_path(value: str | Path, label: str) -> Path:
    """规范化路径字段（创建时解析为绝对路径）。"""
    if value is None or value == "":
        raise ValueError(f"{label} 不能为空")
    return Path(str(value)).expanduser().resolve()


# ============================================================
# Phase 0：WorkspaceRuntimeContext
# ============================================================


@dataclass(frozen=True)
class WorkspaceRuntimeContext:
    """Agent 运行上下文 —— 显式三目录分离，禁止进程级 os.chdir。

    字段在创建时规范化；实例不可变，切换配置必须创建新上下文并重建 Agent。
    """

    workspace_id: str
    workspace_session_id: str
    framework_root: Path
    agent_data_root: Path
    project_root: Path
    working_directory: Path
    extra_workspace_roots: Tuple[Path, ...] = ()
    permission_mode: str = "ask"

    def __post_init__(self):
        object.__setattr__(self, "workspace_id",
                           _validate_id(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "workspace_session_id",
                           _validate_id(self.workspace_session_id, "workspace_session_id"))
        object.__setattr__(self, "framework_root",
                           _norm_path(self.framework_root, "framework_root"))
        object.__setattr__(self, "agent_data_root",
                           _norm_path(self.agent_data_root, "agent_data_root"))
        object.__setattr__(self, "project_root",
                           _norm_path(self.project_root, "project_root"))
        object.__setattr__(self, "working_directory",
                           _norm_path(self.working_directory, "working_directory"))
        object.__setattr__(self, "permission_mode",
                           _validate_enum(self.permission_mode,
                                          VALID_PERMISSION_MODES,
                                          "permission_mode", "ask"))
        if not isinstance(self.extra_workspace_roots, (tuple, list)):
            raise ValueError("extra_workspace_roots 必须是路径元组/列表")
        roots = tuple(_norm_path(p, "extra_workspace_root")
                      for p in self.extra_workspace_roots)
        object.__setattr__(self, "extra_workspace_roots", roots)
        # working_directory 必须位于 project_root 或显式 extra roots 内
        if not self._is_working_directory_valid():
            raise ValueError(
                "working_directory 必须位于 project_root 或显式 extra_workspace_roots 内")

    def _is_working_directory_valid(self) -> bool:
        try:
            self.working_directory.relative_to(self.project_root)
            return True
        except ValueError:
            pass
        return any(
            _is_relative_to(self.working_directory, root)
            for root in self.extra_workspace_roots
        )

    def to_dict(self) -> dict:
        """序列化 —— 不含任何密钥。"""
        return {
            "workspace_id": self.workspace_id,
            "workspace_session_id": self.workspace_session_id,
            "framework_root": str(self.framework_root),
            "agent_data_root": str(self.agent_data_root),
            "project_root": str(self.project_root),
            "working_directory": str(self.working_directory),
            "extra_workspace_roots": [str(p) for p in self.extra_workspace_roots],
            "permission_mode": self.permission_mode,
        }

def _is_relative_to(path: Path, root: Path) -> bool:
    """Python 3.8 兼容的 Path.relative_to 判定。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ============================================================
# Phase 1：领域模型
# ============================================================


@dataclass
class PathValidationResult:
    """路径校验结果（PathValidator 输出）。"""

    path: str
    normalized: str
    exists: bool = False
    is_directory: bool = False
    readable: bool = False
    writable: bool = False
    status: str = "ok"            # ok / blocked / warning
    risk_level: str = "none"      # none / low / medium / high
    blocked: bool = False
    warnings: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "normalized": self.normalized,
            "exists": self.exists,
            "is_directory": self.is_directory,
            "readable": self.readable,
            "writable": self.writable,
            "status": self.status,
            "risk_level": self.risk_level,
            "blocked": self.blocked,
            "warnings": list(self.warnings),
            "reasons": list(self.reasons),
        }


@dataclass
class Workspace:
    """工作区 —— 可持久化业务对象。"""

    workspace_id: str
    name: str
    project_path: str
    description: str = ""
    working_directory: str = ""
    extra_workspace_roots: List[str] = field(default_factory=list)
    default_agent_profile_id: str = ""
    default_model: str = ""
    permission_mode: str = "ask"
    chat_mode: str = "chat"
    include_tools: List[str] = field(default_factory=list)
    exclude_tools: List[str] = field(default_factory=list)
    include_skills: List[str] = field(default_factory=list)
    exclude_skills: List[str] = field(default_factory=list)
    include_mcp_servers: List[str] = field(default_factory=list)
    exclude_mcp_servers: List[str] = field(default_factory=list)
    ui_preferences: Dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    archived_at: str = ""
    path_risk_level: str = "none"
    path_warnings: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.workspace_id = _validate_id(self.workspace_id, "workspace_id")
        if not self.name or not str(self.name).strip():
            raise ValueError("name 不能为空")
        self.name = str(self.name).strip()
        self.project_path = str(self.project_path or "").strip()
        if not self.project_path:
            raise ValueError("project_path 不能为空")
        if not self.working_directory:
            self.working_directory = self.project_path
        self.permission_mode = _validate_enum(
            self.permission_mode, VALID_PERMISSION_MODES, "permission_mode", "ask")
        self.chat_mode = _validate_enum(self.chat_mode, VALID_CHAT_MODES, "chat_mode", "chat")
        self.status = _validate_enum(
            self.status, VALID_WORKSPACE_STATUSES, "status", "active")
        try:
            self.version = max(1, int(self.version))
        except (TypeError, ValueError):
            self.version = 1
        # 去重
        self.include_tools = _dedup(self.include_tools)
        self.exclude_tools = _dedup(self.exclude_tools)
        self.include_skills = _dedup(self.include_skills)
        self.exclude_skills = _dedup(self.exclude_skills)
        self.include_mcp_servers = _dedup(self.include_mcp_servers)
        self.exclude_mcp_servers = _dedup(self.exclude_mcp_servers)
        self.extra_workspace_roots = _dedup(self.extra_workspace_roots)

    def to_dict(self) -> dict:
        data = {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "project_path": self.project_path,
            "working_directory": self.working_directory,
            "extra_workspace_roots": list(self.extra_workspace_roots),
            "description": self.description,
            "default_agent_profile_id": self.default_agent_profile_id,
            "default_model": self.default_model,
            "permission_mode": self.permission_mode,
            "chat_mode": self.chat_mode,
            "include_tools": list(self.include_tools),
            "exclude_tools": list(self.exclude_tools),
            "include_skills": list(self.include_skills),
            "exclude_skills": list(self.exclude_skills),
            "include_mcp_servers": list(self.include_mcp_servers),
            "exclude_mcp_servers": list(self.exclude_mcp_servers),
            "ui_preferences": dict(self.ui_preferences),
            "status": self.status,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "path_risk_level": self.path_risk_level,
            "path_warnings": list(self.path_warnings),
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Workspace":
        if not isinstance(data, dict):
            raise ValueError("workspace 必须是 dict")
        return cls(
            workspace_id=str(data.get("workspace_id") or ""),
            name=str(data.get("name") or ""),
            project_path=str(data.get("project_path") or ""),
            working_directory=str(data.get("working_directory") or ""),
            extra_workspace_roots=list(data.get("extra_workspace_roots") or []),
            description=str(data.get("description") or ""),
            default_agent_profile_id=str(data.get("default_agent_profile_id") or ""),
            default_model=str(data.get("default_model") or ""),
            permission_mode=str(data.get("permission_mode") or "ask"),
            chat_mode=str(data.get("chat_mode") or "chat"),
            include_tools=list(data.get("include_tools") or []),
            exclude_tools=list(data.get("exclude_tools") or []),
            include_skills=list(data.get("include_skills") or []),
            exclude_skills=list(data.get("exclude_skills") or []),
            include_mcp_servers=list(data.get("include_mcp_servers") or []),
            exclude_mcp_servers=list(data.get("exclude_mcp_servers") or []),
            ui_preferences=dict(data.get("ui_preferences") or {}),
            status=str(data.get("status") or "active"),
            version=int(data.get("version") or 1),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            archived_at=str(data.get("archived_at") or ""),
            path_risk_level=str(data.get("path_risk_level") or "none"),
            path_warnings=list(data.get("path_warnings") or []),
        )


@dataclass
class AgentProfile:
    """智能体 Profile —— 可复用配置模板。"""

    profile_id: str
    name: str
    description: str = ""
    system_prompt: str = ""
    tools: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    mcp_servers: List[str] = field(default_factory=list)
    default_model: str = ""
    permission_mode: str = "ask"
    chat_mode: str = "chat"
    max_steps: int = 100
    include_tools: List[str] = field(default_factory=list)
    exclude_tools: List[str] = field(default_factory=list)
    ui_preferences: Dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    version: int = 1
    is_system: bool = False          # 系统内置模板（可复制，不直接覆盖）
    created_at: str = ""
    updated_at: str = ""
    archived_at: str = ""

    def __post_init__(self):
        self.profile_id = _validate_id(self.profile_id, "profile_id")
        if not self.name or not str(self.name).strip():
            raise ValueError("name 不能为空")
        self.name = str(self.name).strip()
        self.permission_mode = _validate_enum(
            self.permission_mode, VALID_PERMISSION_MODES, "permission_mode", "ask")
        self.chat_mode = _validate_enum(self.chat_mode, VALID_CHAT_MODES, "chat_mode", "chat")
        self.status = _validate_enum(self.status, VALID_PROFILE_STATUSES, "status", "active")
        try:
            self.max_steps = max(1, int(self.max_steps))
        except (TypeError, ValueError):
            self.max_steps = 100
        try:
            self.version = max(1, int(self.version))
        except (TypeError, ValueError):
            self.version = 1
        self.tools = _dedup(self.tools)
        self.skills = _dedup(self.skills)
        self.mcp_servers = _dedup(self.mcp_servers)
        self.include_tools = _dedup(self.include_tools)
        self.exclude_tools = _dedup(self.exclude_tools)

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "tools": list(self.tools),
            "skills": list(self.skills),
            "mcp_servers": list(self.mcp_servers),
            "default_model": self.default_model,
            "permission_mode": self.permission_mode,
            "chat_mode": self.chat_mode,
            "max_steps": self.max_steps,
            "include_tools": list(self.include_tools),
            "exclude_tools": list(self.exclude_tools),
            "ui_preferences": dict(self.ui_preferences),
            "status": self.status,
            "version": self.version,
            "is_system": bool(self.is_system),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentProfile":
        if not isinstance(data, dict):
            raise ValueError("profile 必须是 dict")
        return cls(
            profile_id=str(data.get("profile_id") or ""),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            system_prompt=str(data.get("system_prompt") or ""),
            tools=list(data.get("tools") or []),
            skills=list(data.get("skills") or []),
            mcp_servers=list(data.get("mcp_servers") or []),
            default_model=str(data.get("default_model") or ""),
            permission_mode=str(data.get("permission_mode") or "ask"),
            chat_mode=str(data.get("chat_mode") or "chat"),
            max_steps=int(data.get("max_steps") or 100),
            include_tools=list(data.get("include_tools") or []),
            exclude_tools=list(data.get("exclude_tools") or []),
            ui_preferences=dict(data.get("ui_preferences") or {}),
            status=str(data.get("status") or "active"),
            version=int(data.get("version") or 1),
            is_system=bool(data.get("is_system", False)),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            archived_at=str(data.get("archived_at") or ""),
        )


@dataclass
class WorkspaceSession:
    """工作区会话 —— 元数据唯一事实源（SQLite），不写 sessions_map.json。"""

    session_id: str
    workspace_id: str
    session_key: str = ""
    name: str = ""
    agent_profile_id: str = ""
    model: str = ""
    permission_mode: str = "ask"
    chat_mode: str = "chat"
    reasoning_level: str = "inherit"
    status: str = "active"
    client_config_version: int = 0
    last_snapshot_id: str = ""
    last_active_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    archived_at: str = ""
    error_message: str = ""
    is_busy: bool = False

    def __post_init__(self):
        self.session_id = _validate_id(self.session_id, "session_id")
        self.workspace_id = _validate_id(self.workspace_id, "workspace_id")
        self.permission_mode = _validate_enum(
            self.permission_mode, VALID_PERMISSION_MODES, "permission_mode", "ask")
        self.chat_mode = _validate_enum(self.chat_mode, VALID_CHAT_MODES, "chat_mode", "chat")
        self.reasoning_level = _validate_enum(
            self.reasoning_level, VALID_REASONING_LEVELS, "reasoning_level", "inherit")
        self.status = _validate_enum(self.status, VALID_SESSION_STATUSES, "status", "active")
        # session_key 由服务端按 workspace/session ID 生成，忽略客户端值（P1-M-04）
        self.session_key = f"workspace:{self.workspace_id}:{self.session_id}"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "session_key": self.session_key,
            "name": self.name,
            "agent_profile_id": self.agent_profile_id,
            "model": self.model,
            "permission_mode": self.permission_mode,
            "chat_mode": self.chat_mode,
            "reasoning_level": self.reasoning_level,
            "status": self.status,
            "client_config_version": self.client_config_version,
            "last_snapshot_id": self.last_snapshot_id,
            "last_active_at": self.last_active_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "error_message": self.error_message,
            "is_busy": self.is_busy,
        }


@dataclass
class RuntimeSnapshot:
    """运行快照 —— 消息运行期间配置冻结，修改不改变当前消息。"""

    snapshot_id: str
    workspace_id: str = ""
    workspace_session_id: str = ""
    agent_profile_id: str = ""
    agent_profile_version: int = 0
    workspace_version: int = 0
    session_client_config_version: int = 0
    model: str = ""
    permission_mode: str = "ask"
    chat_mode: str = "chat"
    reasoning_level: str = "inherit"
    working_directory: str = ""
    project_root: str = ""
    framework_root: str = ""
    agent_data_root: str = ""
    extra_workspace_roots: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    mcp_servers: List[str] = field(default_factory=list)
    system_prompt: str = ""
    prompt_hash: str = ""
    expected_prompt_hash: str = ""
    capability_hash: str = ""
    dedup_key: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "workspace_id": self.workspace_id,
            "workspace_session_id": self.workspace_session_id,
            "agent_profile_id": self.agent_profile_id,
            "agent_profile_version": self.agent_profile_version,
            "workspace_version": self.workspace_version,
            "session_client_config_version": self.session_client_config_version,
            "model": self.model,
            "permission_mode": self.permission_mode,
            "chat_mode": self.chat_mode,
            "reasoning_level": self.reasoning_level,
            "working_directory": self.working_directory,
            "project_root": self.project_root,
            "framework_root": self.framework_root,
            "agent_data_root": self.agent_data_root,
            "extra_workspace_roots": list(self.extra_workspace_roots),
            "tools": list(self.tools),
            "skills": list(self.skills),
            "mcp_servers": list(self.mcp_servers),
            "system_prompt": self.system_prompt,
            "prompt_hash": self.prompt_hash,
            "expected_prompt_hash": self.expected_prompt_hash,
            "capability_hash": self.capability_hash,
            "dedup_key": self.dedup_key,
            "created_at": self.created_at,
        }


def _dedup(values: List[str]) -> List[str]:
    """去重并保持顺序。"""
    if not values:
        return []
    out = []
    for v in values:
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out
