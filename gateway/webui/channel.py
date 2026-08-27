# -*- coding: utf-8 -*-
"""
WebuiChannel —— WebUI 的回复通道（C1-③ 统一链路版）：

  - 回复统一广播 chat.done（含 plan/goal runtime 字段），不再维护
    legacy future 回环（旧漏斗已随 C1-full 退役）。
  - send_progress 覆写：软超时进度提示只发 SSE 事件，不触碰最终回复。
"""

import logging

from gateway.channels.base import Channel, InboundMessage

logger = logging.getLogger("jk_agent.gateway")


class WebuiChannel(Channel):
    """WebUI 回复通道：统一广播 chat.done（含 plan/goal runtime 字段）"""

    name = "webui"
    handles_chunking = True  # 整段回复，规避分片丢块

    def __init__(self, bus):
        self.bus = bus

    # ---- Channel 接口 ----

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_reply(self, msg: InboundMessage, text: str) -> None:
        """统一链路：回复以 chat.done 广播（含 plan/goal runtime 字段）。"""
        meta = dict(getattr(msg, "metadata", None) or {})
        payload = {
            "session_key": msg.session_key,
            "message_id": msg.message_id,
            "full_text": text,
        }
        if meta.get("task_source") in {"plan", "goal"}:
            payload.update({
                "runtime_source": meta.get("task_source"),
                "plan_id": meta.get("plan_id", ""),
                "plan_task_id": meta.get("plan_task_id", ""),
                "goal_id": meta.get("goal_id", ""),
                "goal_round": meta.get("goal_round", 0),
            })
        self.bus.publish("chat.done", payload)

    async def send_progress(self, msg: InboundMessage, text: str) -> None:
        """进度提示只发 SSE 事件（避免抢占最终回复）"""
        self.bus.publish("chat.progress", {
            "session_key": msg.session_key,
            "message_id": msg.message_id,
            "text": text,
        })

    def status(self) -> dict:
        return {"name": self.name, "status": "ok", "pending": 0}
