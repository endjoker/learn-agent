# -*- coding: utf-8 -*-
"""Unified shell detection for bash / long-running process tools."""

import os

_IS_WINDOWS = os.name == "nt"


def is_windows() -> bool:
    return _IS_WINDOWS


def shell_name() -> str:
    """Human-readable shell name for display."""
    if _IS_WINDOWS:
        return "PowerShell"
    return os.path.basename(os.environ.get("SHELL", "/bin/bash"))


def shell_command(command: str) -> list[str]:
    """Return argv that executes ``command`` in the platform shell.

    Windows -> PowerShell (supports Unix aliases and pipes)
    Others  -> bash -c
    """
    if _IS_WINDOWS:
        return ["powershell", "-NoProfile", "-Command", command]
    return ["bash", "-c", command]
