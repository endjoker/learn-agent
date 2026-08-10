# -*- coding: utf-8 -*-
"""
EventBus —— 进程内线程安全事件总线 + SSE handler + status_providers 注册表

唯一跨线程边界：hook/工具事件在 executor 线程触发 publish()，
内部统一 loop.call_soon_threadsafe 投递到主循环的订阅队列。
"""

import asyncio
import json
import logging
import time

from aiohttp import web

logger = logging.getLogger("hello_agent.gateway")

_SSE_PING_INTERVAL = 15   # 秒
_QUEUE_MAX = 100          # 每订阅者队列上限，满则丢最旧


class EventBus:
    """{sub_id: asyncio.Queue} 订阅表；publish 线程安全"""

    def __init__(self):
        self._subs: dict[str, asyncio.Queue] = {}
        self._loop: asyncio.AbstractEventLoop = None
        self._counter = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        """在 WebUIModule.start()（主循环内）捕获事件循环"""
        self._loop = loop

    def subscribe(self) -> tuple[str, asyncio.Queue]:
        self._counter += 1
        sub_id = f"sub-{self._counter}"
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subs[sub_id] = q
        return sub_id, q

    def unsubscribe(self, sub_id: str):
        self._subs.pop(sub_id, None)

    def close_all(self):
        """停机：向每个订阅队列投 None 哨兵唤醒 SSE handler 使其退出，再清空。

        避免 aiohttp cleanup 因常驻 SSE 流（while True）而挂起。
        """
        for q in self._subs.values():
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._subs.clear()

    def publish(self, event_type: str, payload: dict = None):
        """线程安全发布。任何线程可调（executor 线程的 hook 事件）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        evt = {"type": event_type, "data": payload or {}, "at": time.time()}
        for q in list(self._subs.values()):
            try:
                loop.call_soon_threadsafe(self._put_drop_oldest, q, evt)
            except RuntimeError:
                # 循环已关闭等边界情况，静默丢弃
                pass

    @staticmethod
    def _put_drop_oldest(q: asyncio.Queue, evt: dict):
        while q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            pass


class SSEHandler:
    """GET /api/events —— text/event-stream，15s ping 保活，无 backlog"""

    def __init__(self, bus: EventBus):
        self.bus = bus

    async def handle(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        sub_id, q = self.bus.subscribe()
        try:
            await resp.write(b": connected\n\n")
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=_SSE_PING_INTERVAL)
                    if evt is None:  # 停机哨兵：立即退出，解除 aiohttp cleanup 阻塞
                        break
                    payload = json.dumps(
                        {"type": evt["type"], "data": evt["data"]},
                        ensure_ascii=False)
                    await resp.write(
                        f"data: {payload}\n\n".encode("utf-8"))
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
                except (ConnectionResetError, ConnectionError):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self.bus.unsubscribe(sub_id)
        return resp
