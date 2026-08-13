"""Safe, durable artifacts for task and plan execution outputs."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from core.atomic_io import atomic_write_bytes
from .models import RuntimeEvent, utc_now
from .sqlite_store import RuntimeStore


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    session_id: str
    name: str
    type: str
    path: str
    media_type: str
    size: int
    sha256: str
    summary: str = ""
    plan_id: Optional[str] = None
    plan_task_id: Optional[str] = None
    task_id: Optional[str] = None
    created_by: str = "root"
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.session_id or not self.name or not self.path:
            raise ValueError("artifact_id, session_id, name and path are required")
        if self.size < 0 or not self.sha256:
            raise ValueError("artifact size and sha256 are required")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactRef":
        data = dict(value)
        # Read artifacts written by earlier releases without exposing retired
        # relations to current callers.
        data.pop("goal_id", None)
        data.pop("team_id", None)
        return cls(**data)


class ArtifactStore:
    """Write-once local artifact store with DB-backed metadata and safe paths."""

    def __init__(self, runtime_store: RuntimeStore, root: str | Path | None = None,
                 *, max_file_bytes: int = 50 * 1024 * 1024):
        self.runtime_store = runtime_store
        # Runtime DB is normally ``workspace/.agent/state/runtime.db``.
        self.root = (Path(root) if root else runtime_store.path.parent.parent / "artifacts").expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max(1, int(max_file_bytes))

    def create_text(self, *, session_id: str, name: str, content: str, type: str = "report",
                    summary: str = "", media_type: str = "text/markdown",
                    plan_id: str | None = None, plan_task_id: str | None = None,
                    task_id: str | None = None, created_by: str = "root") -> ArtifactRef:
        data = content.encode("utf-8")
        return self._create_bytes(
            session_id=session_id, name=name, content=data, type=type, summary=summary,
            media_type=media_type, plan_id=plan_id, plan_task_id=plan_task_id,
            task_id=task_id, created_by=created_by,
        )

    def capture_file(self, *, session_id: str, source: str | Path, name: str | None = None,
                     type: str = "file", summary: str = "", plan_id: str | None = None,
                     plan_task_id: str | None = None, task_id: str | None = None,
                     created_by: str = "root") -> ArtifactRef:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise ValueError("artifact source must be an existing file")
        size = source_path.stat().st_size
        if size > self.max_file_bytes:
            raise ValueError(f"artifact exceeds max_file_bytes ({self.max_file_bytes})")
        with source_path.open("rb") as handle:
            content = handle.read()
        media_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        return self._create_bytes(
            session_id=session_id, name=name or source_path.name, content=content, type=type,
            summary=summary, media_type=media_type, plan_id=plan_id,
            plan_task_id=plan_task_id, task_id=task_id, created_by=created_by,
        )

    def get(self, artifact_id: str) -> Optional[ArtifactRef]:
        data = self.runtime_store.get_artifact(artifact_id)
        return ArtifactRef.from_dict(data) if data else None

    def list(self, *, session_id: str | None = None, task_id: str | None = None,
             limit: int = 100) -> list[ArtifactRef]:
        return [ArtifactRef.from_dict(item) for item in self.runtime_store.list_artifacts(
            session_id=session_id, task_id=task_id, limit=limit)]

    def resolve_path(self, artifact: ArtifactRef | str) -> Path:
        ref = self.get(artifact) if isinstance(artifact, str) else artifact
        if ref is None:
            raise KeyError(artifact)
        path = (self.root / ref.path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact path escapes configured root") from exc
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _create_bytes(self, *, session_id: str, name: str, content: bytes, type: str,
                      summary: str, media_type: str, plan_id: str | None,
                      plan_task_id: str | None, task_id: str | None,
                      created_by: str) -> ArtifactRef:
        if not session_id:
            raise ValueError("session_id is required")
        if len(content) > self.max_file_bytes:
            raise ValueError(f"artifact exceeds max_file_bytes ({self.max_file_bytes})")
        artifact_id = f"artifact_{uuid4().hex}"
        safe_name = self._safe_name(name)
        relative = self._relative_path(plan_id=plan_id, task_id=task_id,
                                       artifact_id=artifact_id, name=safe_name)
        destination = (self.root / relative).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact destination escapes configured root") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(destination, content)
        digest = hashlib.sha256(content).hexdigest()
        ref = ArtifactRef(
            artifact_id=artifact_id, session_id=session_id, name=safe_name, type=(type or "file").strip(),
            path=relative.as_posix(), media_type=media_type or "application/octet-stream", size=len(content),
            sha256=digest, summary=(summary or "")[:2000], plan_id=plan_id,
            plan_task_id=plan_task_id, task_id=task_id, created_by=created_by or "root",
        )
        try:
            # Domain events retain the source task in their data even when the
            # artifact is created before a runtime task record exists (for
            # example, a standalone Plan report). The FK column itself is
            # populated only for a durable runtime task.
            event_task_id = task_id if task_id and self.runtime_store.get_task(task_id) else None
            self.runtime_store.save_artifact(
                ref.to_dict(), RuntimeEvent.create(
                    "artifact.created", session_id=session_id, task_id=event_task_id,
                    data={"artifact_id": ref.artifact_id, "plan_id": plan_id,
                          "plan_task_id": plan_task_id, "task_id": task_id,
                          "type": ref.type, "name": ref.name, "sha256": ref.sha256, "size": ref.size},
                ),
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return ref

    @staticmethod
    def _safe_name(name: str) -> str:
        value = _SAFE_NAME.sub("-", Path(name or "artifact").name).strip(".-")
        return (value or "artifact")[:120]

    @staticmethod
    def _relative_path(*, plan_id: str | None, task_id: str | None,
                       artifact_id: str, name: str) -> Path:
        pieces = ["standalone"]
        if plan_id:
            pieces.extend(["plans", plan_id])
        if task_id:
            pieces.extend(["tasks", task_id])
        pieces.append(f"{artifact_id}-{name}")
        return Path(*[str(item) for item in pieces if item])

    @staticmethod
    def _atomic_write(destination: Path, content: bytes) -> None:
        atomic_write_bytes(destination, content, prefix=".artifact-")
