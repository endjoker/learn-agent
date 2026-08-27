# -*- coding: utf-8 -*-
"""
统一会话桥 —— 把现有 Dispatcher/Agent 运行时事件接入统一 Conversation/Turn/Node。

不改写现有执行路径（会话池/Agent/回复投递保持不变），只做"旁路持久化"：
- 入站消息 → 统一 Conversation + 队列（渠道建 queued User Node + 持久化去重）；
- Agent 运行时事件（reasoning_delta / text_delta / tool_call_*）→ TurnNode；
- 最终回复（send_reply / runtime 终态）→ chat.done 权威 + Turn 终态；
- Plan/Goal/Subagent → 独立系统 Conversation + 父会话 Runtime Projection。

旧 chat.* SSE 事件继续由 WebuiChannel 原样发布，前端可平滑迁移到新事件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

from gateway.conversation.models import TurnNodeType, TurnStatus
from gateway.conversation.service import ConversationService, gen_node_id_from_call
from gateway.errors_catalog import AGENT_EXECUTION_FAILED, TOOL_EXECUTION_ERROR

logger = logging.getLogger("jk_agent.gateway")

# 设计方案 16.4：原始 delta 在服务端内存合并，每约 100ms 提交一次
_DELTA_MERGE_SECONDS = 0.1
# C-4：合批缓冲字节上限默认 2MB（bridge.buffer_max_bytes 可配）
_DEFAULT_BUFFER_MAX_BYTES = 2 * 1024 * 1024


class _DeltaMerger:
    """线程安全的内存 delta 合并器。

    Agent 事件在 executor 线程触发；文本 delta（reasoning/assistant）先累积在
    内存，距上次提交超过 interval_seconds 时一次性落库并广播合并后的
    node.delta（契约①：增量 delta + 节点内递增 seq，前端按 (node_id, seq)
    追加渲染）。工具状态/终态事件不走合并。

    C-1：缓冲由空转非空时立即提交一次（首 token 不再等 100ms flusher）；
    C-3：同窗多 key 合并为一次批量回调（单事务落 N 节点 + N 版本 + N outbox），
    批量失败整批回滚并保留缓冲下轮重试；
    C-4：缓冲字节上限（默认 2MB）——超限丢弃该 key 增量并触发 on_drop
    （广播 version_gap，前端快照自愈兜底）。

    兼容两种 flush 回调签名（按形参数自动识别）：
    - 单参数 ``flush(items)``（批量模式，桥生产路径）：C-1/C-3/C-4 全部生效；
    - 四参数 ``flush(conversation_id, turn_id, node_type, text)``（旧按 key 提交）：
      维持旧语义（仅 interval 合批，逐 key 提交），不破坏既有测试/调用方。
    """

    def __init__(self, flush: Callable,
                 interval_seconds: float = _DELTA_MERGE_SECONDS,
                 max_bytes: int = 2 * 1024 * 1024,
                 on_drop: Optional[Callable[[tuple[str, str, str], str], None]] = None):
        self._flush = flush
        self._interval = max(0.01, float(interval_seconds))
        self._max_bytes = max(1024, int(max_bytes))
        self._on_drop = on_drop
        self._lock = threading.Lock()
        self._buffer: dict[tuple[str, str, str], str] = {}
        self._buffer_bytes = 0
        self._last = time.monotonic()
        self._batch = _DeltaMerger._is_batch_flush(flush)

    @staticmethod
    def _is_batch_flush(flush: Callable) -> bool:
        """按可接受位置参数个数识别批量/逐 key 回调（见类文档）。"""
        try:
            import inspect
            params = inspect.signature(flush).parameters.values()
            positional = [p for p in params
                          if p.kind in (p.POSITIONAL_ONLY,
                                        p.POSITIONAL_OR_KEYWORD)]
            return len(positional) == 1
        except (TypeError, ValueError):
            return False

    def accumulate(self, key: tuple[str, str, str], text: str) -> None:
        with self._lock:
            text_bytes = len(text.encode("utf-8", errors="replace"))
            # C-4：合批缓冲超过字节上限 → 丢弃该 key 增量并广播 version_gap
            # （快照自愈兜底：前端 version_gap 特判 → 拉快照补全权威文本）。
            if (text_bytes > self._max_bytes
                    or self._buffer_bytes + text_bytes > self._max_bytes):
                if self._on_drop is not None:
                    try:
                        self._on_drop(key, text)
                    except Exception:
                        logger.exception("delta 缓冲超限丢弃回调失败: %s", key)
                return
            was_empty = not self._buffer
            self._buffer[key] = self._buffer.get(key, "") + text
            self._buffer_bytes += text_bytes
            if was_empty and self._batch:
                # C-1：缓冲由空转非空 → 立即提交（消除首 token ~100ms 延迟）。
                # 关键配合：空缓冲 flush 不刷新 _last（见 _flush_locked），因此
                # 距上次"真实提交"超过 interval 才触发——即新流/新突发的首 token
                # 立即落库；稳态（flusher 每 100ms drain、interval 内持续有 token）
                # 缓冲不会空转非空太久，仍由 100ms 合批。
                if time.monotonic() - self._last >= self._interval:
                    self._flush_locked()
            elif time.monotonic() - self._last >= self._interval:
                self._flush_locked()

    def flush(self) -> None:
        """立即提交全部缓冲（Turn 结束 / 节点切换 / 工具事件前调用）。"""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            # C-1：空缓冲不刷新 _last。若刷新，100ms flusher 在空闲期每 tick
            # 都刷新，新流首 token 将永远"距上次 flush 太近"而等满 100ms。
            return
        buffer, self._buffer = self._buffer, {}
        buffer_bytes = self._buffer_bytes
        self._buffer_bytes = 0
        self._last = time.monotonic()
        items = [(conversation_id, turn_id, node_type, text)
                 for (conversation_id, turn_id, node_type), text in buffer.items()]
        try:
            if self._batch:
                # C-3：同窗多 key 合并进单个事务（一次 commit 完成 N 节点追加 +
                # N 版本 + N outbox），跨 key 失败整批回滚。
                self._flush(items)
            else:
                # 旧语义：逐 key 提交，单个失败不影响其余
                for conversation_id, turn_id, node_type, text in items:
                    self._flush(conversation_id, turn_id, node_type, text)
        except Exception:
            logger.exception("delta 合并批量提交失败（%d keys），缓冲保留待重试",
                             len(items))
            # 整批回滚（事务已回滚）：把未提交的 items 合并回缓冲，下轮重试；
            # 同 key 若有更新累积，先入先出拼接保持顺序。
            for conversation_id, turn_id, node_type, text in items:
                key = (conversation_id, turn_id, node_type)
                self._buffer[key] = self._buffer.get(key, "") + text
            self._buffer_bytes += buffer_bytes

_SYSTEM_SOURCES = frozenset({"plan", "goal", "subagent"})

# 超大工具结果摘要（设计方案 17）：摘要保存前/后片段
_RESULT_SUMMARY_MAX = 2048
# 设计方案 17 阈值：≤64KB 内嵌正文；64KB~10MB 独立 tool_results 表；>10MB 仅摘要
_RESULT_EMBED_MAX = 64 * 1024
_RESULT_STORE_MAX = 10 * 1024 * 1024
# 摘要前/后片段（设计方案 17：前 8KB + 后 8KB + 大小 + 行数 + 类型 + 截断原因）
_RESULT_HEAD_TAIL = 8 * 1024


def _result_size_bytes(value) -> int:
    """工具结果字节数（dict/list 序列化后）。"""
    if value is None:
        return 0
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    return len(text.encode("utf-8", errors="replace"))


def _result_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)
    return str(value)


def _summarize(value, limit: int = 200) -> str:
    """把工具参数/结果压缩为摘要文本（不落正文全文）。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    return text[:limit]


def _tool_error_fields(data: dict, content) -> tuple:
    """工具结果错误字段（X2-P3⑫）：``is_error=True`` → (error_code, 截断 message)。

    返回 (None, None) 表示无错误。message 取结果前 500 字符摘要，
    与 error_code 一起写入工具节点 metadata（机器码 + 人读文案并存）。"""
    if not data.get("is_error"):
        return None, None
    message = _summarize(content, 500) or "工具执行失败"
    return TOOL_EXECUTION_ERROR, message


class ConversationBridge:
    """统一会话旁路桥。``service`` 为 ConversationService 实例。"""

    def __init__(self, service: ConversationService, *,
                 buffer_max_bytes: int = _DEFAULT_BUFFER_MAX_BYTES):
        self.service = service
        # 五个跨线程字段（_stop_requested / _delivery_seq / _turn_stage /
        # _pending_assistant_segment / _active_turn）统一由 _turn_state_lock
        # 保护：Agent 事件在 executor 线程触发，终态/停止请求来自事件循环或
        # runner 线程，dict 单操作虽原子，但"读-改-写"序列（投递序号自增、
        # 停止标志消费）需要互斥才不丢更新。
        self._turn_state_lock = threading.Lock()
        self._stop_requested: dict[str, bool] = {}  # conversation_id → stop 已请求
        # 父 Turn 停止联动（设计方案 14.4）：Plan/Goal 暂停、Subagent 取消
        self._parent_stop_hook: Optional[Callable[[str], object]] = None
        # 统一执行器引用（渠道 /stop 联动，设计方案 11.7）
        self._runner = None
        # 渠道投递版本序号（设计方案 11.5：按会话内投递递增）
        self._delivery_seq: dict[str, int] = {}
        # C-2：session_key → conversation_id 内存缓存（on_agent_event 每 delta
        # 调用 _conversation_id，若每次都查 get_conversation_by_key，流式速率被
        # DB 吞吐钳制；会话删除事件时经 service 钩子失效）。
        self._session_key_cache: dict[str, str] = {}
        self._session_key_cache_lock = threading.Lock()
        # 服务端流式 delta 合并（设计方案 16.4；C-3 批量提交 / C-4 字节上限）
        self._merger = _DeltaMerger(
            self._flush_deltas, max_bytes=buffer_max_bytes,
            on_drop=self._on_delta_drop)
        self._flusher_task: Optional[asyncio.Task] = None
        # C-2：会话删除 → 失效 session_key 缓存
        try:
            self.service.set_conversation_deleted_hook(
                self._invalidate_session_key)
        except Exception:
            logger.debug("注册会话删除钩子失败", exc_info=True)
        # Turn 阶段状态缓存（设计方案 7.4：thinking/tool/answering 流转，变化才提交）
        self._turn_stage: dict[str, str] = {}
        # P2修复：message_start 先于 reasoning_delta 到达时，若立即创建 assistant
        # 空节点，其 position 会先于 reasoning 节点 → 思考卡沉到回复下方。
        # 改为标记新段，首个 text_delta 到达时先 flush（落 reasoning）再建节点。
        self._pending_assistant_segment: dict[tuple[str, str], bool] = {}
        # reasoning 分段标记：每轮（message_start / runtime 工具调用后）的思考
        # 各自成节点，而非整 Turn 合并进一张思考卡。与 assistant 段标记同一
        # 模式：分段点置位 → 下一个 reasoning_delta 先 flush 旧段 + 新建节点。
        self._pending_reasoning_segment: dict[tuple[str, str], bool] = {}
        # 活动 Turn 缓存：conversation_id → turn_id。流式 delta 每个事件都调
        # ensure_turn，若每事件一次 get_active_turn SQLite 查询，会把网络流式
        # 速率钳制在 DB 吞吐之下（见基准 ~0.8ms/delta）。缓存在 Turn 终态失效。
        self._active_turn: dict[str, str] = {}
        # P2：Plan/Goal 子任务实时活动环形缓冲（runtime_id → 最近 K 条活动），
        # 供 get_runtime_status 高频读取，避免每次查询都打 SQLite。
        self._runtime_buffer_lock = threading.Lock()
        self._runtime_buffer: dict[str, deque] = {}
        self._runtime_buffer_max = 50
        # P2：runtime.progress 心跳发布钩子（可选，由宿主注入 EventBus；
        # 运行中按 ≥5s 节流发布，终态由 plan.changed/goal.changed 覆盖）。
        self.runtime_progress_publish: Optional[Callable[[dict], None]] = None
        self._runtime_progress_last: dict[str, float] = {}

    def _set_turn_stage(self, conversation_id: str, turn_id: str,
                        stage: str) -> None:
        """阶段状态流转（变化才写库/广播，避免每个 delta 一次事务）。"""
        with self._turn_state_lock:
            unchanged = self._turn_stage.get(turn_id) == stage
        if unchanged:
            return
        try:
            self.service.set_turn_status(conversation_id, turn_id, stage)
            with self._turn_state_lock:
                self._turn_stage[turn_id] = stage
        except Exception:
            logger.debug("Turn 阶段状态流转失败: %s -> %s", turn_id, stage)

    def _flush_deltas(self, items) -> None:
        """合并器批量提交回调（C-3）：同窗多 key 合并进单个事务。

        items: list[(conversation_id, turn_id, node_type, text)]；
        任一 key 失败整批回滚，合并器保留缓冲下轮重试。"""
        self.service.upsert_node_deltas(list(items))

    def _on_delta_drop(self, key: tuple, text: str) -> None:
        """合并器字节超限丢弃回调（C-4）：广播 version_gap 供前端快照自愈。"""
        try:
            self.service.publish_version_gap(key[0])
        except Exception:
            logger.debug("delta 超限 version_gap 广播失败: %s", key[0])

    def start_flusher(self) -> None:
        """启动后台 delta 刷盘协程（约每 100ms 提交一次，模型停顿时前端仍可见）。"""
        if self._flusher_task is not None and not self._flusher_task.done():
            return
        self._flusher_task = asyncio.create_task(self._flush_loop())

    async def stop_flusher(self) -> None:
        if self._flusher_task is not None:
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except (asyncio.CancelledError, Exception):
                pass
            self._flusher_task = None
        # B2：停机前把积压的 published 标记落库，避免重启后 OutboxPublisher 重复补发
        try:
            await asyncio.to_thread(self.service.flush_outbox_published)
        except Exception:
            logger.debug("停机 Outbox 批量标记失败", exc_info=True)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(_DELTA_MERGE_SECONDS)
            try:
                # flush 内是同步 SQLite 写，移到线程池避免阻塞事件循环
                await asyncio.to_thread(self._flush_sync)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("delta 后台刷盘异常")

    def _flush_sync(self) -> None:
        """100ms flusher 的同步体：delta 合并落库 + Outbox published 批量标记（B2）。

        B2：service._emit 只 publish 并把 outbox_id 积入内存，这里随同一
        flusher 周期用单事务批量标记，避免逐事件开事务。"""
        self._merger.flush()
        try:
            self.service.flush_outbox_published()
        except Exception:
            logger.exception("Outbox published 批量标记失败")

    def set_parent_stop_hook(self, hook: Optional[Callable[[str], object]]) -> None:
        """注册父 Turn 停止联动回调：``hook(session_key)`` 返回协程或 None。"""
        self._parent_stop_hook = hook

    def _fire_parent_stop(self, session_key: str) -> None:
        if self._parent_stop_hook is None:
            return
        try:
            result = self._parent_stop_hook(session_key)
            if result is not None and hasattr(result, "__await__"):
                try:
                    asyncio.get_running_loop().create_task(result)
                except RuntimeError:
                    logger.warning("无运行中事件循环，跳过父 Turn 停止联动: %s",
                                   session_key)
        except Exception:
            logger.exception("父 Turn 停止联动失败: %s", session_key)

    # ------------------------------------------------------------
    # session_key → conversation
    # ------------------------------------------------------------

    @staticmethod
    def _origin_subtype(session_key: str) -> tuple[str, str]:
        if session_key.startswith("workspace:"):
            return "webui", "workspace"
        if session_key.startswith("feishu:"):
            return "channel", "feishu"
        if session_key.startswith("weixin:"):
            return "channel", "weixin"
        if session_key.startswith("debug:"):
            return "channel", "debug"
        if session_key.startswith("sched:") or session_key.startswith("scheduler:"):
            # 定时任务会话键为 sched:name(:ts)（scheduler.py），此前只匹配
            # scheduler: 前缀导致 sched:* 落入下面的 webui,main —— 定时任务会话
            # 以 origin=webui 进主会话列表。补齐 sched: 前缀。
            return "system", "scheduler"
        if session_key.startswith("heartbeat:"):
            return "system", "heartbeat"
        if session_key.startswith("system:"):
            parts = session_key.split(":", 2)
            return "system", (parts[1] if len(parts) > 1 else "other")
        return "webui", "main"

    def resolve(self, session_key: str, *, route_metadata: Optional[dict] = None):
        origin, subtype = self._origin_subtype(session_key)
        workspace_id = None
        if session_key.startswith("workspace:"):
            parts = session_key.split(":", 2)
            if len(parts) >= 2:
                workspace_id = parts[1]
        conversation = self.service.get_or_create_conversation(
            session_key, origin=origin, subtype=subtype,
            workspace_id=workspace_id, route_metadata=route_metadata)
        # C-2：resolve 即预热 session_key → conversation_id 缓存，
        # 后续 on_agent_event / record_channel_delivery 不再查库。
        self._cache_session_key(session_key, conversation.conversation_id)
        return conversation

    # ------------------------------------------------------------
    # 入站
    # ------------------------------------------------------------

    def on_inbound(self, msg) -> Optional[str]:
        """入站消息：渠道持久化去重（设计方案 11.4）→ 入队（渠道建 queued
        User Node）。返回 conversation_id；重复消息返回 None（调用方应跳过）。"""
        session_key = getattr(msg, "session_key", "")
        channel = getattr(msg, "channel", "")
        message_id = getattr(msg, "message_id", "")
        try:
            conv = self.resolve(session_key, route_metadata={
                "channel": channel, "user_id": getattr(msg, "user_id", ""),
                "is_group": bool(getattr(msg, "is_group", False)),
            })
            # 渠道持久化去重（跨重启；窗口 72 小时）。先 resolve 再查重：
            # 首条消息也原子记录收据，修复并发/重试的首条消息重复入队缺口
            # （设计方案 11.4）。
            if channel not in ("webui", "debug") and message_id:
                if self.service.check_and_record_receipt(
                        conv.conversation_id, channel, message_id):
                    logger.info("渠道重复消息跳过: %s %s", channel, message_id)
                    return None
            # WebUI command routes (/model, /perm, /reasoning, …) also enter through
            # the legacy dispatcher. Persist their source node so the unified
            # ConversationPage shows the command and its reply in order.
            # 所有渠道（飞书/微信/debug）统一建 queued User Node（设计方案 11.3），
            # 供统一 runner 出队时原位升级并携带 source_message_id（回复投递匹配）
            create_queued = channel not in ("webui",)
            self.service.enqueue(
                conv.conversation_id,
                getattr(msg, "text", "") or "[图片消息]",
                channel=channel, message_id=message_id,
                sender_id=getattr(msg, "user_id", ""),
                sender_name=getattr(msg, "user_name", ""),
                create_queued_node=create_queued,
            )
            return conv.conversation_id
        except Exception:
            logger.exception("统一会话入站记录失败: %s", session_key)
            return None

    def record_channel_delivery(self, msg, state: str) -> None:
        """渠道回复投递状态（设计方案 11.5/30.2 delivery.status）。

        发送前 pending_delivery → 成功后 delivered / 异常 delivery_failed；
        delivery 版本按会话内投递序号递增。"""
        try:
            session_key = getattr(msg, "session_key", "")
            conversation_id = self._conversation_id(session_key)
            if conversation_id is None:
                return
            channel = getattr(msg, "channel", "")
            message_id = str(getattr(msg, "message_id", "") or "")
            turn_id = str((getattr(msg, "metadata", None) or {}).get("turn_id") or "")
            if not turn_id:
                active = self.service.store.get_active_turn(conversation_id)
                turn_id = active.turn_id if active else ""
            version = 0
            with self._turn_state_lock:
                version = self._delivery_seq.get(conversation_id, 0) + 1
                self._delivery_seq[conversation_id] = version
            self.service.record_delivery(
                conversation_id, turn_id=turn_id, channel=channel,
                message_id=message_id, state=state, version=version)
        except Exception:
            logger.debug("渠道投递状态记录失败: %s",
                         getattr(msg, "session_key", ""))

    def on_inbound_stop(self, msg) -> Optional[str]:
        """渠道 /stop（设计方案 11.7）：停止当前活动 Turn，不进入队列。

        返回 conversation_id（找到活动 Turn 时）；无活动 Turn 返回 None。"""
        session_key = getattr(msg, "session_key", "")
        try:
            conversation_id = self._conversation_id(session_key)
            if conversation_id is None:
                return None
            active = self.service.store.get_active_turn(conversation_id)
            if active is None:
                return None
            with self._turn_state_lock:
                self._stop_requested[conversation_id] = True
            self.service.request_stop(conversation_id)
            # 真正打断运行中的 Agent（若由统一 runner 执行）
            runner = getattr(self, "_runner", None)
            if runner is not None:
                runner.request_stop(conversation_id)
            return conversation_id
        except Exception:
            logger.exception("渠道 /stop 失败: %s", session_key)
            return None

    # ------------------------------------------------------------
    # Turn 生命周期（进程内懒创建）
    # ------------------------------------------------------------

    def ensure_turn(self, conversation_id: str, *,
                    source_message_id: Optional[str] = None,
                    runtime_snapshot_id: Optional[str] = None) -> Optional[str]:
        """确保存在活动 Turn：有则复用；无则出队队首（渠道升级 queued node）。

        缓存命中时直接返回，避免每个流式 delta 一次 ``get_active_turn`` 查询。
        """
        with self._turn_state_lock:
            cached = self._active_turn.get(conversation_id)
        if cached:
            return cached
        active = self.service.store.get_active_turn(conversation_id)
        if active is not None:
            with self._turn_state_lock:
                self._active_turn[conversation_id] = active.turn_id
            return active.turn_id
        channel_node_id = None
        if source_message_id:
            node = self.service.store.find_queued_node(conversation_id,
                                                       source_message_id)
            if node is not None:
                channel_node_id = node.node_id
        result = self.service.send_next(
            conversation_id, runtime_snapshot_id=runtime_snapshot_id,
            channel_node_id=channel_node_id)
        if result is not None:
            # 新 Turn 开始：消费陈旧停止标志（停止请求只作用于其发起时的活动 Turn）
            with self._turn_state_lock:
                self._stop_requested.pop(conversation_id, None)
                self._active_turn[conversation_id] = result[0].turn_id
            return result[0].turn_id
        turn = self.service.start_turn(conversation_id,
                                       runtime_snapshot_id=runtime_snapshot_id)
        with self._turn_state_lock:
            self._stop_requested.pop(conversation_id, None)
            self._active_turn[conversation_id] = turn.turn_id
        return turn.turn_id

    def _discard_active_turn(self, conversation_id: str) -> None:
        """Turn 终态后丢弃活动 Turn 缓存（下次事件重新查询最新活动 Turn）。"""
        with self._turn_state_lock:
            self._active_turn.pop(conversation_id, None)

    def _cache_session_key(self, session_key: str,
                            conversation_id: str) -> None:
        with self._session_key_cache_lock:
            self._session_key_cache[session_key] = conversation_id

    def _invalidate_session_key(self, session_key: str) -> None:
        """C-2：会话删除事件 → 失效 session_key→conversation_id 缓存。"""
        with self._session_key_cache_lock:
            self._session_key_cache.pop(session_key, None)

    def _conversation_id(self, session_key: str) -> Optional[str]:
        # C-2：优先内存缓存，避免 on_agent_event 每 delta 一次
        # get_conversation_by_key 查询（流式速率不再被 DB 吞吐钳制）。
        cached = self._session_key_cache.get(session_key)
        if cached is not None:
            return cached
        conv = self.service.store.get_conversation_by_key(session_key)
        if conv is None:
            return None
        self._cache_session_key(session_key, conv.conversation_id)
        return conv.conversation_id

    # ------------------------------------------------------------
    # Agent 运行时事件 → 节点
    # ------------------------------------------------------------

    def on_agent_event(self, msg, event) -> None:
        try:
            meta = dict(getattr(msg, "metadata", None) or {})
            task_source = str(meta.get("task_source") or "")
            if task_source in _SYSTEM_SOURCES:
                self._on_system_event(msg, task_source, meta, event)
                return
            session_key = getattr(msg, "session_key", "")
            conversation_id = self._conversation_id(session_key)
            if conversation_id is None:
                return
            turn_id = self.ensure_turn(
                conversation_id, source_message_id=getattr(msg, "message_id", ""),
                runtime_snapshot_id=meta.get("snapshot_id") or None)
            event_type = event.get("type") or ""
            data = dict(event.get("data") or {})
            # 审批通过后推进（设计方案 7.7）：Turn 处于 approval 时，任何后续
            # 运行事件（工具结果/文本）到达 → 推进为 tool/answering。审批拒绝
            # （denied）在 ask 返回 "n" 后工具本身被拒，事件流自然终止。
            if turn_id:
                try:
                    active = self.service.store.get_active_turn(conversation_id)
                    if active is not None and active.turn_id == turn_id \
                            and active.status == TurnStatus.APPROVAL.value \
                            and event_type in (
                                "tool_call_start", "tool_call_end",
                                "message_start", "text_delta"):
                        self.service.set_turn_status(
                            conversation_id, turn_id, "tool" if event_type in (
                                "tool_call_start", "tool_call_end") else "answering")
                except Exception:
                    logger.debug("审批后 Turn 推进失败: %s", turn_id)
            if event_type == "reasoning_delta":
                text = str(data.get("text") or "")
                if text:
                    self._set_turn_stage(conversation_id, turn_id, "thinking")
                    segment_key = (conversation_id, turn_id)
                    with self._turn_state_lock:
                        new_reasoning_segment = self._pending_reasoning_segment.pop(
                            segment_key, None)
                    if new_reasoning_segment:
                        # 新一轮思考：先 flush 上一段缓冲（落到旧节点），再新建
                        # reasoning 节点——多轮工具调用间的思考各自成卡。
                        self._merger.flush()
                        self.service.upsert_node_delta(
                            conversation_id, turn_id,
                            TurnNodeType.REASONING.value, "",
                            continue_existing=False)
                    self._merger.accumulate(
                        (conversation_id, turn_id, TurnNodeType.REASONING.value),
                        text)
            elif event_type == "message_start" and data.get("role") == "assistant":
                # 新 Assistant 段（被工具打断后再次 message_start → 新节点）。
                # 修复：不在此处立即创建 assistant 空节点——message_start 先于
                # reasoning_delta 到达，若先建节点，思考节点 position 会更大，
                # 前端思考卡沉到回复下方。改为标记新段，首个 text_delta 到达时
                # 先 flush（缓冲的 reasoning 先落库）再新建 assistant 节点。
                self._set_turn_stage(conversation_id, turn_id, "answering")
                self._merger.flush()  # 先落上一段（含缓冲的 reasoning）
                try:
                    # 新一轮开始 → 上一轮 assistant 是中间输出（后面还有内容）
                    self.service.finalize_node(
                        conversation_id, turn_id, TurnNodeType.ASSISTANT.value,
                        mark_intermediate=True)
                except Exception:
                    logger.debug("assistant 节点收敛失败: %s", turn_id)
                # reasoning 分段：上一轮思考节点收敛为 done，下一轮
                # reasoning_delta 新建节点（多轮思考各自成卡）。
                try:
                    self.service.finalize_node(
                        conversation_id, turn_id, TurnNodeType.REASONING.value)
                except Exception:
                    logger.debug("reasoning 节点收敛失败: %s", turn_id)
                with self._turn_state_lock:
                    self._pending_assistant_segment[
                        (conversation_id, turn_id)] = True
                    self._pending_reasoning_segment[
                        (conversation_id, turn_id)] = True
            elif event_type == "text_delta":
                text = str(data.get("text") or "")
                if text:
                    self._set_turn_stage(conversation_id, turn_id, "answering")
                    segment_key = (conversation_id, turn_id)
                    with self._turn_state_lock:
                        new_segment = self._pending_assistant_segment.pop(
                            segment_key, None)
                    if new_segment:
                        # 新段首个文本：先把缓冲的 reasoning 落库（保证思考节点
                        # position 在前），再新建本段 assistant 节点。
                        self._merger.flush()
                        self.service.upsert_node_delta(
                            conversation_id, turn_id,
                            TurnNodeType.ASSISTANT.value, "",
                            continue_existing=False)
                    self._merger.accumulate(
                        (conversation_id, turn_id, TurnNodeType.ASSISTANT.value),
                        text)
            elif event_type == "tool_call_start":
                self._set_turn_stage(conversation_id, turn_id, "tool")
                self._merger.flush()  # 文本段结束，工具状态立即提交
                # 被打断的 assistant 节点收敛为 done——后面跟工具调用，
                # 后端权威标记 intermediate=True（前端据此渲染条卡）
                try:
                    self.service.finalize_node(
                        conversation_id, turn_id, TurnNodeType.ASSISTANT.value,
                        mark_intermediate=True)
                except Exception:
                    logger.debug("assistant 节点收敛失败: %s", turn_id)
                call_id = str(data.get("tool_call_id") or "")
                if call_id:
                    self.service.upsert_tool_node(
                        conversation_id, turn_id, call_id, status="running",
                        tool_name=str(data.get("tool") or ""))
            elif event_type == "tool_call_end":
                call_id = str(data.get("tool_call_id") or "")
                if call_id:
                    self.service.upsert_tool_node(
                        conversation_id, turn_id, call_id, status="done",
                        tool_name=str(data.get("tool") or ""),
                        params_summary=_summarize(data.get("arguments")))
            elif event_type == "message_start" and data.get("role") == "tool":
                # 工具结果：message_id = "result_{call_id}"（content 随 message_end 到达）
                mid = str(data.get("message_id") or "")
                if mid.startswith("result_"):
                    call_id = mid[len("result_"):]
                    self.service.upsert_tool_node(
                        conversation_id, turn_id, call_id, status="done",
                        tool_name=str(data.get("tool") or ""))
            elif event_type == "message_end" and data.get("role") == "tool":
                # content 实际随 message_end 到达（agent.py 事件契约）。
                # 设计方案 17 分档：≤64KB 内嵌 result_summary；>64KB 独立
                # tool_results 表存储（result_ref + 前/后 8KB 摘要）。
                mid = str(data.get("message_id") or "")
                if mid.startswith("result_"):
                    call_id = mid[len("result_"):]
                    content = data.get("content")
                    size = _result_size_bytes(content)
                    # X2-P3⑫：is_error=True → error_code + message 并存写入
                    # 工具节点 metadata（前端卡片同时展示机器码与人读文案）
                    error_code, error_message = _tool_error_fields(data, content)
                    if size <= _RESULT_EMBED_MAX:
                        self.service.upsert_tool_node(
                            conversation_id, turn_id, call_id, status="done",
                            tool_name=str(data.get("tool") or ""),
                            result_summary=_summarize(
                                content, _RESULT_SUMMARY_MAX),
                            error_code=error_code,
                            error_message=error_message)
                    else:
                        result_ref = f"tool-{call_id[:16]}"
                        text = _result_text(content)
                        summary = {
                            "head": text[:_RESULT_HEAD_TAIL],
                            "tail": text[-_RESULT_HEAD_TAIL:] if len(text) > _RESULT_HEAD_TAIL else "",
                            "size_bytes": size,
                            "lines": text.count("\n") + 1,
                            "content_type": "text/plain",
                            "truncation_reason": (
                                "oversize_64kb" if size <= _RESULT_STORE_MAX
                                else "oversize_10mb"),
                        }
                        node_id = gen_node_id_from_call(call_id, conversation_id)
                        try:
                            self.service.save_tool_result(
                                conversation_id, turn_id=turn_id,
                                result_ref=result_ref, kind="tool_result",
                                node_id=node_id, size_bytes=size,
                                lines=text.count("\n") + 1,
                                content_type="text/plain",
                                summary=summary,
                                truncation_reason=summary["truncation_reason"])
                            self.service.upsert_tool_node(
                                conversation_id, turn_id, call_id, status="done",
                                tool_name=str(data.get("tool") or ""),
                                result_summary=_summarize(
                                    content, _RESULT_SUMMARY_MAX),
                                result_ref=result_ref,
                                result_size_bytes=size,
                                error_code=error_code,
                                error_message=error_message)
                        except Exception:
                            logger.exception("工具大结果存储失败，降级内嵌摘要: %s",
                                             call_id)
                            self.service.upsert_tool_node(
                                conversation_id, turn_id, call_id, status="done",
                                tool_name=str(data.get("tool") or ""),
                                result_summary=_summarize(
                                    content, _RESULT_SUMMARY_MAX),
                                error_code=error_code,
                                error_message=error_message)
        except Exception:
            logger.exception("统一会话节点记录失败: %s", getattr(msg, "session_key", ""))

    # ------------------------------------------------------------
    # 终态
    # ------------------------------------------------------------

    def on_reply(self, msg, text: str) -> None:
        """最终回复：chat.done 权威 + Turn 终态（设计方案 7.2/7.5）。"""
        try:
            meta = dict(getattr(msg, "metadata", None) or {})
            task_source = str(meta.get("task_source") or "")
            if task_source in _SYSTEM_SOURCES:
                self._on_system_reply(msg, task_source, meta, text)
                return
            session_key = getattr(msg, "session_key", "")
            conversation_id = self._conversation_id(session_key)
            if conversation_id is None:
                return
            self._merger.flush()  # Turn 结束前强制落库剩余 delta
            turn_id = self.ensure_turn(
                conversation_id, source_message_id=getattr(msg, "message_id", ""),
                runtime_snapshot_id=meta.get("snapshot_id") or None)
            if self.consume_stop_requested(conversation_id):
                # 停止请求已受理：终态为 stopped（设计方案 7.6）
                self.service.complete_turn(conversation_id, turn_id, "stopped")
                self._cancel_turn_approvals(conversation_id, turn_id)
                self._fire_parent_stop(session_key)
            else:
                self.service.complete_turn(conversation_id, turn_id, "done",
                                           full_text=str(text or ""))
            with self._turn_state_lock:
                self._turn_stage.pop(turn_id, None)
            self._discard_active_turn(conversation_id)
        except Exception:
            logger.exception("统一会话终态记录失败: %s", getattr(msg, "session_key", ""))

    def on_error(self, msg, text: Optional[str] = None) -> None:
        """执行失败：Turn 终态 error（保留诊断，不改写历史）。"""
        try:
            session_key = getattr(msg, "session_key", "")
            conversation_id = self._conversation_id(session_key)
            if conversation_id is None:
                return
            self._merger.flush()
            turn_id = self.ensure_turn(
                conversation_id, source_message_id=getattr(msg, "message_id", ""))
            self.service.complete_turn(conversation_id, turn_id, "error",
                                       error_code=AGENT_EXECUTION_FAILED)
            self._cancel_turn_approvals(conversation_id, turn_id)
            with self._turn_state_lock:
                self._turn_stage.pop(turn_id, None)
                self._stop_requested.pop(conversation_id, None)
            self._discard_active_turn(conversation_id)
        except Exception:
            logger.exception("统一会话错误终态失败: %s", getattr(msg, "session_key", ""))

    def on_stopped(self, session_key: str) -> None:
        """停止确认：活动 Turn → stopped，并联动 Plan/Goal/Subagent（14.4）。"""
        try:
            conversation_id = self._conversation_id(session_key)
            if conversation_id is None:
                return
            self._merger.flush()
            active = self.service.store.get_active_turn(conversation_id)
            if active is not None:
                self.service.complete_turn(conversation_id, active.turn_id, "stopped")
                self._cancel_turn_approvals(conversation_id, active.turn_id)
                with self._turn_state_lock:
                    self._turn_stage.pop(active.turn_id, None)
                    self._stop_requested.pop(conversation_id, None)
                self._discard_active_turn(conversation_id)
                self._fire_parent_stop(session_key)
        except Exception:
            logger.exception("统一会话停止终态失败: %s", session_key)

    def _cancel_turn_approvals(self, conversation_id: str, turn_id: str) -> None:
        """Turn 终态时取消未决审批（X2-P3⑫）：广播 approval.resolved
        (status=cancelled)，避免审批卡片永久悬挂；失败不阻断终态。"""
        try:
            self.service.cancel_pending_approvals(conversation_id, turn_id)
        except Exception:
            logger.debug("取消未决审批失败: turn=%s", turn_id, exc_info=True)

    def mark_stop_requested(self, conversation_id: str) -> None:
        """标记停止已请求（供 API 停止路径联动，设计方案 7.6）。"""
        with self._turn_state_lock:
            self._stop_requested[conversation_id] = True

    def consume_stop_requested(self, conversation_id: str) -> bool:
        """消费停止请求标志（返回是否曾有未消费的停止请求）。

        供 runner 看门终态路径调用：停止被看门/确认处理后必须消费标志，
        否则陈旧标志会让下一个健康 Turn 被误记 stopped 并丢弃最终回复。
        """
        with self._turn_state_lock:
            return self._stop_requested.pop(conversation_id, False)

    def discard_active_turn(self, conversation_id: str) -> None:
        """Turn 终态后丢弃活动 Turn 缓存（供 runner 看门路径调用）。"""
        self._discard_active_turn(conversation_id)

    def request_stop(self, session_key: str) -> None:
        """停止请求：活动 Turn → stopping（无租约校验，控制租约已废弃）。

        同时联动 runner.request_stop：真正打断运行中的 Agent 并登记停止看门
        （统一进看门，设计方案 7.6）。
        """
        try:
            conversation_id = self._conversation_id(session_key)
            if conversation_id is None:
                return
            with self._turn_state_lock:
                self._stop_requested[conversation_id] = True
            self.service.request_stop(conversation_id)
            runner = getattr(self, "_runner", None)
            if runner is not None:
                runner.request_stop(conversation_id)
        except Exception:
            logger.exception("统一会话停止请求失败: %s", session_key)

    # ------------------------------------------------------------
    # Plan/Goal/Subagent → 系统 Conversation + 父会话投影
    # ------------------------------------------------------------

    def _runtime_id(self, task_source: str, meta: dict) -> str:
        if task_source == "plan":
            return str(meta.get("plan_id") or meta.get("plan_task_id") or "")
        if task_source == "goal":
            return str(meta.get("goal_id") or "")
        return str(meta.get("subagent_id") or meta.get("task_id") or "")

    def _parent_runtime_turn(self, msg, task_source: str, runtime_id: str):
        """解析父会话并确保存在 runtime turn（plan/goal 在主会话累积）。

        返回 ``(conversation_id, turn_id)``；复用父会话当前活动 Turn，无则新建。
        直接用 ``start_turn``（复用活动/新建，不弹父队列），避免把用户新排队的
        消息误消费进 plan/goal 的 runtime turn。
        """
        parent_key = getattr(msg, "session_key", "")
        conv = self.resolve(parent_key)
        turn = self.service.start_turn(conv.conversation_id)
        return conv.conversation_id, turn.turn_id

    # ------------------------------------------------------------
    # P2：Plan/Goal 子任务实时活动环形缓冲 + runtime.progress 心跳
    # ------------------------------------------------------------

    _RUNTIME_BUFFER_MAX = 50
    _RUNTIME_PROGRESS_MIN_INTERVAL = 5.0  # 运行中心跳最小间隔（秒）

    def _buffer_runtime_activity(self, runtime_id: str, entry: dict,
                                 session_key: str = "") -> None:
        """把一条子任务实时活动写入环形缓冲（call_id 相同的工具事件原位更新）。

        runtime_id 为空时忽略；缓冲按 runtime_id 分桶，容量 _RUNTIME_BUFFER_MAX，
        超限丢弃最旧。线程安全（bridge 方法可能来自 executor 线程）。
        """
        runtime_id = str(runtime_id or "").strip()
        if not runtime_id:
            return
        entry = dict(entry)
        entry.setdefault("at", time.time())
        entry["session_key"] = str(session_key or "")
        call_id = str(entry.get("call_id") or "")
        # 注意：_publish_runtime_progress 也会拿 _runtime_buffer_lock（非可重入），
        # 必须在锁外调用，否则同 call_id 原位更新路径会自锁死锁。
        with self._runtime_buffer_lock:
            bucket = self._runtime_buffer.get(runtime_id)
            if bucket is None:
                bucket = self._runtime_buffer[runtime_id] = deque(maxlen=self._RUNTIME_BUFFER_MAX)
            if call_id:
                for existing in bucket:
                    if existing.get("call_id") == call_id:
                        # 同一工具调用：原位更新状态/结果，保持时间序。
                        existing.update({k: v for k, v in entry.items() if v is not None})
                        break
                else:
                    bucket.append(entry)
            else:
                bucket.append(entry)
        self._publish_runtime_progress(runtime_id, entry)

    def recent_runtime_activity(self, runtime_id: str, limit: int = 10) -> list[dict]:
        """读取某 runtime_id 最近的活动（快照，新→旧），供 get_runtime_status 快速路径。"""
        runtime_id = str(runtime_id or "").strip()
        if not runtime_id:
            return []
        with self._runtime_buffer_lock:
            bucket = self._runtime_buffer.get(runtime_id)
            if not bucket:
                return []
            entries = [dict(item) for item in bucket]
        entries.reverse()
        return entries[: max(1, min(int(limit or 10), self._RUNTIME_BUFFER_MAX))]

    def _publish_runtime_progress(self, runtime_id: str, entry: dict) -> None:
        """运行中心跳（≥5s 节流）：把最新活动摘要广播给 UI/外部观察者。

        终态由 plan.changed / goal.changed 覆盖，这里只补运行中过程态。
        发布失败静默（心跳属尽力而为）。
        """
        publish = self.runtime_progress_publish
        if publish is None:
            return
        now = time.monotonic()
        with self._runtime_buffer_lock:
            last = self._runtime_progress_last.get(runtime_id, 0.0)
            if now - last < self._RUNTIME_PROGRESS_MIN_INTERVAL:
                return
            self._runtime_progress_last[runtime_id] = now
        try:
            publish({
                "runtime_id": runtime_id,
                "session_key": entry.get("session_key") or "",
                "type": entry.get("type"),
                "tool": entry.get("tool"),
                "status": entry.get("status"),
                "summary": entry.get("result_summary") or entry.get("params_summary") or "",
            })
        except Exception:
            logger.debug("runtime.progress 心跳发布失败", exc_info=True)

    def _write_runtime_event_to_parent(self, msg, task_source: str,
                                       runtime_id: str, event_type: str,
                                       data: dict) -> None:
        """把 plan/goal 运行事件（reasoning/tool）写入父会话 runtime turn。

        只写 reasoning/tool 节点（作为卡片可展开明细）；assistant 流式文本不写
        父会话（避免中间答复平铺），最终回复由 ``_on_system_reply`` 经
        ``append_runtime_node`` 写成带 runtime 标记的 assistant 节点。
        """
        try:
            parent_conv_id, parent_turn_id = self._parent_runtime_turn(
                msg, task_source, runtime_id)
            runtime_meta = {"runtime_type": task_source,
                            "runtime_id": runtime_id or "task"}
            if event_type == "reasoning_delta":
                text = str(data.get("text") or "")
                if text:
                    self._set_turn_stage(parent_conv_id, parent_turn_id, "thinking")
                    segment_key = (parent_conv_id, parent_turn_id)
                    with self._turn_state_lock:
                        new_reasoning_segment = self._pending_reasoning_segment.pop(
                            segment_key, None)
                    if new_reasoning_segment:
                        # runtime 轮内多步思考各自成节点（与普通路径同规则）：
                        # 工具调用后的新一轮思考不再追加进首张思考卡。
                        self._merger.flush()
                        self.service.upsert_node_delta(
                            parent_conv_id, parent_turn_id,
                            TurnNodeType.REASONING.value, "",
                            continue_existing=False, metadata=runtime_meta)
                    self._merger.accumulate(
                        (parent_conv_id, parent_turn_id, TurnNodeType.REASONING.value),
                        text)
            elif event_type == "tool_call_start":
                self._merger.flush()
                # reasoning 分段：工具调用打断思考段，之后的思考新建节点。
                with self._turn_state_lock:
                    self._pending_reasoning_segment[
                        (parent_conv_id, parent_turn_id)] = True
                call_id = str(data.get("tool_call_id") or "")
                if call_id:
                    self.service.upsert_tool_node(
                        parent_conv_id, parent_turn_id, call_id, status="running",
                        tool_name=str(data.get("tool") or ""),
                        extra_metadata=runtime_meta)
                self._buffer_runtime_activity(runtime_id, {
                    "type": "tool", "call_id": call_id,
                    "tool": str(data.get("tool") or ""),
                    "status": "running", "params_summary": "", "result_summary": "",
                }, session_key=getattr(msg, "session_key", ""))
            elif event_type == "tool_call_end":
                call_id = str(data.get("tool_call_id") or "")
                if call_id:
                    self.service.upsert_tool_node(
                        parent_conv_id, parent_turn_id, call_id, status="done",
                        tool_name=str(data.get("tool") or ""),
                        params_summary=_summarize(data.get("arguments")),
                        extra_metadata=runtime_meta)
                self._buffer_runtime_activity(runtime_id, {
                    "type": "tool", "call_id": call_id,
                    "tool": str(data.get("tool") or ""),
                    "status": "done",
                    "params_summary": _summarize(data.get("arguments")),
                    "result_summary": "",
                }, session_key=getattr(msg, "session_key", ""))
            elif event_type == "message_end" and data.get("role") == "tool":
                mid = str(data.get("message_id") or "")
                if mid.startswith("result_"):
                    call_id = mid[len("result_"):]
                    content = data.get("content")
                    size = _result_size_bytes(content)
                    # X2-P3⑫：父会话工具错误同样 error_code + message 并存
                    error_code, error_message = _tool_error_fields(data, content)
                    if size <= _RESULT_EMBED_MAX:
                        self.service.upsert_tool_node(
                            parent_conv_id, parent_turn_id, call_id, status="done",
                            tool_name=str(data.get("tool") or ""),
                            result_summary=_summarize(
                                content, _RESULT_SUMMARY_MAX),
                            error_code=error_code,
                            error_message=error_message,
                            extra_metadata=runtime_meta)
                        self._buffer_runtime_activity(runtime_id, {
                            "type": "tool", "call_id": call_id,
                            "tool": str(data.get("tool") or ""),
                            "status": "error" if error_code else "done",
                            "params_summary": "",
                            "result_summary": _summarize(content, _RESULT_SUMMARY_MAX),
                        }, session_key=getattr(msg, "session_key", ""))
                    else:
                        result_ref = f"tool-{call_id[:16]}"
                        text = _result_text(content)
                        summary = {
                            "head": text[:_RESULT_HEAD_TAIL],
                            "tail": text[-_RESULT_HEAD_TAIL:] if len(text) > _RESULT_HEAD_TAIL else "",
                            "size_bytes": size,
                            "lines": text.count("\n") + 1,
                            "content_type": "text/plain",
                            "truncation_reason": (
                                "oversize_64kb" if size <= _RESULT_STORE_MAX
                                else "oversize_10mb"),
                        }
                        node_id = gen_node_id_from_call(call_id, parent_conv_id)
                        try:
                            self.service.save_tool_result(
                                parent_conv_id, turn_id=parent_turn_id,
                                result_ref=result_ref, kind="tool_result",
                                node_id=node_id, size_bytes=size,
                                lines=text.count("\n") + 1,
                                content_type="text/plain",
                                summary=summary,
                                truncation_reason=summary["truncation_reason"])
                            self.service.upsert_tool_node(
                                parent_conv_id, parent_turn_id, call_id,
                                status="done", tool_name=str(data.get("tool") or ""),
                                result_summary=_summarize(
                                    content, _RESULT_SUMMARY_MAX),
                                result_ref=result_ref,
                                result_size_bytes=size,
                                error_code=error_code,
                                error_message=error_message,
                                extra_metadata=runtime_meta)
                            self._buffer_runtime_activity(runtime_id, {
                                "type": "tool", "call_id": call_id,
                                "tool": str(data.get("tool") or ""),
                                "status": "error" if error_code else "done",
                                "params_summary": "",
                                "result_summary": _summarize(
                                    content, _RESULT_SUMMARY_MAX),
                            }, session_key=getattr(msg, "session_key", ""))
                        except Exception:
                            logger.exception("系统任务父会话工具大结果存储失败: %s",
                                             call_id)
                            self.service.upsert_tool_node(
                                parent_conv_id, parent_turn_id, call_id,
                                status="done", tool_name=str(data.get("tool") or ""),
                                result_summary=_summarize(
                                    content, _RESULT_SUMMARY_MAX),
                                error_code=error_code,
                                error_message=error_message,
                                extra_metadata=runtime_meta)
        except Exception:
            logger.debug("系统任务父会话事件写入失败: %s", runtime_id)

    def _on_system_event(self, msg, task_source: str, meta: dict, event) -> None:
        runtime_id = self._runtime_id(task_source, meta)
        # ---- 父会话 runtime 持久化（对齐 dsh：plan/goal 在主会话累积）----
        data = dict(event.get("data") or {})
        event_type = event.get("type") or ""
        self._write_runtime_event_to_parent(
            msg, task_source, runtime_id, event_type, data)

    def _on_system_reply(self, msg, task_source: str, meta: dict, text: str) -> None:
        runtime_id = self._runtime_id(task_source, meta)
        parent_key = getattr(msg, "session_key", "")
        full_text = str(text or "")
        # ---- 父会话 runtime 终态（对齐 dsh：goal/plan 主会话内累积）----
        try:
            parent_conv_id, parent_turn_id = self._parent_runtime_turn(
                msg, task_source, runtime_id)
            self._merger.flush()
            # plan 最终 step / goal 每轮带 final_response 元数据（dispatcher
            # 注入 msg.metadata）→ 节点标记 runtime_final，前端平铺为正式
            # 渲染消息；中间 step 回复保持折叠卡片。
            self.service.append_runtime_node(
                parent_conv_id, parent_turn_id, task_source,
                runtime_id or "task", full_text, status="done",
                final=bool(meta.get("final_response")))
            self._buffer_runtime_activity(runtime_id, {
                "type": "assistant", "status": "done",
                "result_summary": full_text[:200],
            }, session_key=getattr(msg, "session_key", ""))
            self.service.complete_turn(parent_conv_id, parent_turn_id, "done",
                                       full_text=full_text)
            with self._turn_state_lock:
                self._turn_stage.pop(parent_turn_id, None)
        except Exception:
            logger.exception("父会话 runtime 终态写入失败: %s", runtime_id)
