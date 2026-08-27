"""Provider-neutral models for the single agent execution loop."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class RunStatus(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    TIMEOUT = "timeout"
    MAX_STEPS = "max_steps"
    MAX_TOOL_CALLS = "max_tool_calls"
    CONTEXT_OVERFLOW = "context_overflow"
    # P3-7：hook 拦截输入时 _run_native_loop 以 finish("blocked") 收口；
    # 缺此成员会落入 loop.py 状态映射的默认分支被误报为 ERROR。
    BLOCKED = "blocked"
    ERROR = "error"

@dataclass(frozen=True)
class AgentRunResult:
    run_id: str
    status: RunStatus
    visible_text: str = ""
    summary: str = ""
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
