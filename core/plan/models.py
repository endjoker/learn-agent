"""Durable, provider-neutral Plan workflow models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from core.runtime.models import utc_now


class PlanStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"

    @classmethod
    def terminal(cls) -> set["PlanStatus"]:
        return {cls.COMPLETED, cls.FAILED, cls.CANCELLED, cls.SUPERSEDED}


class PlanTaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls) -> set["PlanTaskStatus"]:
        return {cls.COMPLETED, cls.FAILED, cls.BLOCKED, cls.CANCELLED}


_PLAN_TRANSITIONS: dict[PlanStatus, set[PlanStatus]] = {
    PlanStatus.DRAFT: {PlanStatus.AWAITING_APPROVAL, PlanStatus.CANCELLED},
    PlanStatus.AWAITING_APPROVAL: {PlanStatus.APPROVED, PlanStatus.CANCELLED, PlanStatus.SUPERSEDED},
    PlanStatus.APPROVED: {PlanStatus.ACTIVE, PlanStatus.CANCELLED, PlanStatus.SUPERSEDED},
    PlanStatus.ACTIVE: {PlanStatus.PAUSED, PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED},
    PlanStatus.PAUSED: {PlanStatus.ACTIVE, PlanStatus.CANCELLED},
    PlanStatus.COMPLETED: set(), PlanStatus.FAILED: set(),
    PlanStatus.CANCELLED: set(), PlanStatus.SUPERSEDED: set(),
}


@dataclass
class PlanTask:
    plan_task_id: str
    description: str
    depends_on: tuple[str, ...] = ()
    status: PlanTaskStatus = PlanTaskStatus.PENDING
    weight: float = 1.0
    acceptance: list[dict[str, Any]] = field(default_factory=list)
    retry_limit: int = 0
    attempts: int = 0
    task_id: Optional[str] = None
    result_summary: str = ""
    artifact_ids: list[str] = field(default_factory=list)
    quality_report: list[dict[str, Any]] = field(default_factory=list)
    blocked_reason: Optional[dict[str, Any]] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.plan_task_id = self.plan_task_id.strip()
        self.description = self.description.strip()
        if not self.plan_task_id or not self.description:
            raise ValueError("plan_task_id and description are required")
        self.status = PlanTaskStatus(self.status)
        self.depends_on = tuple(self.depends_on or ())
        if self.plan_task_id in self.depends_on:
            raise ValueError("a PlanTask cannot depend on itself")
        self.weight = float(self.weight)
        if self.weight <= 0:
            raise ValueError("PlanTask weight must be positive")
        self.retry_limit = int(self.retry_limit)
        if self.retry_limit < 0:
            raise ValueError("retry_limit cannot be negative")
        self.acceptance = list(self.acceptance or [])

    @property
    def is_terminal(self) -> bool:
        return self.status in PlanTaskStatus.terminal()

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_task_id": self.plan_task_id, "description": self.description,
            "depends_on": list(self.depends_on), "status": self.status.value, "weight": self.weight,
            "acceptance": self.acceptance, "retry_limit": self.retry_limit, "attempts": self.attempts,
            "task_id": self.task_id, "result_summary": self.result_summary,
            "artifact_ids": list(self.artifact_ids), "quality_report": list(self.quality_report),
            "blocked_reason": self.blocked_reason, "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlanTask":
        data = dict(value)
        data["status"] = PlanTaskStatus(data["status"])
        data["depends_on"] = tuple(data.get("depends_on") or ())
        data["acceptance"] = list(data.get("acceptance") or [])
        data["artifact_ids"] = list(data.get("artifact_ids") or [])
        data["quality_report"] = list(data.get("quality_report") or [])
        return cls(**data)


@dataclass
class Plan:
    plan_id: str
    session_id: str
    title: str
    tasks: list[PlanTask]
    status: PlanStatus = PlanStatus.DRAFT
    version: int = 1
    source_prompt: str = ""
    approved_at: Optional[str] = None
    approval_actor: Optional[str] = None
    archived_at: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.plan_id or not self.session_id:
            raise ValueError("plan_id and session_id are required")
        self.title = self.title.strip() or "执行方案"
        self.status = PlanStatus(self.status)
        self.version = int(self.version)
        if self.version < 1:
            raise ValueError("plan version must be positive")
        self.tasks = [item if isinstance(item, PlanTask) else PlanTask.from_dict(item) for item in self.tasks]
        if not self.tasks:
            raise ValueError("a Plan needs at least one task")
        ids = [task.plan_task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("PlanTask IDs must be unique")
        allowed = set(ids)
        for task in self.tasks:
            unknown = set(task.depends_on).difference(allowed)
            if unknown:
                raise ValueError(f"PlanTask {task.plan_task_id} depends on unknown tasks: {sorted(unknown)}")
        self._validate_dag()

    @classmethod
    def create(cls, *, session_id: str, title: str, tasks: list[PlanTask], **kwargs: Any) -> "Plan":
        return cls(plan_id=kwargs.pop("plan_id", f"plan_{uuid4().hex}"), session_id=session_id,
                   title=title, tasks=tasks, **kwargs)

    @property
    def is_terminal(self) -> bool:
        return self.status in PlanStatus.terminal()

    @property
    def progress(self) -> float:
        total = sum(task.weight for task in self.tasks)
        completed = sum(task.weight for task in self.tasks if task.status is PlanTaskStatus.COMPLETED)
        return completed / total if total else 0.0

    def task(self, plan_task_id: str) -> PlanTask:
        for task in self.tasks:
            if task.plan_task_id == plan_task_id:
                return task
        raise KeyError(plan_task_id)

    def transition(self, target: PlanStatus | str) -> None:
        target = PlanStatus(target)
        if target not in _PLAN_TRANSITIONS[self.status]:
            raise ValueError(f"invalid plan transition: {self.status.value} -> {target.value}")
        self.status = target
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id, "session_id": self.session_id, "title": self.title,
            "tasks": [task.to_dict() for task in self.tasks], "status": self.status.value,
            "version": self.version, "progress": self.progress, "source_prompt": self.source_prompt,
            "approved_at": self.approved_at, "approval_actor": self.approval_actor,
            "archived_at": self.archived_at,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Plan":
        data = dict(value)
        # progress is a derived serialization field, not constructor state.
        data.pop("progress", None)
        # Snapshots written by earlier releases may retain this retired field.
        data.pop("goal_id", None)
        data.setdefault("archived_at", None)
        data["status"] = PlanStatus(data["status"])
        data["tasks"] = [PlanTask.from_dict(item) for item in data.get("tasks") or []]
        return cls(**data)

    def _validate_dag(self) -> None:
        edges = {task.plan_task_id: set(task.depends_on) for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("Plan dependencies contain a cycle")
            if node in visited:
                return
            visiting.add(node)
            for dependency in edges[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for node in edges:
            visit(node)
