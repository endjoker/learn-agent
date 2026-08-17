# -*- coding: utf-8 -*-
"""
Catalog 服务 —— 聚合 ToolRegistry / SkillManager / MCP / Models 的可选能力目录（Phase 2）。

- 工具：排除系统保留工具（create_skill/memory_*/cron_*/proc_*）。
- Skill：只读元数据，不实例化执行工具。
- MCP：只返回 server 名称、transport、状态、工具元数据；
        禁止返回 command env、headers、token、完整错误堆栈。
- Models：来自 config llm.models，不含 api_key。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("jk_agent.gateway")


def _load_cfg():
    from core.config_loader import load_config
    try:
        return load_config()
    except Exception:
        return {}


def get_tools_catalog(registry=None) -> list:
    """内置工具 Catalog（排除系统保留/Skill/MCP）。"""
    if registry is None:
        from tools import ToolRegistry
        registry = ToolRegistry()
        from tools.builtin_tools import register_all_tools
        register_all_tools(registry, memory_manager=None, sandbox=None,
                           process_manager=None)
        from tools.web_tools import register_web_tools
        register_web_tools(registry)
    return registry.get_catalog()


def get_skills_catalog() -> list:
    """Skill 只读 Catalog。"""
    from skills.manager import SkillManager
    from core.config_loader import _find_project_root
    mgr = SkillManager(skills_dir=str(_find_project_root() / "SKILLS"))
    try:
        mgr.load_all()
    except Exception as exc:
        logger.warning("加载 Skill Catalog 失败: %s", exc)
        return []
    return mgr.get_catalog()


def get_mcp_catalog(module=None) -> dict:
    """MCP server Catalog（脱敏）。

    返回 {"servers": [{name, transport, status, tools, available}], "live": {...}}
    """
    cfg = _load_cfg()
    servers = cfg.get("mcp", {}).get("servers", []) or []
    out = []
    live = {}
    if module is not None:
        try:
            live = _live_mcp_status(module)
        except Exception as exc:
            logger.warning("MCP live 状态聚合失败: %s", exc)
    for s in servers:
        name = s.get("name") or ""
        if not name:
            continue
        entry = live.get(name, {})
        out.append({
            "name": name,
            "transport": s.get("transport") or "stdio",
            "url": s.get("url") or "",
            "status": "connected" if entry.get("initialized") else "unknown",
            "available": bool(entry.get("initialized")),
            "tools": entry.get("tools", []),
            # 绝不返回 env/headers/token/command 细节
        })
    return {"servers": out, "live": live}


def _live_mcp_status(module) -> dict:
    """从活跃会话聚合 MCP 连接态（复用 api_system 的语义，独立实现）。"""
    live = {}
    for e in module.session_mgr.list_entries():
        entry = module.session_mgr._sessions.get(e["session_key"])
        agent = entry.agent if entry else None
        mgr = getattr(agent, "mcp_manager", None) if agent else None
        if not mgr:
            continue
        for name in mgr.list_connections():
            conn = mgr.get_connection(name)
            slot = live.setdefault(name, {"initialized": False, "tools": []})
            if conn is not None and conn.is_initialized:
                slot["initialized"] = True
            reg = getattr(agent, "tool_registry", None)
            if reg is not None:
                prefix = f"{name}/"
                tool_names = sorted(
                    t for t in getattr(reg, "_mcp_tool_names", [])
                    if t.startswith(prefix))
                slot["tools"] = sorted(set(slot["tools"]) | set(tool_names))
    return live


def get_models_catalog(module=None) -> list:
    """Models Catalog（不含 api_key）。"""
    cfg = _load_cfg()
    models = cfg.get("llm", {}).get("models", {}) or {}
    out = []
    for name, meta in models.items():
        item = {"id": name}
        if isinstance(meta, dict):
            item["provider"] = meta.get("provider", "cloud")
            item["context_length"] = meta.get("context_length", 0)
        else:
            item["provider"] = "cloud"
            item["context_length"] = 0
        out.append(item)
    return out


def get_all_catalogs(module=None) -> dict:
    """聚合全部 Catalog（API /api/agents/catalog 返回）。"""
    return {
        "tools": get_tools_catalog(),
        "skills": get_skills_catalog(),
        "mcp": get_mcp_catalog(module),
        "models": get_models_catalog(module),
    }
