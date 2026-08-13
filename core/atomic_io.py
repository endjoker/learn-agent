"""Small crash-safe write primitives shared by durable local stores."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: str | Path, content: bytes, *, prefix: str = ".write-") -> None:
    """Write bytes, fsync them, then atomically replace the destination."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_json(path: str | Path, data: Any, *, prefix: str = ".write-") -> None:
    """Serialize UTF-8 JSON through :func:`atomic_write_bytes`."""
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(path, content, prefix=prefix)
