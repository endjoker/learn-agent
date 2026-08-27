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
    # mkstemp 生成 0600 临时文件；目标已存在时记录其原始权限，替换后恢复，
    # 消除"原子写把 0644 配置改成 0600"的副作用。
    old_mode = None
    try:
        old_mode = target.stat().st_mode & 0o7777
    except OSError:
        pass
    try:
        try:
            handle = os.fdopen(fd, "wb")
        except Exception:
            # fdopen 失败时 fd 仍由我们持有，必须显式关闭，避免描述符泄漏窗口
            os.close(fd)
            raise
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if old_mode is not None:
            try:
                os.chmod(temp_name, old_mode)
            except OSError:
                # 权限恢复失败不阻断主写入（最坏情况目标为 0600）
                pass
        os.replace(temp_name, target)
        _fsync_dir(target.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def _fsync_dir(directory: Path) -> None:
    """fsync 目录，持久化 os.replace 的 rename 条目（POSIX）。

    打开 O_RDONLY 目录句柄并 fsync；不支持目录 fd 的平台（如 Windows）
    静默跳过，不影响写入结果。
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


def atomic_write_json(path: str | Path, data: Any, *, prefix: str = ".write-") -> None:
    """Serialize UTF-8 JSON through :func:`atomic_write_bytes`."""
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(path, content, prefix=prefix)
