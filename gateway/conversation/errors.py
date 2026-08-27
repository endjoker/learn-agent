# -*- coding: utf-8 -*-
"""
统一会话错误模型 —— 与设计方案第 30.1/30.3 节错误码一一对应。
"""

from __future__ import annotations


class ConversationError(RuntimeError):
    """统一会话错误基类。"""

    code = "CONVERSATION_ERROR"
    http_status = 400

    def __init__(self, message: str = "", *, code: str = ""):
        super().__init__(message)
        if code:
            self.code = code

    def to_dict(self) -> dict:
        return {"error": self.code, "message": str(self)}


class ResourceNotFound(ConversationError):
    code = "conversation_not_found"
    http_status = 404


class TurnNotFound(ConversationError):
    code = "turn_not_found"
    http_status = 404


class QueueConflict(ConversationError):
    """队列项级乐观锁 / 状态冲突。"""
    code = "queue_item_conflict"
    http_status = 409


class QueueLimit(ConversationError):
    """队列数量 / 长度超限。"""
    code = "queue_limit"
    http_status = 409


class QueueNotSending(ConversationError):
    """队列未处于发送状态。"""
    code = "queue_not_sending"
    http_status = 409


class UndoExpired(ConversationError):
    code = "undo_expired"
    http_status = 409


class SteeringLimit(ConversationError):
    """单 Turn Steering 次数上限（设计方案 9.4：每 Turn 最多 10 次）。"""
    code = "steering_limit"
    http_status = 409


class SteeringTimeout(ConversationError):
    """Steering 中断超时（设计方案 9.3）。"""
    code = "steering_interrupt_timeout"
    http_status = 409


class IdempotencyConflict(ConversationError):
    """同 operation_id 对应不同请求（设计方案 10 节）。"""
    code = "idempotency_conflict"
    http_status = 409


class ExecutionScopeLimit(ConversationError):
    """执行域并发上限（设计方案 13 节）。"""
    code = "execution_scope_concurrency_limit"
    http_status = 409


class GatewaySaturated(ConversationError):
    """进程级全局并发打满。"""
    code = "gateway_concurrency_saturated"
    http_status = 503


class ApprovalConflict(ConversationError):
    """审批状态冲突：非 pending / 非控制端。"""
    code = "approval_not_pending"
    http_status = 409


class ResultNotOwned(ConversationError):
    """结果引用不属于该会话（设计方案 17 节归属校验）。"""
    code = "result_ref_not_owned"
    http_status = 403


class ConversationExists(ConversationError):
    """同 session_key 重复创建（设计方案 30.1）。"""
    code = "conversation_exists"
    http_status = 409


class ValidationFailed(ConversationError):
    code = "validation_failed"
    http_status = 400
