"""Cooperative cancellation used by every long-running runtime task."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional


class TaskCancelled(RuntimeError):
    """Raised by an execution boundary after its cancellation token is set."""


@dataclass(frozen=True)
class CancellationState:
    requested: bool
    reason: str = ""


class CancellationToken:
    """Thread-safe token; Python workers must stop at explicit checkpoints."""

    def __init__(self, parent: Optional["CancellationToken"] = None):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""
        self._parent = parent
        self._callbacks = []

    def cancel(self, reason: str = "cancelled") -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(reason)
            except Exception:
                # Cancellation must remain best-effort even if one consumer
                # has already been disposed.
                pass
        return True

    def add_callback(self, callback) -> None:
        """Run callback once when cancellation is requested."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
            reason = self._reason
        callback(reason)

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set() or bool(self._parent and self._parent.is_cancelled)

    @property
    def reason(self) -> str:
        if self._event.is_set():
            return self._reason
        if self._parent and self._parent.is_cancelled:
            return self._parent.reason
        return ""

    def state(self) -> CancellationState:
        return CancellationState(self.is_cancelled, self.reason)

    def checkpoint(self) -> None:
        if self.is_cancelled:
            raise TaskCancelled(self.reason or "cancelled")

    def child(self) -> "CancellationToken":
        return CancellationToken(parent=self)
