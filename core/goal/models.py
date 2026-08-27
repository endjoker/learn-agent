"""Durable Goal lifecycle and activation models."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import uuid4
from core.runtime.models import utc_now

class GoalStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls):
        return {cls.COMPLETED, cls.CANCELLED}

class GoalActivation(str, Enum):
    ARMED = "armed"
    DISARMED = "disarmed"

@dataclass
class Goal:
    goal_id: str
    session_id: str
    title: str
    objective: str
    status: GoalStatus = GoalStatus.ACTIVE
    activation: GoalActivation = GoalActivation.ARMED
    version: int = 1
    plan_id: Optional[str] = None
    progress: float = 0.0
    continuation: dict[str, Any] = field(default_factory=dict)
    blocked_reason: Optional[dict[str, Any]] = None
    rounds_started: int = 0
    max_rounds: int = 20
    current_task_id: Optional[str] = None
    archived_at: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self):
        if not self.goal_id or not self.session_id or not self.objective.strip():
            raise ValueError("goal_id, session_id and objective are required")
        self.status = GoalStatus(self.status)
        self.activation = GoalActivation(self.activation)
        self.version = max(1, int(self.version))
        self.rounds_started = max(0, int(self.rounds_started))
        self.max_rounds = max(1, int(self.max_rounds))
        self.title = self.title.strip() or self.objective.strip()[:120]
        self.progress = min(1.0, max(0.0, float(self.progress)))
        if self.status is not GoalStatus.ACTIVE:
            self.activation = GoalActivation.DISARMED

    @classmethod
    def create(cls, *, session_id: str, objective: str, title: str = "", **kwargs):
        return cls(goal_id=kwargs.pop("goal_id", f"goal_{uuid4().hex}"), session_id=session_id,
                   objective=objective, title=title or objective[:120], **kwargs)

    @property
    def is_terminal(self):
        return self.status in GoalStatus.terminal()

    @property
    def is_armed(self):
        return self.status is GoalStatus.ACTIVE and self.activation is GoalActivation.ARMED

    def to_dict(self):
        enum_fields = {"status", "activation"}
        return {name: (getattr(self, name).value if name in enum_fields else getattr(self, name))
                for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value):
        data = dict(value)
        data["status"] = GoalStatus(data.get("status", "active"))
        # Legacy active Goals are kept safe on upgrade unless they explicitly
        # persisted activation. Newly-created Goals always persist ARMED.
        data["activation"] = GoalActivation(data.get("activation", "disarmed"))
        data.setdefault("rounds_started", 0)
        data.setdefault("max_rounds", 20)
        data.setdefault("current_task_id", None)
        return cls(**data)
