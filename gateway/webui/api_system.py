# -*- coding: utf-8 -*-
"""
api_system.py —— MCP / Skills 端点（P3c）；Prompt / 设置 端点（P3d 追加）

MCP config 读写经 ConfigService（整表替换 mcp.servers）。
运行期生效经漏斗合成命令：/mcp reload · /mcp reconnect {name}。
"""

import asyncio
import json
import logging

from aiohttp import web

from core.config_writer import mask_key

logger = logging.getLogger("jk_agent.gateway")


def register_routes(app: web.Application, module):
    app.router.add_get("/api/mcp", _make_mcp(module))
    app.router.add_post("/api/mcp/servers", _make_mcp_write(module))
    app.router.add_put("/api/mcp/servers/{name}", _make_mcp_write(module))
    app.router.add_delete("/api/mcp/servers/{name}", _make_mcp_delete(module))
    app.router.add_post("/api/mcp/servers/{name}/reconnect",
                        _make_mcp_reconnect(module))
    app.router.add_post("/api/mcp/apply", _make_mcp_apply(module))
    app.router.add_get("/api/skills", _make_skills(module))
    app.router.add_get("/api/skills/meta", _make_skills_meta(module))
    app.router.add_get("/api/skills/{name}", _make_skill_detail(module))
    # Prompt 端点（P3d）
    app.router.add_get("/api/prompt/files", _make_prompt_files(module))
    app.router.add_get("/api/prompt/files/{name}", _make_prompt_read(module))
    app.router.add_put("/api/prompt/files/{name}", _make_prompt_write(module))
    app.router.add_post("/api/prompt/apply", _make_prompt_apply(module))
    app.router.add_get("/api/prompt/main-session", _make_main_session_get(module))
    app.router.add_put("/api/prompt/main-session", _make_main_session_put(module))
    app.router.add_post("/api/prompt/main-session/preview",
                        _make_main_session_preview(module))


def _err(text, status=400):
    return web.json_response({"error": text}, status=status)


async def _body(request):
    try:
        return await request.json()
    except (json.JSONDecodeError, Exception):
        return None


# ---------- MCP ----------

def _mask_server(s: dict) -> dict:
    """单个 server 条目脱敏：env / headers 逐值打码"""
    out = dict(s)
    for field in ("env", "headers"):
        if isinstance(out.get(field), dict):
            out[field] = {k: mask_key(str(v)) for k, v in out[field].items()}
    return out


def _live_mcp(module) -> dict:
    """聚合活跃会话的 MCP 连接态：{server: {sessions, initialized, tools}}"""
    live = {}
    for e in module.session_mgr.list_entries():
        entry = module.session_mgr._sessions.get(e["session_key"])
        agent = entry.agent if entry else None
        mgr = getattr(agent, "mcp_manager", None) if agent else None
        if not mgr:
            continue
        for name in mgr.list_connections():
            conn = mgr.get_connection(name)
            slot = live.setdefault(name, {"sessions": 0,
                                          "initialized": False, "tools": 0})
            slot["sessions"] += 1
            if conn is not None and conn.is_initialized:
                slot["initialized"] = True
            # 工具数：该会话注册表中属于此 server 的 MCP 工具
            reg = getattr(agent, "tool_registry", None)
            if reg is not None:
                prefix = f"{name}/"
                slot["tools"] = max(
                    slot["tools"],
                    sum(1 for t in getattr(reg, "_mcp_tool_names", [])
                        if t.startswith(prefix)))
    return live


# ---- 网关级 MCP 状态管理器（#2：常驻连接，不再每次探针后 close） ----
_probe_cache = {"at": 0.0, "data": {}}
_PROBE_TTL = 30.0


def _status_mgr(module, servers: list):
    """返回常驻 MCPClientManager；配置服务器集合变化时重建。"""
    from core.mcp_client import MCPClientManager, run_in_mcp_loop
    names = tuple(sorted((s.get("name") or "") for s in servers))
    mgr = getattr(module, "_mcp_status_mgr", None)
    if mgr is None or getattr(module, "_mcp_status_names", None) != names:
        if mgr is not None:
            try:
                run_in_mcp_loop(mgr.close_all(), timeout=10)
            except Exception:
                pass
        mgr = MCPClientManager()
        for s in servers:
            try:
                mgr.add_server(s)
            except Exception:
                pass
        module._mcp_status_mgr = mgr
        module._mcp_status_names = names
    return mgr


def close_status_mgr(module):
    """WebUI 停机时关闭常驻 MCP 状态连接（由 WebUIModule.stop 调用）。"""
    mgr = getattr(module, "_mcp_status_mgr", None)
    if mgr is not None:
        try:
            from core.mcp_client import run_in_mcp_loop
            run_in_mcp_loop(mgr.close_all(), timeout=10)
        except Exception:
            pass
        module._mcp_status_mgr = None


def _probe_mcp_sync(module, servers: list) -> dict:
    from core.mcp_client import run_in_mcp_loop
    mgr = _status_mgr(module, servers)
    out = {}
    try:
        run_in_mcp_loop(mgr.initialize_all(timeout=10), timeout=30)
        for name in mgr.list_connections():
            conn = mgr.get_connection(name)
            out[name] = {"initialized": bool(conn and conn.is_initialized),
                         "tools": 0}
        if any(v["initialized"] for v in out.values()):
            try:
                tools = run_in_mcp_loop(mgr.discover_all_tools(), timeout=15)
                for name, tl in (tools or {}).items():
                    if name in out:
                        out[name]["tools"] = len(tl)
            except Exception:
                pass
    except Exception as e:
        # 连接被服务端重置（Cannot write to closing transport 等）：
        # 丢弃坏会话，下次探针重建，避免反复对 closing transport 写入
        logger.warning("MCP 状态探针连接异常，将重建连接: %s", type(e).__name__)
        module._mcp_status_mgr = None
        return {}
    return out  # 不 close —— 连接保持常驻


async def _mcp_probe(module, servers: list) -> dict:
    import time as _t
    loop = asyncio.get_event_loop()
    if _t.time() - _probe_cache["at"] > _PROBE_TTL and servers:
        data = await loop.run_in_executor(None, _probe_mcp_sync, module, servers)
        _probe_cache["at"] = _t.time()
        _probe_cache["data"] = data
    return _probe_cache["data"]


def _make_mcp(module):
    async def handler(request):
        from core.config_writer import read_raw_config
        data, status = read_raw_config()
        servers = []
        if status != "corrupt":
            servers = data.get("mcp", {}).get("servers", []) or []
        live = _live_mcp(module)
        # 无活跃会话连接时，用探针反映真实连通性（#2）
        if not any(v.get("initialized") for v in live.values()):
            probe = await _mcp_probe(module, servers)
            for s in servers:
                name = s.get("name")
                if name and name not in live and name in probe:
                    live[name] = {"sessions": 0,
                                  "initialized": probe[name]["initialized"],
                                  "tools": probe[name]["tools"]}
        return web.json_response({
            "servers": [_mask_server(s) for s in servers],
            "live": live,
        })
    return handler


def _make_mcp_write(module):
    async def handler(request):
        body = await _body(request)
        if body is None:
            return _err("无效的 JSON")
        name = request.match_info.get("name")
        from core.config_writer import read_raw_config
        data, status = read_raw_config()
        if status == "corrupt":
            return _err("config.json 损坏，请人工修复", 500)
        servers = data.get("mcp", {}).get("servers", []) or []

        if request.method == "POST":
            # 整表传：body 即完整 server 条目
            if not body.get("name"):
                return _err("缺少 name 字段")
            servers = [s for s in servers if s.get("name") != body.get("name")]
            servers.append(body)
        else:  # PUT /servers/{name}
            body["name"] = name
            found = False
            new_list = []
            for s in servers:
                if s.get("name") == name:
                    # env/headers 空值 = 保留原值
                    merged = dict(s)
                    merged.update(body)
                    for field in ("env", "headers"):
                        nv = body.get(field)
                        if not nv and isinstance(s.get(field), dict):
                            merged[field] = s[field]
                    new_list.append(merged)
                    found = True
                else:
                    new_list.append(s)
            if not found:
                new_list.append(body)
            servers = new_list

        try:
            rev = await module.config_service.update_mcp_servers(servers)
        except Exception as e:
            return _err(str(e), 500)
        module.bus.publish("mcp.changed", {"action": "write", "rev": rev})
        return web.json_response({"ok": True, "count": len(servers), "rev": rev})
    return handler


def _make_mcp_delete(module):
    async def handler(request):
        name = request.match_info["name"]
        from core.config_writer import read_raw_config
        data, status = read_raw_config()
        if status == "corrupt":
            return _err("config.json 损坏，请人工修复", 500)
        servers = data.get("mcp", {}).get("servers", []) or []
        new_list = [s for s in servers if s.get("name") != name]
        if len(new_list) == len(servers):
            return _err(f"未找到 MCP 服务器: {name}", 404)
        rev = await module.config_service.update_mcp_servers(new_list)
        module.bus.publish("mcp.changed", {"action": "delete", "server": name})
        return web.json_response({"ok": True, "count": len(new_list), "rev": rev})
    return handler


def _broadcast(module, text: str) -> int:
    """向所有活跃会话漏斗广播合成命令，返回入队数"""
    import uuid
    from gateway.channels.base import InboundMessage
    count = 0
    for key in list(module.session_mgr._sessions.keys()):
        msg = InboundMessage(
            channel="webui", session_key=key,
            user_id="webui", user_name="WebUI",
            text=text, message_id=f"mcp-{uuid.uuid4().hex[:12]}",
        )
        asyncio.get_event_loop().create_task(module.dispatcher.on_inbound(msg))
        count += 1
    return count


def _make_mcp_apply(module):
    async def handler(request):
        n = _broadcast(module, "/mcp reload")
        _probe_cache["at"] = 0.0
        return web.json_response({"queued": n})
    return handler


def _make_mcp_reconnect(module):
    async def handler(request):
        name = request.match_info["name"]
        n = _broadcast(module, f"/mcp reconnect {name}")
        _probe_cache["at"] = 0.0  # 立即重探，状态随之刷新（#2）
        if n == 0:
            return web.json_response({"queued": 0,
                                      "note": "无活跃会话，已刷新连接探针"})
        return web.json_response({"queued": n})
    return handler


# ---------- Skills ----------

def _skill_manager():
    from skills.manager import SkillManager
    from core.config_loader import _find_project_root
    return SkillManager(skills_dir=str(_find_project_root() / "SKILLS"))


def _make_skills(module):
    async def handler(request):
        mgr = _skill_manager()
        skills = mgr.load_all()
        out = []
        for s in skills:
            d = s.to_dict()
            d["instruction_chars"] = len(s.instruction or "")
            out.append(d)
        return web.json_response({"skills": out})
    return handler


def _make_skills_meta(module):
    async def handler(request):
        mgr = _skill_manager()
        import platform
        d = mgr._skills_dir
        note = ""
        if platform.system() == "Linux":
            note = ("Linux 下 <repo>/SKILLS 与 skills/ 包是两个目录，"
                    "请确认技能放在 SKILLS/（大小写敏感）")
        return web.json_response({
            "skills_dir": str(d),
            "exists": d.exists(),
            "platform_note": note,
        })
    return handler


def _make_skill_detail(module):
    async def handler(request):
        name = request.match_info["name"]
        mgr = _skill_manager()
        mgr.load_all()
        skill = mgr.get_skill(name)
        if skill is None:
            return _err(f"未找到技能: {name}", 404)
        return web.json_response({
            "name": name,
            "instruction": skill.instruction or "",
            **skill.to_dict(),
        })
    return handler


# ---------- Prompt（P3d） ----------

# 白名单文件 → 是否注入（SystemPrompt 实读四文件，GUIDE.md 仅展示）
_PROMPT_FILES = {
    "AGENT.md": True, "SOUL.md": True, "TOOLS.md": True,
    "MEMORY.md": True, "GUIDE.md": False,
}
_PROMPT_MAX_BYTES = 64 * 1024


def _prompt_dir():
    from core.config_loader import _find_project_root
    return _find_project_root() / "prompt"


def _resolve_prompt_path(name: str):
    """路径安全：精确匹配白名单 + realpath containment 防符号链接逃逸"""
    if name not in _PROMPT_FILES:
        return None, f"非法文件名: {name}"
    base = _prompt_dir().resolve()
    target = (base / name).resolve()
    if target.parent != base:
        return None, "路径越界"
    return target, None


def _truncation_limit() -> int:
    from core.config_loader import load_config
    return int(load_config().get("prompt", {})
               .get("bootstrap_max_chars_per_file", 8000))


def _make_prompt_files(module):
    async def handler(request):
        out = []
        for name, injected in _PROMPT_FILES.items():
            p, _ = _resolve_prompt_path(name)
            exists = p is not None and p.exists()
            out.append({
                "name": name,
                "exists": exists,
                "size": p.stat().st_size if exists else 0,
                "mtime_ns": p.stat().st_mtime_ns if exists else 0,
                "injected": injected,
            })
        return web.json_response({"files": out})
    return handler


def _make_prompt_read(module):
    async def handler(request):
        name = request.match_info["name"]
        p, err = _resolve_prompt_path(name)
        if err:
            return _err(err, 400)
        if not p.exists():
            return _err(f"文件不存在: {name}", 404)
        try:
            content = p.read_text(encoding="utf-8")
        except OSError as e:
            return _err(f"读取失败: {e}", 500)
        return web.json_response({
            "name": name,
            "content": content,
            "size": p.stat().st_size,
            "mtime_ns": p.stat().st_mtime_ns,
            "truncation_limit": _truncation_limit(),
        })
    return handler


def _make_prompt_write(module):
    async def handler(request):
        name = request.match_info["name"]
        body = await _body(request)
        if body is None:
            return _err("无效的 JSON")
        content = body.get("content")
        base_mtime = body.get("base_mtime_ns")
        if content is None:
            return _err("缺少 content")

        p, err = _resolve_prompt_path(name)
        if err:
            return _err(err, 400)
        if len(content.encode("utf-8")) > _PROMPT_MAX_BYTES:
            return _err("内容超过 64KB 上限", 413)

        # 乐观并发：mtime_ns 不符 → 409 + 当前内容
        if p.exists() and base_mtime is not None:
            cur_mtime = p.stat().st_mtime_ns
            if int(base_mtime) != cur_mtime:
                return web.json_response({
                    "error": "文件已被修改",
                    "current_content": p.read_text(encoding="utf-8"),
                    "mtime_ns": cur_mtime,
                }, status=409)

        # tmp + os.replace 原子写
        import os
        tmp = p.parent / (p.name + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return _err(f"写入失败: {e}", 500)

        module.bus.publish("prompt.updated",
                           {"file": name, "applied_to": 0})
        resp = {"ok": True, "mtime_ns": p.stat().st_mtime_ns}
        if len(content) > _truncation_limit():
            resp["warning"] = "内容超过截断阈值，注入时将被截断"
        return web.json_response(resp)
    return handler


def _make_prompt_apply(module):
    async def handler(request):
        n = _broadcast(module, "/reload-prompt")
        module.bus.publish("prompt.updated", {"file": "", "applied_to": n})
        return web.json_response({"queued": n})
    return handler



# ---------- Prompt 页 · 主会话默认能力（tools / skills / MCP） ----------

_NON_WORKSPACE_SCOPE = "gateway:non-workspace"


def _read_main_session_caps() -> dict:
    """读取 gateway.webui.main_session 配置；缺省返回空 dict。"""
    from core.config_writer import read_raw_config
    data, status = read_raw_config()
    if status == "corrupt":
        raise ValueError("config.json 损坏，请人工修复后重试")
    gateway = data.get("gateway") or {}
    webui = gateway.get("webui") or {}
    caps = webui.get("main_session") or {}
    return {str(k): v for k, v in caps.items()} if isinstance(caps, dict) else {}


def _normalize_caps_value(value, field_name: str):
    """主会话能力数组规范化：None 保留为 null（继承全部），否则仅保留非空字符串并去重。"""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必须是数组")
    seen = set()
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} 只能包含非空字符串")
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _runtime_update_main_session_caps(module, caps: dict) -> None:
    """让运行中的 Dispatcher 在下次创建主会话 Agent 时读取最新能力子集。"""
    dispatcher = getattr(module, "dispatcher", None)
    if dispatcher is None:
        return
    dispatcher.agent_config["main_session_caps"] = dict(caps or {})


def _make_main_session_get(module):
    async def handler(request):
        from gateway.webui import catalog_service
        try:
            caps = _read_main_session_caps()
        except ValueError as exc:
            return _err(str(exc), 500)
        try:
            catalog = catalog_service.get_all_catalogs(module)
        except Exception as exc:
            logger.warning("主会话能力 Catalog 加载失败: %s", exc)
            catalog = {"tools": [], "skills": [], "mcp": {"servers": []}}
        return web.json_response({
            "session_key": _NON_WORKSPACE_SCOPE,
            "config": {
                "tools": caps.get("tools"),
                "skills": caps.get("skills"),
                "mcp_servers": caps.get("mcp_servers"),
            },
            "catalog": catalog,
        })
    return handler


def _make_main_session_put(module):
    async def handler(request):
        body = await _body(request)
        if body is None:
            return _err("无效的 JSON")
        try:
            tools = _normalize_caps_value(body.get("tools"), "tools")
            skills = _normalize_caps_value(body.get("skills"), "skills")
            mcp_servers = _normalize_caps_value(
                body.get("mcp_servers"), "mcp_servers")
        except ValueError as exc:
            return _err(str(exc), 400)

        from core.config_writer import read_raw_config, backup_file, write_config
        from core.config_loader import load_config

        data, status = read_raw_config()
        if status == "corrupt":
            return _err("config.json 损坏，请人工修复后重试", 500)

        if mcp_servers is not None:
            servers = data.get("mcp", {}).get("servers") or []
            configured = {
                s.get("name")
                for s in servers
                if isinstance(s, dict) and s.get("name")
            }
            unknown = [n for n in mcp_servers if n not in configured]
            if unknown:
                return _err("未配置的 MCP 服务器: " + ", ".join(unknown), 400)

        gateway = data.setdefault("gateway", {})
        webui = gateway.setdefault("webui", {})
        webui["main_session"] = {
            "tools": tools,
            "skills": skills,
            "mcp_servers": mcp_servers,
        }
        try:
            backup_file()
            write_config(None, data)
            load_config(force_reload=True)
        except OSError as exc:
            return _err(str(exc), 500)

        _runtime_update_main_session_caps(module, webui["main_session"])
        module.bus.publish("config.updated",
                           {"section": "gateway.webui.main_session"})
        return web.json_response({"ok": True, "config": webui["main_session"]})
    return handler


def _make_main_session_preview(module):
    async def handler(request):
        body = await _body(request)
        if body is None:
            return _err("??? JSON")

        try:
            tools = _normalize_caps_value(body.get("tools"), "tools")
            skills = _normalize_caps_value(body.get("skills"), "skills")
            mcp_servers = _normalize_caps_value(
                body.get("mcp_servers"), "mcp_servers")
        except ValueError as exc:
            return _err(str(exc), 400)

        from gateway.webui import catalog_service
        from gateway.webui import prompt_preview
        from tools import ToolRegistry
        from tools.builtin_tools import register_all_tools
        from tools.web_tools import register_web_tools
        from skills.manager import SkillManager
        from core.config_loader import _find_project_root, load_config
        from pathlib import Path

        _cfg = load_config()
        _workspace_cfg = _cfg.get("permission", {}).get("workspace", "./workspace")
        _workspace_path = Path(_workspace_cfg)
        if not _workspace_path.is_absolute():
            _workspace_path = _find_project_root() / _workspace_path

        # null ??????????????? catalog ????
        try:
            tool_catalog = catalog_service.get_tools_catalog()
            skill_catalog = catalog_service.get_skills_catalog()
            mcp_catalog = catalog_service.get_mcp_catalog(module)
        except Exception as exc:
            logger.warning("??????? catalog ????: %s", exc)
            tool_catalog, skill_catalog = [], []
            mcp_catalog = {"servers": []}

        all_tools = [t.get("name") for t in tool_catalog if t.get("name")]
        all_skills = [s.get("id") or s.get("name") for s in skill_catalog]
        all_mcp = [s.get("name") for s in (mcp_catalog or {}).get("servers", [])
                   if s.get("name")]

        selected_tools = all_tools if tools is None else tools
        selected_skills = all_skills if skills is None else skills
        selected_mcp = all_mcp if mcp_servers is None else mcp_servers

        registry = ToolRegistry()
        register_all_tools(registry, memory_manager=None, sandbox=None,
                           process_manager=None)
        register_web_tools(registry)

        skill_mgr = SkillManager(skills_dir=str(_find_project_root() / "SKILLS"))
        try:
            skill_mgr.load_all()
        except Exception:
            pass

        profile = type("PreviewProfile", (), {
            "name": "Gateway Non-workspace",
            "system_prompt": "",
            "tools": list(selected_tools),
            "skills": list(selected_skills),
            "mcp_servers": list(selected_mcp),
        })()

        try:
            data = prompt_preview.build_preview(
                profile,
                tool_registry=registry,
                skill_manager=skill_mgr,
                framework_root=str(_find_project_root()),
                project_root=str(_find_project_root()),
                working_directory=str(_workspace_path.resolve()),
            )
        except Exception as exc:
            logger.exception("????? preview failed")
            return _err(str(exc), status=500)
        return web.json_response(data)
    return handler

