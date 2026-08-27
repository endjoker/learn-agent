# -*- coding: utf-8 -*-
"""
调试通道 —— HTTP POST /debug/chat，端到端验证

门禁（P0-1 / P1 加固）：
  - 配置了 auth_token：一律校验 Bearer（环回来源也不例外——本机浏览器中
    的恶意页面同样能打到 127.0.0.1，环回豁免等于零鉴权）；
  - 未配置 auth_token：仅允许环回来源（保持本地开发可用）；
  - 校验失败统一 404（避免端点探测）。
默认 enabled=False（server.py 取值处）：需要端到端调试时在
gateway.channels.debug 显式开启，见 config.example.json 注释。
"""

import asyncio
import hmac
import json
import logging
import uuid

from aiohttp import web

from gateway.channels.base import Channel, InboundMessage
from gateway.textutil import sanitize_error

logger = logging.getLogger("jk_agent.gateway")

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


class DebugChannel(Channel):
    """HTTP 调试通道：POST /debug/chat 收消息，回复在 HTTP response 中返回"""

    name = "debug"
    # future 回环只能 set_result 一次：整段回复自行处理，禁用 dispatcher 分片
    # （原默认 False → 1500 字符切片后仅首片进 future，其余静默丢失）
    handles_chunking = True

    def __init__(self, dispatcher, config: dict = None):
        self.dispatcher = dispatcher
        # 门禁配置：auth_token 取 gateway.webui 合并段（含 env 覆盖）
        self._config = config or {}
        # 存放等待回复的 Future：session_key+msg_id → asyncio.Future
        self._pending: dict[str, asyncio.Future] = {}

    def register_routes(self, app: web.Application):
        app.router.add_post("/debug/chat", self._handle_chat)

    async def start(self):
        pass  # 路由在 register_routes 中注册

    async def stop(self):
        # 取消所有等待中的 Future
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def send_reply(self, msg: InboundMessage, text: str):
        """将回复写入 pending Future"""
        key = f"{msg.session_key}:{msg.message_id}"
        fut = self._pending.get(key)
        if fut and not fut.done():
            fut.set_result(text)
        else:
            logger.debug("debug reply (no pending): %s", text[:100])

    async def send_progress(self, msg: InboundMessage, text: str):
        """进度提示（软超时）不触碰 future，避免抢占最终回复"""
        logger.debug("debug progress [%s]: %s", msg.session_key, text[:100])

    def status(self) -> dict:
        return {"name": self.name, "status": "ok", "pending": len(self._pending)}

    async def _handle_chat(self, request: web.Request) -> web.Response:
        """
        POST /debug/chat
        Body: {"session_key": "t1", "text": "你好", "timeout": 120}
        Response: {"reply": "...", "session_key": "t1"}
        """
        # CSRF 纵深（与 webui guard 同语义）：跨站表单请求必然携带
        # urlencoded/multipart/text-plain 的 Content-Type 且带实际 body
        # （浏览器表单无法声明 application/json），先于鉴权拒绝；无 body
        # 的声明头（如 aiohttp 客户端自动补的 octet-stream）没有可注入
        # 载荷，放行。显式 JSON 的跨站 fetch 会触发 CORS 预检，本服务不下
        # 发任何 CORS 头，预检必败。
        ctype = (request.headers.get("Content-Type") or "").strip().lower()
        if (ctype and "application/json" not in ctype
                and request.can_read_body):
            return web.json_response(
                {"error": "Content-Type 必须是 application/json"}, status=415)

        # 门禁（P1 加固）：配置了 auth_token 一律校验（含环回）；未配置
        # token 时仅放行环回来源。失败统一 404 避免端点探测。
        remote = request.remote or ""
        token = str(self._config.get("auth_token") or "")
        if token:
            supplied = request.headers.get("Authorization", "")
            bearer = supplied[7:] if supplied.startswith("Bearer ") else ""
            try:
                authorized = bool(bearer) and hmac.compare_digest(
                    bearer.encode("utf-8"), token.encode("utf-8"))
            except TypeError:
                authorized = False
            if not authorized:
                return web.json_response({"error": "not found"}, status=404)
        elif not _is_loopback(remote):
            return web.json_response({"error": "not found"}, status=404)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "无效的 JSON"}, status=400)

        text = body.get("text", "").strip()
        if not text:
            return web.json_response({"error": "text 不能为空"}, status=400)

        session_key = f"debug:{body.get('session_key', 'default')}"
        # min(timeout, 600) 对非数字容错：非数字回退默认 120s
        try:
            timeout = float(body.get("timeout", 120))
        except (TypeError, ValueError):
            timeout = 120
        timeout = min(timeout, 600)
        msg_id = str(uuid.uuid4())[:8]

        msg = InboundMessage(
            channel="debug",
            session_key=session_key,
            user_id="debug-user",
            user_name="调试用户",
            text=text,
            message_id=msg_id,
        )

        # 创建 Future 等待回复
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        key = f"{session_key}:{msg_id}"
        self._pending[key] = fut
        try:
            try:
                # 投递消息
                await self.dispatcher.on_inbound(msg)
            except Exception as exc:
                # sanitize_error 接线：错误回复脱敏（去路径/密钥）
                return web.json_response(
                    {"error": f"投递失败: {sanitize_error(exc)}"}, status=500)
            # 等待回复
            try:
                reply = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                return web.json_response({
                    "error": f"超时 ({timeout}s)",
                    "session_key": session_key,
                }, status=504)
            return web.json_response({
                "reply": reply,
                "session_key": session_key,
                "message_id": msg_id,
            })
        finally:
            # on_inbound 抛异常/超时都确保 _pending 清理
            self._pending.pop(key, None)
