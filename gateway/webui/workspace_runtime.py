# -*- coding: utf-8 -*-
"""
Workspace 运行链路（Phase 4）—— EffectiveConfigResolver / WorkspaceRuntimeService。

核心逻辑不依赖 HTTP，可单元测试：
- 有效配置解析（继承 / include / exclude / fallback / 缺失能力）
- RuntimeSnapshot 组装、去重、hash
- 运行时上下文（WorkspaceRuntimeContext）
- stale 判定
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from gateway.webui.workspace_models import (
    AgentProfile, RuntimeSnapshot, Workspace, WorkspaceRuntimeContext,
    WorkspaceSession,
)

logger = logging.getLogger("jk_agent.gateway")

# 系统保留工具：内部必需，任何 include 都不能启用/排除它们的管理面
# （cron/memory 等工具由 Profile 可选列表天然排除，这里保留 create_skill 等内部能力）
SYSTEM_INTERNAL_TOOLS = frozenset({
    "create_skill", "notes", "memory_search", "memory_update",
})


# ============================================================
# L5-P0-2：模块级缓存实例 —— ToolRegistry / SkillManager 复用
#
# 原实现每次 build_prompt / build_snapshot 都全量重建 ToolRegistry
# （register_all_tools 注册百余工具并生成描述）并 SkillManager.load_all()
# （读盘解析每个 SKILLS/*/skill.json + instruction.md），属 L5 链路热点。
# 改为模块级单例：
#   - ToolRegistry：工具注册来自代码，进程内恒定，只建一次；
#   - SkillManager：按 SKILLS 目录内容 mtime 指纹变化重建（skill.json /
#     instruction.md 任一文件 mtime 变化即重建，覆盖新增/编辑/删除）。
# 并发创建由 _registry_lock 串行化（多线程懒初始化安全）。
# ============================================================
_tool_registry_cache = None
_skill_manager_cache = None
_skill_mtime_fingerprint = None
_registry_lock = threading.Lock()


def _skills_fingerprint(skills_dir: str) -> float:
    """SKILLS 目录内容指纹：所有 skill.json / instruction.md 的最大 mtime。

    新增/删除技能目录会改变目录自身 mtime，内容编辑改变文件 mtime；
    任一变化都会让指纹改变，从而触发 SkillManager 重建。"""
    try:
        base = Path(skills_dir)
        if not base.is_dir():
            return 0.0
        latest = 0.0
        for folder in base.iterdir():
            if not folder.is_dir():
                continue
            for name in ("skill.json", "instruction.md"):
                try:
                    latest = max(latest, (folder / name).stat().st_mtime)
                except OSError:
                    pass
        return latest
    except OSError:
        return 0.0


def get_cached_tool_registry():
    """模块级 ToolRegistry 单例（工具注册来自代码，进程内恒定）。"""
    global _tool_registry_cache
    if _tool_registry_cache is None:
        with _registry_lock:
            if _tool_registry_cache is None:
                from tools import ToolRegistry
                from tools.builtin_tools import register_all_tools
                from tools.web_tools import register_web_tools
                reg = ToolRegistry()
                register_all_tools(reg, memory_manager=None, sandbox=None,
                                   process_manager=None)
                register_web_tools(reg)
                _tool_registry_cache = reg
    return _tool_registry_cache


def get_cached_skill_manager():
    """模块级 SkillManager 单例；SKILLS 目录内容 mtime 指纹变化时重建。"""
    global _skill_manager_cache, _skill_mtime_fingerprint
    from skills.manager import SkillManager
    from core.config_loader import _find_project_root
    skills_dir = str(_find_project_root() / "SKILLS")
    fingerprint = _skills_fingerprint(skills_dir)
    if (_skill_manager_cache is None
            or fingerprint != _skill_mtime_fingerprint):
        with _registry_lock:
            if (_skill_manager_cache is None
                    or fingerprint != _skill_mtime_fingerprint):
                mgr = SkillManager(skills_dir=skills_dir)
                try:
                    mgr.load_all()
                except Exception:
                    pass
                _skill_manager_cache = mgr
                _skill_mtime_fingerprint = fingerprint
    return _skill_manager_cache


@dataclass
class EffectiveConfig:
    """解析后的有效运行配置（冻结在 RuntimeSnapshot 中）。"""

    model: str = ""
    permission_mode: str = "ask"
    chat_mode: str = "chat"
    reasoning_level: str = "inherit"
    max_steps: int = 100
    tools: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    mcp_servers: List[str] = field(default_factory=list)
    extra_workspace_roots: List[str] = field(default_factory=list)
    framework_root: str = ""
    agent_data_root: str = ""
    project_root: str = ""
    working_directory: str = ""
    capability_hash: str = ""
    warnings: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "permission_mode": self.permission_mode,
            "chat_mode": self.chat_mode,
            "reasoning_level": self.reasoning_level,
            "max_steps": self.max_steps,
            "tools": list(self.tools),
            "skills": list(self.skills),
            "mcp_servers": list(self.mcp_servers),
            "extra_workspace_roots": list(self.extra_workspace_roots),
            "framework_root": self.framework_root,
            "agent_data_root": self.agent_data_root,
            "project_root": self.project_root,
            "working_directory": self.working_directory,
            "capability_hash": self.capability_hash,
            "warnings": list(self.warnings),
        }


def compute_capability_hash(config: EffectiveConfig) -> str:
    """能力指纹：工具/Skill/MCP/模型/权限/路径/步数任一变化都会改变 hash。"""
    payload = {
        "model": config.model,
        "permission_mode": config.permission_mode,
        "chat_mode": config.chat_mode,
        "reasoning_level": config.reasoning_level,
        "max_steps": config.max_steps,
        "tools": sorted(config.tools),
        "skills": sorted(config.skills),
        "mcp_servers": sorted(config.mcp_servers),
        "extra_workspace_roots": sorted(config.extra_workspace_roots),
        "framework_root": config.framework_root,
        "agent_data_root": config.agent_data_root,
        "project_root": config.project_root,
        "working_directory": config.working_directory,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cap:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _merge_lists(base: List[str], include: List[str], exclude: List[str],
                 available: Optional[set] = None) -> List[str]:
    """include 添加可用项，exclude 移除且优先；去重保序。"""
    merged = [x for x in base if available is None or x in available]
    for item in include or []:
        if item and item not in merged:
            if available is None or item in available:
                merged.append(item)
    exclude_set = set(exclude or [])
    return [x for x in merged if x not in exclude_set]


class EffectiveConfigResolver:
    """解析 Workspace + AgentProfile + Session 的有效配置。"""

    def __init__(self, gateway_config: Optional[dict] = None,
                 available_tools: Optional[set] = None,
                 available_skills: Optional[set] = None):
        self.gateway_config = gateway_config or {}
        self.available_tools = available_tools
        self.available_skills = available_skills

    def resolve(self, workspace: Workspace, profile: Optional[AgentProfile],
                session: Optional[WorkspaceSession]) -> EffectiveConfig:
        cfg = EffectiveConfig()
        gw = self.gateway_config

        # ---- 模型回退：Session > Workspace > Profile > Gateway ----
        cfg.model = (session.model if session and session.model else
                     workspace.default_model or
                     (profile.default_model if profile else "") or
                     str(gw.get("model") or ""))

        # ---- 权限：Session > Workspace > Profile > Gateway ----
        cfg.permission_mode = (
            session.permission_mode if session and session.permission_mode else
            workspace.permission_mode or
            (profile.permission_mode if profile else "") or
            str(gw.get("permission_mode") or "ask"))

        # ---- chat mode：Session > Workspace > Profile > Gateway ----
        cfg.chat_mode = (
            session.chat_mode if session and session.chat_mode else
            workspace.chat_mode or
            (profile.chat_mode if profile else "") or "chat")

        # Session selection only: inherit intentionally delegates to the model/provider config.
        cfg.reasoning_level = (session.reasoning_level if session else "inherit") or "inherit"

        cfg.max_steps = int(
            (profile.max_steps if profile and profile.max_steps else None)
            or gw.get("max_steps") or 100)

        # ---- 工具：Profile 用户选择 + Workspace include/exclude ----
        base_tools = list(profile.tools if profile else [])
        include_tools = list(workspace.include_tools or [])
        exclude_tools = list(workspace.exclude_tools or [])
        cfg.tools = _merge_lists(base_tools, include_tools, exclude_tools,
                                 available=self.available_tools)
        # 排除系统保留工具（任何 include 都不能启用管理工具）
        cfg.tools = [t for t in cfg.tools if t not in SYSTEM_INTERNAL_TOOLS]

        # ---- Skills：Profile 选择 + Workspace include/exclude ----
        base_skills = list(profile.skills if profile else [])
        include_skills = list(workspace.include_skills or [])
        exclude_skills = list(workspace.exclude_skills or [])
        cfg.skills = _merge_lists(base_skills, include_skills, exclude_skills,
                                  available=self.available_skills)

        # ---- MCP：Profile 选择 + Workspace include/exclude ----
        base_mcp = list(profile.mcp_servers if profile else [])
        cfg.mcp_servers = _merge_lists(
            base_mcp, list(workspace.include_mcp_servers or []),
            list(workspace.exclude_mcp_servers or []))

        # ---- 路径（三目录分离）----
        cfg.project_root = workspace.project_path
        cfg.working_directory = workspace.working_directory or workspace.project_path
        cfg.extra_workspace_roots = list(workspace.extra_workspace_roots or [])
        cfg.framework_root = str(self.gateway_config.get("framework_root") or "")

        # ---- warnings：缺失能力（基于请求列表检测，即使已被过滤）----
        requested_tools = list(base_tools) + list(include_tools)
        if self.available_tools is not None:
            for t in requested_tools:
                if t not in self.available_tools:
                    cfg.warnings.append({
                        "code": "TOOL_NOT_AVAILABLE",
                        "message": f"工具 {t} 不可用，已从有效配置中移除",
                    })
        requested_skills = list(base_skills) + list(include_skills)
        if self.available_skills is not None:
            for s in requested_skills:
                if s not in self.available_skills:
                    cfg.warnings.append({
                        "code": "SKILL_NOT_AVAILABLE",
                        "message": f"Skill {s} 不可用，已从有效配置中移除",
                    })

        cfg.capability_hash = compute_capability_hash(cfg)
        return cfg


class WorkspaceRuntimeService:
    """聚合 Store，构建/复用 RuntimeSnapshot 与运行时上下文。"""

    def __init__(self, module=None, *, workspace_store=None, profile_store=None,
                 session_store=None, snapshot_store=None,
                 gateway_config=None, framework_root=None,
                 available_tools=None, available_skills=None):
        self.module = module
        self.ws_store = workspace_store or (module.workspace_store if module else None)
        self.prof_store = profile_store or (module.profile_store if module else None)
        self.sess_store = session_store or (module.session_store if module else None)
        self.snap_store = snapshot_store or (module.snapshot_store if module else None)
        self.gateway_config = gateway_config or {}
        self.framework_root = framework_root or str(Path(__file__).resolve().parent.parent.parent)
        if available_tools is None:
            # L5-P0-2：复用模块级缓存实例（避免每次构造 Service 时全量注册）
            reg = get_cached_tool_registry()
            available_tools = set(reg.get_catalog_names()) if hasattr(reg, "get_catalog_names") \
                else {t["name"] for t in reg.get_catalog()}
        self.available_tools = available_tools
        if available_skills is None:
            try:
                mgr = get_cached_skill_manager()
                available_skills = {s.name for s in mgr.get_all_skills()}
            except Exception:
                available_skills = set()
        self.available_skills = available_skills
        self._resolver = EffectiveConfigResolver(
            gateway_config=self.gateway_config,
            available_tools=self.available_tools,
            available_skills=self.available_skills)

    # ---------- 解析 ----------

    def resolve(self, workspace: Workspace, profile: Optional[AgentProfile],
                session: Optional[WorkspaceSession]) -> EffectiveConfig:
        return self._resolver.resolve(workspace, profile, session)

    def load_workspace(self, workspace_id: str) -> Workspace:
        return self.ws_store.get(workspace_id)

    def load_profile(self, profile_id: str) -> Optional[AgentProfile]:
        if not profile_id:
            return None
        try:
            return self.prof_store.get(profile_id)
        except Exception:
            return None

    def load_session(self, workspace_id: str, session_id: str) -> WorkspaceSession:
        return self.sess_store.get_owned(workspace_id, session_id)

    # ---------- Snapshot ----------

    def build_snapshot(self, workspace_id: str, session_id: str,
                       reuse: bool = True) -> RuntimeSnapshot:
        """组装并（可选）去重复用 RuntimeSnapshot。

        L5-P0-2：先按 dedup_key 查快照命中即返回，再做昂贵的 prompt 组装
        （ToolRegistry 全量工具描述 + Skill 描述），未命中才走到 build_prompt。
        """
        workspace = self.load_workspace(workspace_id)
        session = self.load_session(workspace_id, session_id)
        profile_id = session.agent_profile_id or workspace.default_agent_profile_id
        profile = self.load_profile(profile_id)
        config = self.resolve(workspace, profile, session)

        # dedup_key：能力 hash + Profile 版本/内容 + Workspace/Session 版本，
        # 保证改 Prompt/版本后必然产生新快照。
        _dedup = self._snapshot_dedup_key(config, profile, profile_id,
                                          workspace, session)

        # L5-P0-2：dedup_key 命中直接复用已落库快照，跳过 build_prompt /
        # hash_prompt（原实现在 get_or_create 内部才去重，prompt 组装白做）。
        if reuse and self.snap_store is not None:
            existing = self.snap_store.get_by_dedup_key(_dedup)
            if existing is not None:
                return existing

        snapshot = RuntimeSnapshot(
            snapshot_id="",
            workspace_id=workspace_id,
            workspace_session_id=session_id,
            agent_profile_id=profile_id,
            agent_profile_version=profile.version if profile else 0,
            workspace_version=workspace.version,
            session_client_config_version=session.client_config_version,
            model=config.model,
            permission_mode=config.permission_mode,
            chat_mode=config.chat_mode,
            reasoning_level=config.reasoning_level,
            working_directory=config.working_directory,
            project_root=config.project_root,
            framework_root=self.framework_root,
            agent_data_root=self.gateway_config.get("agent_data_root", ""),
            extra_workspace_roots=config.extra_workspace_roots,
            tools=config.tools,
            skills=config.skills,
            mcp_servers=config.mcp_servers,
            system_prompt="",
            capability_hash=config.capability_hash,
            dedup_key=_dedup,
        )
        snapshot.system_prompt = self.build_prompt(snapshot, config)
        snapshot.expected_prompt_hash = self.hash_prompt(snapshot.system_prompt)
        snapshot.prompt_hash = snapshot.expected_prompt_hash
        if reuse and self.snap_store is not None:
            return self.snap_store.get_or_create(snapshot)
        return snapshot

    @staticmethod
    def _snapshot_dedup_key(config: EffectiveConfig, profile, profile_id: str,
                            workspace: Workspace, session: WorkspaceSession) -> str:
        """dedup_key：能力 hash + Profile 版本/内容 + Workspace/Session 版本。"""
        import hashlib as _hl
        _extra = {
            "profile_id": profile_id,
            "profile_version": profile.version if profile else 0,
            "profile_prompt_hash": _hl.sha256(
                (profile.system_prompt or "").encode("utf-8")).hexdigest()
                if profile else "",
            "workspace_version": workspace.version,
            "session_client_config_version": session.client_config_version,
        }
        return "snap:" + _hl.sha256(json.dumps(
            {"cap": config.capability_hash, **_extra},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    def build_prompt(self, snapshot: RuntimeSnapshot,
                     config: Optional[EffectiveConfig] = None) -> str:
        """按快照组装 System Prompt（与 PromptAssembler 一致）。"""
        config = config or EffectiveConfig(
            tools=snapshot.tools, skills=snapshot.skills,
            mcp_servers=snapshot.mcp_servers,
            working_directory=snapshot.working_directory,
            project_root=snapshot.project_root,
            framework_root=snapshot.framework_root,
        )
        from core.system_prompt import SystemPrompt
        builder = SystemPrompt(name="workspace-agent")
        profile = self.load_profile(snapshot.agent_profile_id)
        if profile is not None:
            builder.set_agent_profile_prompt(profile.system_prompt)
        builder.set_runtime_context(
            framework_root=snapshot.framework_root or self.framework_root,
            project_root=snapshot.project_root,
            working_directory=snapshot.working_directory,
        )
        if snapshot.workspace_id:
            from memory.manager import workspace_memory_dir
            builder.set_memory_context(
                memory_path=str(workspace_memory_dir(snapshot.workspace_id)),
                instruction="当用户询问与当前项目相关的问题时，优先调用 memory_search 检索本工作区长期记忆，再作答。",
            )
        # L5-P0-2：复用模块级 ToolRegistry 单例（原实现每次全量重建）
        reg = get_cached_tool_registry()
        tool_descs = reg.get_descriptions_for(snapshot.tools)
        skill_descs = self._build_skill_descs(snapshot.skills)
        mcp_descs = self._build_mcp_descs(snapshot.mcp_servers)
        return builder.build(tool_descs=tool_descs, skill_descs=skill_descs,
                             mcp_descs=mcp_descs)

    def _build_skill_descs(self, skill_names: List[str]) -> str:
        # L5-P0-2：复用模块级 SkillManager 单例（SKILLS 目录 mtime 变化才重建）
        try:
            mgr = get_cached_skill_manager()
            skills = {s.name: s for s in mgr.get_all_skills()}
        except Exception:
            skills = {}
        parts = []
        for name in skill_names:
            skill = skills.get(name)
            if skill:
                parts.append(f"  \u25b6 {skill.name}\n    描述: {skill.description}\n")
        return "\n".join(parts)

    def _build_mcp_descs(self, mcp_names: List[str]) -> str:
        parts = [f"  \u25b6 {name}（MCP server）" for name in mcp_names]
        return "\n".join(parts)

    @staticmethod
    def hash_prompt(prompt: str) -> str:
        return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    # ---------- 运行时上下文 ----------

    def build_runtime_context(self, snapshot: RuntimeSnapshot) -> WorkspaceRuntimeContext:
        return WorkspaceRuntimeContext(
            workspace_id=snapshot.workspace_id,
            workspace_session_id=snapshot.workspace_session_id,
            framework_root=Path(snapshot.framework_root or self.framework_root),
            agent_data_root=Path(snapshot.agent_data_root or self.framework_root),
            project_root=Path(snapshot.project_root),
            working_directory=Path(snapshot.working_directory or snapshot.project_root),
            extra_workspace_roots=[Path(p) for p in snapshot.extra_workspace_roots],
            permission_mode=snapshot.permission_mode,
        )

    # ---------- stale ----------

    def is_stale(self, snapshot: RuntimeSnapshot,
                 workspace: Optional[Workspace] = None,
                 session: Optional[WorkspaceSession] = None) -> bool:
        """Profile/Workspace/Session 任一版本变化 → stale。"""
        try:
            workspace = workspace or self.load_workspace(snapshot.workspace_id)
            session = session or self.load_session(
                snapshot.workspace_id, snapshot.workspace_session_id)
        except Exception:
            return True
        if workspace.version != snapshot.workspace_version:
            return True
        if session.client_config_version != snapshot.session_client_config_version:
            return True
        if snapshot.agent_profile_id:
            profile = self.load_profile(snapshot.agent_profile_id)
            if profile is None or profile.version != snapshot.agent_profile_version:
                return True
        return False

    def mark_stale(self, workspace_id: str, session_id: str) -> bool:
        """在 SessionEntry 上标记 stale（内存级）。返回是否已标记。"""
        if self.module is not None and self.module.session_mgr is not None:
            key = f"workspace:{workspace_id}:{session_id}"
            entry = self.module.session_mgr._sessions.get(key)
            if entry is not None:
                entry.config_stale = True
                return True
        return False
