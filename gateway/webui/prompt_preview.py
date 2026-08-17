# -*- coding: utf-8 -*-
"""
Prompt 预览服务（Phase 2）—— 将未保存 Profile + 可选 Workspace/Session 上下文
转为 Prompt sections；统计每区字符数、估算 token、hash、warnings。
纯函数/只读：不创建 Agent、不连 MCP、不写 DB。
"""

from __future__ import annotations

import hashlib
import json
import logging

from core.system_prompt import SystemPrompt, _estimate_tokens

logger = logging.getLogger("jk_agent.gateway")

# 超出该字符数给 warning（与运行期约束一致）
_MAX_PROFILE_PROMPT_CHARS = 16000


def build_sections(profile, workspace=None, session=None,
                   tool_registry=None, skill_manager=None,
                   framework_root=None, project_root=None,
                   working_directory=None, memory_path=None,
                   memory_instruction=None) -> list[dict]:
    """构建 Profile Prompt 分区（与运行期 PromptAssembler 共用核心逻辑）。

    profile: AgentProfile 或含 system_prompt/tools/skills/mcp_servers 的对象。
    """
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
    skill_names = list(getattr(profile, "skills", None) or [])
    mcp_names = list(getattr(profile, "mcp_servers", None) or [])

    # 工具描述（选中子集）
    if tool_registry is not None:
        tool_descs = tool_registry.get_descriptions_for(tool_names)
    else:
        tool_descs = "（当前没有可用工具）"

    # Skill 描述（选中子集）
    skill_descs = ""
    if skill_manager is not None:
        parts = []
        skills = {s.name: s for s in skill_manager.get_all_skills()}
        for name in skill_names:
            skill = skills.get(name)
            if skill is not None:
                parts.append(f"  \u25b6 {skill.name}\n    描述: {skill.description}\n")
        skill_descs = "\n".join(parts)

    # Preview does not establish MCP connections, but it must still show the
    # selected server names. This keeps the preview consistent with the editor.
    mcp_descs = ""
    if mcp_names:
        parts = [
            f"  \u25b6 {name} (selected MCP server; tools are discovered after runtime connection)"
            for name in mcp_names
        ]
        mcp_descs = "\n".join(parts)

    return builder.build_sections(
        tool_descs=tool_descs, skill_descs=skill_descs, mcp_descs=mcp_descs)


def build_preview(profile, workspace=None, session=None,
                  tool_registry=None, skill_manager=None,
                  framework_root=None, project_root=None,
                  working_directory=None, memory_path=None,
                  memory_instruction=None) -> dict:
    """完整预览响应：sections / total / hash / warnings / effective capabilities。"""
    sections = build_sections(
        profile, workspace=workspace, session=session,
        tool_registry=tool_registry, skill_manager=skill_manager,
        framework_root=framework_root, project_root=project_root,
        working_directory=working_directory, memory_path=memory_path,
        memory_instruction=memory_instruction)

    total_chars = sum(s["chars"] for s in sections)
    total_tokens = sum(s["estimated_tokens"] for s in sections)
    full_prompt = "\n\n".join(s["content"] for s in sections if s["content"])
    prompt_hash = "sha256:" + hashlib.sha256(full_prompt.encode("utf-8")).hexdigest()

    warnings = []
    profile_prompt = getattr(profile, "system_prompt", "") or ""
    if len(profile_prompt) > _MAX_PROFILE_PROMPT_CHARS:
        warnings.append({
            "code": "PROFILE_PROMPT_TOO_LONG",
            "message": f"System Prompt 超过 {_MAX_PROFILE_PROMPT_CHARS} 字符，可能被截断或影响预算",
        })

    # 缺失能力 warning
    available_tools = set()
    if tool_registry is not None:
        available_tools = {t["name"] for t in tool_registry.get_catalog()}
    for name in (getattr(profile, "tools", None) or []):
        if name not in available_tools:
            warnings.append({
                "code": "TOOL_NOT_AVAILABLE",
                "message": f"工具 {name} 不在当前可选目录中",
            })

    available_skills = set()
    if skill_manager is not None:
        available_skills = {s.name for s in skill_manager.get_all_skills()}
    for name in (getattr(profile, "skills", None) or []):
        if name not in available_skills:
            warnings.append({
                "code": "SKILL_NOT_AVAILABLE",
                "message": f"Skill {name} 不存在或不可用",
            })

    effective = {
        "tools": [n for n in (getattr(profile, "tools", None) or [])
                  if n in available_tools],
        "skills": [n for n in (getattr(profile, "skills", None) or [])
                   if n in available_skills],
        "mcp_servers": list(getattr(profile, "mcp_servers", None) or []),
    }

    return {
        "sections": sections,
        "total_chars": total_chars,
        "estimated_tokens": total_tokens,
        "expected_prompt_hash": prompt_hash,
        "warnings": warnings,
        "effective_capabilities": effective,
    }
