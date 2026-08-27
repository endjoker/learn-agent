# -*- coding: utf-8 -*-
"""Compatibility security gate backed by the unified PolicyEngine."""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from core.permission import PermissionChecker, ALLOW, ASK, DENY
from core.policy_engine import PolicyEngine
from core.sandbox.guard import check_command_safety, check_python_code, check_proc_send_input

logger = logging.getLogger("jk_agent")


class SecurityGate:
    """L2 硬检查（默认关闭）→ 4 档权限决策。

    沙箱未挂载或 sandbox.enabled=false 时只做 PolicyEngine 权限决策；
    开启后先跑非覆盖式内容检查（命令/代码/进程/写盘/外发），再决策。
    """

    def __init__(self, permission: PermissionChecker, sandbox=None,
                 workspace: Optional[str] = None):
        self.permission = permission
        self.sandbox = sandbox
        self.workspace = workspace or str(permission.workspace)

    def check(self, tool, params: dict, tool_name: Optional[str] = None) -> Tuple[str, str]:
        name = tool_name or (getattr(tool, "name", "") if tool else "") or ""
        caps = self._resolve_caps(tool, params, name)
        hard = self._hard_checks(caps, params or {}, name)
        if hard is not None:
            ok, reason = hard
            if not ok:
                logger.info("SecurityGate hard deny [%s]: %s", name, reason)
                return DENY, reason
        decision = self.permission.decide(name, params or {}, caps)
        return decision.level, decision.reason

    @staticmethod
    def _resolve_caps(tool, params, name: str = "") -> tuple:
        if tool is not None:
            try:
                resolved = tool.resolve_capabilities(params)
                if resolved:
                    return tuple(dict.fromkeys(resolved))
            except Exception:
                logger.warning("capability resolver failed for %s", name, exc_info=True)
            static = tuple(getattr(tool, "capabilities", ()) or ())
            if static:
                return static
        return PolicyEngine.infer_capabilities(name, params or {})

    def _l2_active(self) -> bool:
        sb = self.sandbox
        return bool(sb is not None and getattr(sb, "enabled", False)
                    and not getattr(sb, "is_bypass_active", False))

    def _hard_checks(self, caps: tuple, params: dict, name: str):
        # L2 沙箱硬闸门默认关闭：沙箱未挂载或 sandbox.enabled=false 时，
        # 命令/代码/进程内容检查与写盘/外发检查整体跳过，交回 4 档
        # PolicyEngine 裁决（readonly/ask/allow/unreviewed 仍是唯一权限层；
        # 高危命令、只读禁执行等策略级拒绝不受影响）。
        if not self._l2_active():
            return None
        cap_set = set(caps)
        command = params.get("command")
        if "exec:shell" in cap_set and command:
            ok, reason = check_command_safety(str(command), "bash", self.workspace,
                                              check_policy_paths=False)
            if not ok:
                return False, reason
        if "exec:code" in cap_set and params.get("code"):
            ok, reason = check_python_code(str(params["code"]))
            if not ok:
                return False, reason
        if "proc:manage" in cap_set and params.get("input"):
            ok, reason = check_proc_send_input(str(params["input"]))
            if not ok:
                return False, reason
        if cap_set & {"fs:write", "fs:edit", "fs:delete", "fs:move"}:
            content = params.get("content") or params.get("new_string") or ""
            for path in self.permission.engine.extract_paths(params):
                ok, reason = self.sandbox.check_write_file(
                    path, str(content), check_policy_paths=self.permission.permission_mode != "unreviewed")
                if not ok:
                    return False, reason
        if "net:egress" in cap_set:
            url = params.get("url") or params.get("uri")
            if url:
                ok, reason = self.sandbox.check_egress(str(url))
                if not ok:
                    return False, reason
        return None
