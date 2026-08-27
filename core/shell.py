# -*- coding: utf-8 -*-
"""Unified shell detection for bash / long-running process tools."""

import os
import signal
import subprocess
import threading
import time

_IS_WINDOWS = os.name == "nt"

# ===== 用户停止直通工具层（停止=立即中断在途工具） =====
# ToolRuntime 在执行工具前 set_stop_check(读取 agent 停止标志的回调)；
# run_killable 等待循环以 ~0.2s 粒度轮询，命中即杀进程组并标记
# user_interrupted——不再等满 subprocess_timeout（默认 1200s）。
# threading.local：工具在调用线程内同步执行（串行=agent 线程，
# 并行=池线程），各自独立，互不串扰。
_stop_check_local = threading.local()


def set_stop_check(fn) -> None:
    """设置当前线程的工具停止检查回调（None 清除）。由 ToolRuntime 管理。"""
    _stop_check_local.stop_check = fn


def get_stop_check():
    """读取当前线程的停止检查回调；未设置返回 None。"""
    return getattr(_stop_check_local, "stop_check", None)


# ===== 在途进程组登记（stop_timeout Fix-2：request_stop 直通强杀）=====
# run_killable 启动的每个进程组按 owner（agent id）登记；request_stop 时对
# 该 owner 名下的在途进程组直接 SIGKILL——不再依赖 run_killable 的 0.2s
# 轮询先观察到停止标志。覆盖：轮询间隙、非 run_killable 轮询的等待路径。
# 组在 run_killable 返回时注销（仅覆盖在途；后台化孤儿如 nohup ... &
# 属用户有意持久化的进程，不纳入强杀范围）。
_proc_group_lock = threading.Lock()
_proc_groups: dict[int, set[int]] = {}


def set_stop_owner(owner: int | None) -> None:
    """设置当前线程启动的子进程组归属（与 set_stop_check 同线程语义）。"""
    _stop_check_local.stop_owner = owner


def get_stop_owner() -> int | None:
    """读取当前线程的进程组归属；未设置返回 None。"""
    return getattr(_stop_check_local, "stop_owner", None)


def register_process_group(pgid: int, owner: int) -> None:
    """登记在途进程组（线程安全；pgid == 组长 pid）。"""
    with _proc_group_lock:
        _proc_groups.setdefault(owner, set()).add(pgid)


def unregister_process_group(pgid: int, owner: int) -> None:
    """注销进程组（run_killable 收尾时调用；空集合顺手清理）。"""
    with _proc_group_lock:
        members = _proc_groups.get(owner)
        if members is None:
            return
        members.discard(pgid)
        if not members:
            _proc_groups.pop(owner, None)


def _kill_pgid(pgid: int) -> bool:
    """SIGKILL 整个进程组（pgid 直接给定）；组不存在返回 False。"""
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pgid, signal.SIGKILL)
            return True
        except OSError:
            return False


def kill_owner_process_groups(owner: int) -> int:
    """强杀 owner 名下所有**存活**的在途进程组（request_stop 直通强杀）。

    返回实际杀掉的组数。杀前先 os.killpg(pgid, 0) 探活，死组顺手注销
    （pgid 会被系统回收复用，绝不对未存活组发信号）。
    """
    with _proc_group_lock:
        pgids = list(_proc_groups.get(owner, ()))
    killed = 0
    for pgid in pgids:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            unregister_process_group(pgid, owner)
            continue
        if _kill_pgid(pgid):
            killed += 1
        unregister_process_group(pgid, owner)
    return killed


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


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group owned by ``proc`` (best-effort)."""
    try:
        if _IS_WINDOWS:
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except Exception:
        pass


# 单个输出流（stdout / stderr）的环形缓冲默认上限（字节）。
# 超过后保留"头部 + 尾部"、丢弃中段，进程不会被提前杀死，
# 只是网关侧内存不再随子进程输出无限增长（P1-3 输出内存炸弹）。
_DEFAULT_STREAM_LIMIT_BYTES = 2 * 1024 * 1024

_DRAIN_CHUNK_SIZE = 64 * 1024


class _BoundedStreamBuffer:
    """有界输出缓冲：保留头部 + 尾部，丢弃中段，内存占用恒定。

    limit <= 0 表示不设上限（全量累积，兼容旧行为的逃生口）。
    """

    _ELISION_MARK = "\n\n...[中间输出过长已省略]...\n\n".encode("utf-8")

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self._head = bytearray()
        self._tail = bytearray()
        if self.limit > 0:
            self._head_cap = self.limit // 2
            self._tail_cap = self.limit - self._head_cap
            self._dropped_middle = False
        else:
            self._head_cap = -1
            self._tail_cap = -1
            self._dropped_middle = False

    def append(self, data: bytes) -> None:
        if not data:
            return
        if self.limit <= 0:
            self._head += data
            return
        # 先填头部
        if len(self._head) < self._head_cap:
            room = self._head_cap - len(self._head)
            self._head += data[:room]
            data = data[room:]
        if not data:
            return
        # 头部已满：其余进入尾部环形区（超出容量丢最旧前缀）
        self._dropped_middle = True
        self._tail += data
        overflow = len(self._tail) - self._tail_cap
        if overflow > 0:
            del self._tail[:overflow]

    @property
    def dropped_middle(self) -> bool:
        return self._dropped_middle

    def value(self) -> bytes:
        out = bytes(self._head)
        if self._dropped_middle:
            out += self._ELISION_MARK
        out += bytes(self._tail)
        return out


def _drain_stream(stream, buffer: "_BoundedStreamBuffer") -> None:
    """后台线程：循环 read(chunk) 喂入有界缓冲，直到 EOF。

    进程被杀/管道关闭导致的异常都就地吞掉，不让读取线程带异常退出。
    """
    try:
        while True:
            chunk = stream.read(_DRAIN_CHUNK_SIZE)
            if not chunk:
                break
            buffer.append(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _default_child_env() -> dict:
    """子进程默认环境：剥离敏感变量（API Key / Token 等），与沙箱路径一致。"""
    try:
        from core.sandbox.guard import sanitize_env
        return sanitize_env(os.environ.copy())
    except Exception:
        return os.environ.copy()


def run_killable(
    command: list[str],
    timeout: float | None = None,
    *,
    stdin=subprocess.DEVNULL,
    env: dict | None = None,
    cwd: str | None = None,
    max_output_bytes: int | None = _DEFAULT_STREAM_LIMIT_BYTES,
) -> subprocess.CompletedProcess:
    """Run a command in its own process group; on timeout kill the whole group.

    ``subprocess.run(timeout=N)`` kills only the direct child, leaving grandchild
    processes (e.g. ``bash -c "sleep 100 &"``) running and holding the tool pool
    worker. This launches with ``start_new_session`` (or a new Windows process
    group) so the child is a session/group leader, then SIGKILLs the entire group
    on timeout (P0-2).

    内存安全（P1-3）：stdout/stderr 由两个后台线程流式读取，各自写入有界
    环形缓冲（保留头部 + 尾部，单流上限 ``max_output_bytes`` 字节，默认 2MB；
    <=0 不设限）。刷屏命令不会把数百 MB 拉进网关内存；超限**不会**提前杀
    进程，超时语义与原先一致。

    其他参数：
        env: 子进程环境变量；None 时使用 sanitize_env 脱敏后的父环境副本，
             避免泄露 WEBUI_AUTH_TOKEN 等网关凭据。
        cwd: 子进程工作目录；None 时继承父进程（由调用方显式传工作区路径）。

    返回 CompletedProcess（returncode/stdout/stderr，stdout/stderr 为 str）。
    """
    popen_kwargs: dict = {}
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=stdin,
        cwd=cwd,
        env=_default_child_env() if env is None else env,
        **popen_kwargs,
    )

    # Fix-2：在途进程组登记（owner = 启动它的 agent，经线程本地传播）。
    # request_stop 时对该 owner 名下的在途组直接 SIGKILL，不再依赖 0.2s 轮询。
    # 仅 POSIX 登记（Windows 无进程组语义，proc.kill 兜底）。
    owner = get_stop_owner()
    registered = False
    if owner is not None and not _IS_WINDOWS:
        register_process_group(proc.pid, owner)
        registered = True

    stream_limit = (
        _DEFAULT_STREAM_LIMIT_BYTES if max_output_bytes is None else max_output_bytes
    )
    stdout_buf = _BoundedStreamBuffer(stream_limit)
    stderr_buf = _BoundedStreamBuffer(stream_limit)
    readers = [
        threading.Thread(target=_drain_stream, args=(proc.stdout, stdout_buf),
                         daemon=True),
        threading.Thread(target=_drain_stream, args=(proc.stderr, stderr_buf),
                         daemon=True),
    ]
    for t in readers:
        t.start()

    timed_out = False
    user_interrupted = False
    # 轮询等待：0.2s 粒度同时检查 超时 / 用户停止——停止请求立即杀进程组，
    # 不再等满 timeout（原 proc.wait(timeout) 最长阻塞 1200s）。
    deadline = (time.monotonic() + timeout) if timeout is not None else None
    stop_check = get_stop_check()
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
                _kill_process_group(proc)
                break
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            _kill_process_group(proc)
            break

    # 收尾：等直接子进程退出 → 给读取线程一小段宽限期排空管道，
    # 之后强制关管道促使仍阻塞的读取线程退出（如残留孙进程握住写端）。
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    for t in readers:
        t.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream and not stream.closed:
                stream.close()
        except Exception:
            pass
    for t in readers:
        t.join(timeout=1)
    if registered:
        # 注销在途登记（仅覆盖在途进程组；后台化孤儿不纳入强杀范围）
        unregister_process_group(proc.pid, owner)

    if timed_out:
        raise subprocess.TimeoutExpired(
            command, timeout,
            output=(stdout_buf.value() + stderr_buf.value()).decode("utf-8", "replace"),
        )
    completed = subprocess.CompletedProcess(
        command,
        proc.returncode,
        stdout_buf.value().decode("utf-8", errors="replace"),
        stderr_buf.value().decode("utf-8", errors="replace"),
    )
    if user_interrupted:
        # 附加标记（不进 CompletedProcess 构造签名，避免兼容性问题）；
        # 工具层据此返回"用户停止"提示而非正常输出。
        completed.user_interrupted = True
    return completed
