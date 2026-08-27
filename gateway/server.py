# -*- coding: utf-8 -*-
"""
Gateway 服务器 —— aiohttp 装配、启动编排、优雅停机
"""

import asyncio
import hmac
import logging
import signal
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from gateway.config import get_gateway_config
from gateway.dispatcher import Dispatcher
from gateway.session import SessionManager
from core.config_loader import _find_project_root, load_config
from core.runtime import RuntimeStore

logger = logging.getLogger("jk_agent.gateway")


def _as_positive_int(value, default=None):
    """解析正整数配置；非法/非正数回退 default（L1-C13 并发上限解析）。"""
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


class GatewayServer:
    """Gateway 主服务器：aiohttp + channels + session 管理"""

    def __init__(self, config: dict = None):
        root_config = load_config()
        # None/空哨兵：调用方未显式传入时惰性读取配置文件 gateway 段；
        # 无论来源如何都显式合并 env 覆盖（FEISHU_*/WEBUI_AUTH_TOKEN）。
        if not config:
            config = root_config.get("gateway", {})
        self.config = get_gateway_config(config)
        runtime_store_cfg = config.get("runtime_store") or root_config.get("runtime_store", {})
        task_runtime_cfg = (config or {}).get("task_runtime") or root_config.get("task_runtime", {})
        artifact_cfg = dict((config or {}).get("artifacts") or root_config.get("artifacts", {}))
        artifact_root = Path(artifact_cfg.get("root", "./workspace/.agent/artifacts"))
        if not artifact_root.is_absolute():
            artifact_root = _find_project_root() / artifact_root
        artifact_cfg["root"] = str(artifact_root.resolve())
        # Plan metadata is durable even while TaskRuntime execution is disabled.
        raw_path = runtime_store_cfg.get("path", "./workspace/.agent/state/runtime.db")
        path = Path(raw_path)
        if not path.is_absolute():
            path = _find_project_root() / path
        self.runtime_store = RuntimeStore(
            path,
            wal=runtime_store_cfg.get("wal", True),
            busy_timeout_ms=runtime_store_cfg.get("busy_timeout_ms", 5000),
        )
        self.task_runtime_config = task_runtime_cfg
        self.artifact_config = artifact_cfg
        retention_cfg = root_config.get("retention", {})
        from core.runtime import ArtifactStore, RetentionManager
        self.retention_manager = RetentionManager(
            self.runtime_store, ArtifactStore(self.runtime_store, artifact_cfg["root"]),
            terminal_days=retention_cfg.get("terminal_days", 30),
            artifact_days=retention_cfg.get("artifact_days", 30))
        # 方案A：system 会话保留窗口（conversation.retention.system_days，
        # 默认 7 天）——统一会话周期维护（_retention_loop）使用。
        self.conversation_system_retention_days = int(
            ((root_config.get("conversation") or {}).get("retention") or {}
             ).get("system_days", 7) or 7)
        self.host = self.config.get("host", "127.0.0.1")
        self.port = self.config.get("port", 9120)
        # /health 与 /metrics 的可选 Bearer 门禁（P3 信息暴露最小修复）：
        # auth_token 与 webui 模块同源（channels.webui + webui 合并段）。
        # 未配置时两个端点维持免鉴权现状（环回部署场景）；配置后要求
        # Bearer——/health 会暴露 active_sessions、session_key 列表等
        # 运行信息，非环回监听时不应无鉴权可读。
        self._ops_auth_token = str(
            {**self.config.get("channels", {}).get("webui", {}),
             **self.config.get("webui", {})}.get("auth_token") or "")

        # 会话管理（从 self.config 提取子配置，避免重复 load_config + deepcopy）
        sess_cfg = self.config.get("sessions", {})
        agent_cfg = self.config.get("agent", {})
        # L1-C13：并发上限统一源。gateway.agent.max_turns_concurrency（新键，
        # 默认 4）为唯一来源；未配置时各点回退各自的旧键（worker_pool_size /
        # task_runtime.max_global_concurrency / webui.conversation.max_global_turns）。
        self._max_turns_concurrency = _as_positive_int(
            agent_cfg.get("max_turns_concurrency"), None)
        self.session_mgr = SessionManager(
            max_sessions=sess_cfg.get("max_sessions", 50),
            idle_timeout_minutes=sess_cfg.get("idle_timeout_minutes", 60),
            persist=sess_cfg.get("persist", True),
            worker_pool_size=(self._max_turns_concurrency
                              if self._max_turns_concurrency is not None
                              else _as_positive_int(
                                  self.config.get("worker_pool_size"), 4)),
            agent_config=agent_cfg,
        )
        # 调度器
        self.dispatcher = Dispatcher(
            session_mgr=self.session_mgr,
            agent_config={
                **agent_cfg,
                "soft_timeout_seconds": sess_cfg.get("soft_timeout_seconds", 90),
                "hard_timeout_seconds": sess_cfg.get("hard_timeout_seconds", 1200),
                # L6#12 长任务分池大小（plan/goal/scheduler 独立线程池，默认 2）
                "long_task_pool_size": self.config.get("long_task_pool_size", 2),
                # 所有非工作区会话的默认能力子集。
                # 为空/缺省时表示继承全部工具、技能和 MCP 服务。
                "main_session_caps": self.config.get("webui", {}).get(
                    "main_session", {}),
            },
            runtime_store=self.runtime_store,
            task_runtime_config=self.task_runtime_config,
        )
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self.scheduler = None
        self.heartbeat = None
        self.webui = None
        self._retention_task: Optional[asyncio.Task] = None

    async def start(self):
        """启动所有服务"""
        # 已启动组件清单（名称, 逆序 stop callable）：start() 中途失败时回滚
        self._rollback_steps: list = []
        self._loop = asyncio.get_event_loop()
        try:
            await self._start_inner()
        except Exception:
            logger.exception("Gateway 启动失败，逆序回滚已启动组件")
            await self._rollback_started()
            raise

    def _track_rollback(self, name: str, step) -> None:
        self._rollback_steps.append((name, step))

    async def _rollback_started(self) -> None:
        """best-effort 逆序回滚已启动组件（start() 中途失败时调用）。"""
        for name, step in reversed(self._rollback_steps):
            try:
                await step()
            except Exception as e:
                logger.warning("启动回滚阶段 %s 失败: %s", name, e)
        self._rollback_steps.clear()

    async def _start_inner(self):
        # L6#15 冷启动分阶段耗时：每阶段结束记 logger.info（毫秒级），
        # 用于定位启动瓶颈（孤儿进程回收 / retention / 会话 / dispatcher /
        # channels / 模块 / HTTP 装配）。
        phase_t0 = time.perf_counter()

        def _mark_phase(name: str) -> None:
            nonlocal phase_t0
            now = time.perf_counter()
            logger.info("冷启动阶段 %s 耗时 %.0fms", name, (now - phase_t0) * 1000)
            phase_t0 = now

        # 回收上次 Gateway 崩溃遗留的孤儿子进程（bash/python/proc_*）
        try:
            from core.orphan_processes import reap_stale_orphans
            _reaped = reap_stale_orphans()
            if _reaped:
                logger.info("回收孤儿子进程: %d", _reaped)
        except Exception as _exc:
            logger.warning("孤儿进程回收失败: %s", _exc)
        logger.info("🚀 Gateway 启动中 → %s:%d", self.host, self.port)
        _mark_phase("孤儿进程+配置校验")

        webui_cfg = {**self.config.get("channels", {}).get("webui", {}),
                     **self.config.get("webui", {})}
        is_loopback = self.host in {"127.0.0.1", "::1", "localhost"}
        if (webui_cfg.get("enabled", True)
                and (webui_cfg.get("allow_non_loopback") or not is_loopback)
                and not (webui_cfg.get("auth_token")
                         or webui_cfg.get("allowed_ips"))):
            raise ValueError(
                "非环回 WebUI 必须配置 gateway.webui.auth_token "
                "或 gateway.webui.allowed_ips")

        retention_cfg = load_config().get("retention", {})
        if retention_cfg.get("enabled", True):
            # retention collect 移入线程执行，避免阻塞事件循环
            await self._loop.run_in_executor(
                None, lambda: self.retention_manager.collect(dry_run=False))
            interval = max(60, int(retention_cfg.get("interval_seconds", 3600)))
            self._retention_task = asyncio.create_task(self._retention_loop(interval))
            self._track_rollback("retention", self._stop_retention)
        _mark_phase("retention")
        # 启动会话管理
        await self.session_mgr.start()
        self._track_rollback("session_mgr", self.session_mgr.stop)
        await self.dispatcher.start()
        self._track_rollback("dispatcher", self.dispatcher.stop)
        _mark_phase("session+dispatcher")

        # 启动 channels
        await self._start_channels()
        self._track_rollback("channels", self._stop_channels)
        _mark_phase("channels")

        # 启动 aiohttp
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        # Prometheus 文本格式 /metrics（与 /health 同级：配置 auth_token 时
        # 两端点均在 handler 内自查 Bearer，见 _require_ops_bearer；webui 的
        # _guard_middleware 只作用于 /api/* 与 /ui/*，不影响本端点）
        self._app.router.add_get("/metrics", self._handle_metrics)
        # debug channel 注册路由（门禁复用 webui.auth_token）。
        # P1 加固：默认 enabled=False（原默认 True 等于默认开放无鉴权 Agent
        # 执行入口）；需要端到端调试时在 gateway.channels.debug 显式开启。
        channels_cfg = self.config.get("channels", {})
        if channels_cfg.get("debug", {}).get("enabled", False):
            from gateway.channels.debug_channel import DebugChannel
            debug = DebugChannel(
                self.dispatcher,
                config={"auth_token": webui_cfg.get("auth_token", "")})
            self.dispatcher.register_channel(debug)
            debug.register_routes(self._app)

        # 定时任务（P1）
        sched_cfg = self.config.get("scheduler", {})
        if sched_cfg.get("enabled", True):
            from gateway.scheduler import Scheduler
            self.scheduler = Scheduler(sched_cfg, self.dispatcher, self.session_mgr)
            # 注入主循环引用（run_job 跨线程触发用）
            self.scheduler.set_event_loop(self._loop)
            self.dispatcher.register_channel(self.scheduler.channel)
            self.dispatcher.add_command(
                "/cron", self.scheduler.handle_command,
                "/cron [list|run|pause|resume|history|reload] — 定时任务管理")
            await self.scheduler.start()
            self._track_rollback("scheduler", self.scheduler.stop)

        # 心跳检查（P2，依赖 scheduler 引用做 defer_when_busy 判定）
        hb_cfg = self.config.get("heartbeat", {})
        if hb_cfg.get("enabled", True):
            from gateway.heartbeat import Heartbeat
            self.heartbeat = Heartbeat(
                hb_cfg, self.dispatcher, self.session_mgr, self.scheduler)
            self.dispatcher.register_channel(self.heartbeat.channel)
            self.dispatcher.add_command(
                "/heartbeat", self.heartbeat.handle_command,
                "/heartbeat [status|pause|resume|run] — 心跳管理")
            await self.heartbeat.start()
            self._track_rollback("heartbeat", self.heartbeat.stop)
        _mark_phase("scheduler+heartbeat")

        # WebUI 六模块控制台（P3a 基建，AppRunner 之前挂载）
        webui_ch_cfg = channels_cfg.get("webui", {})
        if webui_ch_cfg.get("enabled", True):
            from gateway.webui import WebUIModule
            # L1-C13：统一并发上限已配置（gateway.agent.max_turns_concurrency）
            # 时，覆盖 conversation 全局 Turn 并发上限，使三处并发上限同源
            # 派生；未配置时保持 webui.conversation.max_global_turns 原值。
            webui_module_cfg = {**webui_ch_cfg,
                                **self.config.get("webui", {}),
                                "artifacts": self.artifact_config}
            # 顶层 conversation 段（如 conversation.retention.system_days，方案A）
            # 合并进模块配置：webui init 模块的 config 是合并子集，不合并的话模块
            # 内 config["conversation"] 读不到顶层键（只能读到 webui.conversation.*）。
            # 模块级 webui.conversation.* / channels.webui.conversation.* 覆盖优先。
            webui_module_cfg["conversation"] = {
                **dict(self.config.get("conversation") or {}),
                **dict(webui_module_cfg.get("conversation") or {}),
            }
            if self._max_turns_concurrency is not None:
                conv_cfg = dict(webui_module_cfg.get("conversation") or {})
                conv_cfg["max_global_turns"] = self._max_turns_concurrency
                webui_module_cfg["conversation"] = conv_cfg
            self.webui = WebUIModule(
                self.dispatcher, self.session_mgr, webui_module_cfg,
                runtime_store=self.runtime_store)
            await self.webui.start()
            self.webui.register_routes(self._app)
            self._track_rollback("webui", self.webui.stop)
        _mark_phase("webui")

        # L1-C13 启动校验：三处并发上限同源一致性（仅告警，不阻断启动）
        self._validate_concurrency_uniform()

        # scheduler/heartbeat 注入 webui（/api/scheduler 用）+ 注册 status provider
        if self.webui:
            self.webui.scheduler = self.scheduler
            self.webui.heartbeat = self.heartbeat
            # D3：/api/status 聚合网关运行指标（turn/延迟/usage/guard/delta）
            self.webui.add_status_provider(
                "metrics", lambda: self.dispatcher.metrics())
            if self.scheduler:
                self.webui.add_status_provider(
                    "scheduler", lambda: self.scheduler.channel.status())
            if self.heartbeat:
                self.webui.add_status_provider(
                    "heartbeat", lambda: self.heartbeat.channel.status())

        # All channels are registered now; only at this point can persisted
        # task envelopes be safely rebound to their delivery contexts.
        await self.dispatcher.recover_channel_deliveries()
        await self.dispatcher.resume_persisted_tasks()
        if self.webui:
            await self.webui.recover_persisted_plans()

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._track_rollback("runner", self._cleanup_runner)
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self._site = site
        _mark_phase("aiohttp 装配+监听")

        logger.info("✅ Gateway 已启动 → http://%s:%d", self.host, self.port)
        logger.info("   /health 健康检查 | /debug/chat 调试通道")

    async def _retention_loop(self, interval_seconds: int):
        # 磁盘回收的每日闸门（VACUUM 是重操作，每日至多一次）
        self._last_reclaim_date: str = ""
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                # collect 移入线程执行，避免阻塞事件循环
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.retention_manager.collect(dry_run=False))
                # 磁盘空间回收（清理机制收尾 2026-08）：保留期删除只标空闲页，
                # 不还空间给 OS——每日至多一次按 freelist 占比触发收缩 +
                # 迁移备份轮换（bak 永不回收曾放大到 ~106MB）。executor 执行
                # （VACUUM 需独占与临时空间），失败不终止循环。
                try:
                    import datetime as _dt
                    today = _dt.date.today().isoformat()
                    if today != self._last_reclaim_date:
                        self._last_reclaim_date = today
                        runtime_store = getattr(self, "runtime_store", None)
                        if runtime_store is not None:
                            def _reclaim():
                                stats = runtime_store.reclaim_if_bloated()
                                rotated = runtime_store.rotate_backups()
                                if stats.get("triggered") or rotated:
                                    logger.info("磁盘回收完成: %s, bak 轮换 %d 份",
                                                stats, rotated)
                            await asyncio.get_running_loop().run_in_executor(
                                None, _reclaim)
                except Exception:
                    logger.exception("磁盘空间回收失败（不终止保留期循环）")
                # 统一会话周期维护（方案A）：system 会话（定时任务 sched:* 等）
                # 每次触发都会新增行，仅启动时清理一次不够，须按保留窗口周期回收
                #（conversation.retention.system_days，默认 7 天）。失败不终止循环。
                webui = getattr(self, "webui", None)
                svc = getattr(webui, "conversation_service", None) \
                    if webui is not None else None
                if svc is not None:
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            None, lambda: svc.cleanup(
                                system_retention_days=
                                self.conversation_system_retention_days))
                    except Exception:
                        logger.exception("统一会话周期维护失败")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("retention collection failed")

    async def _stop_retention(self):
        if self._retention_task:
            self._retention_task.cancel()
            try:
                await self._retention_task
            except asyncio.CancelledError:
                pass
            self._retention_task = None

    async def _stop_channels(self):
        for ch in self.dispatcher._channels.values():
            try:
                await ch.stop()
            except Exception as e:
                logger.error("停止 channel %s 失败: %s", ch.name, e)

    async def _cleanup_runner(self):
        # 停止 aiohttp（超时兜底，防常驻连接/慢 handler 卡住停机）
        if self._runner:
            try:
                await asyncio.wait_for(self._runner.cleanup(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("aiohttp 清理超时(5s)，强制继续停机")
            self._runner = None

    async def stop(self):
        """优雅停机"""
        logger.info("🛑 Gateway 停止中…")
        await self._stop_retention()
        # 先 request_stop 所有 agent（协作式停止），再限时等待收尾（join 上限 10s）
        for _key, entry in list(self.session_mgr._sessions.items()):
            agent = entry.agent
            if agent is not None:
                try:
                    agent.request_stop()
                except Exception:
                    pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not any(e.is_busy for e in self.session_mgr._sessions.values()
                       if e.agent is not None):
                break
            await asyncio.sleep(0.1)
        # 先停 WebUI（探针与 SSE），再停心跳与调度器（不再产生新触发）
        if self.webui:
            await self.webui.stop()
        if self.heartbeat:
            await self.heartbeat.stop()
        if self.scheduler:
            await self.scheduler.stop()
        # 停止 channels
        await self._stop_channels()
        # 停止统一任务运行时（先停止创建新任务）
        await self.dispatcher.stop()
        # 停止会话管理
        await self.session_mgr.stop()
        # 停止 aiohttp（超时兜底）
        await self._cleanup_runner()
        self._site = None
        logger.info("👋 Gateway 已停止")

    async def _start_channels(self):
        """按配置启动各 channel"""
        channels_cfg = self.config.get("channels", {})

        # 飞书
        feishu_cfg = channels_cfg.get("feishu", {})
        if feishu_cfg.get("enabled", False):
            try:
                from gateway.channels.feishu_channel import FeishuChannel
                ch = FeishuChannel(feishu_cfg, self.dispatcher)
                self.dispatcher.register_channel(ch)
                await ch.start()
                logger.info("📮 飞书 channel 已启动")
            except Exception as e:
                logger.error("飞书 channel 启动失败: %s", e)

        # 微信
        weixin_cfg = channels_cfg.get("weixin", {})
        if weixin_cfg.get("enabled", False):
            try:
                from gateway.channels.weixin_channel import WeixinChannel
                ch = WeixinChannel(weixin_cfg, self.dispatcher)
                self.dispatcher.register_channel(ch)
                await ch.start()
                logger.info("💬 微信 channel 已启动")
            except Exception as e:
                logger.error("微信 channel 启动失败: %s", e)

    def _validate_concurrency_uniform(self) -> None:
        """L1-C13 启动校验：三处并发上限应同源一致（仅告警，不阻断启动）。

        会话 worker 池（gateway.worker_pool_size）、TaskRuntime
        （task_runtime.max_global_concurrency）、统一会话全局 Turn 上限
        （webui.conversation.max_global_turns）统一由 gateway.agent.
        max_turns_concurrency（新键，默认 4）派生；未配置新键时各自回落
        旧键默认值。配置不一致说明误用了旧键，告警提示收敛到新键。
        """
        values: dict = {}
        executor = getattr(self.session_mgr, "_executor", None)
        workers = getattr(executor, "_max_workers", None)
        if workers is not None:
            values["session_worker_pool"] = workers
        task_runtime = getattr(self.dispatcher, "_task_runtime", None)
        if task_runtime is not None:
            values["task_runtime"] = getattr(
                task_runtime, "max_global_concurrency", None)
        if self.webui is not None:
            conv_svc = getattr(self.webui, "conversation_service", None)
            if conv_svc is not None:
                values["conversation_turns"] = getattr(
                    conv_svc, "max_global_turns", None)
        if not values:
            return
        distinct = {v for v in values.values() if v is not None}
        if len(distinct) > 1:
            logger.warning(
                "并发上限配置不一致（L1-C13）: %s；建议统一使用 "
                "gateway.agent.max_turns_concurrency=%s 作为唯一来源",
                values,
                self._max_turns_concurrency
                if self._max_turns_concurrency is not None else "（未配置，默认 4）")
        else:
            logger.info("并发上限一致（L1-C13）: %s", values)

    def _require_ops_bearer(self, request: web.Request) -> Optional[web.Response]:
        """/health、/metrics 的可选 Bearer 校验（P3 信息暴露最小修复）。

        配置了 auth_token 时要求请求头携带一致的 Bearer；未配置 token 时
        返回 None 维持免鉴权现状（环回部署场景，探活/监控不中断）。
        """
        token = self._ops_auth_token
        if not token:
            return None
        supplied = request.headers.get("Authorization", "")
        bearer = supplied[7:] if supplied.startswith("Bearer ") else ""
        try:
            authorized = bool(bearer) and hmac.compare_digest(
                bearer.encode("utf-8"), token.encode("utf-8"))
        except TypeError:
            authorized = False
        if authorized:
            return None
        return web.json_response(
            {"error": "authentication required"}, status=401)

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Prometheus 文本格式 /metrics 端点（/health 同级）。

        数据源为 dispatcher 内嵌的线程安全指标注册表（进程启动至今累计值），
        与 /health 的 JSON metrics 子对象同源打点、互不影响。Content-Type
        严格按 Prometheus 文本格式规范：text/plain; version=0.0.4。
        P3：配置了 auth_token 时要求 Bearer（未配置维持现状）。
        """
        denied = self._require_ops_bearer(request)
        if denied is not None:
            return denied
        body = self.dispatcher.metrics_prometheus()
        return web.Response(
            body=body.encode("utf-8"),
            headers={"Content-Type": "text/plain; version=0.0.4"})

    async def _handle_health(self, request: web.Request) -> web.Response:
        """健康检查端点。

        P3：配置了 auth_token 时要求 Bearer——响应含 active_sessions、
        session_key 列表等运行信息；未配置 token 维持现状（环回部署）。
        """
        denied = self._require_ops_bearer(request)
        if denied is not None:
            return denied
        channels_status = {}
        for name, ch in self.dispatcher._channels.items():
            channels_status[name] = ch.status()
        data = {
            "status": "ok",
            "active_sessions": self.session_mgr.active_count(),
            "max_sessions": self.session_mgr.max_sessions,
            "channels": channels_status,
            "executor": self.session_mgr.executor_stats(),
            # D3 指标（metrics 子对象）：turn/延迟/usage/guard/delta 累计值
            "metrics": self.dispatcher.metrics(),
        }
        # scheduler / heartbeat 状态（探针已周期采集，直接取快照）
        if self.scheduler:
            data["scheduler"] = self.scheduler.channel.status()
        if self.heartbeat:
            data["heartbeat"] = self.heartbeat.channel.status()
        return web.json_response(data)

    def run_forever(self):
        """阻塞运行直到 Ctrl+C"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _main():
            await self.start()
            # 等待停止信号
            stop_event = asyncio.Event()
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                except NotImplementedError:
                    # Windows 不支持 add_signal_handler
                    pass
            try:
                await stop_event.wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            await self.stop()

        try:
            loop.run_until_complete(_main())
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，正在停止…")
            loop.run_until_complete(self.stop())
        finally:
            loop.close()
