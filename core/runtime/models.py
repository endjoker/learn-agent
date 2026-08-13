"""Provider-neutral persistent models for the unified task runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Optional
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TaskStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_DEPENDENCY = "waiting_dependency"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"

    @classmethod
    def terminal(cls) -> set["TaskStatus"]:
        return {cls.COMPLETED, cls.FAILED, cls.BLOCKED, cls.CANCELLED, cls.TIMED_OUT}


_ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.QUEUED: {TaskStatus.LEASED, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
    TaskStatus.LEASED: {TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_APPROVAL, TaskStatus.RETRY_WAIT, TaskStatus.COMPLETED,
        TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT, TaskStatus.INTERRUPTED,
    },
    TaskStatus.WAITING_APPROVAL: {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
    TaskStatus.WAITING_DEPENDENCY: {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
    TaskStatus.RETRY_WAIT: {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.INTERRUPTED: {TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    # A missing volatile delivery context is recoverable after a Gateway
    # restart. RuntimeStore only exposes this transition through its explicit
    # recovery helper, so ordinary task code cannot arbitrarily reopen a
    # terminal blocked task.
    TaskStatus.BLOCKED: {TaskStatus.QUEUED},
    TaskStatus.CANCELLED: set(),
    TaskStatus.TIMED_OUT: set(),
}


@dataclass(frozen=True)
class Budget:
    """Execution ceilings. ``None`` means no additional limit."""

    max_steps: Optional[int] = None
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    max_total_tokens: Optional[int] = None
    max_wall_seconds: Optional[int] = None
    max_tool_calls: Optional[int] = None
    max_failures: Optional[int] = None
    max_children: Optional[int] = None

    def to_dict(self) -> dict[str, Optional[int]]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Optional[dict[str, Any]]) -> "Budget":
        value = value or {}
        return cls(**{name: value.get(name) for name in cls.__dataclass_fields__})

    @classmethod
    def strictest(cls, *budgets: Optional["Budget"]) -> "Budget":
        result: dict[str, Optional[int]] = {}
        for name in cls.__dataclass_fields__:
            limits = [getattr(item, name) for item in budgets if item and getattr(item, name) is not None]
            result[name] = min(limits) if limits else None
        return cls(**result)


@dataclass(frozen=True)
class TaskEnvelope:
    """Immutable task input persisted before it is eligible for execution."""

    task_id: str
    session_id: str
    session_key: str
    source: str
    prompt: str
    created_at: str
    plan_id: Optional[str] = None
    plan_task_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    assignee: str = "root"
    priority: int = 0
    timeout_seconds: int = 1200
    max_steps: int = 100
    idempotency_key: Optional[str] = None
    context_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    budget: Budget = field(default_factory=Budget)

    VALID_SOURCES: ClassVar[set[str]] = {
        "user", "plan", "heartbeat", "scheduler", "system", "retry",
    }

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id is required")
        if not self.session_id or not self.session_key:
            raise ValueError("session_id and session_key are required")
        if self.source not in self.VALID_SOURCES:
            raise ValueError(f"unsupported task source: {self.source}")
        if not self.prompt.strip():
            raise ValueError("task prompt cannot be empty")
        if self.timeout_seconds <= 0 or self.max_steps <= 0:
            raise ValueError("timeout_seconds and max_steps must be positive")

    @classmethod
    def create(cls, *, session_id: str, session_key: str, source: str,
               prompt: str, **kwargs: Any) -> "TaskEnvelope":
        return cls(
            task_id=kwargs.pop("task_id", f"task_{uuid4().hex}"),
            session_id=session_id,
            session_key=session_key,
            source=source,
            prompt=prompt,
            created_at=kwargs.pop("created_at", utc_now()),
            **kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["context_refs"] = list(self.context_refs)
        data["budget"] = self.budget.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskEnvelope":
        value = dict(data)
        # Runtime databases created by earlier releases can contain retired
        # linkage fields. Ignore them when reading persisted tasks.
        value.pop("goal_id", None)
        value.pop("team_id", None)
        # Earlier runtime experiments emitted retired producer labels. They
        # are historical records, not resumable work, so map them to a valid
        # neutral source when reading instead of rejecting the whole database.
        if value.get("source") in {"goal", "subagent"}:
            value["source"] = "system"
        value["context_refs"] = tuple(value.get("context_refs") or ())
        value["budget"] = Budget.from_dict(value.get("budget"))
        return cls(**value)


@dataclass
class TaskRecord:
    """Mutable task lifecycle state; all changes must pass transition()."""

    task_id: str
    status: TaskStatus = TaskStatus.CREATED
    attempt: int = 0
    queued_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    cancel_requested: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    result_summary: Optional[str] = None
    artifact_ids: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def transition(self, target: TaskStatus, *, now: Optional[str] = None,
                   error_code: Optional[str] = None,
                   error_message: Optional[str] = None,
                   result_summary: Optional[str] = None) -> None:
        if target == self.status:
            return
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(f"illegal task transition: {self.status.value} -> {target.value}")
        now = now or utc_now()
        self.status = target
        if target == TaskStatus.QUEUED:
            self.queued_at = now
            self.lease_owner = None
            self.lease_expires_at = None
        elif target == TaskStatus.LEASED:
            self.lease_owner = self.lease_owner or "runtime"
        elif target == TaskStatus.RUNNING:
            self.started_at = self.started_at or now
        if target in TaskStatus.terminal():
            self.finished_at = now
            self.lease_owner = None
            self.lease_expires_at = None
        if error_code is not None:
            self.error_code = error_code
        if error_message is not None:
            self.error_message = error_message
        if result_summary is not None:
            self.result_summary = result_summary

    @property
    def is_terminal(self) -> bool:
        return self.status in TaskStatus.terminal()

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        value = dict(data)
        value["status"] = TaskStatus(value["status"])
        return cls(**value)


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: TaskStatus
    visible_text: str = ""
    summary: str = ""
    artifact_ids: tuple[str, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in TaskStatus.terminal():
            raise ValueError("TaskResult requires a terminal status")

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["status"] = self.status.value
        data["artifact_ids"] = list(self.artifact_ids)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskResult":
        value = dict(data)
        value["status"] = TaskStatus(value["status"])
        value["artifact_ids"] = tuple(value.get("artifact_ids") or ())
        return cls(**value)


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    event_type: str
    created_at: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    sequence: Optional[int] = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, event_type: str, **kwargs: Any) -> "RuntimeEvent":
        return cls(
            event_id=kwargs.pop("event_id", f"evt_{uuid4().hex}"),
            event_type=event_type,
            created_at=kwargs.pop("created_at", utc_now()),
            **kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)
