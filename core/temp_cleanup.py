# -*- coding: utf-8 -*-
"""TTL cleanup for temporary output/download directories."""

import os
import time
from pathlib import Path

_AGENT_OUTPUT_PREFIX = "agent-output-"
# B4 spill 落盘（沙箱关路径）产生的转录文件前缀
_BASH_SPILL_PREFIX = "jk-bash-"
_BASH_SPILL_DIR = "jk-tool-spill"


def _safe_remove(path: Path) -> None:
    try:
        if path.is_dir():
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except Exception:
        pass


def cleanup_old_files(roots, max_age_seconds: int) -> int:
    """Remove files/dirs older than max_age_seconds under the given roots."""
    if max_age_seconds <= 0:
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        try:
            for child in root.iterdir():
                try:
                    st = child.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    _safe_remove(child)
                    removed += 1
        except Exception:
            continue
    return removed


def cleanup_agent_output_logs(tmp_root, max_age_seconds: int) -> int:
    """Remove old agent-output-*.log files created by large-output spill."""
    tmp_root = Path(tmp_root)
    if not tmp_root.exists():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        for child in tmp_root.iterdir():
            # 同时覆盖 bash spill 转录文件（jk-bash-*.log）与 jk-tool-spill/ 目录
            if child.name.startswith(_AGENT_OUTPUT_PREFIX) or child.name.startswith(_BASH_SPILL_PREFIX):
                try:
                    if child.stat().st_mtime < cutoff:
                        _safe_remove(child)
                        removed += 1
                except OSError:
                    continue
        spill_dir = tmp_root / _BASH_SPILL_DIR
        if spill_dir.is_dir():
            for child in spill_dir.iterdir():
                try:
                    if child.stat().st_mtime < cutoff:
                        _safe_remove(child)
                        removed += 1
                except OSError:
                    continue
    except Exception:
        pass
    return removed
