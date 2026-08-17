# -*- coding: utf-8 -*-
"""
EventBus —— 进程内线程安全事件总线 + SSE handler + status_providers 注册表

唯一跨线程边界：hook/工具事件在 executor 线程触发 publish()，
内部统一 loop.call_soon_threadsafe 投递到主循环的订阅队列。

Phase 5：
- 单调 event_id（ping 不占业务 sequence）
- 有界 ring backlog（按数量/时间），支持 Last-Event-ID 断线补齐
- 无 backlog 客户端仍收到实时事件（向下兼容）
"""

import asyncio
import json
import logging
import time
from collections import deque

from aiohttp import web

logger = logging.getLogger("jk_agent.gateway")

_SSE_PING_INTERVAL = 15   # 秒
_QUEUE_MAX = 100          # 每订阅者队列上限，满则丢最旧
_BACKLOG_MAX = 200        # ring backlog 事件数上限
_BACKLOG_TTL = 600        # backlog 事件保留秒数


class EventBus:
    """{sub_id: asyncio.Queue} 订阅表；publish 线程安全；带 event_id + backlog。"""

    def __init__(self, backlog_size: int = _BACKLOG_MAX,
                 backlog_ttl: float = _BACKLOG_TTL):
        self._subs: dict[str, asyncio.Queue] = {}
        self._loop: asyncio.AbstractEventLoop = None
        self._counter = 0
        self._seq = 0
        self._backlog: deque = deque(maxlen=max(1, int(backlog_size)))
        self._backlog_ttl = float(backlog_ttl)

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
        self._seq += 1
        evt = {"type": event_type, "data": payload or {},
               "at": time.time(), "event_id": self._seq}
        self._backlog.append(evt)
        for q in list(self._subs.values()):
            try:
                loop.call_soon_threadsafe(self._put_drop_oldest, q, evt)
            except RuntimeError:
                # 循环已关闭等边界情况，静默丢弃
                pass

    def last_event_id(self) -> int:
        """当前最大 event_id（连接状态展示用）。"""
        return self._seq

    def replay(self, after_event_id: int = 0, since: float = 0.0) -> list:
        """返回 backlog 中 event_id > after_event_id 且 at >= since 的事件。"""
        now = time.time()
        out = []
        for evt in self._backlog:
            if evt.get("event_id", 0) > after_event_id:
                if since and evt.get("at", 0) < since:
                    continue
                if evt.get("at", 0) < now - self._backlog_ttl:
                    continue
                out.append(evt)
        return out

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
    """GET /api/events —— text/event-stream，15s ping 保活，event_id + backlog 重放。"""

    def __init__(self, bus: EventBus):
        self.bus = bus

    def _last_event_id(self, request) -> int:
        raw = ""
        try:
            raw = request.headers.get("Last-Event-ID") or \
                request.query.get("last_event_id") or ""
        except Exception:
            raw = ""
        try:
            return int(str(raw).strip() or 0)
        except (TypeError, ValueError):
            return 0

    def _since(self, request) -> float:
        try:
            return float(request.query.get("since") or 0)
        except Exception:
            return 0.0

    def _encode(self, evt: dict) -> bytes:
        payload = json.dumps(
            {"type": evt["type"], "data": evt.get("data") or {},
             "event_id": evt.get("event_id", 0), "at": evt.get("at", 0)},
            ensure_ascii=False)
        lines = [f"id: {evt.get('event_id', 0)}", f"data: {payload}"]
        return ("\n".join(lines) + "\n\n").encode("utf-8")

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
            # 断线补齐：Last-Event-ID / since → 先重放 backlog 再进实时
            last_id = self._last_event_id(request)
            since = self._since(request)
            if last_id or since:
                for evt in self.bus.replay(after_event_id=last_id, since=since):
                    await resp.write(self._encode(evt))
            while True:
                try:
                    try:
                        evt = await asyncio.wait_for(
                            q.get(), timeout=_SSE_PING_INTERVAL)
                    except asyncio.TimeoutError:
                        message = b": ping\n\n"
                    else:
                        if evt is None:  # 停机哨兵：立即退出，解除 aiohttp cleanup 阻塞
                            break
                        message = self._encode(evt)
                    # The client can close while wait_for() times out.  Keep
                    # this write inside the same guarded block so aiohttp's
                    # ClientConnectionResetError is treated as a normal SSE
                    # disconnect rather than logged as a request failure.
                    await resp.write(message)
                except (ConnectionResetError, ConnectionError):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self.bus.unsubscribe(sub_id)
        return resp
