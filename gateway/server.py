# -*- coding: utf-8 -*-
"""
Gateway 服务器 —— aiohttp 装配、启动编排、优雅停机
"""

import asyncio
import logging
import signal
from typing import Optional

from aiohttp import web

from gateway.config import get_gateway_config
from gateway.dispatcher import Dispatcher
from gateway.session import SessionManager

logger = logging.getLogger("hello_agent.gateway")


class GatewayServer:
    """Gateway 主服务器：aiohttp + channels + session 管理"""

    def __init__(self, config: dict = None):
        self.config = config or get_gateway_config()
        self.host = self.config.get("host", "127.0.0.1")
        self.port = self.config.get("port", 9120)

        # 会话管理（从 self.config 提取子配置，避免重复 load_config + deepcopy）
        sess_cfg = self.config.get("sessions", {})
        agent_cfg = self.config.get("agent", {})
        self.session_mgr = SessionManager(
            max_sessions=sess_cfg.get("max_sessions", 50),
            idle_timeout_minutes=sess_cfg.get("idle_timeout_minutes", 60),
            persist=sess_cfg.get("persist", True),
            worker_pool_size=self.config.get("worker_pool_size", 4),
            agent_config=agent_cfg,
        )
        # 调度器
        self.dispatcher = Dispatcher(
            session_mgr=self.session_mgr,
            agent_config={
                **agent_cfg,
                "soft_timeout_seconds": sess_cfg.get("soft_timeout_seconds", 90),
                "hard_timeout_seconds": sess_cfg.get("hard_timeout_seconds", 600),
            },
        )
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self.scheduler = None
        self.heartbeat = None
        self.webui = None

    async def start(self):
        """启动所有服务"""
        logger.info("🚀 Gateway 启动中 → %s:%d", self.host, self.port)

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

        # 启动会话管理
        await self.session_mgr.start()

        # 启动 channels
        await self._start_channels()

        # 启动 aiohttp
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        # debug channel 注册路由
        channels_cfg = self.config.get("channels", {})
        if channels_cfg.get("debug", {}).get("enabled", True):
            from gateway.channels.debug_channel import DebugChannel
            debug = DebugChannel(self.dispatcher)
            self.dispatcher.register_channel(debug)
            debug.register_routes(self._app)

        # 定时任务（P1）
        sched_cfg = self.config.get("scheduler", {})
        if sched_cfg.get("enabled", True):
            from gateway.scheduler import Scheduler
            self.scheduler = Scheduler(sched_cfg, self.dispatcher, self.session_mgr)
            self.dispatcher.register_channel(self.scheduler.channel)
            self.dispatcher.add_command(
                "/cron", self.scheduler.handle_command,
                "/cron [list|run|pause|resume|history|reload] — 定时任务管理")
            await self.scheduler.start()

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

        # WebUI 六模块控制台（P3a 基建，AppRunner 之前挂载）
        webui_ch_cfg = channels_cfg.get("webui", {})
        if webui_ch_cfg.get("enabled", True):
            from gateway.webui import WebUIModule
            self.webui = WebUIModule(
                self.dispatcher, self.session_mgr,
                {**webui_ch_cfg, **self.config.get("webui", {})})
            await self.webui.start()
            self.webui.register_routes(self._app)

        # scheduler/heartbeat 注入 webui（/api/scheduler 用）+ 注册 status provider
        if self.webui:
            self.webui.scheduler = self.scheduler
            self.webui.heartbeat = self.heartbeat
            if self.scheduler:
                self.webui.add_status_provider(
                    "scheduler", lambda: self.scheduler.channel.status())
            if self.heartbeat:
                self.webui.add_status_provider(
                    "heartbeat", lambda: self.heartbeat.channel.status())

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

        logger.info("✅ Gateway 已启动 → http://%s:%d", self.host, self.port)
        logger.info("   /health 健康检查 | /debug/chat 调试通道")

    async def stop(self):
        """优雅停机"""
        logger.info("🛑 Gateway 停止中…")
        # 先停 WebUI（探针与 SSE），再停心跳与调度器（不再产生新触发）
        if self.webui:
            await self.webui.stop()
        if self.heartbeat:
            await self.heartbeat.stop()
        if self.scheduler:
            await self.scheduler.stop()
        # 停止 channels
        for ch in self.dispatcher._channels.values():
            try:
                await ch.stop()
            except Exception as e:
                logger.error("停止 channel %s 失败: %s", ch.name, e)
        # 停止会话管理
        await self.session_mgr.stop()
        # 停止 aiohttp（超时兜底，防常驻连接/慢 handler 卡住停机）
        if self._runner:
            try:
                await asyncio.wait_for(self._runner.cleanup(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("aiohttp 清理超时(5s)，强制继续停机")
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

    async def _handle_health(self, request: web.Request) -> web.Response:
        """健康检查端点"""
        channels_status = {}
        for name, ch in self.dispatcher._channels.items():
            channels_status[name] = ch.status()
        data = {
            "status": "ok",
            "active_sessions": self.session_mgr.active_count(),
            "max_sessions": self.session_mgr.max_sessions,
            "channels": channels_status,
            "executor": self.session_mgr.executor_stats(),
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
