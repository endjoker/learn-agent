# -*- coding: utf-8 -*-
"""
WebuiChannel —— WebUI 的回复通道

future 回环范式（照抄 debug_channel，修掉其缺陷）：
  - handles_chunking=True：整段回复一次进 future，不分片
  - send_progress 覆写：软超时进度提示不触碰 pending future（避免抢占最终回复）
  - 无 pending future 时广播 chat.done：飞书/定时任务触发的同会话回复也能刷出来
"""

import asyncio
import logging

from gateway.channels.base import Channel, InboundMessage

logger = logging.getLogger("jk_agent.gateway")


class WebuiChannel(Channel):
    """WebUI 回复通道：future 回环 + 无 future 时广播 chat.done"""

    name = "webui"
    handles_chunking = True  # 整段回复，规避分片丢块

    def __init__(self, bus):
        self.bus = bus
        # 等待回复的 Future：(session_key:message_id) → asyncio.Future
        self._pending: dict[str, asyncio.Future] = {}

    # ---- future 管理 ----

    def register_future(self, session_key: str, message_id: str) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[f"{session_key}:{message_id}"] = fut
        return fut

    def discard_future(self, session_key: str, message_id: str):
        key = f"{session_key}:{message_id}"
        fut = self._pending.pop(key, None)
        if fut and not fut.done():
            fut.cancel()

    # ---- Channel 接口 ----

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def send_reply(self, msg: InboundMessage, text: str) -> None:
        key = f"{msg.session_key}:{msg.message_id}"
        fut = self._pending.pop(key, None)
        if fut and not fut.done():
            fut.set_result(text)
        else:
            # 无 pending future：广播 chat.done，让打开该会话的浏览器也能收到
            self.bus.publish("chat.done", {
                "session_key": msg.session_key,
                "message_id": msg.message_id,
                "full_text": text,
            })

    async def send_progress(self, msg: InboundMessage, text: str) -> None:
        """进度提示不触碰 future，只发 SSE 事件（避免抢占最终回复）"""
        self.bus.publish("chat.progress", {
            "session_key": msg.session_key,
            "message_id": msg.message_id,
            "text": text,
        })

    def publish_agent_event(self, msg: InboundMessage, event: dict) -> None:
        """Forward a worker-thread Agent runtime event to the SSE bus.

        Phase 5：填充公共字段（workspace/session/message/snapshot/sequence），
        便于前端按 identity 精确过滤，防止迟到事件串页。
        """
        event_type = event.get("type")
        if not event_type:
            return
        payload = dict(event.get("data") or {})
        meta = dict(getattr(msg, "metadata", None) or {})
        payload.update({
            "session_key": msg.session_key,
            "message_id": msg.message_id,
            "workspace_id": meta.get("workspace_id", ""),
            "workspace_session_id": meta.get("workspace_session_id", ""),
            "snapshot_id": meta.get("snapshot_id", ""),
            "timestamp": event.get("at") or 0,
        })
        if "sequence" not in payload:
            payload["sequence"] = event.get("sequence", 0)
        self.bus.publish(f"chat.{event_type}", payload)

    def status(self) -> dict:
        return {"name": self.name, "status": "ok",
                "pending": len(self._pending)}
