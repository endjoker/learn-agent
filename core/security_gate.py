# -*- coding: utf-8 -*-
"""
中央安全闸门 —— 在 agent action 执行点统一跑 L1 权限 + L2 沙箱

设计动机：
    L2 沙箱原本是 opt-in（每个工具在 execute() 内自己调 check_*），
    file_mgr / skill / MCP 没接就绕过。本模块把 L2 上移到与 L1 同一个
    执行点——对每次工具调用（内置/skill/MCP/未来）都按 capability 跑
    L1+L2，覆盖不再依赖工具自觉。

    L2 检查按工具声明的 capability 选；L1 策略按工具名查 PermissionChecker，
    未知工具名默认 ASK（修复原 trusted 时未知工具自动 ALLOW 的缺口）。

调用方：agent 的 run / stream_run / _run_task_list 在执行每个 action 前调用
    level, reason = gate.check(tool, params, tool_name)
    level ∈ {ALLOW, ASK, DENY}
"""

import logging
from typing import Optional, Tuple

from core.permission import PermissionChecker, ALLOW, ASK, DENY
from core.sandbox.guard import check_command_safety, check_python_code, check_proc_send_input

logger = logging.getLogger("hello_agent")


class SecurityGate:
    """统一安全闸门：L1 权限 + L2 沙箱内容检查。"""

    def __init__(
        self,
        permission: PermissionChecker,
        sandbox=None,            # SandboxExecutor | None
        workspace: Optional[str] = None,
    ):
        self.permission = permission
        self.sandbox = sandbox
        self.workspace = workspace or ""

    # ================================================================
    # 主入口
    # ================================================================

    def check(self, tool, params: dict, tool_name: Optional[str] = None) -> Tuple[str, str]:
        """
        对一次工具调用做安全检查。

        参数:
            tool:      BaseTool 实例（可能为 None，表示未知工具）
            params:    工具参数
            tool_name: 工具名（LLM 给的；可能与 tool.name 一致）

        返回:
            (level, reason) — level ∈ {ALLOW, ASK, DENY}
        """
        name = tool_name or (tool.name if tool else "") or ""
        caps = self._resolve_caps(tool, params)

        # ---- L2 内容硬拦（按 capability + 参数形状）----
        if self._l2_active():
            l2 = self._l2_check(caps, params, name)
            if l2 is not None:
                ok, reason = l2
                if not ok:
                    logger.info(f"SecurityGate L2 拦截 [{name}]: {reason}")
                    return DENY, reason

        # ---- L1 策略 ----
        return self._policy(name, params, caps, tool)

    # ================================================================
    # 内部
    # ================================================================

    @staticmethod
    def _resolve_caps(tool, params) -> tuple:
        """取工具能力（支持 resolve_capabilities 钩子，按 action 决定）"""
        if tool is None:
            return ()
        try:
            caps = tool.resolve_capabilities(params)
            return tuple(caps or ())
        except Exception:
            return tuple(getattr(tool, "capabilities", ()) or ())

    def _l2_active(self) -> bool:
        """L2 是否生效（沙箱开启且未临时绕过）"""
        sb = self.sandbox
        if sb is None:
            return False
        if not getattr(sb, "enabled", False):
            return False
        if getattr(sb, "is_bypass_active", False):
            return False
        return True

    def _l2_check(self, caps: tuple, params: dict, name: str):
        """
        按 capability 跑 L2 内容检查。

        返回:
            (ok, reason) — 有对应检查时
            None —— 该工具无对应 L2 检查（交给 L1）
        """
        cap_set = set(caps)
        # 文件写入类：path/file_path/dest/paths
        if cap_set & {"fs:write", "fs:delete", "fs:move"}:
            content = params.get("content", "") or ""
            checked = []
            for key in ("path", "file_path", "dest"):
                v = params.get(key)
                if v:
                    checked.append(v)
            paths = params.get("paths") or []
            if isinstance(paths, list):
                checked.extend(p for p in paths if p)
            if not checked:
                return None  # 没有路径参数，交给 L1
            for p in checked:
                ok, reason = self.sandbox.check_write_file(p, content)
                if not ok:
                    return False, reason
            return None  # 路径都通过 L2，继续走 L1

        # 网络外发
        if "net:egress" in caps:
            url = params.get("url") or params.get("uri")
            if not url:
                return None
            ok, reason = self.sandbox.check_egress(url)
            return (ok, reason) if not ok else None

        # shell 命令
        if "exec:shell" in caps:
            cmd = params.get("command")
            if not cmd:
                return None
            ok, reason = check_command_safety(cmd, "bash", self.workspace)
            return (ok, reason) if not ok else None

        # python 代码
        if "exec:code" in caps:
            code = params.get("code")
            if not code:
                return None
            ok, reason = check_python_code(code)
            return (ok, reason) if not ok else None

        # proc_send 内容检查（投喂到 REPL 的 input，shell+python 危险模式）
        # 注意：此处硬编码 params["input"] 与 proc_send 的参数名耦合；
        # 若未来新增 proc:manage 工具用了不同参数名（如 data/content），
        # 需同步更新此处或改为按 capability 注册检查函数。
        if "proc:manage" in cap_set:
            inp = params.get("input")
            if not inp:
                return None   # 无 input（如 proc_stop）→ 交 L1
            ok, reason = check_proc_send_input(inp)
            return (ok, reason) if not ok else None

        return None

    def _policy(self, name: str, params: dict, caps: tuple, tool) -> Tuple[str, str]:
        """L1 策略：MCP trust / 已知规则 / 未知→ASK"""
        # MCP 远程工具：按服务器 trust 标志
        if "remote:call" in caps:
            if tool is not None and getattr(tool, "trust", False):
                return ALLOW, ""
            return ASK, "MCP 远程工具需确认（可在 config.json 的 mcp.servers 中配 trust:true 放行）"

        # 未知工具名 → ASK（修复原 trusted 时自动 ALLOW 的缺口）
        if self.permission.get_rule(name) is None:
            return ASK, f"未知工具 '{name}'，需确认"

        level = self.permission.check(name, params)
        return level, ""
