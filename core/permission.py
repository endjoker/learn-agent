"""Compatibility facade for the unified :mod:`core.policy_engine` policy."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Union

from core.policy_engine import ALLOW, ASK, DENY, PolicyDecision, PolicyEngine, VALID_MODES
from core.sandbox.guard import _match_dangerous

_UNSET = object()
READONLY_COMMANDS: list[str] = []  # retained import compatibility; no runtime semantics
WRITE_COMMANDS: list[str] = []     # retained import compatibility; no runtime semantics


def resolve_path(path_str: str, workspace: Path) -> Path:
    path = Path(path_str).expanduser()
    return (workspace / path).resolve(strict=False) if not path.is_absolute() else path.resolve(strict=False)


def is_within_workspace(path: Path, workspace: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(workspace.resolve(strict=False))
        return True
    except ValueError:
        return False


def classify_bash_command(command: str) -> str:
    """Legacy helper: execution is never classified as a permission-free read."""
    return DENY if _match_dangerous((command or "").lower()) else ASK


class PermissionChecker:
    """Legacy API backed by a single capability/path based :class:`PolicyEngine`.

    ``set_rule``/``get_rule`` remain available for integrations that use them as
    registration metadata. They cannot override the effective mode policy.
    """

    def __init__(self, workspace: str | None = None, config: dict | None = None,
                 extra_workspaces: object = _UNSET, working_directory: str | None = None):
        if config is None:
            try:
                from core.config_loader import load_config
                config = load_config().get("permission", {})
            except Exception:
                config = {}
        config = config or {}
        try:
            from core.config_loader import _find_project_root
            root = _find_project_root()
        except Exception:
            root = Path.cwd()
        if workspace:
            self.workspace = Path(workspace).expanduser().resolve()
        elif config.get("workspace"):
            candidate = Path(str(config["workspace"])).expanduser()
            self.workspace = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        else:
            self.workspace = root.resolve()
        if extra_workspaces is not _UNSET and extra_workspaces is not None:
            extra = list(extra_workspaces)
        else:
            extra = list(config.get("extra_workspaces", []))
        roots = []
        for value in extra:
            path = Path(str(value)).expanduser()
            roots.append((root / path).resolve() if not path.is_absolute() else path.resolve())
        self._extra_roots = roots
        self._permission_mode = str(config.get("default_mode") or "ask")
        if self._permission_mode not in VALID_MODES:
            self._permission_mode = "ask"
        self._rules: dict[str, Union[str, Callable]] = {}
        self._workspace_trusted = False
        self.engine = PolicyEngine(
            project_root=self.workspace,
            working_directory=working_directory or self.workspace,
            extra_workspace_roots=self._extra_roots,
            mode=self._permission_mode,
        )
        self._legacy_config_keys = tuple(
            key for key in ("tool_rules", "bash_commands") if config.get(key))

    def set_permission_mode(self, mode: str) -> None:
        self._permission_mode = mode if mode in VALID_MODES else "ask"
        self.engine.set_mode(self._permission_mode)

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def default_mode(self) -> str:
        return self._permission_mode

    @default_mode.setter
    def default_mode(self, mode: str) -> None:
        self.set_permission_mode(mode)

    def is_unreviewed_mode(self) -> bool:
        return self._permission_mode == "unreviewed"

    def decide(self, tool_name: str, params: dict | None = None,
               capabilities=()) -> PolicyDecision:
        return self.engine.decide(tool_name, params or {}, capabilities)

    def check(self, tool_name: str, params: dict | None = None,
              capabilities=()) -> str:
        return self.decide(tool_name, params, capabilities).level

    def set_rule(self, tool_name: str, rule: Union[str, Callable]):
        """Store a legacy registration rule without changing effective policy."""
        self._rules[tool_name] = rule

    def get_rule(self, tool_name: str) -> Optional[Union[str, Callable]]:
        return self._rules.get(tool_name)

    def _init_default_rules(self, config: dict | None = None):
        """Deprecated no-op retained for callers during migration."""
        return None

    def allow_workspace(self):
        """Compatibility alias for the ``allow`` mode; outside operations still ASK."""
        self._workspace_trusted = True
        self.set_permission_mode("allow")

    def is_workspace_trusted(self) -> bool:
        return self._workspace_trusted or self._permission_mode in ("allow", "unreviewed")

    def describe_rules(self) -> dict:
        return {
            "tools": {},
            "meta": {
                "workspace": str(self.workspace),
                "extra_workspace_roots": [str(p) for p in self._extra_roots],
                "workspace_trusted": self.is_workspace_trusted(),
                "permission_mode": self._permission_mode,
                "legacy_config_ignored": list(self._legacy_config_keys),
            },
        }

    def format_permission_info(self, tool_name: str, params: dict,
                               level: str, result: str | None = None) -> str:
        labels = {ALLOW: "✅ 允许", ASK: "❓ 需要确认", DENY: "⛔ 已拒绝"}
        lines = [f"  🔒 权限检查: {labels.get(level, level)}", f"     工具: {tool_name}"]
        for key, value in (params or {}).items():
            text = str(value)
            lines.append(f"     {key}: {text[:100]}{'...' if len(text) > 100 else ''}")
        if result:
            lines.append(f"     → {result}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return f"PermissionChecker(workspace={self.workspace}, mode={self._permission_mode})"
