# -*- coding: utf-8 -*-
"""
统一会话服务 —— 方案的业务门面。

每个写操作 = 单事务（业务状态 + 版本递增 + Outbox 写入）→ 提交后广播；
广播 payload 即完整 GatewayEvent 数据（含 conversation 上下文 / scope / version），
因此 OutboxPublisher 补发时无需重新组装（设计方案 16.3 / 18.4）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from gateway.conversation.errors import (
    ExecutionScopeLimit,
    GatewaySaturated,
    QueueConflict,
    ResourceNotFound,
    ResultNotOwned,
    SteeringLimit,
    TurnNotFound,
    ValidationFailed,
)
from gateway.errors_catalog import (
    APPROVAL_CANCELLED,
    APPROVAL_TIMED_OUT,
    DELTA_BUFFER_OVERFLOW,
    GATEWAY_RESTART,
)
from gateway.conversation.models import (
    Approval,
    ApprovalStatus,
    ConversationOrigin,
    ConversationSession,
    ConversationSubtype,
    QueueItem,
    QueueItemStatus,
    Turn,
    TurnNode,
    TurnNodeType,
    TurnStatus,
    _loads_json,
    gen_node_id,
    utc_now,
)
from gateway.conversation.store import (
    APPROVAL_TIMEOUT_SECONDS,
    IDEMPOTENCY_TTL_SECONDS,
    MAX_STEERING_PER_TURN,
    OUTBOX_TTL_SECONDS,
    RECEIPT_TTL_SECONDS,
    STEERING_TIMEOUT_SECONDS,
    ConversationStore,
)

logger = logging.getLogger("jk_agent.gateway")

SCOPE_SESSION = "session"
SCOPE_TURN = "turn"
SCOPE_DELIVERY = "delivery"

# ---- 图片信封校验（修正版方案 A）------------------------------------------
IMAGE_ENVELOPE_MAX_COUNT = 4
IMAGE_ENVELOPE_MAX_BYTES = 4 * 1024 * 1024      # 单张 base64 解码后 ≤4MB
_IMAGE_MIME_ALLOW = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/gif",
})
_BASE64_RE = None  # 惰性编译


def _validate_image_envelope(images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """校验并归一化图片信封；非法输入抛 ValidationFailed（fail-closed）。

    - ≤4 张；单张解码后 ≤4MB；media_type 白名单 png/jpeg/webp/gif
    - data 必须是合法 base64；media_type 缺省回退 image/png（历史行为）
    - 返回规整后的 [{data, media_type}]（去空白、保序）
    """
    import base64
    import re as _re
    global _BASE64_RE
    if not isinstance(images, list) or not images:
        raise ValidationFailed("images 必须是非空数组")
    if len(images) > IMAGE_ENVELOPE_MAX_COUNT:
        raise ValidationFailed(f"图片最多 {IMAGE_ENVELOPE_MAX_COUNT} 张")
    if _BASE64_RE is None:
        _BASE64_RE = _re.compile(r"^[A-Za-z0-9+/\r\n]+={0,2}$")
    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(images):
        if not isinstance(item, dict):
            raise ValidationFailed(f"images[{idx}] 必须是对象")
        data = str(item.get("data") or "").strip()
        if not data:
            raise ValidationFailed(f"images[{idx}].data 不能为空")
        if not _BASE64_RE.match(data):
            raise ValidationFailed(f"images[{idx}].data 不是合法 base64")
        media_type = str(item.get("media_type") or "image/png").lower()
        if media_type not in _IMAGE_MIME_ALLOW:
            raise ValidationFailed(
                f"images[{idx}].media_type 不支持: {media_type}（允许 "
                "png/jpeg/webp/gif）")
        try:
            size = len(base64.b64decode(data, validate=False))
        except Exception as exc:
            raise ValidationFailed(f"images[{idx}] base64 解码失败: {exc}") from exc
        if size > IMAGE_ENVELOPE_MAX_BYTES:
            raise ValidationFailed(
                f"images[{idx}] 超过单张 {IMAGE_ENVELOPE_MAX_BYTES // (1024 * 1024)}MB 上限")
        normalized.append({"data": data, "media_type": media_type})
    return normalized


def default_execution_scope(origin: str, subtype: str,
                            workspace_id: Optional[str] = None) -> str:
    """设计方案 12/13：执行域映射。"""
    if subtype == ConversationSubtype.WORKSPACE.value and workspace_id:
        return f"workspace:{workspace_id}"
    if origin == ConversationOrigin.SYSTEM.value:
        # plan/goal/subagent/scheduler/heartbeat/debug 各自独立系统域
        return f"system:{subtype}"
    return "gateway:default"


def _request_hash(payload: dict) -> str:
    import json
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   default=str).encode("utf-8")).hexdigest()


class ConversationService:
    """统一会话业务服务。``publish(event_type, payload)`` 为广播回调
    （WebUI EventBus.publish 或测试中的记录器）。"""

    def __init__(self, store: ConversationStore,
                 publish: Callable[[str, dict], None],
                 *, max_turns_per_scope: int = 5,
                 max_global_turns: int = 4,
                 image_store=None):
        self.store = store
        self._publish = publish
        self.max_turns_per_scope = int(max_turns_per_scope)
        self.max_global_turns = int(max_global_turns)
        # 图片存取（修正版方案 A）：出队时把队列信封里的 base64 落盘并建
        # image 引用节点；None（未装配/测试）时图片走纯文本降级不入库。
        self.image_store = image_store
        # Steering 运行时等待（设计方案 9.1/9.3）：conversation_id → {qids, deadline}
        self._steering_pending: Dict[str, Dict[str, Any]] = {}
        self._steering_lock = threading.Lock()
        # B10：Steering 注册/结束时唤醒 runner 看门（conversation_id → None）
        self._steering_wait_hook: Optional[Callable[[str], None]] = None
        # B2：实时 publish 成功的 outbox_id 积入内存，随 100ms flusher 单事务批量标记
        self._published_outbox: List[str] = []
        self._published_lock = threading.Lock()
        # C-2：会话删除通知钩子（bridge 的 session_key→conversation_id 缓存失效用）
        self._conversation_deleted_hook: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------
    # Steering 运行时协调（设计方案 9.1-9.3）
    # ------------------------------------------------------------

    def set_conversation_deleted_hook(
            self, hook: Optional[Callable[[str], None]]) -> None:
        """注册会话删除通知（C-2：bridge 的 session_key→conversation_id
        内存缓存在此失效，避免删除后缓存命中已不存在的会话）。"""
        self._conversation_deleted_hook = hook

    def _notify_conversation_deleted(self, session_key: str) -> None:
        hook = self._conversation_deleted_hook
        if hook is None:
            return
        try:
            hook(session_key)
        except Exception:
            logger.debug("会话删除通知失败: %s", session_key)

    def set_steering_wait_hook(self, hook: Optional[Callable[[str], None]]) -> None:
        """注册 Steering 等待通知回调（B10：runner 看门事件唤醒用）。

        register_steering_wait / conclude_steering 时触发 ``hook(conversation_id)``，
        使 runner 的 _await_with_watchdogs 无需 0.5s 轮询即可感知 Steering 状态变化。"""
        self._steering_wait_hook = hook

    def _notify_steering_hook(self, conversation_id: str) -> None:
        hook = self._steering_wait_hook
        if hook is None:
            return
        try:
            hook(conversation_id)
        except Exception:
            logger.debug("Steering 通知回调失败: %s", conversation_id)

    def register_steering_wait(self, conversation_id: str,
                               queue_item_ids: list[str]) -> None:
        """记录 Steering 等待：从此刻起最多等待 10 秒（工具自然结束），
        超时 → 中断超时（SteeringTimeout）。"""
        import time as _time
        with self._steering_lock:
            self._steering_pending[conversation_id] = {
                "qids": list(queue_item_ids),
                "deadline": _time.time() + STEERING_TIMEOUT_SECONDS,
            }
        self._notify_steering_hook(conversation_id)

    def pending_steering(self, conversation_id: str) -> list[str] | None:
        """返回等待中的 Steering 队列项（无则 None）。"""
        with self._steering_lock:
            item = self._steering_pending.get(conversation_id)
            return list(item["qids"]) if item else None

    def steering_remaining(self, conversation_id: str) -> float | None:
        """Steering 等待剩余秒数（无等待或未启动返回 None）。"""
        import time as _time
        with self._steering_lock:
            item = self._steering_pending.get(conversation_id)
            if item is None:
                return None
            return max(0.0, item["deadline"] - _time.time())

    def conclude_steering(self, conversation_id: str) -> None:
        """Steering 已提交/中止，清除等待记录。"""
        with self._steering_lock:
            self._steering_pending.pop(conversation_id, None)
        self._notify_steering_hook(conversation_id)

    def check_steering_timeout(self, conversation_id: str) -> list[str] | None:
        """若等待超时（>10s）返回待恢复的队列项并清除；未超时返回 None。"""
        import time as _time
        with self._steering_lock:
            item = self._steering_pending.get(conversation_id)
            if item is None:
                return None
            if _time.time() < item["deadline"]:
                return None
            qids = list(item["qids"])
            self._steering_pending.pop(conversation_id, None)
            return qids

    # ------------------------------------------------------------
    # 内部助手
    # ------------------------------------------------------------

    def _conv(self, conversation_id: str) -> ConversationSession:
        return self.store.get_conversation(conversation_id)

    def _assert_turn_owned(self, conversation_id: str, turn_id: str) -> None:
        """归属校验：turn_id 必须属于 conversation_id（防跨会话状态机越权推进）。"""
        turn = self.store.get_turn(turn_id)
        if turn.conversation_id != conversation_id:
            raise TurnNotFound(f"Turn 不存在: {turn_id}")

    def _assert_queue_item_owned(self, conversation_id: str,
                                 queue_item_id: str) -> None:
        """归属校验：queue_item_id 必须属于 conversation_id（防跨会话队列操作）。"""
        item = self.store.get_queue_item(queue_item_id)
        if item.conversation_id != conversation_id:
            raise ResourceNotFound(f"队列项不存在: {queue_item_id}")

    def _assert_approval_owned(self, conversation_id: str,
                               approval_id: str) -> Approval:
        """归属校验：approval_id 必须属于 conversation_id（防跨会话审批越权）。"""
        approval = self.store.get_approval(approval_id)
        if approval.conversation_id != conversation_id:
            raise ResourceNotFound(f"审批不存在: {approval_id}")
        return approval

    def _event(self, conv: ConversationSession, scope: str, version: int,
               turn_id: Optional[str] = None, **data) -> dict:
        payload: Dict[str, Any] = {
            "conversation_id": conv.conversation_id,
            "session_key": conv.session_key,
            "origin": conv.origin,
            "subtype": conv.subtype,
            "workspace_id": conv.workspace_id,
            "scope": scope,
            "version": version,
        }
        if turn_id:
            payload["turn_id"] = turn_id
        payload["data"] = dict(data)
        return payload

    def _emit(self, event_type: str, payload: dict, outbox_id: Optional[str] = None) -> None:
        """实时广播事件（B2：只 publish，不再逐事件开事务标记 published）。

        publish 成功的 outbox_id 积入内存列表，由 100ms flusher 经
        flush_outbox_published 单事务批量标记；publish 失败保持 pending
        供 OutboxPublisher 兜底补发。"""
        published = False
        try:
            self._publish(event_type, payload)
            published = True
        except Exception:
            logger.exception("事件广播失败: %s", event_type)
        if outbox_id is not None and published:
            with self._published_lock:
                self._published_outbox.append(outbox_id)

    def flush_outbox_published(self) -> int:
        """批量标记已 publish 的 outbox 事件（B2，随 100ms flusher 单事务执行）。

        返回本次标记条数；事务失败时把未标记的 id 放回队列尾部，下轮重试
        （事件保持 pending，OutboxPublisher 兜底不会丢）。"""
        with self._published_lock:
            if not self._published_outbox:
                return 0
            batch, self._published_outbox = self._published_outbox, []
        try:
            with self.store.transaction() as conn:
                for outbox_id in batch:
                    self.store.mark_outbox_published(conn, outbox_id)
            return len(batch)
        except Exception:
            logger.debug("Outbox 批量标记失败（%d 条回退待重试）", len(batch))
            with self._published_lock:
                self._published_outbox = batch + self._published_outbox
            return 0

    def _append_and_emit(self, _pending: list, conn, conv: ConversationSession,
                         event_type: str, scope: str, version: int, *,
                         turn_id: Optional[str] = None,
                         data: Optional[dict] = None) -> None:
        data = dict(data or {})
        if turn_id is None:
            turn_id = data.pop("turn_id", None)
        payload = self._event(conv, scope, version, turn_id=turn_id, **data)
        outbox = self.store.append_outbox(conn, conv.conversation_id, event_type,
                                          scope, version, payload)
        _pending.append((event_type, payload, outbox.outbox_id))

    # ------------------------------------------------------------
    # Conversation
    # ------------------------------------------------------------

    def get_or_create_conversation(
        self,
        session_key: str,
        *,
        origin: str,
        subtype: str,
        workspace_id: Optional[str] = None,
        execution_scope: Optional[str] = None,
        route_metadata: Optional[Dict[str, Any]] = None,
    ) -> ConversationSession:
        scope = execution_scope or default_execution_scope(origin, subtype, workspace_id)
        conversation, created = self.store.create_conversation(
            session_key, origin=origin, subtype=subtype,
            execution_scope=scope, workspace_id=workspace_id,
            route_metadata=route_metadata)
        if created:
            _pending: list = []
            with self.store.transaction() as conn:
                self._append_and_emit(
                    _pending, conn, conversation, "conversation.upserted",
                    SCOPE_SESSION, conversation.session_version)
            for event_type, payload, outbox_id in _pending:
                self._emit(event_type, payload, outbox_id)
        return conversation

    def update_prefs(self, conversation_id: str, **prefs) -> ConversationSession:
        """更新会话偏好（模型/推理等级/权限档位等，设计方案：管理操作统一化）。

        合并写入 route_metadata["prefs"] 并广播 conversation.upserted，
        runner 创建 Agent 时读取应用（替代旧 sessions_map 持久化）。"""
        conv = self._conv(conversation_id)
        _pending: list = []
        with self.store.transaction() as conn:
            existing = dict(conv.route_metadata or {})
            merged_prefs = {**dict(existing.get("prefs") or {}), **prefs}
            updated = self.store.update_conversation_metadata(
                conn, conversation_id, {**existing, "prefs": merged_prefs})
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, updated, "conversation.upserted", SCOPE_SESSION, version,
                data={"prefs": merged_prefs})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return updated

    def conversation_prefs(self, conversation_id: str) -> dict:
        """读取会话偏好（模型/推理/权限）。"""
        conv = self._conv(conversation_id)
        return dict((conv.route_metadata or {}).get("prefs") or {})

    # ------------------------------------------------------------
    # 队列
    # ------------------------------------------------------------

    def enqueue(
        self,
        conversation_id: str,
        text: str,
        *,
        operation_id: Optional[str] = None,
        channel: Optional[str] = None,
        message_id: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_name: Optional[str] = None,
        create_queued_node: bool = False,
        images: Optional[List[Dict[str, Any]]] = None,
    ) -> QueueItem:
        """入队（设计方案 8.1/8.2；渠道 11.3 可创建 queued User Node）。

        images: 可选图片信封 [{data: base64, media_type: str}]，v16 起随队列
        项持久化，出队执行时消费（转存 artifacts + 建 image 引用节点）。
        """
        if images is not None:
            images = _validate_image_envelope(images)
        conv = self._conv(conversation_id)
        _pending: list = []
        with self.store.transaction() as conn:
            if operation_id:
                hit, result = self.store.check_idempotency(
                    conn, operation_id, conversation_id,
                    _request_hash({"op": "enqueue", "text": text}))
                if hit:
                    return self.store.get_queue_item(result["queue_item_id"])
            item = self.store.enqueue_item(conn, conversation_id, text,
                                           operation_id=operation_id,
                                           images=images)
            node_id = None
            if create_queued_node:
                node = self.store.create_node(
                    conn, conversation_id=conversation_id, type=TurnNodeType.USER.value,
                    status="queued", text=text, source_channel=channel,
                    source_message_id=message_id, sender_id=sender_id,
                    sender_name=sender_name)
                node_id = node.node_id
            if operation_id:
                self.store.record_idempotency(
                    conn, operation_id, conversation_id,
                    _request_hash({"op": "enqueue", "text": text}),
                    result={"queue_item_id": item.queue_item_id})
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, version,
                data={"queue_item_id": item.queue_item_id,
                      "status": item.status, "position": item.position,
                      "text_len": len(text), "node_id": node_id,
                      "queue": _queue_summary(self.store.list_active_queue(conversation_id, conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return item

    def send_next(self, conversation_id: str, *,
                  runtime_snapshot_id: Optional[str] = None,
                  channel_node_id: Optional[str] = None,
                  operation_id: Optional[str] = None,
                  parent_conversation_id: Optional[str] = None,
                  parent_turn_id: Optional[str] = None) -> tuple[Turn, TurnNode] | None:
        """出队事务（设计方案 8.5）：校验队首/无活动 Turn/执行域并发 →
        创建 Turn → 创建或关联 User Node → 归档 sent → Outbox。

        无活动队列项或已有活动 Turn 时返回 None（幂等推进）。
        """
        conv = self._conv(conversation_id)
        active = self.store.get_active_turn(conversation_id)
        if active is not None:
            return None
        _pending: list = []
        with self.store.transaction() as conn:
            queue = self.store.list_active_queue(conversation_id)
            # 自愈：steering 等待是进程内存态——无活动 Turn 时仍处于
            # waiting_for_steering 的项必为陈旧态（此前收口顺序缺陷的遗留/
            # 进程重启丢失），按普通等待项分派，不再永久卡队。
            head = next((it for it in queue
                         if it.status in (QueueItemStatus.WAITING.value,
                                          QueueItemStatus.WAITING_FOR_STEERING.value)), None)
            if head is None:
                return None
            if operation_id:
                hit, result = self.store.check_idempotency(
                    conn, operation_id, conversation_id,
                    _request_hash({"op": "send_next", "head": head.queue_item_id}))
                if hit:
                    return (self.store.get_turn(result["turn_id"]),
                            self.store.get_node(result["node_id"]))
            # 执行域并发校验（设计方案 13；与 Turn 创建同一事务）
            scope_count = self.store.count_active_turns_in_scope(conv.execution_scope)
            if scope_count >= self.max_turns_per_scope:
                raise ExecutionScopeLimit(
                    f"执行域 {conv.execution_scope} 并发已满"
                    f"（{self.max_turns_per_scope} 个非终态 Turn）")
            # 进程级全局并发校验（设计方案 13/30.3：503 gateway_concurrency_saturated）
            global_count = self.store.count_active_turns()
            if global_count >= self.max_global_turns:
                raise GatewaySaturated(
                    f"全局并发已满（{self.max_global_turns} 个非终态 Turn），"
                    f"请等待运行中的任务完成")
            # 条件创建：事务内原子判定"已有活动 Turn"（修复并发双活动 Turn →
            # 队列死锁，TOCTOU）；冲突时幂等返回 None，不重复出队
            turn = self.store.create_turn_if_no_active(
                conn, conversation_id, runtime_snapshot_id=runtime_snapshot_id,
                parent_conversation_id=parent_conversation_id,
                parent_turn_id=parent_turn_id)
            if turn is None:
                return None
            if channel_node_id:
                try:
                    existing_node = self.store.get_node(channel_node_id)
                except ResourceNotFound:
                    existing_node = None
                if existing_node is not None and existing_node.turn_id is None:
                    # 原位升级（设计方案 11.3）：node_id 不变
                    node = self.store.assign_node_to_turn(
                        conn, channel_node_id, turn.turn_id, 1)
                else:
                    node = self.store.create_node(
                        conn, conversation_id=conversation_id,
                        turn_id=turn.turn_id, type=TurnNodeType.USER.value,
                        status="dispatched", text=head.text)
            else:
                node = self.store.create_node(
                    conn, conversation_id=conversation_id,
                    turn_id=turn.turn_id, type=TurnNodeType.USER.value,
                    status="dispatched", text=head.text,
                    source_channel=conv.route_metadata.get("channel"),
                    source_message_id=head.operation_id)
            # 图片信封消费（修正版方案 A）：base64 落盘 artifacts + 建 image
            # 引用节点（紧随 User 节点），引用写回 user 节点 metadata.images
            # 供 runner 重建 InboundMessage.images / 前端缩略图 / 回放占位。
            # image 节点广播 node.image 事件：保证**不刷新页面**也能实时出现
            # 缩略图（此前只建节点不发事件，前端直到下次历史拉取才可见）。
            if getattr(head, "images", None) and self.image_store is not None:
                refs: List[Dict[str, Any]] = []
                for img in head.images:
                    try:
                        saved = self.image_store.save(
                            conversation_id, str(img.get("data") or ""),
                            str(img.get("media_type") or "image/png"))
                    except Exception as exc:
                        logger.warning("图片落盘失败（跳过该张）: %s", exc)
                        continue
                    img_node = self.store.create_node(
                        conn, conversation_id=conversation_id,
                        turn_id=turn.turn_id, type=TurnNodeType.IMAGE.value,
                        status="done", text="",
                        metadata={"ref": saved["ref"],
                                  "media_type": saved["media_type"],
                                  "size": saved["size"],
                                  "call_id": f"img-{saved['ref'][:16]}"})
                    refs.append({"ref": saved["ref"],
                                 "media_type": saved["media_type"],
                                 "node_id": img_node.node_id,
                                 "size": saved["size"]})
                    version = self.store.bump_turn_version(conn, turn.turn_id)
                    self._append_and_emit(
                        _pending, conn, conv, "node.image", SCOPE_TURN,
                        version, turn_id=turn.turn_id,
                        data={"node_id": img_node.node_id,
                              "type": TurnNodeType.IMAGE.value,
                              "status": "done",
                              "position": img_node.position,
                              "ref": saved["ref"],
                              "media_type": saved["media_type"],
                              "size": saved["size"]})
                if refs:
                    node = self.store.update_node(
                        conn, node.node_id,
                        metadata={**(dict(node.metadata or {})),
                                  "images": refs})
            self.store.mark_queue_sent(conn, head.queue_item_id,
                                       turn.turn_id, node.node_id)
            if operation_id:
                self.store.record_idempotency(
                    conn, operation_id, conversation_id,
                    _request_hash({"op": "send_next", "head": head.queue_item_id}),
                    result={"turn_id": turn.turn_id, "node_id": node.node_id})
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "turn.status", SCOPE_TURN, turn.turn_version,
                turn_id=turn.turn_id, data={"status": turn.status})
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, version,
                data={"queue_item_id": head.queue_item_id, "status": "sent",
                      "turn_id": turn.turn_id,
                      "queue": _queue_summary(self.store.list_active_queue(conversation_id, conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return turn, node

    def list_queue(self, conversation_id: str) -> list[QueueItem]:
        return self.store.list_active_queue(conversation_id)

    def edit_queue_item(self, conversation_id: str, queue_item_id: str, *,
                        expected_revision: int, text: Optional[str] = None,
                        operation_id: Optional[str] = None) -> QueueItem:
        conv = self._conv(conversation_id)
        self._assert_queue_item_owned(conversation_id, queue_item_id)
        _pending: list = []
        with self.store.transaction() as conn:
            if operation_id:
                hit, result = self.store.check_idempotency(
                    conn, operation_id, conversation_id,
                    _request_hash({"op": "edit_queue", "qid": queue_item_id,
                                   "text": text, "rev": expected_revision}))
                if hit:
                    return self.store.get_queue_item(result["queue_item_id"])
            item = self.store.update_queue_item(
                conn, queue_item_id, expected_revision=expected_revision, text=text)
            if operation_id:
                self.store.record_idempotency(
                    conn, operation_id, conversation_id,
                    _request_hash({"op": "edit_queue", "qid": queue_item_id,
                                   "text": text, "rev": expected_revision}),
                    result={"queue_item_id": item.queue_item_id})
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, version,
                data={"queue_item_id": item.queue_item_id, "status": item.status,
                      "revision": item.revision,
                      "queue": _queue_summary(self.store.list_active_queue(conversation_id, conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return item

    def delete_queue_item(self, conversation_id: str, queue_item_id: str, *,
                          expected_revision: int) -> QueueItem:
        """删除进入 pending_delete（设计方案 8.6，提供 5 秒撤销）。"""
        conv = self._conv(conversation_id)
        self._assert_queue_item_owned(conversation_id, queue_item_id)
        _pending: list = []
        with self.store.transaction() as conn:
            item = self.store.update_queue_item(
                conn, queue_item_id, expected_revision=expected_revision,
                status=QueueItemStatus.PENDING_DELETE.value)
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, version,
                data={"queue_item_id": item.queue_item_id,
                      "status": item.status,
                      "queue": _queue_summary(self.store.list_active_queue(conversation_id, conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return item

    def undo_delete(self, conversation_id: str, queue_item_id: str) -> QueueItem:
        conv = self._conv(conversation_id)
        self._assert_queue_item_owned(conversation_id, queue_item_id)
        _pending: list = []
        with self.store.transaction() as conn:
            item = self.store.undo_delete(conn, queue_item_id)
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, version,
                data={"queue_item_id": item.queue_item_id,
                      "status": item.status,
                      "queue": _queue_summary(self.store.list_active_queue(conversation_id, conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return item

    def move_queue_item(self, conversation_id: str, queue_item_id: str,
                        direction: str) -> list[QueueItem]:
        conv = self._conv(conversation_id)
        _pending: list = []
        with self.store.transaction() as conn:
            items = self.store.move_queue_item(conn, conversation_id,
                                               queue_item_id, direction)
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, version,
                data={"queue_item_id": queue_item_id, "direction": direction,
                      "queue": _queue_summary(items)})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return items

    def clear_waiting(self, conversation_id: str) -> int:
        """清空普通等待项（设计方案 8.6；二次确认由 API 层负责）。"""
        conv = self._conv(conversation_id)
        _pending: list = []
        with self.store.transaction() as conn:
            count = self.store.clear_waiting_queue(conn, conversation_id)
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, version,
                data={"cleared": count,
                      "queue": _queue_summary(self.store.list_active_queue(conversation_id, conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return count

    def clear_history(self, conversation_id: str) -> dict:
        """清空会话全部历史（/clear 与"清空聊天"统一入口，设计方案 §30）。

        单事务：删除 turns/nodes/queue/approvals/tool_results，
        保留 conversation 行；版本递增并广播 conversation.cleared（实时
        publish 成功即 mark published，出故障由 Outbox 补发）。"""
        conv = self._conv(conversation_id)
        _pending: list = []
        with self.store.transaction() as conn:
            counts = self.store.clear_history(conn, conversation_id)
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "conversation.cleared", SCOPE_SESSION, version,
                data={"counts": counts})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return counts

    def delete_conversation_by_key(self, session_key: str) -> bool:
        """真正删除一个会话：删除 conversation_sessions 行及其全部关联数据。

        用于主会话"删除会话"（不再只清空历史——否则会话仍留在 /api/sessions 列表里）。
        返回是否删除了会话行。"""
        if not session_key:
            return False
        conv = self.store.get_conversation_by_key(session_key)
        if conv is None:
            return False
        with self.store.transaction() as conn:
            deleted = self.store.delete_conversation(conn, conv.conversation_id)
        if deleted:
            self._notify_conversation_deleted(session_key)
            # 图片级联（修正版方案 A 收尾）：conversation 行已删，其图片目录
            # 失去归属——按会话 id 清理 images/<cid>/，避免文件残留。
            if self.image_store is not None:
                try:
                    removed_files = self.image_store.delete_conversation(
                        conv.conversation_id)
                    if removed_files:
                        logger.info("会话删除级联清理图片 %d 个: %s",
                                    removed_files, conv.conversation_id)
                except Exception:
                    logger.warning("会话图片级联清理失败: %s",
                                   conv.conversation_id, exc_info=True)
        return deleted

    # ------------------------------------------------------------
    # Steering（设计方案 9：两阶段 prepare → commit / abort）
    # ------------------------------------------------------------

    def list_conversation_keys_with_prefix(self, prefix: str) -> List[str]:
        """按前缀列出 conversation session_key（不含关联数据）。

        用于删除工作区会话时级联找出其派生的 subagent 子会话
        （键形如 ``subagent:workspace:{wid}:{sid}:{child_id}``）。
        参数化 LIKE + ESCAPE：prefix 中的 ``%`` / ``_`` / ``\\`` 视为字面量，
        避免通配符注入或误匹配。"""
        if not prefix:
            return []
        escaped = (prefix.replace("\\", "\\\\")
                         .replace("%", "\\%")
                         .replace("_", "\\_"))
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT session_key FROM conversation_sessions "
                "WHERE session_key LIKE ? ESCAPE '\\'",
                (escaped + "%",),
            ).fetchall()
        return [row["session_key"] for row in rows]

    def prepare_steering(self, conversation_id: str, queue_item_ids: list[str]) -> tuple[Turn, list[QueueItem]]:
        """第一阶段：校验 Turn 活动/次数上限，将选中项置为
        waiting_for_steering 并等待已运行工具自然结束（设计方案 9.1/9.4）。
        （控制租约已随设计方案 10 废弃：并发防护由 exec_lock / 执行域上限承担。）"""
        conv = self._conv(conversation_id)
        active = self.store.get_active_turn(conversation_id)
        if active is None:
            raise QueueConflict("当前没有活动 Turn，无法 Steering")
        if not queue_item_ids:
            raise ValidationFailed("未选择待插入的队列项")
        _pending: list = []
        with self.store.transaction() as conn:
            count = self.store.count_steering_nodes(conn, active.turn_id)
            if count >= MAX_STEERING_PER_TURN:
                raise SteeringLimit(
                    f"每个 Turn 最多 {MAX_STEERING_PER_TURN} 次 Steering")
            items = []
            for qid in queue_item_ids:
                item = self.store.get_queue_item(qid)
                if item.conversation_id != conversation_id:
                    raise ResourceNotFound(f"队列项不存在: {qid}")
                if item.status != QueueItemStatus.WAITING.value:
                    raise QueueConflict(f"队列项 {qid} 状态为 {item.status}，不能参与 Steering")
                updated = self.store.set_queue_status(
                    conn, qid, QueueItemStatus.WAITING_FOR_STEERING.value)
                items.append(updated)
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, version,
                data={"steering": [it.queue_item_id for it in items],
                      "queue": _queue_summary(self.store.list_active_queue(conversation_id, conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return active, items

    def commit_steering(self, conversation_id: str, queue_item_ids: list[str]) -> list[TurnNode]:
        """第二阶段：已运行工具结束（运行时回调）→ 注入 user_steering 节点
        → 项归档 injected（设计方案 9.1/9.5）。"""
        conv = self._conv(conversation_id)
        active = self.store.get_active_turn(conversation_id)
        if active is None:
            # Turn 已结束：待插入项恢复为普通队列项
            with self.store.transaction() as conn:
                for qid in queue_item_ids:
                    try:
                        self.store.set_queue_status(conn, qid, QueueItemStatus.WAITING.value)
                    except ResourceNotFound:
                        continue
            return []
        nodes = []
        _pending: list = []
        with self.store.transaction() as conn:
            for qid in queue_item_ids:
                item = self.store.get_queue_item(qid)
                if item.conversation_id != conversation_id:
                    continue  # 归属校验：非本会话队列项不可注入
                if item.status != QueueItemStatus.WAITING_FOR_STEERING.value:
                    continue
                node = self.store.create_node(
                    conn, conversation_id=conversation_id, turn_id=active.turn_id,
                    type=TurnNodeType.USER_STEERING.value, status="injected",
                    text=item.text, metadata={"role": "user", "source": "steering"})
                self.store.mark_queue_injected(conn, qid, node.node_id)
                nodes.append(node)
            if not nodes:
                return []
            version = self.store.bump_turn_version(conn, active.turn_id)
            self._append_and_emit(
                _pending, conn, conv, "node.user_steering", SCOPE_TURN, version,
                turn_id=active.turn_id,
                data={"nodes": [n.node_id for n in nodes],
                      "texts": [n.text for n in nodes]})
            sversion = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, sversion,
                data={"injected": [n.node_id for n in nodes],
                      "queue": _queue_summary(self.store.list_active_queue(conversation_id, conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return nodes

    def abort_steering(self, conversation_id: str, queue_item_ids: list[str]) -> None:
        """Steering 中断超时（设计方案 9.3）：待插入消息恢复为普通队列项，
        迟到事件仅进入诊断，不再更新可见内容。"""
        conv = self._conv(conversation_id)
        _pending: list = []
        with self.store.transaction() as conn:
            for qid in queue_item_ids:
                try:
                    item = self.store.get_queue_item(qid)
                    if item.conversation_id != conversation_id:
                        continue  # 归属校验：非本会话队列项不恢复
                    if item.status == QueueItemStatus.WAITING_FOR_STEERING.value:
                        self.store.set_queue_status(conn, qid, QueueItemStatus.WAITING.value)
                except ResourceNotFound:
                    continue
            version = self.store.bump_session_version(conn, conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "queue.updated", SCOPE_SESSION, version,
                data={"aborted_steering": list(queue_item_ids),
                      "queue": _queue_summary(self.store.list_active_queue(conversation_id, conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)

    # ------------------------------------------------------------
    # Turn / Node（运行时事件落库）
    # ------------------------------------------------------------

    def start_turn(self, conversation_id: str, *,
                   runtime_snapshot_id: Optional[str] = None,
                   parent_conversation_id: Optional[str] = None,
                   parent_turn_id: Optional[str] = None) -> Turn:
        """直接创建活动 Turn（无队列路径，如 Plan/Goal 系统 Turn 或渠道空闲直发）。"""
        conv = self._conv(conversation_id)
        active = self.store.get_active_turn(conversation_id)
        if active is not None:
            return active
        _pending: list = []
        with self.store.transaction() as conn:
            if self.store.count_active_turns_in_scope(conv.execution_scope) >= self.max_turns_per_scope:
                raise ExecutionScopeLimit(
                    f"执行域 {conv.execution_scope} 并发已满")
            # 条件创建：事务内原子判定"已有活动 Turn"（修复并发双活动 Turn → 队列死锁）
            turn = self.store.create_turn_if_no_active(
                conn, conversation_id, runtime_snapshot_id=runtime_snapshot_id,
                parent_conversation_id=parent_conversation_id,
                parent_turn_id=parent_turn_id)
            if turn is None:
                # 并发已建活动 Turn：复用（幂等推进）
                return self.store.get_active_turn(conversation_id)
            self._append_and_emit(
                _pending, conn, conv, "turn.status", SCOPE_TURN, turn.turn_version,
                turn_id=turn.turn_id, data={"status": turn.status})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return turn

    def set_turn_status(self, conversation_id: str, turn_id: str, status: str, *,
                        error_code: Optional[str] = None) -> Turn:
        """瞬态状态流转：queued → thinking/tool/answering/approval/steering/stopping。"""
        conv = self._conv(conversation_id)
        self._assert_turn_owned(conversation_id, turn_id)
        _pending: list = []
        with self.store.transaction() as conn:
            turn = self.store.update_turn_status(conn, turn_id, status,
                                                 error_code=error_code)
            self._append_and_emit(
                _pending, conn, conv, "turn.status", SCOPE_TURN, turn.turn_version,
                turn_id=turn_id, data={"status": turn.status,
                                       "error_code": turn.error_code})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return turn

    def upsert_node_delta(self, conversation_id: str, turn_id: str, node_type: str,
                          text: str, *, continue_existing: bool = True,
                          metadata: Optional[Dict[str, Any]] = None,
                          source_channel: Optional[str] = None,
                          source_message_id: Optional[str] = None,
                          sender_id: Optional[str] = None,
                          sender_name: Optional[str] = None) -> TurnNode:
        """节点 delta 落库（设计方案 5.3/16.4）。连续同类型 delta 合并到当前
        节点；continue_existing=False 表示被 tool/assistant 打断后新建节点。

        B1：追加改由 store.append_node_text 在 SQL 内完成（COALESCE 拼接 +
        text_seq 递增），本方法不再读全文拼接；node.delta 事件按契约①只携带
        delta + seq（seq 为节点内单调递增整数），终态事件仍携带权威全量 text。"""
        conv = self._conv(conversation_id)
        self._assert_turn_owned(conversation_id, turn_id)
        _pending: list = []
        with self.store.transaction() as conn:
            node = None
            seq = 1
            if continue_existing:
                node_id = self.store.find_last_node_id(conn, turn_id, node_type)
                if node_id is not None:
                    node = self.store.append_node_text(conn, node_id, text)
                    seq = int(getattr(node, "text_seq", 1))
            if node is None:
                node = self.store.create_node(
                    conn, conversation_id=conversation_id, turn_id=turn_id,
                    type=node_type, status="streaming", text=text,
                    metadata=metadata, source_channel=source_channel,
                    source_message_id=source_message_id,
                    sender_id=sender_id, sender_name=sender_name)
            version = self.store.bump_turn_version(conn, turn_id)
            self._append_and_emit(
                _pending, conn, conv, "node.delta", SCOPE_TURN, version, turn_id=turn_id,
                data={"node_id": node.node_id, "type": node_type,
                      "delta": text, "seq": seq, "text_len": len(text or ""),
                      "status": node.status, "position": node.position})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return node

    def upsert_node_deltas(
            self, items: List[tuple[str, str, str, str]]) -> int:
        """批量节点 delta 落库（C-3：同窗多 key 合并进单个事务）。

        一次 commit 完成 N 个 (conversation_id, turn_id, node_type, text)
        的节点追加/新建 + 各自 turn_version 递增 + N 条 outbox 写入，取代
        逐 key 各开事务（100ms 合批窗口内多会话并发时写放大显著下降）。
        任一 key 的 SQL 失败 → 整个事务回滚并抛出，调用方（_DeltaMerger）
        保留缓冲下轮重试；Turn 已不存在/归属不匹配的陈旧 key 视为正常竞态
        静默跳过（不阻断同批其余 key）。返回实际写入的 key 数。"""
        _pending: list = []
        convs: Dict[str, Any] = {}
        written = 0
        with self.store.transaction() as conn:
            for conversation_id, turn_id, node_type, text in items:
                try:
                    turn = self.store.get_turn(turn_id)
                except TurnNotFound:
                    logger.debug("delta 批量落库跳过（Turn 已不存在）: %s", turn_id)
                    continue
                if turn.conversation_id != conversation_id:
                    logger.debug("delta 批量落库跳过（归属不符）: %s", turn_id)
                    continue
                conv = convs.get(conversation_id)
                if conv is None:
                    conv = self._conv(conversation_id)
                    convs[conversation_id] = conv
                node = None
                seq = 1
                node_id = self.store.find_last_node_id(conn, turn_id, node_type)
                if node_id is not None:
                    node = self.store.append_node_text(conn, node_id, text)
                    seq = int(getattr(node, "text_seq", 1))
                if node is None:
                    node = self.store.create_node(
                        conn, conversation_id=conversation_id, turn_id=turn_id,
                        type=node_type, status="streaming", text=text)
                version = self.store.bump_turn_version(conn, turn_id)
                self._append_and_emit(
                    _pending, conn, conv, "node.delta", SCOPE_TURN, version,
                    turn_id=turn_id,
                    data={"node_id": node.node_id, "type": node_type,
                          "delta": text, "seq": seq, "text_len": len(text or ""),
                          "status": node.status, "position": node.position})
                written += 1
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return written

    def publish_version_gap(self, conversation_id: str, *,
                            reason: str = DELTA_BUFFER_OVERFLOW) -> None:
        """广播 version_gap（C-4 快照自愈兜底）。

        合批缓冲超限丢弃增量后调用：前端 version_gap 特判跳过版本门控 → 置
        缺口 → 拉取 Snapshot 修复，因此本事件不写 outbox、不占版本号（version=0）。"""
        try:
            conv = self._conv(conversation_id)
        except Exception:
            return
        payload = self._event(conv, SCOPE_SESSION, 0,
                              reason=reason,
                              error_code=DELTA_BUFFER_OVERFLOW)
        # 与 EventBus 队列溢出版 version_gap（_gap_event）数据格式对齐：
        # data.version=0 表示"不参与版本门控"
        payload["data"]["version"] = 0
        self._emit("version_gap", payload, None)

    def finalize_node(self, conversation_id: str, turn_id: str,
                      node_type: str, *,
                      mark_intermediate: bool = False) -> Optional[TurnNode]:
        """把 Turn 内最后一个指定类型节点从 streaming 置为 done 并广播
        （设计方案 5.3：被 tool/assistant 打断的节点收敛为终态，避免多个
        "正在生成…"并存）。无 streaming 节点时返回 None。

        mark_intermediate：被打断的 assistant 节点（后面跟工具/新一轮回复）
        在 metadata 写入 intermediate=True——后端权威标记"这是过程性输出"，
        前端按标记渲染条卡，不再靠节点顺序推断（方案 B）。"""
        conv = self._conv(conversation_id)
        self._assert_turn_owned(conversation_id, turn_id)
        _pending: list = []
        with self.store.transaction() as conn:
            node = self.store.get_last_turn_node(conn, turn_id, node_type)
            if node is None or node.status != "streaming":
                return None
            if mark_intermediate:
                node = self.store.update_node(
                    conn, node.node_id, status="done",
                    metadata={**(node.metadata or {}), "intermediate": True})
            else:
                node = self.store.set_node_status(conn, node.node_id, "done")
            version = self.store.bump_turn_version(conn, turn_id)
            self._append_and_emit(
                _pending, conn, conv, "node.delta", SCOPE_TURN, version, turn_id=turn_id,
                data={"node_id": node.node_id, "type": node_type,
                      "text": node.text or "", "status": "done",
                      "seq": int(getattr(node, "text_seq", 0)),
                      # 扁平下发分类标记（前端 pick 契约）：中间输出条卡依据
                      "intermediate": bool((node.metadata or {}).get("intermediate")),
                      })
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return node

    def upsert_tool_node(self, conversation_id: str, turn_id: str, call_id: str, *,
                         status: str, params_summary: Optional[str] = None,
                         result_summary: Optional[str] = None,
                         error: Optional[str] = None,
                         error_code: Optional[str] = None,
                         error_message: Optional[str] = None,
                         result_ref: Optional[str] = None,
                         result_size_bytes: int = 0,
                         tool_name: Optional[str] = None,
                         extra_metadata: Optional[Dict[str, Any]] = None) -> TurnNode:
        """每个工具按 call_id 形成独立节点（设计方案 5.3），节点 ID 由 call_id
        派生保证幂等。多次事件（start/end/result）**合并** metadata，绝不整体
        覆盖——否则后到的事件会把前一次写入的 params_summary/result_summary
        抹掉（工具卡片展开后输入/返回为空的老问题）。"""
        conv = self._conv(conversation_id)
        self._assert_turn_owned(conversation_id, turn_id)
        # 节点 ID 派生加入 conversation_id：不同会话相同 call_id 不串扰
        node_id = gen_node_id_from_call(call_id, conversation_id)
        _pending: list = []
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM turn_nodes WHERE node_id=?", (node_id,),
            ).fetchone()
            if row is not None:
                # 合并 metadata：旧值基础上只更新本次传入的字段
                metadata: Dict[str, Any] = dict(
                    _loads_json(row["metadata"]) or {})
                metadata.setdefault("call_id", call_id)
                if tool_name is not None:
                    metadata["tool"] = tool_name
                if params_summary is not None:
                    metadata["params_summary"] = params_summary
                if result_summary is not None:
                    metadata["result_summary"] = result_summary
                if error is not None:
                    metadata["error"] = error
                # X2-P3⑫：error_code 与 message 并存（机器码 + 人读文案）
                if error_code is not None:
                    metadata["error_code"] = error_code
                if error_message is not None:
                    metadata["error_message"] = error_message
                if result_ref is not None:
                    metadata["result_ref"] = result_ref
                if result_size_bytes:
                    metadata["result_size_bytes"] = result_size_bytes
                if extra_metadata:
                    metadata.update(extra_metadata)
                node = self.store.update_node(conn, node_id, status=status,
                                              metadata=metadata)
            else:
                metadata = {"call_id": call_id}
                if tool_name is not None:
                    metadata["tool"] = tool_name
                if params_summary is not None:
                    metadata["params_summary"] = params_summary
                if result_summary is not None:
                    metadata["result_summary"] = result_summary
                if error is not None:
                    metadata["error"] = error
                if error_code is not None:
                    metadata["error_code"] = error_code
                if error_message is not None:
                    metadata["error_message"] = error_message
                if result_ref is not None:
                    metadata["result_ref"] = result_ref
                if result_size_bytes:
                    metadata["result_size_bytes"] = result_size_bytes
                if extra_metadata:
                    metadata.update(extra_metadata)
                node = self.store.create_node(
                    conn, conversation_id=conversation_id, turn_id=turn_id,
                    type=TurnNodeType.TOOL.value, status=status,
                    text=params_summary or "", metadata=metadata, node_id=node_id)
            version = self.store.bump_turn_version(conn, turn_id)
            self._append_and_emit(
                _pending, conn, conv, "node.tool", SCOPE_TURN, version, turn_id=turn_id,
                data={"node_id": node.node_id, "call_id": call_id,
                      "status": status, "result_ref": result_ref,
                      "tool": metadata.get("tool") or "",
                      "params_summary": metadata.get("params_summary") or "",
                      "result_summary": metadata.get("result_summary") or "",
                      "error": metadata.get("error") or None,
                      "error_code": metadata.get("error_code") or None,
                      "position": node.position})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return node

    def complete_turn(self, conversation_id: str, turn_id: str, status: str, *,
                      full_text: Optional[str] = None,
                      error_code: Optional[str] = None) -> Turn:
        """Turn 终态（chat.done 权威，设计方案 7.2/7.5）。full_text 覆盖流式文本。"""
        conv = self._conv(conversation_id)
        _pending: list = []
        with self.store.transaction() as conn:
            turn = self.store.get_turn(turn_id)
            if turn.conversation_id != conversation_id:
                raise TurnNotFound(f"Turn 不存在: {turn_id}")
            if turn.status in ("done", "stopped", "error", "interrupted"):
                return turn  # 幂等：终态不重复处理
            final_node_id = None
            if full_text is not None and status == TurnStatus.DONE.value:
                node = self.store.get_last_turn_node(conn, turn_id,
                                                     TurnNodeType.ASSISTANT.value)
                if node is None:
                    node = self.store.create_node(
                        conn, conversation_id=conversation_id, turn_id=turn_id,
                        type=TurnNodeType.ASSISTANT.value, status="done",
                        text=full_text,
                        metadata={"final": True})
                else:
                    existing_meta = dict(node.metadata or {})
                    if ("intermediate" in existing_meta or "final" in existing_meta
                            or existing_meta.get("runtime_final")):
                        # 已分类节点（runtime 步回复 append_runtime_node 已打
                        # intermediate/final）：保留原分类，只同步权威全文——
                        # 否则会把 plan/goal 中间步的 intermediate 洗成 final
                        node = self.store.update_node(
                            conn, node.node_id, text=full_text, status="done")
                    else:
                        # 方案 B：后端权威标记最终答复（metadata.final=True），
                        # 前端按标记渲染正式气泡，不再靠节点顺序推断。
                        node = self.store.update_node(
                            conn, node.node_id, text=full_text, status="done",
                            metadata={**existing_meta,
                                      "final": True, "intermediate": False})
                final_node_id = node.node_id
            turn = self.store.update_turn_status(
                conn, turn_id, status, error_code=error_code,
                finished_at=utc_now(), final_assistant_node_id=final_node_id)
            # 残留流式/排队节点统一置 done，避免历史中出现"正在生成…"占位
            self.store.finalize_turn_nodes(conn, turn_id)
            # 工具节点终态兜底收口：仍 running 的工具卡（end 事件因停止/
            # 超时/zombie 事件隔离永久缺失）翻转为 error 并广播 node.tool，
            # 避免历史里永远"执行中"。done 终态同样适用（未返回即未返回）。
            _sweep_note = "工具调用未返回（轮次已停止/超时/中断），已自动收口"
            swept = self.store.sweep_running_tool_nodes(
                conn, turn_id, note=_sweep_note, error_code="tool_no_return")
            for item in swept:
                self._append_and_emit(
                    _pending, conn, conv, "node.tool", SCOPE_TURN,
                    turn.turn_version, turn_id=turn_id,
                    data={"node_id": item["node_id"], "call_id": item["call_id"],
                          "status": "error", "result_ref": None,
                          "tool": item["tool"],
                          "params_summary": item["params_summary"],
                          "result_summary": item["result_summary"],
                          "error": _sweep_note,
                          "error_code": "tool_no_return",
                          "position": item["position"]})
            # X2-P3⑫：中止/失败类终态（stopped/error/interrupted）时工具已
            # 不可能继续执行 → 未决审批置 cancelled 并广播 approval.resolved
            # （同一事务：终态标记 + 审批取消 + outbox 原子）。done 终态不在此列
            # （审批早已通过/拒绝，天然无 pending）。
            if status in (TurnStatus.STOPPED.value, TurnStatus.ERROR.value,
                          TurnStatus.INTERRUPTED.value):
                rows = conn.execute(
                    "SELECT approval_id, tool_name FROM approvals "
                    "WHERE conversation_id=? AND turn_id=? AND status='pending'",
                    (conversation_id, turn_id),
                ).fetchall()
                if rows:
                    conn.execute(
                        "UPDATE approvals SET status='cancelled', resolved_at=?, "
                        "resolved_by='system' "
                        "WHERE conversation_id=? AND turn_id=? AND status='pending'",
                        (utc_now(), conversation_id, turn_id),
                    )
                    for row in rows:
                        self._append_and_emit(
                            _pending, conn, conv, "approval.resolved", SCOPE_TURN,
                            turn.turn_version, turn_id=turn_id,
                            data={"approval_id": row["approval_id"],
                                  "status": ApprovalStatus.CANCELLED.value,
                                  "tool_name": row["tool_name"] or "",
                                  "error_code": APPROVAL_CANCELLED,
                                  "reason": "turn_terminal"})
            self._append_and_emit(
                _pending, conn, conv, "turn.status", SCOPE_TURN, turn.turn_version,
                turn_id=turn_id, data={"status": turn.status,
                                       "error_code": turn.error_code})
            _full_text = full_text if full_text is not None else ""
            # X2-P3⑫：chat.done 补 error_code（统一注册表）与 full_text_len（字符数），
            # 前端可据此展示错误类别/统计回复长度，无需再数文本。
            self._append_and_emit(
                _pending, conn, conv, "chat.done", SCOPE_TURN, turn.turn_version,
                turn_id=turn_id,
                data={"full_text": _full_text,
                      "full_text_len": len(_full_text),
                      "final_assistant_node_id": final_node_id,
                      "status": turn.status,
                      "error_code": turn.error_code})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return turn

    def append_runtime_node(self, conversation_id: str, turn_id: str,
                            runtime_type: str, runtime_id: str, text: str,
                            status: str = "done", *,
                            final: bool = False) -> TurnNode:
        """把 Plan/Goal 轮次的最终回复写成父会话的 assistant 节点。

        metadata 带 runtime 标记（``runtime_type`` / ``runtime_id`` /
        ``runtime_status``），前端据此把该节点内联为卡片而非平铺消息
        （对齐 dsh：goal/plan 在主会话同一 Agent/session 内累积）。
        `final=True`（plan 最终 step / goal 每轮终答，对应任务元数据
        `final_response`）时追加 `runtime_final` 标记：前端把该节点平铺为
        正式渲染消息（中间进度保持折叠卡片，最终答复完整持久可见）。
        """
        conv = self._conv(conversation_id)
        self._assert_turn_owned(conversation_id, turn_id)
        _pending: list = []
        metadata: Dict[str, Any] = {
            "runtime_type": runtime_type,
            "runtime_id": runtime_id,
            "runtime_status": status,
        }
        if final:
            metadata["runtime_final"] = True
            # 方案 B 统一标记：与普通 Turn 的最终答复同一语义（前端可只认
            # metadata.final / intermediate，不再按会话类型分支推断）
            metadata["final"] = True
        else:
            # 非 final 的步回复 = 过程性输出：显式标记 intermediate，
            # 与普通 Turn 的中间 assistant 节点同一语义（方案 B 全覆盖）
            metadata["intermediate"] = True
        with self.store.transaction() as conn:
            node = self.store.create_node(
                conn, conversation_id=conversation_id, turn_id=turn_id,
                type=TurnNodeType.ASSISTANT.value, status=status, text=str(text or ""),
                metadata=metadata)
            version = self.store.bump_turn_version(conn, turn_id)
            self._append_and_emit(
                _pending, conn, conv, "node.delta", SCOPE_TURN, version, turn_id=turn_id,
                data={"node_id": node.node_id, "type": TurnNodeType.ASSISTANT.value,
                      "text": node.text or "", "status": node.status,
                      "position": node.position})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return node

    def request_stop(self, conversation_id: str, *,
                     operation_id: Optional[str] = None) -> Turn | None:
        """停止当前 Turn（设计方案 7.6 第 1–2 步）：置 stopping 并广播。
        停止确认/超时由运行时通过 complete_turn / set_turn_status 完成。
        （控制租约已废弃：不再校验 holder_id。）"""
        conv = self._conv(conversation_id)
        active = self.store.get_active_turn(conversation_id)
        if active is None:
            return None
        _pending: list = []
        with self.store.transaction() as conn:
            if operation_id:
                hit, result = self.store.check_idempotency(
                    conn, operation_id, conversation_id,
                    _request_hash({"op": "stop"}))
                if hit:
                    return self.store.get_turn(result["turn_id"])
            turn = self.store.update_turn_status(conn, active.turn_id,
                                                 TurnStatus.STOPPING.value)
            if operation_id:
                self.store.record_idempotency(
                    conn, operation_id, conversation_id,
                    _request_hash({"op": "stop"}),
                    result={"turn_id": turn.turn_id})
            self._append_and_emit(
                _pending, conn, conv, "turn.status", SCOPE_TURN, turn.turn_version,
                turn_id=turn.turn_id, data={"status": turn.status})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return turn

    # ------------------------------------------------------------
    # 审批（设计方案 7.7）
    # ------------------------------------------------------------

    def request_approval(self, conversation_id: str, turn_id: str, *,
                         tool_name: str, params_summary: str,
                         operation_id: Optional[str] = None) -> Approval:
        conv = self._conv(conversation_id)
        self._assert_turn_owned(conversation_id, turn_id)
        _pending: list = []
        with self.store.transaction() as conn:
            approval = self.store.create_approval(
                conn, conversation_id=conversation_id, turn_id=turn_id,
                tool_name=tool_name, params_summary=params_summary)
            turn = self.store.update_turn_status(conn, turn_id,
                                                 TurnStatus.APPROVAL.value)
            self._append_and_emit(
                _pending, conn, conv, "approval.requested", SCOPE_TURN, turn.turn_version,
                turn_id=turn_id,
                data={"approval_id": approval.approval_id, "tool_name": tool_name,
                      "params_summary": params_summary})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return approval

    def resolve_approval(self, conversation_id: str, approval_id: str, decision: str,
                         *, resolved_by: str = "") -> Approval:
        """审批通过/拒绝（设计方案 7.7）。通过后 Turn 由运行时推进到 tool。
        （控制租约已废弃：不再校验 holder_id / require_lease。）"""
        conv = self._conv(conversation_id)
        self._assert_approval_owned(conversation_id, approval_id)
        if decision not in (ApprovalStatus.APPROVED.value, ApprovalStatus.DENIED.value):
            raise ValidationFailed("decision 必须是 approved 或 denied")
        _pending: list = []
        with self.store.transaction() as conn:
            approval = self.store.resolve_approval(conn, approval_id, decision,
                                                   resolved_by=resolved_by)
            turn = self.store.get_turn(approval.turn_id)
            self._append_and_emit(
                _pending, conn, conv, "approval.resolved", SCOPE_TURN, turn.turn_version,
                turn_id=approval.turn_id,
                data={"approval_id": approval.approval_id,
                      "status": approval.status, "tool_name": approval.tool_name})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return approval

    def cancel_pending_approvals(self, conversation_id: str,
                                 turn_id: str) -> int:
        """取消 Turn 全部未决审批并广播 approval.resolved(status=cancelled)
        （X2-P3⑫：审批超时/取消与通过/拒绝走同一事件，前端统一收敛卡片）。

        用于 Turn 终态（stopped/error/interrupted）或显式取消：审批挂起时
        工具执行已不可能继续，保持 pending 会让审批卡片永久悬挂。返回取消数。"""
        conv = self._conv(conversation_id)
        _pending: list = []
        cancelled = 0
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT approval_id, tool_name FROM approvals "
                "WHERE conversation_id=? AND turn_id=? AND status='pending'",
                (conversation_id, turn_id),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE approvals SET status='cancelled', resolved_at=?, "
                    "resolved_by='system' "
                    "WHERE conversation_id=? AND turn_id=? AND status='pending'",
                    (utc_now(), conversation_id, turn_id),
                )
                turn = self.store.get_turn(turn_id)
                for row in rows:
                    self._append_and_emit(
                        _pending, conn, conv, "approval.resolved", SCOPE_TURN,
                        turn.turn_version, turn_id=turn_id,
                        data={"approval_id": row["approval_id"],
                              "status": ApprovalStatus.CANCELLED.value,
                              "tool_name": row["tool_name"] or "",
                              "error_code": APPROVAL_CANCELLED,
                              "reason": "turn_terminal"})
                cancelled = len(rows)
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return cancelled

    def _expire_and_broadcast_approvals(self, conn,
                                        _pending: list) -> int:
        """审批超时广播（X2-P3⑫）：cleanup 前先捕获过期 pending 审批，
        expire_stale_approvals 落库后逐个广播 approval.resolved(status=timed_out)。

        在同一事务内完成（过期标记 + outbox 写入原子）；超时默认 300 秒，
        与 fail-closed 语义一致（超时视为拒绝，绝不替用户放行）。"""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=APPROVAL_TIMEOUT_SECONDS)
                  ).isoformat(timespec="milliseconds")
        rows = conn.execute(
            """SELECT a.approval_id, a.conversation_id, a.turn_id, a.tool_name,
                      c.session_key, c.origin, c.subtype, c.workspace_id,
                      t.turn_version
               FROM approvals a
               JOIN conversation_sessions c ON c.conversation_id = a.conversation_id
               LEFT JOIN turns t ON t.turn_id = a.turn_id
               WHERE a.status='pending' AND a.created_at < ?""",
            (cutoff,),
        ).fetchall()
        count = self.store.expire_stale_approvals(conn)
        for row in rows:
            payload = {
                "conversation_id": row["conversation_id"],
                "session_key": row["session_key"],
                "origin": row["origin"],
                "subtype": row["subtype"],
                "workspace_id": row["workspace_id"],
                "scope": SCOPE_TURN,
                "version": int(row["turn_version"] or 0),
                "turn_id": row["turn_id"],
                "data": {
                    "approval_id": row["approval_id"],
                    "status": ApprovalStatus.TIMED_OUT.value,
                    "tool_name": row["tool_name"] or "",
                    "error_code": APPROVAL_TIMED_OUT,
                    "reason": "timeout",
                },
            }
            outbox = self.store.append_outbox(
                conn, row["conversation_id"], "approval.resolved",
                SCOPE_TURN, int(row["turn_version"] or 0), payload)
            _pending.append(("approval.resolved", payload, outbox.outbox_id))
        return count

    # ------------------------------------------------------------
    # 控制租约（设计方案 10）
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # 工具结果 / 投影 / 投递
    # ------------------------------------------------------------

    def save_tool_result(self, conversation_id: str, turn_id: str, *,
                         result_ref: str, kind: str, node_id: Optional[str] = None,
                         size_bytes: int = 0, lines: int = 0,
                         content_type: Optional[str] = None,
                         summary: Optional[Dict[str, Any]] = None,
                         truncation_reason: Optional[str] = None) -> None:
        with self.store.transaction() as conn:
            self.store.save_tool_result(
                conn, conversation_id=conversation_id, turn_id=turn_id,
                result_ref=result_ref, kind=kind, node_id=node_id,
                size_bytes=size_bytes, lines=lines, content_type=content_type,
                summary=summary, truncation_reason=truncation_reason)

    def get_result(self, conversation_id: str, turn_id: str,
                   result_ref: str) -> dict:
        result = self.store.get_tool_result(result_ref)
        if result.conversation_id != conversation_id or result.turn_id != turn_id:
            raise ResultNotOwned("结果引用不属于该会话")
        return result.to_dict()

    def record_delivery(self, conversation_id: str, *, turn_id: str,
                        channel: str, message_id: str, state: str,
                        version: int = 0) -> None:
        conv = self._conv(conversation_id)
        self._assert_turn_owned(conversation_id, turn_id)
        _pending: list = []
        with self.store.transaction() as conn:
            self._append_and_emit(
                _pending, conn, conv, "delivery.status", SCOPE_DELIVERY, version,
                turn_id=turn_id,
                data={"channel": channel, "message_id": message_id, "state": state})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
    # ------------------------------------------------------------
    # 去重 / 快照 / 历史 / 恢复 / 清理
    # ------------------------------------------------------------

    def check_and_record_receipt(self, conversation_id: str, channel: str,
                                 message_id: str) -> bool:
        """渠道去重（设计方案 11.4）：窗口内重复返回 True（应跳过）。

        单语句原子完成"检查 + 记录"（INSERT ... ON CONFLICT + rowcount 判定）：
        - 无记录 → 插入成功（rowcount=1）→ 新消息，返回 False；
        - 窗口内已有 → DO UPDATE 被 WHERE 拦截（rowcount=0）→ 重复，返回 True；
        - 超窗旧记录 → DO UPDATE 刷新窗口（rowcount=1）→ 视为新消息。
        修复并发首条消息/重试场景下"检查与记录分离"导致的重复入队缺口。"""
        if not message_id:
            return False
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RECEIPT_TTL_SECONDS)) \
            .isoformat(timespec="milliseconds")
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO channel_message_receipts
                   (channel, message_id, conversation_id, created_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(channel, message_id) DO UPDATE SET
                     conversation_id=excluded.conversation_id,
                     created_at=excluded.created_at
                   WHERE channel_message_receipts.created_at < ?""",
                (channel, message_id, conversation_id, utc_now(), cutoff),
            )
            inserted = int(cursor.rowcount or 0) > 0
        return not inserted

    def snapshot(self, conversation_id: str) -> dict:
        return self.store.snapshot(conversation_id)

    def history(self, conversation_id: str, *, before: Optional[str] = None,
                limit: int = 30) -> dict:
        return self.store.history_page(conversation_id, before=before, limit=limit)

    def recover_after_restart(self) -> dict:
        """重启恢复（设计方案 5.2/9.3）：活动 Turn → interrupted；
        Steering 待插入项恢复为普通队列项。

        X2-P1② 后端半边：对每个受影响会话广播 turn.status(interrupted) +
        queue.updated（与中断标记同一事务写入 outbox），断线前端恢复后立即
        看到"任务已中断"而非悬挂的 thinking/answering 状态。webui/__init__.py
        的调用点保持不变。"""
        _pending: list = []
        with self.store.transaction() as conn:
            # 先捕获本次受影响 Turn（中断前的非终态行），
            # interrupt_active_turns 之后按 turn_id 反查递增后的版本号。
            affected = conn.execute(
                "SELECT turn_id, conversation_id FROM turns "
                "WHERE status NOT IN ('done','stopped','error','interrupted')",
            ).fetchall()
            turns = self.store.interrupt_active_turns(conn)
            items = self.store.reset_interrupted_queue_items(conn)
            for row in affected:
                try:
                    turn = self.store.get_turn(row["turn_id"])
                except TurnNotFound:
                    continue
                conv = self._conv(row["conversation_id"])
                self._append_and_emit(
                    _pending, conn, conv, "turn.status", SCOPE_TURN,
                    turn.turn_version, turn_id=turn.turn_id,
                    data={"status": turn.status,
                          "error_code": turn.error_code or GATEWAY_RESTART})
                version = self.store.bump_session_version(
                    conn, row["conversation_id"])
                self._append_and_emit(
                    _pending, conn, conv, "queue.updated", SCOPE_SESSION,
                    version,
                    data={"interrupted": True,
                          "queue": _queue_summary(self.store.list_active_queue(
                              row["conversation_id"], conn=conn))})
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return {"interrupted_turns": turns, "reset_queue_items": items}

    def cleanup(self, *, system_retention_days: int = 7) -> dict:
        """保留策略（设计方案 16.5）：过期租约 / 幂等 / Outbox / 收据 / 审批超时 /
        过期 pending_delete 归档（设计方案 8.6）。

        X2-P3⑫：审批超时在同事务内广播 approval.resolved(status=timed_out)
        （过期标记 + outbox 写入原子），前端卡片即时收敛而非等到页面刷新。

        system_retention_days：system 会话（定时任务 sched:* / 心跳 heartbeat:*
        等，origin='system'）的保留窗口，超期连同 turns/nodes 等关联数据一并
        删除（方案A，默认 7 天，配置 conversation.retention.system_days）。
        webui / channel / workspace 会话不受影响。"""
        _pending: list = []
        with self.store.transaction() as conn:
            idempotency = self.store.cleanup_idempotency(conn)
            outbox = self.store.cleanup_outbox(conn)
            receipts = self.store.cleanup_receipts(conn)
            approvals = self._expire_and_broadcast_approvals(conn, _pending)
            pending_deletes = self.store.archive_expired_pending_deletes(conn)
            system_conversations = self.store.delete_stale_system_conversations(
                conn, system_retention_days)
        for event_type, payload, outbox_id in _pending:
            self._emit(event_type, payload, outbox_id)
        return {"idempotency": idempotency,
                "outbox": outbox, "receipts": receipts, "approvals": approvals,
                "pending_deletes": pending_deletes,
                "system_conversations": system_conversations}

    # ------------------------------------------------------------
    # 执行域查询
    # ------------------------------------------------------------

    def scope_usage(self, execution_scope: str) -> int:
        return self.store.count_active_turns_in_scope(execution_scope)


def _queue_summary(items: list[QueueItem]) -> list[dict]:
    return [it.to_dict() for it in items]


def gen_node_id_from_call(call_id: str, conversation_id: Optional[str] = None) -> str:
    """工具 call_id → 稳定节点 ID（幂等）。

    派生时加入 conversation_id 前缀：不同会话可能产生相同 call_id，
    不加前缀会导致跨会话节点串扰（A 会话的事件改写 B 会话的工具节点）。"""
    seed = f"{conversation_id or ''}:{call_id}"
    return "node_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
