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
import signal
import subprocess
import tempfile
import threading
import time
import logging
from pathlib import Path
from typing import Sequence

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
from .. import shell as _shell

logger = logging.getLogger("jk_agent")


def _child_pids(pid: int) -> list[int]:
    """枚举 pid 的直接子进程 pid（ps 不可用/超时返回空列表）。"""
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        return [int(line.strip()) for line in out.stdout.splitlines()
                if line.strip().isdigit()]
    except Exception:
        return []


def _kill_pid_tree(pid: int) -> None:
    """SIGKILL pid 及其全部子孙（先子后父，best-effort）。"""
    for child in _child_pids(pid):
        _kill_pid_tree(child)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the whole process tree rooted at proc (Windows: taskkill /T).

    非 Windows：优先 killpg（start_new_session 保证 proc 是会话组长）；
    killpg 失败（进程组已退出/不可用）时回退为逐个 kill 子进程树（P1）。
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=8,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # 进程组不可用：逐个 kill 子进程树（含 proc.pid 自身）
                _kill_pid_tree(proc.pid)
    except Exception:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


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
        full_output_path: str = "",
        spilled: bool = False,
        interrupted: bool = False,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timeout = timeout
        self.blocked = blocked
        self.block_reason = block_reason
        self.full_output_path = full_output_path
        self.spilled = spilled
        # 用户停止中断：进程树已被杀、当前输出丢弃（工具层转 ⏹️ 提示）
        self.interrupted = interrupted

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
        if self.full_output_path:
            parts.append(f"📄 完整输出已落盘: {self.full_output_path}")
        if not parts:
            parts.append("（执行完毕，无输出）")
        return "\n".join(parts)


class OutputDrainer:
    """流式读取子进程管道，支持一次性命令、长驻 ring、以及超限落盘。

    - sink 为 list + kill_on_exceed=True + 无 spill：累计超 max_bytes 则 kill（旧行为）
    - sink 为 list + kill_on_exceed=True + spill：超 threshold 写临时文件，内存只留尾部
    - sink 为 deque(maxlen) + kill_on_exceed=False：长驻会话 ring，记 dropped
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        pipe,
        sink,
        max_bytes: int,
        kill_on_exceed: bool = True,
        spill_dir: str | None = None,
        spill_threshold: int = 0,
        tail_limit: int = 8192,
    ):
        self._proc = proc
        self._pipe = pipe
        self._sink = sink
        self._max_bytes = max_bytes
        self._kill_on_exceed = kill_on_exceed
        self._spill_dir = spill_dir
        self._spill_threshold = spill_threshold
        self._tail_limit = tail_limit
        self._spill_fh = None
        self._spill_bytes = 0
        self.full_output_path = ""
        self.spilled = False
        self.truncated = False   # 一次性：是否触发截断（kill 或落盘截断）
        self.dropped = 0          # 长驻：ring 驱丢的 chunk 累计计数
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def join(self, timeout: float = 5) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def _ensure_spill(self) -> None:
        if self._spill_fh is not None:
            return
        directory = self._spill_dir or tempfile.gettempdir()
        fd, path = tempfile.mkstemp(prefix="agent-output-", suffix=".log", dir=directory)
        self._spill_fh = os.fdopen(fd, "wb")
        self.full_output_path = path
        self.spilled = True

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._pipe.read1(65536)
                if not chunk:
                    break
                if self._kill_on_exceed:
                    if self._spill_threshold > 0:
                        # spill mode: 全程累计字节数；超 spill 阈值 → 完整输出落盘 +
                        # 内存只留尾部（不杀进程，命令可继续产出，spill 文件保证完整）。
                        # 旧实现 _spill_bytes 只在落盘后才累加，导致触发条件恒假、分支不可达（P0-4）。
                        # 硬上限 max_bytes 仍生效：超出才杀进程树防 OOM，spill 文件保留供续读。
                        self._spill_bytes += len(chunk)
                        self._sink.append(chunk)
                        if self._spill_fh is None and self._spill_bytes > self._spill_threshold:
                            self._ensure_spill()
                            try:
                                # 阈值前已积累的内存 chunk 一并落盘，保证 spill 文件为完整输出
                                for c in self._sink:
                                    self._spill_fh.write(c)
                            except OSError:
                                pass
                            self.truncated = True
                        elif self._spill_fh is not None:
                            try:
                                self._spill_fh.write(chunk)
                            except OSError:
                                pass
                        if self._spill_fh is not None:
                            # 内存只留尾部（tail_limit）
                            total = sum(len(c) for c in self._sink)
                            while total > self._tail_limit and len(self._sink) > 1:
                                total -= len(self._sink.pop(0))
                        if self._spill_bytes > self._max_bytes:
                            self.truncated = True
                            try:
                                _kill_process_tree(self._proc)
                            except Exception:
                                pass
                            break
                    else:
                        if sum(len(c) for c in self._sink) + len(chunk) > self._max_bytes:
                            self.truncated = True
                            try:
                                self._proc.kill()
                            except (ProcessLookupError, OSError):
                                pass
                            break
                        self._sink.append(chunk)
                else:
                    mx = getattr(self._sink, "maxlen", None)
                    if mx is not None and len(self._sink) >= mx:
                        self.dropped += 1
                    self._sink.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            if self._spill_fh is not None:
                try:
                    self._spill_fh.close()
                except Exception:
                    pass

    def result_bytes(self) -> bytes:
        """一次性模式用：拼接 sink 为 bytes。ring 模式由 ProcessManager 自管读指针。"""
        return b"".join(self._sink)


class SandboxExecutor:
    """
    沙箱执行器

    两层防护：
      L2-A: 内容拦截（敏感文件/防泄露/系统路径/Python AST/网络黑名单）
      L2-C: subprocess 执行 + 超时控制 + 输出上限/落盘（脱敏下沉到工具层截断后统一执行，C6）

    L2-B 资源隔离层（nanosandbox）为设计预留，暂未实现。

    沙箱关闭时（enabled=False）:
      L2 全部绕过，直接 subprocess 执行

    绕过机制（bypass_once）:
      开启后下一条命令绕过 L2-A，执行后自动恢复
    """

    def __init__(
        self,
        workspace: str | None = None,
        extra_workspace_roots: Sequence[str | Path] = (),
    ):
        self._workspace = Path(workspace or os.getcwd()).resolve()
        self._extra_workspace_roots = tuple(
            Path(p).expanduser().resolve() for p in (extra_workspace_roots or ()))
        self._config = profile_loader.load_config()

        # 沙箱开关（默认关闭；config.json → sandbox.enabled = true 开启）
        self.enabled: bool = self._config.get("enabled", False)
        self.current_profile: str = self._config.get("default_profile", "agent")

        # spill 阈值（沙箱路径）：默认 256KB，可配 sandbox.spill_threshold_kb；
        # <=0 表示禁用 spill，回退为超 max_output_mb 直接杀进程的旧行为。
        spill_kb = self._config.get("spill_threshold_kb", 256)
        if spill_kb is None:
            spill_kb = 256
        try:
            spill_kb = int(spill_kb)
        except (TypeError, ValueError):
            spill_kb = 256
        self.spill_threshold_bytes = spill_kb * 1024 if spill_kb > 0 else 0

        # 临时绕过标志（下一条命令绕过，执行后自动复位）
        self._bypass_once: bool = False
        # Permission mode is an approval policy, not an L2 bypass. Keep this
        # field only for compatibility with older callers.
        self._unreviewed_mode: bool = False

        logger.info(
            "沙箱执行器初始化: enabled=%s, profile=%s",
            self.enabled,
            self.current_profile,
        )

    # ================================================================
    # 配置管理
    # ================================================================

    def get_current_profile(self) -> dict | None:
        """获取当前配置档"""
        return profile_loader.get_profile(self._config, self.current_profile)

    def _get_timeout(self) -> int:
        """获取当前超时设置"""
        prof = self.get_current_profile()
        return prof.get("timeout_seconds", 60) if prof else 60

    def get_max_output_bytes(self) -> int:
        """获取当前配置档的最大输出字节数（max_output_mb → bytes）。

        防止子进程把 stdout/stderr 全量读进内存导致 OOM；
        与资源隔离层（L2-B）无关，纯标准库即可生效。
        """
        prof = self.get_current_profile()
        if not prof:
            return 10 * 1024 * 1024  # 默认 10 MB
        mb = prof.get("max_output_mb", 10)
        return max(int(mb) * 1024 * 1024, 64 * 1024)  # 至少 64KB，避免误杀

    def is_profile_network_enabled(self) -> bool:
        """当前配置档是否允许网络外发（profile.network）。

        与资源隔离层无关，纯配置开关。
        """
        prof = self.get_current_profile()
        return bool(prof.get("network", True)) if prof else True

    def get_idle_timeout(self) -> float:
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

    def set_unreviewed_mode(self, enabled: bool) -> None:
        """Allow policy-sensitive paths in unreviewed mode, never hard L2 rules."""
        self._unreviewed_mode = bool(enabled)

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
        timeout: float | None = None,
    ) -> SandboxResult:
        """
        在沙箱中执行命令

        参数:
            command: 可执行文件路径或命令名
            args:    参数列表
            cwd:     工作目录
            env:     环境变量
            tool_name: 工具名称（用于内容拦截判断）
            timeout:  调用方指定的超时秒数（可选）；提供时优先生效并覆盖
                      profile 默认值，None 时保持 profile 默认行为不变。
        """
        full_cmd = f"{command} {' '.join(args or [])}"

        if not self.enabled or self._bypass_once:
            if self._bypass_once:
                self._bypass_once = False
                log_bypass(tool_name, "bypass_once")
            return self._execute(command, args, cwd, env, timeout)

        # ``unreviewed`` only bypasses policy-sensitive path matching. The
        # command/content hard checks below remain enabled.
        is_safe, reason = check_command_safety(
            full_cmd, tool_name, str(self._workspace),
            check_policy_paths=not self._unreviewed_mode)
        if not is_safe:
            log_interception(tool_name, full_cmd, reason)
            return SandboxResult(blocked=True, block_reason=reason)

        # ===== L2-C: subprocess 执行 =====
        return self._execute(command, args, cwd, env, timeout)

    def _execute(
        self,
        command: str,
        args: list | None = None,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
    ) -> SandboxResult:
        """L2-C subprocess 执行（L2-A 内容拦截已在前置步骤完成）

        使用 Popen + OutputDrainer 双线程流式读取 stdout/stderr，超限输出落盘，
        避免 ``yes``/``cat /dev/zero`` 类命令把输出全量读进内存导致 OOM。
        超时/取消时杀掉整个进程树，避免残留子进程。
        """
        timeout = self._get_timeout() if timeout is None else timeout
        max_bytes = self.get_max_output_bytes()
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "cwd": cwd or str(self._workspace),
            "env": sanitize_env(env or os.environ.copy()),
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen([command] + (args or []), **popen_kwargs)
        except FileNotFoundError:
            return SandboxResult(
                stderr=f"命令未找到: {command}", exit_code=-1
            )
        except Exception as e:
            log_error(f"{command} run", str(e))
            return SandboxResult(stderr=str(e), exit_code=-1)

        from ..orphan_processes import record as _record_orphan
        _record_orphan(proc.pid, True)

        out_sink: list[bytes] = []
        err_sink: list[bytes] = []
        out_drainer = OutputDrainer(
            proc, proc.stdout, out_sink, max_bytes, kill_on_exceed=True,
            spill_threshold=self.spill_threshold_bytes, tail_limit=8192,
        )
        err_drainer = OutputDrainer(
            proc, proc.stderr, err_sink, max_bytes, kill_on_exceed=True,
            spill_threshold=self.spill_threshold_bytes, tail_limit=8192,
        )
        out_drainer.start()
        err_drainer.start()

        timed_out = False
        user_interrupted = False
        # 轮询等待：0.2s 粒度同时检查 超时 / 用户停止——停止请求立即杀进程
        # 树（与 run_killable 同模式），不再等满 timeout（默认 1200s）。
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        stop_check = _shell.get_stop_check()
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if stop_check is not None:
                try:
                    should_stop = bool(stop_check())
                except Exception:
                    should_stop = False
                if should_stop:
                    user_interrupted = True
                    _kill_process_tree(proc)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                    break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                _kill_process_tree(proc)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                break

        # 给 drainer 一段收尾时间；Windows 上孙进程可能继承管道句柄，
        # 超过 grace 后主动关闭管道，避免阻塞在 read。
        out_drainer.join(timeout=2)
        err_drainer.join(timeout=2)
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass

        _record_orphan(proc.pid, False)

        full_path = out_drainer.full_output_path or err_drainer.full_output_path
        spilled = out_drainer.spilled or err_drainer.spilled

        if timed_out:
            return SandboxResult(timeout=True, full_output_path=full_path, spilled=spilled)
        if user_interrupted:
            # 用户停止：杀进程树后立即返回，当前输出丢弃（转录由上层合成
            # "⏹️ 已中断"占位结果，保证 tool_call/result 配对完整）。
            return SandboxResult(interrupted=True, full_output_path=full_path, spilled=spilled)

        stdout = out_drainer.result_bytes().decode("utf-8", errors="replace")
        stderr = err_drainer.result_bytes().decode("utf-8", errors="replace")
        if (out_drainer.truncated or err_drainer.truncated) and not spilled:
            # spill 路径的落盘提示由工具层 _format_output 附“完整输出已落盘: <path>”；
            # 这里只保留无 spill（kill 截断）模式的提示，避免重复。
            stderr = (
                (stderr + "\n" if stderr else "")
                + f"[沙箱] 输出超过 max_output_mb 上限（{max_bytes // (1024 * 1024)}MB），已截断"
            )

        # C6：不再对全量输出预脱敏——返回原始 stdout/stderr，由工具层
        # _format_output 先截断再统一 sanitize_output（幂等安全，避免对
        # MB 级输出做全量正则脱敏）。
        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            full_output_path=full_path,
            spilled=spilled,
        )

    # ================================================================
    # 文件写入检查接口（供 WriteTool / EditTool 调用）
    # ================================================================

    def check_write_file(
        self, file_path: str, content: str, *,
        check_policy_paths: bool = True,
    ) -> tuple[bool, str]:
        """检查文件写入操作（L2-A 文件保护）

        委托 guard.check_write_content 做敏感文件 / 系统路径 / 内容注入扫描，
        再补充工作区边界检查（guard 不感知 workspace）。

        沙箱未启用时不拦截（整体权限遵循 PolicyEngine 四档裁决）；启用后才执行
        内容/边界硬检查，与 SecurityGate 的 L2 一致。
        """
        if not self.enabled:
            return True, ""
        is_safe, reason = check_write_content(
            file_path, content, check_policy_paths=check_policy_paths)
        if not is_safe:
            log_interception("write", file_path, reason)
            return False, reason

        # 工作区边界由统一 PolicyEngine 裁决；工具直调时仍保持本地边界。
        # 与 PolicyEngine 的 allowed_roots（project_root + extra_workspace_roots）一致，
        # 避免 extra 根下的合法写入被单工作区误判为 OUTSIDE_WORKSPACE。
        path = Path(file_path).resolve()
        if check_policy_paths and not self._is_within_any_workspace(path):
            log_interception("write", file_path, "OUTSIDE_WORKSPACE")
            return False, f"写入路径不在工作区内: {path}"

        return True, ""

    def _is_within_any_workspace(self, path: Path) -> bool:
        """路径是否落在任一受信工作区根下（主工作区 + extra_workspace_roots）。"""
        if _is_within_workspace(path, self._workspace):
            return True
        for root in self._extra_workspace_roots:
            if _is_within_workspace(path, root):
                return True
        return False

    # ================================================================
    # Python 代码检查接口（供 PythonTool 调用）
    # ================================================================

    def check_python(self, code: str) -> tuple[bool, str]:
        """检查 Python 代码安全性（沙箱启用时才拦截，否则交由四档裁决）"""
        if not self.enabled:
            return True, ""
        return check_python_code(code)

    # ================================================================
    # 网络请求检查接口（供 HttpTool 调用）
    # ================================================================

    def check_egress(self, url: str) -> tuple[bool, str]:
        """检查外发请求目标

        1. 当前配置档 network 开关为 False → 直接拒绝所有外发
        2. 域名/IP 黑名单（注：blocked_ips 由 check_network_target 处理）

        沙箱未启用时不拦截（整体权限遵循 PolicyEngine 四档裁决）。
        """
        if not self.enabled:
            return True, ""
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
