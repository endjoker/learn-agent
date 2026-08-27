# -*- coding: utf-8 -*-
"""
统一会话领域模型 —— Conversation / Turn / TurnNode / QueueItem / Lease /
OutboxEvent / ToolResult / Approval / Receipt。

对应设计方案第 5、8、10、11、14、16、17 节。
接口字段采用 camelCase（序列化时与 snake_case 数据库列一一对应）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _gen_id(prefix: str) -> str:
    import secrets
    return f"{prefix}{secrets.token_hex(8)}"


def gen_conversation_id() -> str:
    return _gen_id("conv_")


def gen_turn_id() -> str:
    return _gen_id("turn_")


def gen_node_id() -> str:
    return _gen_id("node_")


def gen_queue_item_id() -> str:
    return _gen_id("q_")


def gen_approval_id() -> str:
    return _gen_id("apr_")


def gen_outbox_id() -> str:
    return _gen_id("obx_")


def gen_projection_id() -> str:
    return _gen_id("prj_")


# ============================================================
# 枚举
# ============================================================


class ConversationOrigin(str, Enum):
    WEBUI = "webui"
    CHANNEL = "channel"
    SYSTEM = "system"


class ConversationSubtype(str, Enum):
    MAIN = "main"
    WORKSPACE = "workspace"
    FEISHU = "feishu"
    WEIXIN = "weixin"
    SCHEDULER = "scheduler"
    HEARTBEAT = "heartbeat"
    PLAN = "plan"
    GOAL = "goal"
    SUBAGENT = "subagent"
    DEBUG = "debug"
    OTHER = "other"


class TurnStatus(str, Enum):
    QUEUED = "queued"
    THINKING = "thinking"
    TOOL = "tool"
    APPROVAL = "approval"
    ANSWERING = "answering"
    STEERING = "steering"
    STOPPING = "stopping"
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"
    INTERRUPTED = "interrupted"


TERMINAL_TURN_STATUSES = frozenset({
    TurnStatus.DONE, TurnStatus.STOPPED, TurnStatus.ERROR, TurnStatus.INTERRUPTED,
})


class TurnNodeType(str, Enum):
    USER = "user"
    REASONING = "reasoning"
    TOOL = "tool"
    ASSISTANT = "assistant"
    USER_STEERING = "user_steering"
    STATUS = "status"
    # 用户随消息发送的图片（修正版方案 A）：存 artifacts 引用（ref/mime/size），
    # 正文不进 SQLite；回放降级为占位文本块。
    IMAGE = "image"


class QueueItemStatus(str, Enum):
    WAITING = "waiting"
    WAITING_FOR_STEERING = "waiting_for_steering"
    INJECTING = "injecting"
    SENDING = "sending"
    PENDING_DELETE = "pending_delete"
    FAILED = "failed"
    SENT = "sent"
    INJECTED = "injected"
    DELETED = "deleted"


# 设计方案 8.4：前一 Turn 终态 → 队列行为
TURN_TERMINAL_QUEUE_RULES = {
    TurnStatus.DONE: "countdown",        # 最终回复非空 → 5 秒倒计时
    TurnStatus.STOPPED: "countdown",
    TurnStatus.ERROR: "pause",
    TurnStatus.INTERRUPTED: "pause",
}


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class DeliveryState(str, Enum):
    PENDING_DELIVERY = "pending_delivery"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"


class LeaseState(str, Enum):
    HELD = "held"
    EXPIRED = "expired"


# ============================================================
# 领域对象
# ============================================================


@dataclass
class ConversationSession:
    conversation_id: str
    session_key: str
    origin: str
    subtype: str
    execution_scope: str
    workspace_id: Optional[str] = None
    route_metadata: Dict[str, Any] = field(default_factory=dict)
    session_version: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "session_key": self.session_key,
            "origin": self.origin,
            "subtype": self.subtype,
            "workspace_id": self.workspace_id,
            "execution_scope": self.execution_scope,
            "route_metadata": dict(self.route_metadata),
            "session_version": self.session_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Turn:
    turn_id: str
    conversation_id: str
    status: str
    turn_version: int = 0
    runtime_snapshot_id: Optional[str] = None
    started_at: str = ""
    finished_at: Optional[str] = None
    final_assistant_node_id: Optional[str] = None
    error_code: Optional[str] = None
    parent_conversation_id: Optional[str] = None
    parent_turn_id: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TURN_STATUSES

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "turn_version": self.turn_version,
            "runtime_snapshot_id": self.runtime_snapshot_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "final_assistant_node_id": self.final_assistant_node_id,
            "error_code": self.error_code,
            "parent_conversation_id": self.parent_conversation_id,
            "parent_turn_id": self.parent_turn_id,
        }


@dataclass
class TurnNode:
    node_id: str
    conversation_id: str
    type: str
    position: int
    status: str
    turn_id: Optional[str] = None
    text: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_channel: Optional[str] = None
    source_message_id: Optional[str] = None
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "type": self.type,
            "position": self.position,
            "status": self.status,
            "text": self.text,
            "metadata": dict(self.metadata),
            "source_channel": self.source_channel,
            "source_message_id": self.source_message_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class QueueItem:
    queue_item_id: str
    conversation_id: str
    position: int
    revision: int
    status: str
    text: str
    target_turn_id: Optional[str] = None
    created_turn_id: Optional[str] = None
    created_node_id: Optional[str] = None
    operation_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    # 图片信封（v16）：[{data: base64, media_type: str}]；出队执行后即消费，
    # 不参与历史回放（历史以 artifacts 文件 + image 引用节点承载）。
    images: Optional[list] = None

    def to_dict(self) -> dict:
        return {
            "queue_item_id": self.queue_item_id,
            "conversation_id": self.conversation_id,
            "position": self.position,
            "revision": self.revision,
            "status": self.status,
            "text": self.text,
            "target_turn_id": self.target_turn_id,
            "created_turn_id": self.created_turn_id,
            "created_node_id": self.created_node_id,
            "operation_id": self.operation_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "image_count": len(self.images or []),
        }


@dataclass
class IdempotencyRecord:
    operation_id: str
    conversation_id: str
    request_hash: str
    result_json: Optional[str] = None
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "conversation_id": self.conversation_id,
            "request_hash": self.request_hash,
            "result_json": self.result_json,
            "created_at": self.created_at,
        }


@dataclass
class OutboxEvent:
    outbox_id: str
    conversation_id: str
    event_type: str
    scope: str
    version: int
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    published_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "outbox_id": self.outbox_id,
            "conversation_id": self.conversation_id,
            "event_type": self.event_type,
            "scope": self.scope,
            "version": self.version,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "published_at": self.published_at,
        }


@dataclass
class ToolResult:
    result_ref: str
    conversation_id: str
    turn_id: str
    kind: str
    node_id: Optional[str] = None
    size_bytes: int = 0
    lines: int = 0
    content_type: Optional[str] = None
    summary: Dict[str, Any] = field(default_factory=dict)
    truncation_reason: Optional[str] = None
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "result_ref": self.result_ref,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "node_id": self.node_id,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "lines": self.lines,
            "content_type": self.content_type,
            "summary": dict(self.summary),
            "truncation_reason": self.truncation_reason,
            "created_at": self.created_at,
        }


@dataclass
class ChannelMessageReceipt:
    channel: str
    message_id: str
    conversation_id: str
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
        }


@dataclass
class Approval:
    approval_id: str
    conversation_id: str
    turn_id: str
    tool_name: str
    params_summary: str
    status: str
    node_id: Optional[str] = None
    created_at: str = ""
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "node_id": self.node_id,
            "tool_name": self.tool_name,
            "params_summary": self.params_summary,
            "status": self.status,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
        }


def _dumps_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads_json(raw: Optional[str], default=None):
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}
