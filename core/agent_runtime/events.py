"""Ordered runtime events with bounded async fan-out."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4
from core.runtime.models import utc_now

@dataclass(frozen=True)
class AgentEvent:
    type: str
    run_id: str
    session_id: str
    sequence: int
    data: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: f"agent_evt_{uuid4().hex}")
    created_at: str = field(default_factory=utc_now)

class AgentEventBus:
    def __init__(self, max_queue_size: int = 512):
        self._max_queue_size = max(1, max_queue_size)
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._sequence: dict[str, int] = {}

    def subscribe(self, session_id: str) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.setdefault(session_id, set()).add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(session_id)
        if subscribers:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(session_id, None)

    def forget(self, run_id: str) -> None:
        """Drop per-run sequence state once the run has ended (P2)。

        run 结束后调用，避免 _sequence 随 task_id 无限增长；已入队的
        AgentEvent 自带 sequence，不受影响。
        """
        self._sequence.pop(run_id, None)

    def publish(self, event_type: str, *, run_id: str, session_id: str, **data: Any) -> AgentEvent:
        sequence = self._sequence.get(run_id, 0) + 1
        self._sequence[run_id] = sequence
        event = AgentEvent(event_type, run_id, session_id, sequence, data)
        for queue in tuple(self._subscribers.get(session_id, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event
