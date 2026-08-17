# -*- coding: utf-8 -*-
"""Orphan child-process journal + startup reaping.

Append-only JSONL records each spawned pid with its owner pid and process start
identity, so a later Gateway process can kill children left behind by a crash
without being fooled by pid reuse.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

_JOURNAL = Path(__file__).resolve().parent.parent / ".agent" / "orphan-processes.jsonl"


def _journal_path() -> Path:
    return _JOURNAL


def process_start_id(pid: int) -> str | None:
    """Return a stable identity for a pid that changes across pid reuse."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-Command", f"([System.Diagnostics.Process]::GetProcessById({pid})).StartTime.ToUniversalTime().Ticks"],
                capture_output=True, text=True, timeout=5,
            )
            s = out.stdout.strip()
            return f"win:{s}" if s.isdigit() else None
        except Exception:
            return None
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            data = f.read()
        end = data.rfind(")")
        fields = data[end + 2:].split()
        if len(fields) > 19:
            return f"proc:{fields[19]}"
    except Exception:
        pass
    return None


def record(pid: int, active: bool) -> None:
    """Append a journal entry. Never raises; tracking must not break a command."""
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        rec = {
            "version": 1,
            "pid": pid,
            "owner_pid": os.getpid(),
            "active": bool(active),
            "process_start_id": process_start_id(pid) if active else None,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        p = _journal_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def _is_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=5)
            return str(pid) in out.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_tree_pid(pid: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=8)
        except Exception:
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def reap_stale_orphans() -> int:
    """Kill active children whose owning Gateway process is no longer alive."""
    p = _journal_path()
    if not p.exists():
        return 0
    latest: dict[int, dict] = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r.get("pid"), int):
                    latest[r["pid"]] = r
    except Exception:
        return 0

    current_owner = os.getpid()
    killed = 0
    for pid, r in latest.items():
        if not r.get("active"):
            continue
        owner = r.get("owner_pid")
        if owner == current_owner:
            continue
        if isinstance(owner, int) and _is_alive(owner):
            continue
        # Child pid may have been reused; only kill when identity still matches.
        if r.get("process_start_id"):
            cur = process_start_id(pid)
            if cur is None or cur != r["process_start_id"]:
                continue
        if _is_alive(pid):
            _kill_tree_pid(pid)
            killed += 1
    return killed
