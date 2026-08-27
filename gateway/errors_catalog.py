# -*- coding: utf-8 -*-
"""
统一 error_code 注册表（JKagent 收官 X2-P3⑫）。

所有落库/广播的 error_code 都从这里取常量，保证前后端看到同一套
"code → label（人读文案）/ retryable（可否重试）" 映射，避免散落的
魔法字符串。非本注册表的 code 也可存在（历史数据 / 上游透传），
读取侧用 :func:`info` 兜底（label=code，retryable=False）。

注册表分三组：
- Turn / 节点 / 工具（本切片主要使用）；
- 审批（approval.resolved 的 status 对应 error_code）；
- 任务域（对齐 core/runtime 既有 code，仅登记不改造）；
- 统一会话 API（对齐 gateway/conversation/errors.py 的 ConversationError.code）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ErrorCode:
    """单个错误码的静态元数据。"""

    code: str
    label: str
    retryable: bool = False


_REGISTRY: Dict[str, ErrorCode] = {}


def _def(code: str, label: str, *, retryable: bool = False) -> str:
    """注册一个错误码并返回常量值（供模块内常量赋值）。"""
    _REGISTRY[code] = ErrorCode(code=code, label=label, retryable=retryable)
    return code


# ---------------------------------------------------------------
# Turn / 节点 / 工具
# ---------------------------------------------------------------

#: Agent 执行失败（bridge.on_error 终态）
AGENT_EXECUTION_FAILED = _def(
    "agent_execution_failed", "Agent 执行失败", retryable=False)
#: 工具执行错误（工具结果 is_error=True）
TOOL_EXECUTION_ERROR = _def(
    "tool_execution_error", "工具执行错误", retryable=True)
#: 停止确认超时（runner 看门，设计方案 7.6）
STOP_TIMEOUT = _def("stop_timeout", "停止确认超时", retryable=False)
#: Steering 中断超时（设计方案 9.3）
STEERING_INTERRUPT_TIMEOUT = _def(
    "steering_interrupt_timeout", "Steering 中断超时", retryable=True)
#: 后端重启中断（recover_after_restart，设计方案 5.2）
GATEWAY_RESTART = _def("gateway_restart", "网关重启中断", retryable=True)
#: 流式增量合批缓冲超限丢弃（C-4，version_gap 快照自愈兜底）
DELTA_BUFFER_OVERFLOW = _def(
    "delta_buffer_overflow", "流式增量缓冲超限", retryable=True)

# ---------------------------------------------------------------
# 审批（approval.resolved status 对应的 error_code）
# ---------------------------------------------------------------

APPROVAL_TIMED_OUT = _def("approval_timed_out", "审批超时", retryable=True)
APPROVAL_CANCELLED = _def("approval_cancelled", "审批已取消", retryable=False)
APPROVAL_NOT_PENDING = _def("approval_not_pending", "审批状态冲突", retryable=False)

# ---------------------------------------------------------------
# 任务域（对齐 core/runtime 既有 error_code，登记不改行为）
# ---------------------------------------------------------------

TASK_CANCELLED = _def("TASK_CANCELLED", "任务已取消", retryable=False)
RUNTIME_INTERRUPTED = _def("RUNTIME_INTERRUPTED", "运行时中断", retryable=True)
RUNTIME_CONTEXT_MISSING = _def(
    "RUNTIME_CONTEXT_MISSING", "运行时上下文缺失", retryable=True)
CHANNEL_UNAVAILABLE = _def("CHANNEL_UNAVAILABLE", "渠道不可用", retryable=True)
TASK_EXECUTION_ERROR = _def(
    "TASK_EXECUTION_ERROR", "任务执行错误", retryable=False)
TASK_TIMEOUT = _def("TASK_TIMEOUT", "任务执行超时", retryable=True)

# ---------------------------------------------------------------
# 统一会话 API（对齐 gateway/conversation/errors.py 的 code）
# ---------------------------------------------------------------

CONVERSATION_NOT_FOUND = _def("conversation_not_found", "会话不存在")
TURN_NOT_FOUND = _def("turn_not_found", "Turn 不存在")
QUEUE_ITEM_CONFLICT = _def("queue_item_conflict", "队列项状态冲突")
QUEUE_LIMIT = _def("queue_limit", "队列数量/长度超限")
QUEUE_NOT_SENDING = _def("queue_not_sending", "队列未处于发送状态")
UNDO_EXPIRED = _def("undo_expired", "删除撤销窗口已过")
STEERING_LIMIT = _def("steering_limit", "单 Turn Steering 次数超限")
IDEMPOTENCY_CONFLICT = _def("idempotency_conflict", "幂等冲突")
EXECUTION_SCOPE_CONCURRENCY_LIMIT = _def(
    "execution_scope_concurrency_limit", "执行域并发已满")
GATEWAY_CONCURRENCY_SATURATED = _def(
    "gateway_concurrency_saturated", "全局并发已满", retryable=True)
RESULT_REF_NOT_OWNED = _def("result_ref_not_owned", "结果引用不属于该会话")
CONVERSATION_EXISTS = _def("conversation_exists", "会话已存在")
VALIDATION_FAILED = _def("validation_failed", "参数校验失败")


def info(code: str) -> Optional[ErrorCode]:
    """按 code 取元数据；未注册返回 None（调用方自行兜底）。"""
    return _REGISTRY.get(code)


def label(code: str) -> str:
    """人读文案；未注册的 code 原样返回（保持可读）。"""
    entry = _REGISTRY.get(code)
    return entry.label if entry is not None else code


def is_retryable(code: str) -> bool:
    """该错误码对应的操作是否可安全重试；未知 code 默认不可重试。"""
    entry = _REGISTRY.get(code)
    return bool(entry and entry.retryable)


def all_codes() -> Dict[str, ErrorCode]:
    """注册表全量快照（文档生成 / 调试用）。"""
    return dict(_REGISTRY)
