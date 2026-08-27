# -*- coding: utf-8 -*-
"""
Prompt 预览服务（Phase 2）—— 将未保存 Profile + 可选 Workspace/Session 上下文
转为与运行期 SystemPrompt.build() 对齐的完整 Prompt、分区、provider tools schema。

纯函数/只读：不创建 Agent、不连接 MCP、不写 DB。
"""

from __future__ import annotations

import hashlib
import logging

from core.system_prompt import (
    SystemPrompt,
    _DEFAULT_SOUL,
    _DEFAULT_TOOLS,
    _NATIVE_TOOL_RULES,
    _estimate_tokens,
)
from tools.registry import SYSTEM_RESERVED_TOOLS, ToolNameCodec

logger = logging.getLogger("jk_agent.gateway")

# 超出该字符数给 warning（与运行期约束一致）
_MAX_PROFILE_PROMPT_CHARS = 16000


def live_mcp_tools(module, selected_names=None) -> list[dict]:
    """从已运行 Agent 的注册表读取已发现 MCP schema；不建立新连接。"""
    selected = set(selected_names or [])
    found = {}
    try:
        entries = module.session_mgr.list_entries()
        sessions = getattr(module.session_mgr, "_sessions", {})
        for entry in entries:
            session = sessions.get(entry.get("session_key"))
            agent = getattr(session, "agent", None) if session else None
            registry = getattr(agent, "tool_registry", None)
            if registry is None:
                continue
            provider_tools, mapping = registry.get_provider_tools()
            for item in provider_tools:
                function = item.get("function", {})
                internal = mapping.get(function.get("name"), "")
                if not internal.startswith(tuple(f"{name}/" for name in selected)):
                    continue
                found[internal] = item
    except Exception:
        logger.exception("读取已发现 MCP 预览 schema 失败")
    return [found[name] for name in sorted(found)]


def _prepare(profile, framework_root=None, project_root=None,
             working_directory=None, memory_path=None,
             memory_instruction=None, tool_registry=None,
             skill_manager=None, mcp_tools=None):
    """创建与运行期相同的 SystemPrompt builder 和各能力文本。"""
    builder = SystemPrompt(name=getattr(profile, "name", "agent"))
    builder.set_agent_profile_prompt(getattr(profile, "system_prompt", "") or "")

    roots = {}
    if framework_root:
        roots["framework_root"] = framework_root
    if project_root:
        roots["project_root"] = project_root
    if working_directory:
        roots["working_directory"] = working_directory
    if roots:
        builder.set_runtime_context(**roots)
    if memory_path:
        builder.set_memory_context(memory_path=memory_path,
                                   instruction=memory_instruction)

    tool_names = list(getattr(profile, "tools", None) or [])
    runtime_tool_names = [
        name for name in SYSTEM_RESERVED_TOOLS
        if tool_registry is not None and tool_registry.get_tool(name) is not None
    ]
    prompt_tool_names = list(dict.fromkeys(tool_names + runtime_tool_names))
    skill_names = list(getattr(profile, "skills", None) or [])
    mcp_names = list(getattr(profile, "mcp_servers", None) or [])

    if tool_registry is not None:
        tool_descs = tool_registry.get_descriptions_for(prompt_tool_names)
    else:
        tool_descs = "（当前没有可用工具）"

    skill_descs = ""
    if skill_manager is not None:
        parts = []
        skills = {s.name: s for s in skill_manager.get_all_skills()}
        for name in skill_names:
            skill = skills.get(name)
            if skill is not None:
                parts.append(f"  \u25b6 {skill.name}\n    描述: {skill.description}\n")
        skill_descs = "\n".join(parts)

    # 预览不主动建立 MCP 连接。若已有运行中会话发现了真实工具，则展示
    # 已发现的描述；否则明确显示 server 已选择、工具需运行时发现。
    mcp_descs = ""
    if mcp_tools:
        parts = []
        for item in mcp_tools:
            function = item.get("function", item) if isinstance(item, dict) else {}
            name = function.get("name", "unknown")
            description = function.get("description", "")
            parts.append(f"  \u25b6 {name}\n    描述: {description}")
        mcp_descs = "\n".join(parts)
    elif mcp_names:
        mcp_descs = "\n".join(
            f"  \u25b6 {name}（已选择；工具将在运行时连接后发现）"
            for name in mcp_names
        )

    return builder, prompt_tool_names, skill_names, mcp_names, tool_descs, skill_descs, mcp_descs


def build_sections(profile, workspace=None, session=None,
                   tool_registry=None, skill_manager=None,
                   framework_root=None, project_root=None,
                   working_directory=None, memory_path=None,
                   memory_instruction=None, mcp_tools=None) -> list[dict]:
    """构建与运行时内容对应的 Prompt 分区。"""
    (builder, _tool_names, _skill_names, _mcp_names, tool_descs,
     skill_descs, mcp_descs) = _prepare(
        profile, framework_root=framework_root, project_root=project_root,
        working_directory=working_directory, memory_path=memory_path,
        memory_instruction=memory_instruction, tool_registry=tool_registry,
        skill_manager=skill_manager, mcp_tools=mcp_tools)

    sections = []

    def _add(name, content):
        content = content or ""
        sections.append({
            "name": name,
            "content": content,
            "chars": len(content),
            "estimated_tokens": _estimate_tokens(content),
        })

    # 这些 section 与 SystemPrompt._build_static/_build_dynamic 使用相同的
    # 来源；动态区整体也单独显示，避免用户只看到静态片段。
    soul = builder._load_prompt_file("SOUL.md") or _DEFAULT_SOUL.format(
        name=getattr(profile, "name", "agent"))
    tools_md = builder._load_prompt_file("TOOLS.md") or _DEFAULT_TOOLS
    agent_md = builder._load_prompt_file("AGENT.md") or ""
    if builder._agent_profile_prompt:
        _add("AGENT_PROFILE", f"[Agent Profile]\n{builder._agent_profile_prompt}")
    else:
        _add("FRAMEWORK_IDENTITY", soul)
    _add("TOOL_POLICY", tools_md)
    builtin_desc = builder._render_builtin_tools(tool_descs) if tool_descs else "（当前没有可用工具）"
    _add("BUILTIN_TOOLS", f"【内置工具】\n{builtin_desc}")
    _add("MCP_TOOLS", f"【MCP 工具】\n{mcp_descs or '（当前没有可用 MCP 工具）'}")
    _add("SKILLS", f"【可用技能】\n{skill_descs or '（当前没有可用技能）'}")

    _add("FRAMEWORK_RULES", _NATIVE_TOOL_RULES)
    _add("PROJECT_CONTEXT", f"【项目行为准则（AGENT.md）】\n{agent_md}" if agent_md else "")

    # _build_dynamic() 包含 bootstrap notice、日期、OS、工作目录、AGENT.md、
    # MEMORY.md、工作区记忆路径和会话指令，是此前预览缺失的完整动态区。
    _add("DYNAMIC_CONTEXT", builder._build_dynamic())
    return sections


def _provider_tools_for_preview(tool_registry, tool_names, skill_manager,
                                skill_names, mcp_tools=None) -> list[dict]:
    """返回运行时会传给 provider 的工具 schema（不含密钥等配置）。"""
    selected = set(tool_names or []) | set(SYSTEM_RESERVED_TOOLS)
    result = []
    seen = set()
    if tool_registry is not None:
        all_tools, mapping = tool_registry.get_provider_tools()
        for item in all_tools:
            function = item.get("function", {})
            internal_name = mapping.get(function.get("name"), function.get("name"))
            if internal_name in selected:
                result.append(item)
                seen.add(function.get("name"))

    if skill_manager is not None:
        try:
            from skills.skill_tool import SkillTool
            skills = {s.name: s for s in skill_manager.get_all_skills()}
            for name in skill_names or []:
                skill = skills.get(name)
                if skill is None:
                    continue
                tool = SkillTool(skill)
                provider_name = ToolNameCodec.encode(tool.name)
                if provider_name in seen:
                    continue
                result.append({"type": "function", "function": {
                    "name": provider_name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }})
                seen.add(provider_name)
        except Exception:
            logger.exception("preview skill provider schema failed")

    for item in mcp_tools or []:
        function = item.get("function", item) if isinstance(item, dict) else {}
        name = function.get("name")
        if not name or name in seen:
            continue
        result.append(item)
        seen.add(name)
    return result


def build_preview(profile, workspace=None, session=None,
                  tool_registry=None, skill_manager=None,
                  framework_root=None, project_root=None,
                  working_directory=None, memory_path=None,
                  memory_instruction=None, mcp_tools=None) -> dict:
    """返回完整 system prompt、分区及 provider tools schema。"""
    sections = build_sections(
        profile, workspace=workspace, session=session,
        tool_registry=tool_registry, skill_manager=skill_manager,
        framework_root=framework_root, project_root=project_root,
        working_directory=working_directory, memory_path=memory_path,
        memory_instruction=memory_instruction, mcp_tools=mcp_tools)

    (builder, tool_names, skill_names, _mcp_names, tool_descs,
     skill_descs, mcp_descs) = _prepare(
        profile, framework_root=framework_root, project_root=project_root,
        working_directory=working_directory, memory_path=memory_path,
        memory_instruction=memory_instruction, tool_registry=tool_registry,
        skill_manager=skill_manager, mcp_tools=mcp_tools)
    # 关键：full_prompt 直接走 SystemPrompt.build()，不是仅拼 section，保证
    # <SYSTEM_STATIC_CONTEXT> / <SYSTEM_DYNAMIC_CONTEXT> 包裹和运行时一致。
    full_prompt = builder.build(tool_descs=tool_descs, skill_descs=skill_descs,
                                mcp_descs=mcp_descs)
    total_chars = len(full_prompt)
    total_tokens = _estimate_tokens(full_prompt)
    prompt_hash = "sha256:" + hashlib.sha256(full_prompt.encode("utf-8")).hexdigest()

    warnings = []
    profile_prompt = getattr(profile, "system_prompt", "") or ""
    if len(profile_prompt) > _MAX_PROFILE_PROMPT_CHARS:
        warnings.append({
            "code": "PROFILE_PROMPT_TOO_LONG",
            "message": f"System Prompt 超过 {_MAX_PROFILE_PROMPT_CHARS} 字符，可能被截断或影响预算",
        })
    available_tools = set()
    if tool_registry is not None:
        available_tools = {t["name"] for t in tool_registry.get_catalog()}
    for name in (getattr(profile, "tools", None) or []):
        if name not in available_tools:
            warnings.append({"code": "TOOL_NOT_AVAILABLE", "message": f"工具 {name} 不在当前可选目录中"})
    available_skills = set()
    if skill_manager is not None:
        available_skills = {s.name for s in skill_manager.get_all_skills()}
    for name in (getattr(profile, "skills", None) or []):
        if name not in available_skills:
            warnings.append({"code": "SKILL_NOT_AVAILABLE", "message": f"Skill {name} 不存在或不可用"})

    provider_tools = _provider_tools_for_preview(
        tool_registry, tool_names, skill_manager, skill_names, mcp_tools=mcp_tools)
    effective = {
        "tools": [n for n in (getattr(profile, "tools", None) or []) if n in available_tools],
        "skills": [n for n in (getattr(profile, "skills", None) or []) if n in available_skills],
        "mcp_servers": list(getattr(profile, "mcp_servers", None) or []),
    }
    return {
        "sections": sections,
        "full_prompt": full_prompt,
        "total_chars": total_chars,
        "estimated_tokens": total_tokens,
        "expected_prompt_hash": prompt_hash,
        "warnings": warnings,
        "effective_capabilities": effective,
        "provider_tools": provider_tools,
        "provider_tools_count": len(provider_tools),
        "mcp_tools_live": bool(mcp_tools),
    }
