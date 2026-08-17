# -*- coding: utf-8 -*-
"""TTL cleanup for temporary output/download directories."""

import os
import time
from pathlib import Path

_AGENT_OUTPUT_PREFIX = "agent-output-"


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
            if child.name.startswith(_AGENT_OUTPUT_PREFIX):
                try:
                    if child.stat().st_mtime < cutoff:
                        _safe_remove(child)
                        removed += 1
                except OSError:
                    continue
    except Exception:
        pass
    return removed
