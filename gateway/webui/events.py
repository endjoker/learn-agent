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
import threading
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
        self._watermarks: dict[str, int] = {}   # sub_id -> 订阅时 seq 水位
        # sub_id -> 订阅 scope（session_key/workspace_id/workspace_session_id）。
        # L5-P0-2：发布侧按 scope 过滤后再投递队列，避免无关事件占队列。
        self._sub_scopes: dict[str, dict] = {}
        self._loop: asyncio.AbstractEventLoop = None
        self._counter = 0
        self._seq = 0
        self._backlog: deque = deque(maxlen=max(1, int(backlog_size)))
        self._backlog_ttl = float(backlog_ttl)
        self._publish_lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        """在 WebUIModule.start()（主循环内）捕获事件循环"""
        self._loop = loop

    def subscribe(self, *, session_key: str = "",
                  workspace_id: str = "",
                  workspace_session_id: str = "") -> tuple[str, asyncio.Queue]:
        """订阅实时事件队列。

        scope 参数（L5-P0-2）：记录订阅者期望的 session/workspace 范围，
        发布侧按 scope 过滤后再投递队列（原实现在 handler 收到后才丢弃，
        无关事件白白占用队列）。全部为空 = 全局订阅（向后兼容）。
        """
        with self._publish_lock:
            self._counter += 1
            sub_id = f"sub-{self._counter}"
            q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
            self._subs[sub_id] = q
            # 订阅时记录当前 seq 水位：水位以下的事件由 replay（Last-Event-ID）
            # 补齐，实时队列必须过滤 <= 水位的事件，否则断线重连会重复投递。
            self._watermarks[sub_id] = self._seq
            self._sub_scopes[sub_id] = {
                "session_key": str(session_key or ""),
                "workspace_id": str(workspace_id or ""),
                "workspace_session_id": str(workspace_session_id or ""),
            }
        return sub_id, q

    def unsubscribe(self, sub_id: str):
        with self._publish_lock:
            self._subs.pop(sub_id, None)
            self._watermarks.pop(sub_id, None)
            self._sub_scopes.pop(sub_id, None)

    def watermark(self, sub_id: str) -> int:
        """订阅时记录的 seq 水位（replay 与实时队列的去重分界）。"""
        with self._publish_lock:
            return self._watermarks.get(sub_id, 0)

    def close_all(self):
        """停机：向每个订阅队列投 None 哨兵唤醒 SSE handler 使其退出，再清空。

        避免 aiohttp cleanup 因常驻 SSE 流（while True）而挂起。
        """
        with self._publish_lock:
            queues = list(self._subs.values())
            self._subs.clear()
            self._watermarks.clear()
            self._sub_scopes.clear()
        for q in queues:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def publish(self, event_type: str, payload: dict = None):
        """线程安全发布。任何线程可调（executor 线程的 hook 事件）。

        L5-P0-2 两项优化：
        1. SSE 预编码：同一事件对所有订阅者的 SSE 帧相同，发布侧只
           json.dumps 一次并缓存在事件上（多 tab N→1），handler/replay
           直接复用字节；
        2. 发布侧按订阅 scope 过滤后再投递队列：不匹配的事件不再占用
           订阅队列（原实现投递后在 handler 里才丢弃）。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._publish_lock:
            self._seq += 1
            evt = {"type": event_type, "data": payload or {},
                   "at": time.time(), "event_id": self._seq}
            self._backlog.append(evt)
            subs = list(self._subs.items())       # (sub_id, q)
            scopes = dict(self._sub_scopes)
        # 预编码（锁外执行，避免长 payload 的 json.dumps 阻塞发布串行化）
        try:
            evt["_sse_encoded"] = EventBus._encode_sse(evt)
        except Exception:
            pass   # 编码失败不影响分发（handler 有兜底重编码）
        for sub_id, q in subs:
            if not self.matches_scope(evt, **scopes.get(sub_id, {})):
                continue
            try:
                loop.call_soon_threadsafe(self._put_drop_oldest, q, evt)
            except RuntimeError:
                # 循环已关闭等边界情况，静默丢弃
                pass

    def last_event_id(self) -> int:
        """当前最大 event_id（连接状态展示用）。"""
        with self._publish_lock:
            return self._seq

    @staticmethod
    def _encode_sse(evt: dict) -> bytes:
        """把事件编码为 SSE 帧（data 为 JSON payload）。

        L5-P0-2：发布侧预编码一次并缓存（evt["_sse_encoded"]），
        SSE handler 与 backlog replay 直接复用（多 tab N→1）。
        """
        payload = json.dumps(
            {"type": evt["type"], "data": evt.get("data") or {},
             "event_id": evt.get("event_id", 0), "at": evt.get("at", 0)},
            ensure_ascii=False)
        lines = [f"id: {evt.get('event_id', 0)}", f"data: {payload}"]
        return ("\n".join(lines) + "\n\n").encode("utf-8")

    @staticmethod
    def matches_scope(evt: dict, *, session_key: str = "",
                      workspace_id: str = "",
                      workspace_session_id: str = "") -> bool:
        """Return whether an event belongs to the requested SSE scope.

        Scoped subscriptions require every requested field to be present and
        equal. Only an entirely unscoped subscription acts as a global feed.
        """
        data = evt.get("data") or {}
        if session_key and data.get("session_key") != session_key:
            return False
        if workspace_id and data.get("workspace_id") != workspace_id:
            return False
        if (workspace_session_id
                and data.get("workspace_session_id") != workspace_session_id):
            return False
        return True

    def replay(self, after_event_id: int = 0, since: float = 0.0,
               session_key: str = "", workspace_id: str = "",
               workspace_session_id: str = "") -> list:
        """Replay only events matching the optional session/workspace scope."""
        now = time.time()
        out = []
        with self._publish_lock:
            backlog = list(self._backlog)
        for evt in backlog:
            if evt.get("event_id", 0) <= after_event_id:
                continue
            if since and evt.get("at", 0) < since:
                continue
            if evt.get("at", 0) < now - self._backlog_ttl:
                continue
            if not self.matches_scope(
                    evt, session_key=session_key, workspace_id=workspace_id,
                    workspace_session_id=workspace_session_id):
                continue
            out.append(evt)
        return out

    def min_replayable_event_id(self) -> int:
        """当前仍可重放（未过 TTL）的最小 event_id；无可重放事件返回 0。

        用途：SSE 断线重连的缺口检测——客户端 Last-Event-ID 与可重放下界
        之间出现跳变，说明中间事件已被 TTL/容量淘汰、永远补不回来了。
        与 replay() 一致按 TTL 过滤（已过期的事件不可"用"），但不按 scope
        过滤：event_id 是全局单调序列，跨 scope 的空洞同样是该连接错过的
        序列段。
        """
        cutoff = time.time() - self._backlog_ttl
        with self._publish_lock:
            return min((int(e.get("event_id", 0)) for e in self._backlog
                        if e.get("at", 0) >= cutoff), default=0)

    @staticmethod
    def _put_drop_oldest(q: asyncio.Queue, evt: dict):
        """有界队列满 → 丢最旧，并向该订阅者广播 version_gap（设计方案 18.4）。

        背压丢事件意味着订阅者本地水位与后端出现不可恢复的跳变，必须通知
        客户端拉取 Snapshot 修复（前端 version_gap 特判跳过版本门控 → 置
        gaps → useConversation 自动 applySnapshot）。"""
        dropped = None
        while q.full():
            try:
                dropped = q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            pass
        if dropped is not None:
            gap = EventBus._gap_event(dropped)
            if gap is not None:
                # 队列刚被 evt 占满：version_gap 再挤掉一个最旧，确保送达
                while q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    q.put_nowait(gap)
                except asyncio.QueueFull:
                    pass

    @staticmethod
    def _gap_event(dropped: dict) -> dict | None:
        """从被丢事件构造 version_gap 事件（带 conversation_id/session_key 路由）。"""
        data = dropped.get("data") or {}
        cid = data.get("conversation_id")
        if isinstance(cid, str) and cid:
            return {
                "type": "version_gap",
                "data": {
                    "conversation_id": cid,
                    "session_key": data.get("session_key", ""),
                    "scope": "session",
                    "version": 0,  # 前端特判，不参与版本门控
                    # 缺口来源：订阅队列背压丢弃（与 C-4 合批缓冲超限的
                    # version_gap 同协议，前端统一按缺口快照自愈）
                    "reason": "queue_overflow",
                },
                "at": time.time(),
                "event_id": dropped.get("event_id", 0),
            }
        # 无 conversation_id 的事件（如 question/approval 桥）：用 session_key
        # 构造通用 gap，供会话级订阅收到版本缺口提示（设计方案 18.4 扩展）。
        skey = data.get("session_key")
        if isinstance(skey, str) and skey:
            return {
                "type": "version_gap",
                "data": {
                    "session_key": skey,
                    "scope": "session",
                    "version": 0,
                },
                "at": time.time(),
                "event_id": dropped.get("event_id", 0),
            }
        return None

    @staticmethod
    def _backlog_gap_event(*, conversation_id: str, session_key: str,
                           event_id: int) -> dict:
        """构造 backlog 缺口的 version_gap 帧（与 _gap_event 同协议风格）。

        reason 区分缺口来源：TTL/容量淘汰导致 Last-Event-ID 与可重放
        backlog 之间出现不可恢复的序列空洞。注意前端 store.applyEvent 在
        version_gap 分支之前就丢弃缺 conversation_id 的事件（store.ts
        L244 前置检查），因此调用方必须保证能推导出 conversation_id。
        """
        return {
            "type": "version_gap",
            "data": {
                "conversation_id": str(conversation_id),
                "session_key": str(session_key or ""),
                "scope": "session",
                "version": 0,  # 前端特判，不参与版本门控
                "reason": "backlog_expired",
            },
            "at": time.time(),
            "event_id": int(event_id or 0),
        }


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

    @staticmethod
    def _scope(request) -> dict:
        return {"session_key": str(request.query.get("session_key") or ""),
                "workspace_id": str(request.query.get("workspace_id") or ""),
                "workspace_session_id": str(request.query.get("workspace_session_id") or "")}

    def _encode(self, evt: dict) -> bytes:
        # L5-P0-2：优先复用发布侧预编码结果（多 tab N→1，避免逐订阅者重编码）；
        # version_gap / 测试直投事件无缓存字段时兜底现编。
        cached = evt.get("_sse_encoded")
        if isinstance(cached, bytes):
            return cached
        return EventBus._encode_sse(evt)

    def _backlog_gap_frame(self, scope: dict, *, last_id: int,
                           replayed: list) -> dict | None:
        """检测 Last-Event-ID 与可重放 backlog 之间的序列缺口。

        最小可重放 event_id > last_id+1 说明中间事件已被 TTL/容量淘汰，
        客户端永远收不到了 → 应补发 version_gap(reason=backlog_expired)
        触发前端快照自愈。前端两道消费方（useConversation.isGatewayEvent
        与 store.applyEvent 的前置检查）都会直接丢弃缺 conversation_id 的
        事件——帧不会报错但完全无效，因此只在能从本次重放事件中推导出
        conversation_id 时合成帧；否则记 info 日志并跳过。
        """
        if last_id <= 0:
            return None
        min_available = self.bus.min_replayable_event_id()
        if min_available <= last_id + 1:
            return None
        session_key = str(scope.get("session_key") or "")
        conversation_id = ""
        for evt in reversed(replayed):
            cid = (evt.get("data") or {}).get("conversation_id")
            if isinstance(cid, str) and cid:
                conversation_id = cid
                break
        if not conversation_id:
            logger.info(
                "SSE 重放缺口：last_event_id=%d 但最小可用 event_id=%d"
                "（session_key=%s），无法推导 conversation_id，跳过合成"
                " version_gap", last_id, min_available, session_key or "-")
            return None
        logger.info(
            "SSE 重放缺口：last_event_id=%d 与最小可用 event_id=%d 之间存在"
            "丢失（backlog_expired），补发 version_gap（conversation_id=%s）",
            last_id, min_available, conversation_id)
        anchor = max((int(e.get("event_id", 0)) for e in replayed),
                     default=last_id)
        return EventBus._backlog_gap_event(
            conversation_id=conversation_id, session_key=session_key,
            event_id=anchor)

    def _replay_plan(self, scope: dict, *, last_id: int, since: float,
                     watermark: int) -> tuple[list, dict | None, int]:
        """计算重放列表、缺口帧与抬升后的实时去重水位。

        订阅水位 W 与 replay 快照之间存在窗口：期间新发布的事件会同时
        出现在 replay 结果和实时队列里。若沿用订阅时水位 W 去重，这些
        事件会被投递两次（delta 有前端 seq 兜底，其余事件重复触发
        handler）——因此 replay 结束后必须把水位抬到已重放的最大 id。
        """
        events: list = []
        if last_id or since:
            events = self.bus.replay(after_event_id=last_id, since=since,
                                     **scope)
            if events:
                watermark = max(
                    watermark,
                    *(int(e.get("event_id", 0)) for e in events))
        gap_frame = self._backlog_gap_frame(scope, last_id=last_id,
                                            replayed=events)
        return events, gap_frame, watermark

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
        # L5-P0-2：订阅时登记 scope，发布侧据此过滤后再投递队列；
        # 订阅前解析 scope（订阅只接受关键字参数）。
        scope = self._scope(request)
        sub_id, q = self.bus.subscribe(**scope)
        # 订阅时水位：replay（断线补齐）与实时队列的去重分界
        watermark = self.bus.watermark(sub_id)
        try:
            await resp.write(b": connected\n\n")
            # 断线补齐：Last-Event-ID / since → 先重放 backlog 再进实时。
            # 水位随重放抬升（_replay_plan），实时循环用抬升后的水位去重，
            # 保证同 id 事件只投递一次；缺口帧放在重放之后发送——若放在
            # 前面，紧随其后的连续 session 事件会在前端清除缺口标记，
            # 快照修复可能不再触发。
            last_id = self._last_event_id(request)
            since = self._since(request)
            replay_events, gap_frame, watermark = self._replay_plan(
                scope, last_id=last_id, since=since, watermark=watermark)
            for evt in replay_events:
                await resp.write(self._encode(evt))
            if gap_frame is not None:
                await resp.write(self._encode(gap_frame))
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
                        if evt.get("event_id", 0) <= watermark:
                            # 已在 replay 阶段投递（订阅时水位以下，或 replay
                            # 窗口内已随重放发出——水位已抬升）→ 去重
                            continue
                        if not self.bus.matches_scope(evt, **scope):
                            continue
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
