# -*- coding: utf-8 -*-
"""
Hook 事件定义 — 生命周期事件枚举 + 上下文数据类 + 裁决模型

与设计文档 code/learn/HOOK/design.md 一致：
- 12 个生命周期事件（Phase 1 接 6 个，其余后续）
- HookContext 承载事件上下文（payload 按事件类型约定）
- Decision / HookResult 承载裁决结果
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ================================================================
# 事件枚举
# ================================================================

class HookEvent(str, Enum):
    """Hook 生命周期事件（str 枚举，方便 JSON 序列化 & 配置文件匹配）"""
    SESSION_START = "session_start"
    USER_PROMPT   = "user_prompt"
    PRE_TOOL      = "pre_tool"
    POST_TOOL     = "post_tool"
    NOTIFICATION  = "notification"
    DENIED        = "denied"
    STOP          = "stop"
    PLAN_APPROVED = "plan_approved"


# ================================================================
# 裁决模型
# ================================================================

class Decision(str, Enum):
    """Hook 裁决"""
    ALLOW    = "allow"     # 明确放行
    BLOCK    = "block"     # 拦截，终止该动作
    MODIFY   = "modify"    # 改写 payload 后继续
    CONTINUE = "continue"  # 仅观察，不影响流程（dispatch 默认值）


@dataclass
class HookResult:
    """Hook 执行结果"""
    decision: Decision = Decision.CONTINUE
    reason: str = ""
    data: dict | None = None   # MODIFY 时的改写数据


# ================================================================
# 事件上下文
# ================================================================

@dataclass
class HookContext:
    """传给 hook 的上下文 — 不含完整 messages（避免超大 JSON 撑爆 CommandHook stdin）。

    payload 约定（按事件）：
      user_prompt:   {"prompt": str}
      pre_tool:      {"tool_name": str, "params": dict, "gate_level": str}
      post_tool:     {"tool_name": str, "params": dict, "result": str, "is_error": bool}
      notification:  {"tool_name": str, "params": dict, "message": str}
      denied:        {"tool_name": str, "reason": str, "level": str}
      stop:          {"answer": str, "step_count": int}
      plan_approved: {"plan": str, "tasks": list}
      session_start: {}
    """
    event: HookEvent
    agent_name: str = ""
    session_id: str = ""
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        """序列化为 JSON（CommandHook stdin 用）"""
        import json as _json
        return _json.dumps({
            "event": self.event.value,
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }, ensure_ascii=False)


# ================================================================
# 工具函数
# ================================================================

def _coerce(out: Any) -> HookResult:
    """标准化 PythonHook 回调返回值：None/dict/bool/str → HookResult"""
    if out is None:
        return HookResult(Decision.CONTINUE)
    if isinstance(out, HookResult):
        return out
    if isinstance(out, dict):
        return HookResult(
            decision=Decision(out.get("decision", "continue")),
            reason=out.get("reason", ""),
            data=out.get("data"),
        )
    if isinstance(out, bool):
        return HookResult(Decision.ALLOW if out else Decision.BLOCK)
    if isinstance(out, str):
        return HookResult(Decision.BLOCK, reason=out)
    return HookResult(Decision.CONTINUE)
