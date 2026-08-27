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
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from gateway.webui.events import EventBus, SSEHandler
from gateway.webui.channel import WebuiChannel
from gateway.webui.glue import Glue
from gateway.webui.config_service import ConfigService
from gateway.plan_runtime import PlanRuntime
from core.goal import GoalRuntime
from core.subagent import SubagentRuntime
from core.runtime import ArtifactStore
from core.runtime.quality_gate import QualityGate
from gateway.webui.workspace_store import (
    AgentProfileStore,
    RuntimeSnapshotStore,
    WorkspaceDatabase,
    WorkspaceSessionStore,
    WorkspaceStore,
)
from gateway.webui.path_validator import PathValidator, default_validator
from gateway.conversation import (
    ConversationService,
    ConversationStore,
    OutboxPublisher,
)
from gateway.conversation.images import ImageStore

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


# 变更类 HTTP 方法：这些方法上的 /api/* 请求做 Content-Type 纵深校验
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_mismatch(origin: str, host: str) -> bool:
    """Origin 存在时的同源判定：null/无法解析/跨源一律视为不匹配。

    - 字面 "null"（沙盒 iframe、file:// 页面、跨源重定向发起的请求）
      没有可信源，直接拒绝——旧实现 urlparse("null").netloc == '' 会
      短路放行，构成 CSRF 绕过（P2）；
    - netloc 缺失（畸形 Origin）或与 request.host（含端口）不一致 → 跨源；
    - 同源浏览器请求会带同源 Origin，netloc 与 host 一致 → 放行。
    """
    if not origin:
        return False
    if origin.strip().lower() == "null":
        return True
    try:
        o_netloc = urlparse(origin).netloc
    except Exception:
        return True
    if not o_netloc:
        return True
    return o_netloc.lower() != (host or "").lower()


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
            # ---- 统一会话模型（gateway-unified-conversation-design）----
            self.conversation_store = ConversationStore(self.workspace_db)
            _conv_cfg = self.config.get("conversation", {}) or {}
            # L1-C13：全局 Turn 上限优先读统一键 gateway.agent.max_turns_concurrency，
            # 未配置时回退旧键 conversation.max_global_turns，最后默认 4。
            _mtc = ((self.config.get("gateway", {}).get("agent", {})
                     or {}).get("max_turns_concurrency"))
            if not _mtc:
                _mtc = _conv_cfg.get("max_global_turns", 4) or 4
            self.conversation_service = ConversationService(
                self.conversation_store,
                lambda event_type, payload: self.bus.publish(event_type, payload),
                max_global_turns=int(_mtc) or 4,
                image_store=ImageStore(
                    Path(runtime_store.path).parent / "images"))
            # 方案A：system 会话保留窗口（定时任务 sched:*/heartbeat:* 等，
            # origin='system'），配置 conversation.retention.system_days，默认 7 天。
            self.conversation_system_retention_days = int(
                ((_conv_cfg.get("retention") or {}).get("system_days", 7)) or 7)
            self.outbox_publisher = OutboxPublisher(
                self.conversation_store,
                lambda event_type, payload: self.bus.publish(event_type, payload))
            from gateway.conversation.bridge import ConversationBridge
            from gateway.conversation.runner import ConversationTurnRunner
            _buffer_max = int(((self.config.get("conversation") or {})
                               .get("buffer_max_bytes", 2 * 1024 * 1024)))
            self.conversation_bridge = ConversationBridge(
                self.conversation_service, buffer_max_bytes=_buffer_max)
            # P2：runtime.progress 运行中心跳 → EventBus（≥5s 节流由 bridge 控制；
            # payload 自带 session_key，UI/外部观察者可按会话过滤）。
            self.conversation_bridge.runtime_progress_publish = (
                lambda payload: self.bus.publish("runtime.progress", payload))
            self.conversation_runner = ConversationTurnRunner(self.dispatcher)
            # 渠道 /stop 联动（设计方案 11.7）：bridge 直接命中 runner 打断 Agent
            self.conversation_bridge._runner = self.conversation_runner
            # 工作区会话经统一链路执行：挂接快照冻结的运行上下文
            from gateway.webui.api_workspace import build_workspace_entry_context
            self.conversation_runner.set_workspace_context_provider(
                lambda wid, sid: build_workspace_entry_context(self, wid, sid))
            # plan/goal/subagent 后台任务的 entry 也要挂接工作区上下文，
            # 否则这些任务因缺少 runtime_context 走"非工作区"分支，权限档位被误判为 ask
            #（即便会话是 unreviewed/allow 也会误弹审批）。
            self.dispatcher.set_workspace_context_provider(
                lambda wid, sid: build_workspace_entry_context(self, wid, sid))
        else:
            self.conversation_store = None
            self.conversation_service = None
            self.outbox_publisher = None
            self.conversation_bridge = None
        ws_cfg = self.config.get("workspace", {})
        self.path_validator = PathValidator(
            allow_unc=bool(ws_cfg.get("allow_unc", False)),
            block_system_paths=bool(ws_cfg.get("block_system_paths", True)),
        )
        self.bus = EventBus()
        self.channel = WebuiChannel(self.bus)
        self._sse = SSEHandler(self.bus)
        self.plan_runtime = PlanRuntime(dispatcher, runtime_store, self.bus, artifact_store=self.artifact_store)
        self.goal_runtime = GoalRuntime(
            runtime_store, plan_manager=self.plan_runtime.manager,
            plan_executor=self.plan_runtime._executor,
            quality_gate=QualityGate(self.artifact_store),
            publish=self._publish_goal_event)
        self.plan_runtime.goal_runtime = self.goal_runtime
        # 停止会话时联动取消 Plan/Goal 后台任务（dispatcher.stop_session_runtime）。
        dispatcher._plan_runtime = self.plan_runtime
        dispatcher._goal_runtime = self.goal_runtime
        from core.goal import GoalRoundDriver
        self.goal_driver = GoalRoundDriver(
            self.goal_runtime, runtime_store, submit=self.dispatcher.submit_goal_task,
            wait=self.dispatcher.wait_runtime_task, session_key=self._runtime_session_key,
            publish=self._publish_goal_event,
            cancel=self.dispatcher.cancel_runtime_task)
        # P2：armed Goal 所在会话豁免 janitor 空闲回收——Goal 轮间隙/pause 期间
        # 会话可能长时间无活动，回收会丢失 proc 子进程会话（REPL/dev server 等
        # 纯内存态），下一轮重建实例后无法恢复。
        if self.session_mgr is not None:
            from gateway.dispatcher import Dispatcher

            def _goal_evict_guard(session_key: str) -> bool:
                try:
                    session_id = Dispatcher._runtime_session_id(session_key)
                    return bool(self.goal_runtime.has_armed_for_session(session_id))
                except Exception:
                    return False
            self.session_mgr.evict_guard = _goal_evict_guard
        self.subagent_runtime = SubagentRuntime(
            runtime_store, submit=self.dispatcher.submit_subagent_task,
            wait=self.dispatcher.wait_runtime_task, cancel=self.dispatcher.cancel_runtime_task,
            publish=lambda event, payload: self.bus.publish(event, payload))
        self.glue = Glue(self)
        # 让 dispatcher 能访问 Glue（apply_prefs 运行时切换权限档位依赖它）。
        dispatcher._webui_glue = self.glue
        self.config_service = ConfigService()
        self.scheduler = None   # 由 server 装配后注入（/api/scheduler 用）
        self.heartbeat = None
        self._probe_task = None
        # fire-and-forget 后台任务登记（队列 Turn 投递等；stop 时统一取消）
        self._fire_and_forget_tasks: set = set()
        # 运行期可用标记：结构化提问工具在未启动/已停止时 fail-closed
        self._started = False
        # status_providers：name -> callable，/api/status 组装时逐个调用
        self._status_providers: dict = {}
        # 队列出队后是否自动投递给 Agent 执行（设计方案 8.5；测试可关闭）
        self.conversation_auto_execute = bool(
            (self.config.get("conversation") or {}).get("auto_execute_on_send_next", True))

    # ---------- 生命周期 ----------

    async def start(self):
        self._started = True
        self.bus.bind_loop(asyncio.get_event_loop())
        self.dispatcher.register_channel(self.channel)
        if self.conversation_bridge is not None:
            self.dispatcher.set_conversation_bridge(self.conversation_bridge)
            self._wire_parent_stop_hook()
        if getattr(self, "conversation_runner", None) is not None:
            self.dispatcher.set_conversation_runner(self.conversation_runner)
            self.conversation_runner.start()
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
        if self.session_store is not None:
            try:
                reset_count = self.session_store.reset_stale_busy()
                if reset_count:
                    logger.warning("清理 %d 个网关重启后残留的工作区 busy 会话", reset_count)
            except Exception:
                logger.exception("清理工作区 stale busy 状态失败")
        # 统一会话：重启恢复（活动 Turn → interrupted）+ Outbox 补发
        if self.conversation_service is not None:
            try:
                recovered = self.conversation_service.recover_after_restart()
                if recovered.get("interrupted_turns"):
                    logger.warning(
                        "重启恢复：%d 个活动 Turn 置为 interrupted，%d 个 Steering 项复位",
                        recovered["interrupted_turns"], recovered["reset_queue_items"])
            except Exception:
                logger.exception("统一会话重启恢复失败")
            try:
                # system 会话保留窗口（方案A）：定时任务/心跳等 system 会话超期
                # 连同 turns/nodes 一并回收（窗口见 __init__ 的
                # conversation_system_retention_days，配置 conversation.retention.system_days）。
                self.conversation_service.cleanup(
                    system_retention_days=self.conversation_system_retention_days)
            except Exception:
                logger.exception("统一会话启动清理失败")
            await self.outbox_publisher.start()
            self.conversation_bridge.start_flusher()
        # 子任务对账：重启后把无重放源的 INTERRUPTED 子任务成员置终态，
        # 解除 team_members 中永久 "running" 的状态（P2 修复集成点）。
        if getattr(self, "subagent_runtime", None) is not None:
            try:
                reconciled = self.subagent_runtime.reconcile_interrupted()
                if reconciled:
                    logger.warning("子任务对账：%d 个中断子任务成员已置终态", reconciled)
            except Exception:
                logger.exception("子任务重启对账失败")
        logger.info("🖥️ WebUI 已启动（static: %s）", STATIC_DIR)

    def _runtime_session_key(self, session_id: str) -> str:
        # P4：sessions 表是注册态（几乎不变），加 60s TTL 内存缓存——Goal 事件
        # 高频期每条事件一次 SQLite 查询纯属浪费；TTL 保证新注册会话最迟 60s 生效。
        now = time.monotonic()
        cached = getattr(self, "_session_key_cache", None)
        if cached is None:
            cached = self._session_key_cache = {}
        hit = cached.get(session_id)
        if hit is not None and now - hit[1] < 60.0:
            return hit[0]
        with self.runtime_store.connection() as connection:
            row = connection.execute("SELECT session_key FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise RuntimeError("Goal session is not registered")
        cached[session_id] = (row["session_key"], now)
        return row["session_key"]

    def _wire_parent_stop_hook(self) -> None:
        """父 Turn 停止联动（设计方案 14.4）：
        停止父会话 Turn → 活动 Plan/Goal 自动暂停、关联 Subagent 自动取消。"""
        from gateway.dispatcher import Dispatcher

        async def pause_runtimes(session_key: str) -> None:
            session_id = Dispatcher._runtime_session_id(session_key)
            # 活动 Plan 暂停
            try:
                for plan in self.plan_runtime.manager.list(session_id):
                    if getattr(plan.status, "value", str(plan.status)) in ("active", "running"):
                        await self.plan_runtime.pause(plan.plan_id)
            except Exception:
                logger.exception("父 Turn 停止：Plan 暂停失败 %s", session_key)
            # 活动 Goal 暂停
            try:
                for goal in self.goal_runtime.list(session_id):
                    if getattr(goal.status, "value", str(goal.status)) in ("active", "armed"):
                        self.goal_runtime.pause(goal.goal_id)
            except Exception:
                logger.exception("父 Turn 停止：Goal 暂停失败 %s", session_key)
            # 关联 Subagent 取消
            try:
                await self.subagent_runtime.cancel_parent(session_id,
                                                          reason="parent_turn_stopped")
            except Exception:
                logger.exception("父 Turn 停止：Subagent 取消失败 %s", session_key)

        self.conversation_bridge.set_parent_stop_hook(pause_runtimes)

    def _publish_goal_event(self, event: str, payload: dict) -> None:
        """Attach durable session identity to every Goal lifecycle event."""
        data = dict(payload or {})
        session_id = str(data.get("session_id") or "")
        if session_id:
            try:
                session_key = self._runtime_session_key(session_id)
            except RuntimeError:
                session_key = ""
            if session_key:
                data["session_key"] = session_key
                if session_key.startswith("workspace:"):
                    parts = session_key.split(":", 2)
                    if len(parts) == 3:
                        data["workspace_id"], data["workspace_session_id"] = parts[1], parts[2]
        self.bus.publish(event, data)


    async def recover_persisted_plans(self) -> None:
        """Restart executable Plans and explicitly armed Goal drivers."""
        if not self.dispatcher.task_runtime_enabled:
            return
        for goal in self.goal_runtime.list_armed():
            if goal.current_task_id:
                self.goal_runtime.disarm(goal.goal_id, expected_version=goal.version)
            else:
                self.goal_driver.trigger(goal.goal_id)
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
        """注册 WebUI 运行期命令（统一会话链路执行；C1-③ 旧漏斗已退役）"""
        g = self.glue
        d = self.dispatcher
        d.add_command("/perm", g.handle_perm_command,
                      "/perm readonly|ask|allow|unreviewed — 权限档位切换",
                      "[ask|allow|unreviewed]")
        d.add_command("/plan-preview", g.handle_plan_preview_command,
                      "", "", client_hint="plan-flow")
        d.add_command("/plan", g.handle_plan_command,
                      "/plan <任务> — 生成结构化 Plan 并立即执行", "[任务]", client_hint="plan-flow")
        d.add_command("/goal", g.handle_goal_command,
                      "/goal <目标>|pause|resume|edit|clear — 管理长期 Goal", "[目标|pause|resume|edit|clear]")
        d.add_command("/subagent", g.handle_subagent_command,
                      "/subagent <任务> — 创建一个直接子 Agent", "[任务]")
        d.add_command("/reload-prompt", g.handle_reload_prompt_command,
                      "/reload-prompt — 重载提示词文件")
        d.add_command("/mcp", g.handle_mcp_command,
                      "/mcp reload|reconnect <name> — MCP 运行期管理",
                      "[reload|reconnect <name>]")

    async def stop(self):
        # pending 审批/问题 fail-closed（问题不替用户自动选择）
        self._started = False
        try:
            self.glue.question_bridge.fail_close_all()
        except Exception:
            pass
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
        # 取消 fire-and-forget 任务（队列 Turn 投递等），避免停止后悬挂执行
        pending_tasks = list(self._fire_and_forget_tasks)
        self._fire_and_forget_tasks.clear()
        for task in pending_tasks:
            if not task.done():
                task.cancel()
        await self.goal_driver.stop()
        await self.channel.stop()
        if getattr(self, "conversation_runner", None) is not None:
            await self.conversation_runner.stop()
        if getattr(self, "conversation_bridge", None) is not None:
            await self.conversation_bridge.stop_flusher()
        if getattr(self, "outbox_publisher", None) is not None:
            await self.outbox_publisher.stop()
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
        """Apply WebUI session identity, approvals, and structured capabilities."""
        self.glue.init_agent(agent, entry)
        # These adapters are native tools, so the capability descriptions are
        # included in the same system prompt/tool schema the model already uses.
        # They are registered only for root WebUI sessions; child sessions never
        # receive a second delegation layer.
        if not str(getattr(entry, "session_key", "")).startswith("subagent:"):
            from gateway.webui.runtime_tools import register_structured_capability_tools
            register_structured_capability_tools(agent, self, entry)

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
                pending_questions = self.glue.question_bridge.count_pending()
                return {
                    "enabled": True,
                    "workspaces": self.workspace_store.count(),
                    "profiles": self.profile_store.count(),
                    "active_sessions": sum(
                        1 for w in self.workspace_store.list(status="active")
                        if self.session_store is not None),
                    "pending_approvals": pending_approvals,
                    "pending_questions": pending_questions,
                    "schema_version": 9,
                }
            except Exception as exc:  # pragma: no cover
                return {"enabled": True, "error": str(exc)}
        self._status_providers["workspace"] = provider

    def register_routes(self, app: web.Application):
        app.middlewares.append(self._static_cache_control_middleware)
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
        # 统一会话（gateway-unified-conversation-design）
        from gateway.webui import api_conversation
        api_conversation.register_routes(app, self)
        app.router.add_get("/", self._redirect_index)
        # /ui/ 显式返回 index.html（须在 add_static 之前注册；
        # aiohttp 的 show_index 是"目录列表"语义，不会自动给 index.html）
        app.router.add_get("/ui/", self._serve_index)
        app.router.add_static("/ui/", STATIC_DIR)

    async def _serve_index(self, request: web.Request) -> web.Response:
        # index.html 永不缓存：资源 URL 已带版本号（?v=），index 本身必须
        # 每次拉新，否则浏览器缓存的旧 index 会引用旧版本资源（改动不生效）。
        resp = web.FileResponse(STATIC_DIR / "index.html")
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    async def _redirect_index(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/ui/")

    @web.middleware
    async def _static_cache_control_middleware(self, request: web.Request, handler):
        """/ui/ 静态资源统一 no-cache。

        背景：style.css / workspace.css 未带内容版本号（只有 JS bundle 是
        哈希文件名），若响应不带 Cache-Control，浏览器会按启发式规则缓存，
        前端改动后用户仍看到旧 CSS（虚拟化消息列表缺布局规则 → 会话格式乱）。
        JS/CSS 都很小，no-cache 只增加一次条件请求，换取每次刷新都拿最新资源。
        """
        resp = await handler(request)
        path = request.path
        if path.startswith("/ui/") and resp.status == 200:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    @web.middleware
    async def _guard_middleware(self, request: web.Request, handler):
        """环回约束 + 门禁组合 + Origin/Content-Type 检查（仅 /api/*、/ui* 与 /）"""
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
                ip_failure = None      # (status, message)
                token_failure = None
                if self._allowed_networks:
                    try:
                        remote_ip = ipaddress.ip_address(remote)
                    except ValueError:
                        ip_failure = (403, "invalid client IP")
                    else:
                        if not any(remote_ip in network
                                   for network in self._allowed_networks):
                            ip_failure = (403, "client IP is not allowed")
                if self._auth_token:
                    supplied = request.headers.get("Authorization", "")
                    bearer = supplied[7:] if supplied.startswith("Bearer ") else ""
                    try:
                        token_ok = bool(bearer) and hmac.compare_digest(
                            bearer.encode("utf-8"),
                            self._auth_token.encode("utf-8"))
                    except TypeError:
                        token_ok = False
                    if not token_ok:
                        token_failure = (401, "authentication required")
                # 门禁组合语义（P2 修复：原实现是"或"——命中 allowed_ips 即
                # 完全跳过 Bearer 校验）：
                #   两者都配置 → 必须同时满足；
                #   只配其一项 → 仅按所配项校验；
                #   都未配置  → 维持 fail-closed（旧实现对空 token 的 Bearer
                #               校验必然失败，行为一致）。
                if self._allowed_networks and self._auth_token:
                    failure = ip_failure or token_failure
                elif self._allowed_networks:
                    failure = ip_failure
                elif self._auth_token:
                    failure = token_failure
                else:
                    failure = (401, "authentication required")
                if failure is not None:
                    return web.json_response(
                        {"error": failure[1]}, status=failure[0])
            # Origin 同源校验（存在才校验）：缺失放行，不误杀同源 SSE/curl。
            # P2：origin=="null" 或 netloc 缺失/与 host 不符一律 403，
            # 不再因 netloc 为空而短路放行。
            origin = request.headers.get("Origin")
            if origin and _origin_mismatch(origin, request.host):
                return web.json_response(
                    {"error": "Origin 不匹配"}, status=403)
            # Content-Type 纵深校验（P2 CSRF）：变更类方法且携带实际 body、
            # 又显式声明了非 JSON Content-Type 的 /api/* 请求拒绝。浏览器跨站
            # 表单只能发 urlencoded/multipart/text-plain 且必带该头 → 被拦；
            # 合法前端要么 application/json（带 body），要么无 body 不带该头
            # （动作型端点，如 queue/send-next）→ 放行；无 body 的声明头
            # （如 aiohttp 客户端对空请求自动补的 octet-stream）没有可注入
            # 载荷，一并放行。跨站 JSON fetch 触发 CORS 预检，本服务不下发
            # CORS 头，预检必败。aiohttp request.json() 本身不校验该头。
            if request.method in _MUTATING_METHODS and path.startswith("/api/"):
                ctype = (request.headers.get("Content-Type") or "").strip().lower()
                if (ctype and "application/json" not in ctype
                        and request.can_read_body):
                    return web.json_response(
                        {"error": "Content-Type 必须是 application/json"},
                        status=415)
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
