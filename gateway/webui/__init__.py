# -*- coding: utf-8 -*-
"""
WebUIModule —— 六模块控制台装配入口（P3a 基建）

装配：register_routes（REST + SSE + 静态 + 环回中间件）/ start / stop。
硬约束：所有后台 task 只在 start() 创建、stop() 取消（Windows 无事件循环信号）。
"""

import asyncio
import hmac
import ipaddress
import logging
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from gateway.webui.events import EventBus, SSEHandler
from gateway.webui.channel import WebuiChannel
from gateway.webui.glue import Glue
from gateway.webui.config_service import ConfigService
from gateway.plan_runtime import PlanRuntime
from core.runtime import ArtifactStore
from gateway.webui.workspace_store import (
    AgentProfileStore,
    RuntimeSnapshotStore,
    WorkspaceDatabase,
    WorkspaceSessionStore,
    WorkspaceStore,
)
from gateway.webui.path_validator import PathValidator, default_validator

logger = logging.getLogger("jk_agent.gateway")

STATIC_DIR = Path(__file__).parent / "static"
_PROBE_INTERVAL = 30  # channel.status 探针周期（秒）

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(remote: str) -> bool:
    if not remote:
        return False
    if remote in _LOOPBACK:
        return True
    if remote.startswith("127."):
        return True
    # IPv6 映射地址 ::ffff:127.0.0.1
    tail = remote.rsplit(":", 1)[-1]
    return tail in _LOOPBACK or tail.startswith("127.")


def _parse_allowed_networks(values) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse configured client IPs/CIDRs once when the WebUI starts."""
    if not values:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError("gateway.webui.allowed_ips 必须是 IP 或 CIDR 字符串列表")

    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError as exc:
            raise ValueError(
                f"gateway.webui.allowed_ips 包含无效地址: {value!r}"
            ) from exc
    return tuple(networks)


class WebUIModule:
    """WebUI 装配类"""

    def __init__(self, dispatcher, session_mgr, config: dict = None, runtime_store=None):
        self.dispatcher = dispatcher
        self.session_mgr = session_mgr
        self.config = config or {}
        self.runtime_store = runtime_store
        artifact_cfg = self.config.get("artifacts", {})
        self.artifact_store = ArtifactStore(
            runtime_store, artifact_cfg.get("root") or None,
            max_file_bytes=int(artifact_cfg.get("max_file_bytes", 50 * 1024 * 1024)),
        )
        self._auth_token = str(self.config.get("auth_token") or "")
        self._allowed_networks = _parse_allowed_networks(
            self.config.get("allowed_ips", []))
        # ---- Phase 1：Workspace 存储服务（共享同一个 runtime.db）----
        self.workspace_db = None
        self.workspace_store = None
        self.profile_store = None
        self.session_store = None
        self.snapshot_store = None
        if runtime_store is not None:
            self.workspace_db = WorkspaceDatabase(runtime_store=runtime_store)
            self.workspace_store = WorkspaceStore(self.workspace_db)
            self.profile_store = AgentProfileStore(self.workspace_db)
            self.session_store = WorkspaceSessionStore(self.workspace_db)
            self.snapshot_store = RuntimeSnapshotStore(self.workspace_db)
        ws_cfg = self.config.get("workspace", {})
        self.path_validator = PathValidator(
            allow_unc=bool(ws_cfg.get("allow_unc", False)),
            block_system_paths=bool(ws_cfg.get("block_system_paths", True)),
        )
        self.bus = EventBus()
        self.channel = WebuiChannel(self.bus)
        self._sse = SSEHandler(self.bus)
        self.plan_runtime = PlanRuntime(dispatcher, runtime_store, self.bus, artifact_store=self.artifact_store)
        self.glue = Glue(self)
        self.config_service = ConfigService()
        self.scheduler = None   # 由 server 装配后注入（/api/scheduler 用）
        self.heartbeat = None
        self._probe_task = None
        # status_providers：name -> callable，/api/status 组装时逐个调用
        self._status_providers: dict = {}

    # ---------- 生命周期 ----------

    async def start(self):
        self.bus.bind_loop(asyncio.get_event_loop())
        self.dispatcher.register_channel(self.channel)
        # Agent instances are created lazily by Dispatcher in the executor
        # thread. Keep WebUI-specific session/approval wiring on that
        # creation path so resumed sessions behave exactly like new ones.
        self.dispatcher.add_agent_initializer(self._init_agent)
        self._register_commands()
        self.session_mgr.on_created.append(self._on_session_created)
        self.session_mgr.on_evicted.append(self._on_session_evicted)
        self._probe_task = asyncio.create_task(self._probe_loop())
        self._mcp_warm_task = asyncio.create_task(self._mcp_warm())
        self._register_workspace_status_provider()
        self._seed_system_profiles()
        logger.info("🖥️ WebUI 已启动（static: %s）", STATIC_DIR)

    async def recover_persisted_plans(self) -> None:
        """Restart approved/active Plans after Gateway and TaskRuntime recovery."""
        if not self.dispatcher.task_runtime_enabled:
            return
        for plan in self.plan_runtime.manager.list_recoverable():
            try:
                self.plan_runtime.start(plan.plan_id)
                logger.info("recovered persisted Plan: %s (%s)", plan.plan_id, plan.status.value)
            except Exception:
                logger.exception("failed to recover persisted Plan: %s", plan.plan_id)

    async def _mcp_warm(self):
        """启动后预热常驻 MCP 连接（#2：启动即连上，不再等打开页面）"""
        try:
            await asyncio.sleep(2)
            from core.config_writer import read_raw_config
            from gateway.webui import api_system
            data, status = read_raw_config()
            if status == "corrupt":
                return
            servers = data.get("mcp", {}).get("servers", []) or []
            if servers:
                await api_system._mcp_probe(self, servers)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("MCP 预热跳过: %s", e)

    def _register_commands(self):
        """注册 WebUI 运行期命令（全部合成走漏斗，P3b）"""
        g = self.glue
        d = self.dispatcher
        d.add_command("/perm", g.handle_perm_command,
                      "/perm readonly|ask|allow|unreviewed — 权限档位切换",
                      "[ask|allow|unreviewed]")
        d.add_command("/plan-preview", g.handle_plan_preview_command,
                      "", "", client_hint="plan-flow")
        d.add_command("/reload-prompt", g.handle_reload_prompt_command,
                      "/reload-prompt — 重载提示词文件")
        d.add_command("/mcp", g.handle_mcp_command,
                      "/mcp reload|reconnect <name> — MCP 运行期管理",
                      "[reload|reconnect <name>]")

    async def stop(self):
        # pending 审批 fail-closed 置 n
        try:
            self.glue.bridge.fail_close_all()
        except Exception:
            pass
        if self._probe_task:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass
        if getattr(self, "_mcp_warm_task", None):
            self._mcp_warm_task.cancel()
        await self.channel.stop()
        self.bus.close_all()
        # 关闭常驻 MCP 状态连接（#2）
        try:
            from gateway.webui.api_system import close_status_mgr
            close_status_mgr(self)
        except Exception:
            pass
        logger.info("🖥️ WebUI 已停止")

    def _on_session_created(self, session_key, reason=""):
        self.bus.publish("session.created", {"session_key": session_key})

    def _init_agent(self, agent, entry):
        """Apply WebUI session identity and the configured approval mode."""
        self.glue.init_agent(agent, entry)

    def _on_session_evicted(self, session_key, reason=""):
        self.bus.publish("session.evicted",
                         {"session_key": session_key, "reason": reason})

    def add_status_provider(self, name: str, provider):
        """/api/status 段落 provider（scheduler/heartbeat 注册即自动出现）"""
        self._status_providers[name] = provider

    # ---------- 探针 ----------

    async def _probe_loop(self):
        while True:
            try:
                for name, ch in self.dispatcher.channels().items():
                    try:
                        st = ch.status()
                    except Exception as e:
                        st = {"status": "error", "error": str(e)}
                    self.bus.publish("channel.status",
                                     {"channel": name, **st})
            except Exception as e:
                logger.debug("webui 探针异常: %s", e)
            await asyncio.sleep(_PROBE_INTERVAL)

    # ---------- 路由 ----------

    def _seed_system_profiles(self):
        """Phase 6：预置内置默认智能体模板（方案 7.1/7.2，幂等）。"""
        if self.profile_store is None:
            return
        try:
            added = self.profile_store.seed_system_profiles()
            if added:
                logger.info("预置内置智能体模板 %d 个", added)
        except Exception as exc:  # pragma: no cover
            logger.warning("预置内置智能体失败: %s", exc)

    def _register_workspace_status_provider(self):
        """Phase 6：/api/status 聚合 Workspace 运行指标（只返回计数/健康，不含路径与秘密）。"""
        def provider():
            if self.workspace_store is None:
                return {"enabled": False}
            try:
                pending_approvals = len(self.glue.bridge.list_pending())
                return {
                    "enabled": True,
                    "workspaces": self.workspace_store.count(),
                    "profiles": self.profile_store.count(),
                    "active_sessions": sum(
                        1 for w in self.workspace_store.list(status="active")
                        if self.session_store is not None),
                    "pending_approvals": pending_approvals,
                    "schema_version": 9,
                }
            except Exception as exc:  # pragma: no cover
                return {"enabled": True, "error": str(exc)}
        self._status_providers["workspace"] = provider

    def register_routes(self, app: web.Application):
        app.middlewares.append(self._guard_middleware)
        app.router.add_get("/api/events", self._sse.handle)
        app.router.add_get("/api/status", self._handle_status)
        # 会话页端点（P3b）
        from gateway.webui import api_chat
        api_chat.register_routes(app, self)
        from gateway.webui import api_artifacts
        api_artifacts.register_routes(app, self)
        # MCP / Skills / Prompt 端点（P3c/P3d）
        from gateway.webui import api_system
        api_system.register_routes(app, self)
        # 设置页端点（P3d）
        from gateway.webui import api_settings
        api_settings.register_routes(app, self)
        # 定时任务端点（修 #6）
        from gateway.webui import api_scheduler
        api_scheduler.register_routes(app, self)
        # 智能体编辑（Phase 2）
        from gateway.webui import api_agents
        api_agents.register_routes(app, self)
        # 工作区管理（Phase 3）
        from gateway.webui import api_workspace
        api_workspace.register_routes(app, self)
        app.router.add_get("/", self._redirect_index)
        # /ui/ 显式返回 index.html（须在 add_static 之前注册；
        # aiohttp 的 show_index 是"目录列表"语义，不会自动给 index.html）
        app.router.add_get("/ui/", self._serve_index)
        app.router.add_static("/ui/", STATIC_DIR)

    async def _serve_index(self, request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _redirect_index(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/ui/")

    @web.middleware
    async def _guard_middleware(self, request: web.Request, handler):
        """环回约束 + Origin 检查（仅作用于 WebUI 的 /api/* 与 /ui/*）"""
        path = request.path
        guarded = path.startswith("/api/") or path.startswith("/ui") or path == "/"
        if guarded:
            remote = request.remote or ""
            if not _is_loopback(remote) and not self.config.get(
                    "allow_non_loopback", False):
                return web.json_response(
                    {"error": "WebUI 仅监听环回地址；如需远程访问请设置 "
                               "gateway.webui.allow_non_loopback（并知悉风险）"},
                    status=403)
            if not _is_loopback(remote):
                if self._allowed_networks:
                    try:
                        remote_ip = ipaddress.ip_address(remote)
                    except ValueError:
                        return web.json_response(
                            {"error": "invalid client IP"}, status=403)
                    if not any(remote_ip in network
                               for network in self._allowed_networks):
                        return web.json_response(
                            {"error": "client IP is not allowed"}, status=403)
                else:
                    supplied = request.headers.get("Authorization", "")
                    if (not supplied.startswith("Bearer ") or not self._auth_token
                            or not hmac.compare_digest(supplied[7:], self._auth_token)):
                        return web.json_response(
                            {"error": "authentication required"}, status=401)
            # Origin：仅当存在且与 host 不匹配时拒绝（防跨站发起环回请求；
            # 缺失放行，不误杀同源 SSE/curl）
            origin = request.headers.get("Origin")
            if origin:
                try:
                    o_host = urlparse(origin).netloc
                    if o_host and o_host != request.host:
                        return web.json_response(
                            {"error": "Origin 不匹配"}, status=403)
                except Exception:
                    pass
        return await handler(request)

    # ---------- API ----------

    async def _handle_status(self, request: web.Request) -> web.Response:
        sm = self.session_mgr
        entries = sm.list_entries()
        data = {
            "channels": {},
            "executor": sm.executor_stats(),
            "sessions": {
                "active": sm.active_count(),
                "max": sm.max_sessions,
                "busy": [e["session_key"] for e in entries if e["is_busy"]],
                "list": entries,
            },
        }
        for name, ch in self.dispatcher.channels().items():
            try:
                data["channels"][name] = ch.status()
            except Exception as e:
                data["channels"][name] = {"status": "error", "error": str(e)}
        for pname, provider in self._status_providers.items():
            try:
                data[pname] = {"present": True, **(provider() or {})}
            except Exception as e:
                data[pname] = {"present": False, "error": str(e)}
        return web.json_response(data)
