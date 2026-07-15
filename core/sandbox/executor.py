# -*- coding: utf-8 -*-
"""
L2 沙箱执行器 —— 两层防护：内容拦截 → 子进程执行

架构:
    ┌─ SandboxExecutor.run() ──────────────────────────┐
    │  L2-A: content guard  (始终运行, 纯 Python)       │
    │  L2-C: subprocess     (OS 执行层 + 超时控制)      │
    └──────────────────────────────────────────────────┘

L2-B 资源隔离层（nanosandbox）为设计预留，暂未实现。
沙箱关闭时（enabled=False），仅保留 L1 权限检查，L2 全部绕过。
"""

import os
import subprocess
import logging
from pathlib import Path

from .guard import (
    check_command_safety,
    sanitize_output,
    check_python_code,
    sanitize_env,
    check_network_target,
    SENSITIVE_FILES as SENSITIVE_FILE_PATTERNS,
    _is_system_path,
    _is_within_workspace,
)
from .audit import log_interception, log_bypass, log_error
from . import profiles as profile_loader

logger = logging.getLogger("hello_agent")


class SandboxResult:
    """沙箱执行结果"""

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        timeout: bool = False,
        blocked: bool = False,
        block_reason: str = "",
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timeout = timeout
        self.blocked = blocked
        self.block_reason = block_reason

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timeout and not self.blocked

    def to_message(self) -> str:
        """转换为工具返回消息"""
        if self.blocked:
            return f"⛔ 沙箱拦截: {self.block_reason}"
        if self.timeout:
            return f"⏰ 执行超时"
        parts = []
        if self.stdout:
            parts.append(self.stdout.rstrip()[:8000])
        if self.stderr:
            parts.append(f"⚠️ 错误:\n{self.stderr.rstrip()[:2000]}")
        if not parts:
            parts.append("（执行完毕，无输出）")
        return "\n".join(parts)


class SandboxExecutor:
    """
    沙箱执行器

    两层防护：
      L2-A: 内容拦截（敏感文件/防泄露/系统路径/Python AST/网络黑名单）
      L2-C: subprocess 执行 + 超时控制 + 输出脱敏

    L2-B 资源隔离层（nanosandbox）为设计预留，暂未实现。

    沙箱关闭时（enabled=False）:
      L2 全部绕过，直接 subprocess 执行

    绕过机制（bypass_once）:
      开启后下一条命令绕过 L2-A，执行后自动恢复
    """

    def __init__(
        self,
        workspace: str | None = None,
    ):
        self._workspace = Path(workspace or os.getcwd()).resolve()
        self._config = profile_loader.load_config()

        # 沙箱开关
        self.enabled: bool = self._config.get("enabled", True)
        self.current_profile: str = self._config.get("default_profile", "agent")

        # 临时绕过标志（下一条命令绕过，执行后自动复位）
        self._bypass_once: bool = False

        logger.info(
            "沙箱执行器初始化: enabled=%s, profile=%s",
            self.enabled,
            self.current_profile,
        )

    # ================================================================
    # 配置管理
    # ================================================================

    def _get_current_profile(self) -> dict | None:
        """获取当前配置档"""
        return profile_loader.get_profile(self._config, self.current_profile)

    def _get_timeout(self) -> int:
        """获取当前超时设置"""
        prof = self._get_current_profile()
        return prof.get("timeout_seconds", 60) if prof else 60

    def set_profile(self, name: str) -> str:
        """切换配置档，返回状态消息"""
        available = profile_loader.list_profiles(self._config)
        if name not in available:
            return (
                f"未知配置档: {name}，可用: {', '.join(available)}"
            )

        self.current_profile = name
        return f"已切换到配置档: {name}"

    def list_profiles(self) -> list[str]:
        """列出所有可用配置档"""
        return profile_loader.list_profiles(self._config)

    def get_network_config(self) -> dict:
        """获取网络控制配置"""
        return profile_loader.get_network_config(self._config)

    # ================================================================
    # 绕过机制
    # ================================================================

    def bypass_next(self):
        """标记下一条命令绕过沙箱"""
        self._bypass_once = True

    @property
    def is_bypass_active(self) -> bool:
        return self._bypass_once

    # ================================================================
    # 核心执行
    # ================================================================

    def run(
        self,
        command: str,
        args: list | None = None,
        cwd: str | None = None,
        env: dict | None = None,
        tool_name: str = "bash",
    ) -> SandboxResult:
        """
        在沙箱中执行命令

        参数:
            command: 可执行文件路径或命令名
            args:    参数列表
            cwd:     工作目录
            env:     环境变量
            tool_name: 工具名称（用于内容拦截判断）
        """
        full_cmd = f"{command} {' '.join(args or [])}"

        # ===== 沙箱关闭 or 临时绕过 =====
        if not self.enabled or self._bypass_once:
            if self._bypass_once:
                self._bypass_once = False
                log_bypass(tool_name, "bypass_once")
            return self._execute(command, args, cwd, env)

        # ===== L2-A: 内容拦截 =====
        is_safe, reason = check_command_safety(
            full_cmd, tool_name, str(self._workspace)
        )
        if not is_safe:
            log_interception(tool_name, full_cmd, reason)
            return SandboxResult(blocked=True, block_reason=reason)

        # ===== L2-C: subprocess 执行 =====
        return self._execute(command, args, cwd, env)

    def _execute(
        self,
        command: str,
        args: list | None = None,
        cwd: str | None = None,
        env: dict | None = None,
    ) -> SandboxResult:
        """L2-C subprocess 执行（L2-A 内容拦截已在前置步骤完成）"""
        timeout = self._get_timeout()

        try:
            result = subprocess.run(
                [command] + (args or []),
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd or str(self._workspace),
                env=sanitize_env(env or os.environ.copy()),
                encoding="utf-8",
                errors="replace",
            )
            return SandboxResult(
                stdout=sanitize_output(result.stdout),
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(timeout=True)
        except Exception as e:
            log_error(f"{command} run", str(e))
            return SandboxResult(
                stderr=str(e), exit_code=-1
            )

    # ================================================================
    # 文件写入检查接口（供 WriteTool / EditTool 调用）
    # ================================================================

    def check_write_file(
        self, file_path: str, content: str
    ) -> tuple[bool, str]:
        """检查文件写入操作（L2-A 文件保护）"""
        path = Path(file_path).resolve()

        # 1. 保护敏感文件
        for sensitive in SENSITIVE_FILE_PATTERNS:
            if str(path).endswith(sensitive):
                log_interception("write", file_path, f"SENSITIVE_FILE:{sensitive}")
                return False, f"禁止修改关键文件: {sensitive}"

        # 2. 保护系统路径
        if _is_system_path(path):
            log_interception("write", file_path, "SYSTEM_PATH")
            return False, f"禁止写入系统路径: {path}"

        # 3. 工作区外写入需要额外确认（走 L1 权限）
        if not _is_within_workspace(path, self._workspace):
            log_interception("write", file_path, "OUTSIDE_WORKSPACE")
            return False, f"写入路径不在工作区内: {path}"

        return True, ""

    # ================================================================
    # Python 代码检查接口（供 PythonTool 调用）
    # ================================================================

    def check_python(self, code: str) -> tuple[bool, str]:
        """检查 Python 代码安全性"""
        return check_python_code(code)

    # ================================================================
    # 网络请求检查接口（供 HttpTool 调用）
    # ================================================================

    def check_egress(self, url: str) -> tuple[bool, str]:
        """检查外发请求目标"""
        net = self.get_network_config()
        is_safe, reason = check_network_target(
            url,
            blocked_domains=net.get("blocked_domains"),
            blocked_ips=net.get("blocked_ips"),
        )
        if not is_safe:
            log_interception("http", url, f"EGRESS:{reason}")
        return is_safe, reason

    # ================================================================
    # 状态
    # ================================================================

    def get_status_text(self) -> str:
        """返回沙箱状态文本"""
        status = "开启" if self.enabled else "关闭"
        icon = "[ON]" if self.enabled else "[OFF]"
        lines = [
            f"  [Sandbox] {icon} 沙箱状态: {status}",
            f"  配置档: {self.current_profile}",
        ]
        if self._bypass_once:
            lines.append(f"  [BYPASS] 下条命令绕过沙箱")
        lines.append(f"  内容拦截: {icon if self.enabled else '[OFF] (绕过)'}")
        lines.append(f"  执行方式: subprocess（L2-B 资源隔离暂未实现）")
        return "\n".join(lines)

    def __str__(self) -> str:
        return (
            f"SandboxExecutor(enabled={self.enabled}, "
            f"profile={self.current_profile})"
        )
