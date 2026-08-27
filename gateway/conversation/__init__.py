# -*- coding: utf-8 -*-
"""
统一会话模型（gateway-unified-conversation-design 实施核心包）。

Conversation 是会话持久化边界，Turn 是运行边界，Node 是展示/局部更新边界；
后端状态机是唯一权威，数据库提交先于事件广播（Outbox）。
"""
from gateway.conversation.models import (
    Approval,
    ApprovalStatus,
    ChannelMessageReceipt,
    ConversationOrigin,
    ConversationSession,
    ConversationSubtype,
    DeliveryState,
    IdempotencyRecord,
    OutboxEvent,
    QueueItem,
    QueueItemStatus,
    ToolResult,
    Turn,
    TurnNode,
    TurnNodeType,
    TurnStatus,
)
from gateway.conversation.errors import (
    ApprovalConflict,
    ConversationError,
    ExecutionScopeLimit,
    IdempotencyConflict,
    QueueConflict,
    QueueLimit,
    ResourceNotFound,
    ResultNotOwned,
    SteeringLimit,
    SteeringTimeout,
)
from gateway.conversation.store import ConversationStore
from gateway.conversation.outbox import OutboxPublisher
from gateway.conversation.service import ConversationService, gen_node_id_from_call

__all__ = [
    "Approval", "ApprovalStatus", "ChannelMessageReceipt", "ConversationOrigin",
    "ConversationSession", "ConversationSubtype", "DeliveryState",
    "IdempotencyRecord", "OutboxEvent", "QueueItem", "QueueItemStatus",
    "ToolResult", "Turn", "TurnNode", "TurnNodeType", "TurnStatus",
    "ApprovalConflict", "ConversationError", "ExecutionScopeLimit", "IdempotencyConflict",
    "QueueConflict", "QueueLimit", "ResourceNotFound", "ResultNotOwned",
    "SteeringLimit", "SteeringTimeout",
    "ConversationStore", "OutboxPublisher", "ConversationService", "gen_node_id_from_call",
]
