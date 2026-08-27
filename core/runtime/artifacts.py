"""Safe, durable artifacts for task and plan execution outputs."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from core.atomic_io import atomic_write_bytes
from .models import RuntimeEvent, utc_now
from .sqlite_store import RuntimeStore


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

# capture_file 流式复制块大小：64KB，内存占用有上界且哈希随写递增。
_STREAM_CHUNK_SIZE = 64 * 1024


def _fsync_dir(directory: Path) -> None:
    """fsync 目录，持久化 os.replace 的 rename 条目（POSIX）。

    与 core.atomic_io 内部实现保持一致；不引入对私有符号的跨模块依赖，
    便于并行团队独立演进。
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
    goal_id: Optional[str] = None
    plan_id: Optional[str] = None
    plan_task_id: Optional[str] = None
    team_id: Optional[str] = None
    child_session_id: Optional[str] = None
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
        # Relation fields are retained for Goal/Subagent projections and
        # reference-aware retention.
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
        if not session_id:
            raise ValueError("session_id is required")
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise ValueError("artifact source must be an existing file")
        size = source_path.stat().st_size
        if size > self.max_file_bytes:
            raise ValueError(f"artifact exceeds max_file_bytes ({self.max_file_bytes})")
        media_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        # L4#11: 不再整文件 read() 进内存，改为 64KB 分块流式复制到目标，
        # 写入同时增量计算 sha256（与 _create_bytes 的整块哈希结果一致）。
        artifact_id = f"artifact_{uuid4().hex}"
        safe_name, relative, destination = self._prepare_destination(
            name=name or source_path.name, plan_id=plan_id, task_id=task_id,
            artifact_id=artifact_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("rb") as handle:
            digest = self._atomic_write_stream(destination, handle)
        return self._finalize(
            session_id=session_id, artifact_id=artifact_id, name=safe_name, type=type,
            summary=summary, media_type=media_type, plan_id=plan_id,
            plan_task_id=plan_task_id, task_id=task_id, created_by=created_by,
            destination=destination, relative=relative, size=size, digest=digest,
        )

    def get(self, artifact_id: str) -> Optional[ArtifactRef]:
        data = self.runtime_store.get_artifact(artifact_id)
        return ArtifactRef.from_dict(data) if data else None

    def list(self, *, session_id: str | None = None, task_id: str | None = None,
             limit: int = 100, offset: int = 0) -> list[ArtifactRef]:
        return [ArtifactRef.from_dict(item) for item in self.runtime_store.list_artifacts(
            session_id=session_id, task_id=task_id, limit=limit, offset=offset)]

    def delete(self, artifact: ArtifactRef | str) -> bool:
        """Two-stage delete: filesystem first, then durable metadata."""
        ref = self.get(artifact) if isinstance(artifact, str) else artifact
        if ref is None:
            return False
        try:
            self.resolve_path(ref).unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        return self.runtime_store.delete_artifact(ref.artifact_id)

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
        safe_name, relative, destination = self._prepare_destination(
            name=name, plan_id=plan_id, task_id=task_id, artifact_id=artifact_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(destination, content)
        digest = hashlib.sha256(content).hexdigest()
        return self._finalize(
            session_id=session_id, artifact_id=artifact_id, name=safe_name, type=type,
            summary=summary, media_type=media_type, plan_id=plan_id,
            plan_task_id=plan_task_id, task_id=task_id, created_by=created_by,
            destination=destination, relative=relative, size=len(content), digest=digest,
        )

    def _prepare_destination(self, *, name: str, plan_id: str | None, task_id: str | None,
                             artifact_id: str) -> tuple[str, Path, Path]:
        """Compute safe artifact name / relative path / resolved destination."""
        safe_name = self._safe_name(name)
        relative = self._relative_path(plan_id=plan_id, task_id=task_id,
                                       artifact_id=artifact_id, name=safe_name)
        destination = (self.root / relative).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact destination escapes configured root") from exc
        return safe_name, relative, destination

    def _finalize(self, *, session_id: str, artifact_id: str, name: str, type: str,
                  summary: str, media_type: str, plan_id: str | None,
                  plan_task_id: str | None, task_id: str | None, created_by: str,
                  destination: Path, relative: Path, size: int, digest: str) -> ArtifactRef:
        """Persist metadata + event for an already-written artifact file."""
        ref = ArtifactRef(
            artifact_id=artifact_id, session_id=session_id, name=name, type=(type or "file").strip(),
            path=relative.as_posix(), media_type=media_type or "application/octet-stream", size=size,
            sha256=digest, summary=(summary or "")[:2000], plan_id=plan_id,
            plan_task_id=plan_task_id, task_id=task_id, created_by=created_by or "root",
        )
        try:
            # Domain events retain the source task in their data even when the
            # artifact is created before a runtime task record exists (for
            # example, a standalone Plan report). The FK column itself is
            # populated only for a durable runtime task. 不再为每次写入额外
            # get_task：任务行不存在时由 runtime_events.task_id 外键报
            # IntegrityError，去掉 task 关联重试一次。
            data = ref.to_dict()
            event = RuntimeEvent.create(
                "artifact.created", session_id=session_id, task_id=task_id,
                data={"artifact_id": ref.artifact_id, "plan_id": plan_id,
                      "plan_task_id": plan_task_id, "task_id": task_id,
                      "type": ref.type, "name": ref.name, "sha256": ref.sha256, "size": ref.size},
            )
            try:
                self.runtime_store.save_artifact(data, event)
            except sqlite3.IntegrityError:
                event = RuntimeEvent.create(
                    "artifact.created", session_id=session_id, task_id=None,
                    data={"artifact_id": ref.artifact_id, "plan_id": plan_id,
                          "plan_task_id": plan_task_id, "task_id": task_id,
                          "type": ref.type, "name": ref.name, "sha256": ref.sha256, "size": ref.size},
                )
                self.runtime_store.save_artifact(data, event)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return ref

    @staticmethod
    def _atomic_write_stream(destination: Path, source,
                             chunk_size: int = _STREAM_CHUNK_SIZE) -> str:
        """Stream-copy ``source`` into ``destination`` atomically, hashing incrementally.

        Mirrors :func:`core.atomic_io.atomic_write_bytes` crash-safety (temp
        file in the same directory + fsync + atomic replace + directory fsync)
        without materializing the whole file in memory. Returns the sha256
        accumulated over the chunks, identical to hashing the full bytes.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".artifact-", suffix=".tmp", dir=destination.parent)
        temp_path = Path(temp_name)
        old_mode = None
        try:
            try:
                old_mode = destination.stat().st_mode & 0o7777
            except OSError:
                pass
            try:
                handle = os.fdopen(fd, "wb")
            except Exception:
                # fdopen 失败时 fd 仍由我们持有，必须显式关闭，避免描述符泄漏窗口
                os.close(fd)
                raise
            hasher = hashlib.sha256()
            with handle:
                while True:
                    chunk = source.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if old_mode is not None:
                try:
                    os.chmod(temp_name, old_mode)
                except OSError:
                    pass
            os.replace(temp_name, destination)
            _fsync_dir(destination.parent)
        finally:
            temp_path.unlink(missing_ok=True)
        return hasher.hexdigest()

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
