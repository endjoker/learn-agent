# -*- coding: utf-8 -*-
"""
调试通道 —— HTTP POST /debug/chat，零凭据端到端验证
"""

import asyncio
import json
import logging
import uuid

from aiohttp import web

from gateway.channels.base import Channel, InboundMessage

logger = logging.getLogger("jk_agent.gateway")


class DebugChannel(Channel):
    """HTTP 调试通道：POST /debug/chat 收消息，回复在 HTTP response 中返回"""

    name = "debug"
    # future 回环只能 set_result 一次：整段回复自行处理，禁用 dispatcher 分片
    # （原默认 False → 1500 字符切片后仅首片进 future，其余静默丢失）
    handles_chunking = True

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
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
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "无效的 JSON"}, status=400)

        text = body.get("text", "").strip()
        if not text:
            return web.json_response({"error": "text 不能为空"}, status=400)

        session_key = f"debug:{body.get('session_key', 'default')}"
        timeout = min(body.get("timeout", 120), 600)
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

        # 投递消息
        await self.dispatcher.on_inbound(msg)

        # 等待回复
        try:
            import asyncio as aio
            reply = await aio.wait_for(fut, timeout=timeout)
            return web.json_response({
                "reply": reply,
                "session_key": session_key,
                "message_id": msg_id,
            })
        except asyncio.TimeoutError:
            return web.json_response({
                "error": f"超时 ({timeout}s)",
                "session_key": session_key,
            }, status=504)
        finally:
            self._pending.pop(key, None)
