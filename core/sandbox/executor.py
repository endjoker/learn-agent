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
import threading
import logging
from pathlib import Path

from .guard import (
    check_command_safety,
    check_write_content,
    sanitize_output,
    check_python_code,
    sanitize_env,
    check_network_target,
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


class OutputDrainer:
    """流式读取子进程管道写入 sink，供 SandboxExecutor._execute 和 ProcessManager 共用。

    - sink 为 list + kill_on_exceed=True：一次性命令，累计超 max_bytes 则 kill 进程（BashTool）
    - sink 为 deque(maxlen) + kill_on_exceed=False：长驻会话，ring 天然驱丢，记 dropped（ProcessManager）
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        pipe,
        sink,
        max_bytes: int,
        kill_on_exceed: bool = True,
    ):
        self._proc = proc
        self._pipe = pipe
        self._sink = sink
        self._max_bytes = max_bytes
        self._kill_on_exceed = kill_on_exceed
        self.truncated = False   # 一次性：是否触发 kill 截断
        self.dropped = 0          # 长驻：ring 驱丢的 chunk 累计计数
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def join(self, timeout: float = 5) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def _drain(self) -> None:
        try:
            while True:
                # read1：一次底层读取，返回当前可得字节（不阻塞等待填满缓冲），
                # 否则低频输出（如 dev server 日志）会被 read(65536) 阻塞到 EOF
                chunk = self._pipe.read1(65536)
                if not chunk:
                    break
                if self._kill_on_exceed:
                    # 一次性模式：累计超限则 kill
                    if sum(len(c) for c in self._sink) + len(chunk) > self._max_bytes:
                        self.truncated = True
                        try:
                            self._proc.kill()
                        except (ProcessLookupError, OSError):
                            pass
                        break
                    self._sink.append(chunk)
                else:
                    # ring 模式：deque(maxlen) 自动驱丢最旧；append 前若满则记一次 drop
                    mx = getattr(self._sink, "maxlen", None)
                    if mx is not None and len(self._sink) >= mx:
                        self.dropped += 1
                    self._sink.append(chunk)
        except (OSError, ValueError):
            pass

    def result_bytes(self) -> bytes:
        """一次性模式用：拼接 sink 为 bytes。ring 模式由 ProcessManager 自管读指针。"""
        return b"".join(self._sink)


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

    def _get_max_output_bytes(self) -> int:
        """获取当前配置档的最大输出字节数（max_output_mb → bytes）。

        防止子进程把 stdout/stderr 全量读进内存导致 OOM；
        与资源隔离层（L2-B）无关，纯标准库即可生效。
        """
        prof = self._get_current_profile()
        if not prof:
            return 10 * 1024 * 1024  # 默认 10 MB
        mb = prof.get("max_output_mb", 10)
        return max(int(mb) * 1024 * 1024, 64 * 1024)  # 至少 64KB，避免误杀

    def is_profile_network_enabled(self) -> bool:
        """当前配置档是否允许网络外发（profile.network）。

        与资源隔离层无关，纯配置开关。
        """
        prof = self._get_current_profile()
        return bool(prof.get("network", True)) if prof else True

    def _get_idle_timeout(self) -> float:
        """长驻进程空闲上限（秒）。读 config 顶层 idle_timeout_seconds，默认 300。

        供 ProcessManager idle watchdog 使用；BashTool 等一次性工具不涉及。
        """
        return float(self._config.get("idle_timeout_seconds", 300))

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
        """L2-C subprocess 执行（L2-A 内容拦截已在前置步骤完成）

        使用 Popen + OutputDrainer 双线程流式读取 stdout/stderr，按 max_output_mb
        截断输出，避免 `yes`/`cat /dev/zero` 类命令把输出全量读进内存导致 OOM。
        """
        timeout = self._get_timeout()
        max_bytes = self._get_max_output_bytes()

        try:
            proc = subprocess.Popen(
                [command] + (args or []),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or str(self._workspace),
                env=sanitize_env(env or os.environ.copy()),
            )
        except FileNotFoundError:
            return SandboxResult(
                stderr=f"命令未找到: {command}", exit_code=-1
            )
        except Exception as e:
            log_error(f"{command} run", str(e))
            return SandboxResult(stderr=str(e), exit_code=-1)

        out_sink: list[bytes] = []
        err_sink: list[bytes] = []
        out_drainer = OutputDrainer(
            proc, proc.stdout, out_sink, max_bytes, kill_on_exceed=True
        )
        err_drainer = OutputDrainer(
            proc, proc.stderr, err_sink, max_bytes, kill_on_exceed=True
        )
        out_drainer.start()
        err_drainer.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            proc.wait()

        out_drainer.join()
        err_drainer.join()

        # 关闭管道，释放文件描述符
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass

        if timed_out:
            return SandboxResult(timeout=True)

        stdout = out_drainer.result_bytes().decode("utf-8", errors="replace")
        stderr = err_drainer.result_bytes().decode("utf-8", errors="replace")
        if out_drainer.truncated or err_drainer.truncated:
            stderr = (
                (stderr + "\n" if stderr else "")
                + f"[沙箱] 输出超过 max_output_mb 上限（{max_bytes // (1024 * 1024)}MB），已截断"
            )

        return SandboxResult(
            stdout=sanitize_output(stdout),
            stderr=stderr,
            exit_code=proc.returncode,
        )

    # ================================================================
    # 文件写入检查接口（供 WriteTool / EditTool 调用）
    # ================================================================

    def check_write_file(
        self, file_path: str, content: str
    ) -> tuple[bool, str]:
        """检查文件写入操作（L2-A 文件保护）

        委托 guard.check_write_content 做敏感文件 / 系统路径 / 内容注入扫描，
        再补充工作区边界检查（guard 不感知 workspace）。
        """
        is_safe, reason = check_write_content(file_path, content)
        if not is_safe:
            log_interception("write", file_path, reason)
            return False, reason

        # 工作区外写入需要额外确认（走 L1 权限）
        path = Path(file_path).resolve()
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
        """检查外发请求目标

        1. 当前配置档 network 开关为 False → 直接拒绝所有外发
        2. 域名/IP 黑名单（注：blocked_ips 由 check_network_target 处理）
        """
        # 1. per-profile 网络开关：restricted 档 network=False 时禁网
        if not self.is_profile_network_enabled():
            reason = f"当前配置档 '{self.current_profile}' 禁止网络访问"
            log_interception("http", url, f"EGRESS:profile_network_off")
            return False, reason

        # 2. 域名/IP 黑名单
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
