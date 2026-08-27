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
import threading
import time

logger = logging.getLogger("jk_agent.gateway")

# ============================================================
# B8 Catalog 缓存
#   - 注册表单例：内置工具注册表进程内只构建一次（get_catalog_registry）
#   - get_tools_catalog：TTL 30s 结果缓存（内置工具来自代码，不随磁盘变化）
#   - get_skills_catalog：SkillManager 单例 + SKILLS 目录 mtime 签名失效
#     （见 skills/manager.py 的类级签名缓存）
#   - 线程安全：所有共享状态读写均在 _CATALOG_LOCK 内完成（简单 Lock）
# ============================================================
_TOOLS_CATALOG_TTL = 30.0  # 秒
_CATALOG_LOCK = threading.RLock()
_CATALOG_REGISTRY = None      # 内置工具注册表单例
_SKILLS_MANAGER = None        # SkillManager 单例
_TOOLS_CATALOG_CACHE = None   # get_tools_catalog 结果缓存
_TOOLS_CATALOG_TS = 0.0       # 缓存构建时间戳（time.monotonic）


def _load_cfg():
    from core.config_loader import load_config
    try:
        return load_config()
    except Exception:
        return {}


def _acquire_catalog_lock_or_dump(timeout=10.0):
    import faulthandler
    got = _CATALOG_LOCK.acquire(timeout=timeout)
    if not got:
        try:
            logger.error("catalog lock held over %ss; dumping threads", timeout)
            import faulthandler as _fh
            _fh.dump_traceback()
        except Exception:
            pass
    return got


def get_catalog_registry():
    """内置工具注册表单例（B8 注册表单例复用）：进程内仅构建一次。

    只读复用：调用方不得向其注册/修改工具；仅用于 Catalog / Preview 等
    只读场景（构造参数与 catalog_service 原逻辑完全一致）。
    """
    global _CATALOG_REGISTRY
    if _CATALOG_REGISTRY is None:
        if not _acquire_catalog_lock_or_dump():
            return _build_registry()
        try:
            if _CATALOG_REGISTRY is None:
                _CATALOG_REGISTRY = _build_registry()
        finally:
            _CATALOG_LOCK.release()
    return _CATALOG_REGISTRY


def _build_registry():
    from tools import ToolRegistry
    from tools.builtin_tools import register_all_tools
    from tools.web_tools import register_web_tools
    registry = ToolRegistry()
    register_all_tools(registry, memory_manager=None, sandbox=None,
                       process_manager=None)
    register_web_tools(registry)
    return registry


def get_tools_catalog(registry=None) -> list:
    """内置工具 Catalog（排除系统保留/Skill/MCP）。

    B8：默认路径（registry=None）复用注册表单例 + TTL 30s 结果缓存；
    显式传入自定义 registry 时跳过缓存直接计算。
    """
    if registry is not None:
        return registry.get_catalog()
    global _TOOLS_CATALOG_CACHE, _TOOLS_CATALOG_TS
    now = time.monotonic()
    cached = _TOOLS_CATALOG_CACHE
    # 快速路径：无锁读缓存（仅返回浅拷贝，缓存内容视为不可变）
    if cached is not None and (now - _TOOLS_CATALOG_TS) < _TOOLS_CATALOG_TTL:
        return list(cached)
    if not _acquire_catalog_lock_or_dump():
        # 超时降级：本次请求现算，不写缓存、不阻塞事件循环
        return get_catalog_registry().get_catalog()
    try:
        now = time.monotonic()
        cached = _TOOLS_CATALOG_CACHE
        if cached is not None and (now - _TOOLS_CATALOG_TS) < _TOOLS_CATALOG_TTL:
            return list(cached)
        result = get_catalog_registry().get_catalog()
        _TOOLS_CATALOG_CACHE = result
        _TOOLS_CATALOG_TS = now
        return list(result)
    finally:
        _CATALOG_LOCK.release()


def get_skills_manager():
    """SkillManager 单例（B8）：load_all 走类级 mtime 签名缓存快速路径。"""
    global _SKILLS_MANAGER
    if _SKILLS_MANAGER is None:
        if not _acquire_catalog_lock_or_dump():
            from skills.manager import SkillManager
            from core.config_loader import _find_project_root
            return SkillManager(skills_dir=str(_find_project_root() / "SKILLS"))
        try:
            if _SKILLS_MANAGER is None:
                from skills.manager import SkillManager
                from core.config_loader import _find_project_root
                _SKILLS_MANAGER = SkillManager(
                    skills_dir=str(_find_project_root() / "SKILLS"))
        finally:
            _CATALOG_LOCK.release()
    return _SKILLS_MANAGER


def get_skills_catalog() -> list:
    """Skill 只读 Catalog（B8：SKILLS 目录 mtime 变化自动失效缓存）。"""
    mgr = get_skills_manager()
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
