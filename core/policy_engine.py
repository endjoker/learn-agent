"""Unified capability, path, execution, and network permission policy."""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from core.sandbox.guard import _is_system_path, _match_dangerous

ALLOW = "allow"
ASK = "ask"
DENY = "deny"
VALID_MODES = ("readonly", "ask", "allow", "unreviewed")

FILE_READ_CAPS = frozenset({"fs:read", "proc:read"})
FILE_MUTATION_CAPS = frozenset({"fs:write", "fs:edit", "fs:delete", "fs:move"})
EXEC_CAPS = frozenset({"exec:shell", "exec:code", "proc:manage"})
NETWORK_CAPS = frozenset({"net:egress", "remote:call"})

_TOOL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "read": ("fs:read",), "grep": ("fs:read",), "glob": ("fs:read",),
    "write": ("fs:write",), "edit": ("fs:edit",), "bash": ("exec:shell",),
    "python": ("exec:code",), "http": ("net:egress",),
    "search": ("net:egress",), "web_fetch": ("net:egress",),
    "proc_start": ("proc:manage", "exec:shell"), "proc_send": ("proc:manage",),
    "proc_read": ("proc:read",), "proc_list": ("proc:read",),
    "proc_stop": ("proc:manage",),
}

_PATH_KEYS = ("file_path", "path", "source", "src", "dest", "destination", "cwd")
_SHELL_PATH_RE = re.compile(r"(?:^|\s|[;&|<>])((?:/|~(?:/|$)|[A-Za-z]:[\\/]|\\\\)[^\s;&|<>]*)")


@dataclass(frozen=True)
class PolicyDecision:
    level: str
    rule_id: str
    reason: str = ""
    paths: tuple[str, ...] = ()
    path_scope: str = "none"
    capabilities: tuple[str, ...] = ()
    requires_approval: bool = False

    def __post_init__(self) -> None:
        if self.level not in (ALLOW, ASK, DENY):
            raise ValueError(f"invalid policy level: {self.level}")
        object.__setattr__(self, "requires_approval", self.level == ASK)


class PolicyEngine:
    """One policy implementation shared by every session and task type."""

    def __init__(self, *, project_root: str | Path, working_directory: str | Path | None = None,
                 extra_workspace_roots: Sequence[str | Path] = (), mode: str = "ask"):
        self.project_root = Path(project_root).expanduser().resolve()
        self.working_directory = Path(working_directory or self.project_root).expanduser().resolve()
        self.extra_workspace_roots = tuple(Path(p).expanduser().resolve() for p in (extra_workspace_roots or ()))
        self.set_mode(mode)

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return (self.project_root, *self.extra_workspace_roots)

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in VALID_MODES else "ask"

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = self.working_directory / path
        return path.resolve(strict=False)

    def is_workspace_path(self, path: str | Path) -> bool:
        resolved = self.resolve_path(path)
        return any(_relative_to(resolved, root) for root in self.allowed_roots)

    def decide(self, tool_name: str, params: dict[str, Any] | None = None,
               capabilities: Iterable[str] = ()) -> PolicyDecision:
        params = dict(params or {})
        caps = tuple(dict.fromkeys(capabilities or self.infer_capabilities(tool_name, params)))
        paths = self.extract_paths(params)

        command = str(params.get("command") or "")
        if "exec:shell" in caps and command:
            dangerous = _match_dangerous(command.lower())
            if dangerous:
                return self._decision(DENY, "hard.high_risk_command",
                                      f"检测到高危命令，已拒绝: {dangerous}", paths, caps)
            shell_paths = self.extract_shell_paths(command)
            paths = tuple(dict.fromkeys((*paths, *shell_paths)))

        resolved_paths = tuple(self.resolve_path(p) for p in paths)
        cap_set = set(caps)

        # 系统路径：非 unreviewed 下需要确认（四档语义）。
        if any(_is_system_path(p) for p in resolved_paths):
            if self.mode != "unreviewed":
                return self._decision(ASK, "path.system", "系统路径操作需要确认", paths, caps)

        if cap_set & NETWORK_CAPS:
            level = ALLOW if self.mode in {"allow", "unreviewed"} else ASK
            return self._decision(level, f"mode.{self.mode}.network",
                                  "网络或远程访问需要确认" if level == ASK else "免审模式允许网络访问",
                                  paths, caps)

        if cap_set & EXEC_CAPS:
            if self.mode == "readonly":
                return self._decision(DENY, "mode.readonly.execution", "只读模式禁止执行命令、代码或管理进程", paths, caps)
            if self.mode == "ask":
                return self._decision(ASK, "mode.ask.execution", "执行命令、代码或管理进程需要确认", paths, caps)
            if self.mode == "allow" and self._paths_outside_workspace(resolved_paths):
                return self._decision(ASK, "mode.allow.external_execution", "工作区外执行需要确认", paths, caps)
            if self.mode in {"allow", "unreviewed"}:
                return self._decision(ALLOW, f"mode.{self.mode}.execution", paths=paths, caps=caps)

        if cap_set & FILE_MUTATION_CAPS:
            if self.mode == "readonly":
                return self._decision(DENY, "mode.readonly.mutation", "只读模式禁止写入、编辑、删除或移动", paths, caps)
            if self.mode == "ask":
                return self._decision(ASK, "mode.ask.mutation", "写入、编辑、删除或移动需要确认", paths, caps)
            if self.mode == "allow" and self._paths_outside_workspace(resolved_paths):
                return self._decision(ASK, "mode.allow.external_mutation", "工作区外副作用操作需要确认", paths, caps)
            return self._decision(ALLOW, f"mode.{self.mode}.mutation", paths=paths, caps=caps)

        if cap_set & FILE_READ_CAPS:
            return self._decision(ALLOW, f"mode.{self.mode}.read", paths=paths, caps=caps)

        # Capability-less tools are treated as pure local operations. State-changing
        # tools must declare a capability. Fold unknowns into the four-mode
        # semantics instead of blanket-ALLOW: readonly → DENY (only known-pure
        # tools stay allowed), ask → ASK (confirm unclassified tools), and
        # allow/unreviewed keep ALLOW (trusted modes run them without friction).
        if self.mode == "readonly" and tool_name not in _PURE_TOOLS:
            return self._decision(DENY, "mode.readonly.unknown", f"只读模式拒绝未分类工具 '{tool_name}'", paths, caps)
        if self.mode == "ask" and tool_name not in _PURE_TOOLS:
            return self._decision(ASK, "mode.ask.unknown", f"未分类工具 '{tool_name}' 需要确认", paths, caps)
        return self._decision(ALLOW, f"mode.{self.mode}.pure", paths=paths, caps=caps)

    @staticmethod
    def infer_capabilities(tool_name: str, params: dict[str, Any]) -> tuple[str, ...]:
        if tool_name == "file_mgr":
            action = str(params.get("action") or "").lower()
            if action in ("ls", "list"):
                return ("fs:read",)
            if action == "delete":
                return ("fs:delete",)
            if action in ("move", "mv", "rename"):
                return ("fs:move",)
            return ("fs:write",)
        return _TOOL_CAPABILITIES.get(tool_name, ())

    @staticmethod
    def extract_paths(params: dict[str, Any]) -> tuple[str, ...]:
        values: list[str] = []
        for key in _PATH_KEYS:
            value = params.get(key)
            if isinstance(value, (str, Path)) and str(value):
                values.append(str(value))
        batch = params.get("paths")
        if isinstance(batch, (list, tuple)):
            values.extend(str(v) for v in batch if isinstance(v, (str, Path)) and str(v))
        return tuple(dict.fromkeys(values))

    @staticmethod
    def extract_shell_paths(command: str) -> tuple[str, ...]:
        paths = [m.group(1).strip("'\"") for m in _SHELL_PATH_RE.finditer(command)]
        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            tokens = []
        for token in tokens:
            cleaned = token.strip("'\"")
            if cleaned.startswith(("/", "~/", "./", "../", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", cleaned):
                paths.append(cleaned)
        # 过滤 shell 重定向/设备目标：/dev/* 是设备/特殊文件（/dev/null、/dev/zero、
        # /dev/tty 等），几乎总用于输出丢弃或特殊流，不是数据文件路径。若不排除，
        # `cmd 2>/dev/null` 这类纯重定向会被当成"工作区外路径"，导致 allow 模式误弹确认框。
        paths = [p for p in paths if not (p.startswith("/dev/") or p == "/dev")]
        return tuple(dict.fromkeys(paths))

    def _paths_outside_workspace(self, paths: tuple[Path, ...]) -> bool:
        return bool(paths) and any(not any(_relative_to(path, root) for root in self.allowed_roots) for path in paths)

    @staticmethod
    def _decision(level: str, rule_id: str, reason: str = "", paths: Iterable[str] = (),
                  caps: Iterable[str] = ()) -> PolicyDecision:
        path_tuple = tuple(str(p) for p in paths)
        return PolicyDecision(level=level, rule_id=rule_id, reason=reason,
                              paths=path_tuple, path_scope="multiple" if len(path_tuple) > 1 else ("single" if path_tuple else "none"),
                              capabilities=tuple(caps))


def _relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


_PURE_TOOLS = frozenset({
    "datetime", "calculate", "notes", "memory_search", "memory_update",
    "create_skill", "skill", "think",
})
