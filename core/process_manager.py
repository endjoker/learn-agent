# -*- coding: utf-8 -*-
"""
长驻交互式子进程会话管理器 —— ProcessManager

让 agent 能启动跨 ReAct 步存活的长驻进程（dev server / watcher / REPL），
按 session 增量读取输出、向 stdin 投喂、统一停止。

设计见 code/learn/AIsubwey/design.md。要点：
- 子进程经 SecurityGate 的 exec:shell 过 check_command_safety（含 DANGEROUS）后才启动
- env 统一经 sanitize_env 脱敏（与 bash 路径一致）
- 输出用 ring buffer（deque(maxlen)）内存有界，不因输出量 kill；病态 spam 由 idle 兜底
- read 用 per-session 消费模型（读后 clear），ring 驱丢未读 → truncated 标志
- idle watchdog：无 read/send 超时 → kill；并检测自然退出
- 不泄露裸 Popen 句柄；区外 cwd → 拒绝启动
"""

import os
import time
import signal
import threading
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .sandbox.guard import sanitize_env, sanitize_output
from .shell import shell_command
from .sandbox.executor import OutputDrainer

_IS_WINDOWS = os.name == "nt"
_CHUNK = 65536


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀整个进程树（shell 包装层启动 python 时，杀 shell 会留下 python 孤儿，
    其仍持有管道 → drain 线程阻塞。必须杀树让子进程退出、管道关闭）。

    统一实现见 core.sandbox.executor._kill_process_tree（taskkill /T + killpg）。
    """
    from .sandbox.executor import _kill_process_tree
    _kill_process_tree(proc)


@dataclass
class ProcessSession:
    id: int
    name: str
    proc: subprocess.Popen
    stdout_buf: deque
    stderr_buf: deque
    lock: threading.Lock
    started_at: float
    last_active: float
    status: str                # running / exited / killed / idle_killed
    exit_code: Optional[int] = None
    out_drainer: Optional[OutputDrainer] = None
    err_drainer: Optional[OutputDrainer] = None
    last_out_dropped: int = 0
    last_err_dropped: int = 0
    exited_at: float = 0.0


class ProcessManager:
    """管理多个长驻子进程会话。"""

    # 四档权限模式：这些模式下 cwd 边界交还 PolicyEngine 裁决
    # （_PATH_KEYS 含 "cwd"，proc_start 的界外 cwd 在 allow 档会 ASK、
    # unreviewed 放行、ask 全量 ASK——授权确认后的执行必须可达）
    _LADDER_MODES = {"ask", "allow", "unreviewed"}

    def __init__(self, sandbox, workspace: str, permission=None):
        self._sandbox = sandbox
        self._workspace = Path(workspace).resolve()
        self._permission = permission
        self._sessions: dict[int, ProcessSession] = {}
        self._next_id = 1
        prof = sandbox.get_current_profile() or {}
        self._max_sessions = int(prof.get("max_processes", 8))
        self._idle_timeout = float(sandbox.get_idle_timeout())
        self._retain_exited = 600.0
        self._lock = threading.Lock()
        self._stop = False
        self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog.start()

    # ================================================================
    # 工具方法
    # ================================================================

    def _is_within_workspace(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self._workspace)
            return True
        except ValueError:
            return False

    def _max_chunks(self) -> int:
        """ring buffer chunk 数（≈ max_output_bytes / chunk_size）"""
        return max(1, self._sandbox.get_max_output_bytes() // _CHUNK)

    def _evict_oldest_exited(self) -> bool:
        """驱逐最老的 exited 会话以腾容量。返回是否驱逐成功。"""
        for sid in sorted(
            self._sessions,
            key=lambda k: self._sessions[k].exited_at or float("inf"),
        ):
            s = self._sessions[sid]
            if s.status in ("exited", "killed", "idle_killed"):
                self._close_session(s)
                return True
        return False

    def _close_session(self, s: ProcessSession):
        """关闭会话的管道/drain，从注册表移除"""
        try:
            from .orphan_processes import record as _record_orphan
            _record_orphan(s.proc.pid, False)
        except Exception:
            pass
        for d in (s.out_drainer, s.err_drainer):
            if d:
                d.join(timeout=1)
        for pipe in (s.proc.stdout, s.proc.stderr, s.proc.stdin):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass
        with self._lock:
            self._sessions.pop(s.id, None)

    # ================================================================
    # 主接口
    # ================================================================

    def start(self, command: str, cwd: str = None, name: str = None) -> tuple[int, str]:
        """启动长驻进程，返回 (session_id, 初始输出)。
        失败返回 (-1, 错误信息)。
        """
        cwd_path = Path(cwd).resolve() if cwd else self._workspace
        # cwd 边界四档权限感知：PolicyEngine _PATH_KEYS 含 "cwd"，proc_start
        # 的界外 cwd 在授权层已按档位裁决（ask 全量 ASK / allow 界外 ASK /
        # unreviewed 放行）——确认后的执行必须可达，不得在此一票否决。
        # readonly（与 DENY 一致）与未注入权限（无裁决层的直调路径，硬边界
        # 是唯一防线）保持硬拒绝。
        permission_mode = str(getattr(self._permission, "permission_mode", "") or "")
        if permission_mode not in self._LADDER_MODES and not self._is_within_workspace(cwd_path):
            return -1, f"❌ 区外 cwd 不允许: {cwd_path}（请用工作区内路径）"

        with self._lock:
            if len(self._sessions) >= self._max_sessions:
                if not self._evict_oldest_exited():
                    return -1, f"❌ 已达进程数上限 {self._max_sessions}，请先 proc_stop 释放"
            sid = self._next_id
            self._next_id += 1

        # Popen 包装 shell（与 BashTool 一致；L2 已查 command 原串）
        # CREATE_NEW_PROCESS_GROUP / start_new_session：便于 _kill_tree 杀整树
        shell = shell_command(command)
        kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            cwd=str(cwd_path),
            env=sanitize_env(os.environ.copy()),  # 与 bash 路径一致统一脱敏
        )
        if _IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(shell, **kwargs)
        except FileNotFoundError:
            return -1, f"❌ shell 未找到: {shell[0]}"
        except Exception as e:
            return -1, f"❌ 启动失败: {e}"

        from .orphan_processes import record as _record_orphan
        _record_orphan(proc.pid, True)

        # 确保 Popen 成功后无论 drainer 创建/启动是否异常，都能清理子进程
        session = None
        try:
            maxlen = self._max_chunks()
            out_buf: deque = deque(maxlen=maxlen)
            err_buf: deque = deque(maxlen=maxlen)
            max_bytes = self._sandbox.get_max_output_bytes()
            now = time.time()
            session = ProcessSession(
                id=sid,
                name=name or f"proc-{sid}",
                proc=proc,
                stdout_buf=out_buf,
                stderr_buf=err_buf,
                lock=threading.Lock(),
                started_at=now,
                last_active=now,
                status="running",
            )
            session.out_drainer = OutputDrainer(
                proc, proc.stdout, out_buf, max_bytes, kill_on_exceed=False
            )
            session.err_drainer = OutputDrainer(
                proc, proc.stderr, err_buf, max_bytes, kill_on_exceed=False
            )
            session.out_drainer.start()
            session.err_drainer.start()
        except Exception:
            # drainer 创建/启动失败 → 杀子进程，避免僵尸
            _kill_tree(proc)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            return -1, f"❌ 输出读取器启动失败，进程已清理"

        with self._lock:
            self._sessions[sid] = session

        # grace 取初始输出（最多等 200ms × 4 轮，快速进程不浪费时间）
        for _ in range(4):
            time.sleep(0.2)
            out, err, _trunc = self._read_internal(session)
            if out or err:
                break
        init_parts = []
        if out:
            init_parts.append(out.rstrip())
        if err:
            init_parts.append(f"[stderr]\n{err.rstrip()}")
        if not init_parts:
            init_parts.append("（暂无输出，可用 proc_read 后续读取）")
        return sid, "\n".join(init_parts)

    def send(self, session_id: int, data: str) -> str:
        """向 stdin 投喂（自动追加 \\n）。"""
        s = self._sessions.get(int(session_id))
        if not s:
            return f"❌ 会话不存在: {session_id}"
        if s.status != "running":
            return f"❌ 进程已结束（{s.status}, exit={s.exit_code}）"
        try:
            s.proc.stdin.write((data + "\n").encode("utf-8", errors="replace"))
            s.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError, UnicodeEncodeError) as e:
            return f"❌ 写入 stdin 失败（进程可能已关闭 stdin）: {e}"
        s.last_active = time.time()
        return f"✅ 已发送 {len(data)} 字节到 {s.name}"

    def read(self, session_id: int) -> tuple[str, str, bool, str]:
        """增量读取 stdout/stderr，返回 (out, err, truncated, status_msg)。
        读后清空 buffer（per-session 消费模型）。
        """
        s = self._sessions.get(int(session_id))
        if not s:
            return "", "", False, f"❌ 会话不存在: {session_id}"
        out, err, trunc = self._read_internal(s)
        s.last_active = time.time()
        status = "" if s.status == "running" else f"\n[进程状态: {s.status}, exit={s.exit_code}]"
        return out, err, trunc, status

    def _read_internal(self, s: ProcessSession) -> tuple[str, str, bool]:
        # 使用 popleft() 逐条消费，而非 list()+clear()，避免与 _drain 线程的 append
        # 竞态丢失 chunk（_drain 不持 s.lock，list()+clear() 之间 GIL 切换会丢数据）
        with s.lock:
            out_chunks = []
            while s.stdout_buf:
                out_chunks.append(s.stdout_buf.popleft())
            err_chunks = []
            while s.stderr_buf:
                err_chunks.append(s.stderr_buf.popleft())
            new_out_drop = s.out_drainer.dropped if s.out_drainer else 0
            new_err_drop = s.err_drainer.dropped if s.err_drainer else 0
        delta_out = new_out_drop - s.last_out_dropped
        delta_err = new_err_drop - s.last_err_dropped
        s.last_out_dropped = new_out_drop
        s.last_err_dropped = new_err_drop
        out = b"".join(out_chunks).decode("utf-8", errors="replace")
        err = b"".join(err_chunks).decode("utf-8", errors="replace")
        trunc = delta_out > 0 or delta_err > 0
        return sanitize_output(out), sanitize_output(err), trunc

    def list_sessions(self) -> list[dict]:
        now = time.time()
        out = []
        # 持锁遍历：与 proc_start 的并发插入互斥，避免 "dict changed size"
        with self._lock:
            sessions = sorted(self._sessions.values(), key=lambda x: x.id)
        for s in sessions:
            out.append({
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "exit_code": s.exit_code,
                "started_at": s.started_at,
                "idle_for": int(now - s.last_active),
            })
        return out

    def stop(self, session_id: int) -> str:
        s = self._sessions.get(int(session_id))
        if not s:
            return f"❌ 会话不存在: {session_id}"
        if s.status != "running":
            return f"⚠️ 进程已结束（{s.status}, exit={s.exit_code}）"
        # 先关 stdin（让读 stdin 的子进程收 EOF 自然退出），再杀整树
        try:
            if s.proc.stdin:
                s.proc.stdin.close()
        except Exception:
            pass
        _kill_tree(s.proc)
        try:
            s.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        with s.lock:
            s.status = "killed"
            s.exit_code = s.proc.returncode
            s.exited_at = time.time()
        return f"✅ 已停止 {s.name} (exit={s.exit_code})"

    def cleanup_all(self):
        """agent 退出时调用，停所有进程"""
        self._stop = True
        for s in list(self._sessions.values()):
            if s.status == "running":
                try:
                    if s.proc.stdin:
                        s.proc.stdin.close()
                except Exception:
                    pass
                _kill_tree(s.proc)
                try:
                    s.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            self._close_session(s)
        self._sessions.clear()
        self._watchdog.join(timeout=5)

    # ================================================================
    # watchdog：idle 超时 + 自然退出检测 + exited 保留清理
    # ================================================================

    def _watchdog_loop(self):
        while not self._stop:
            time.sleep(5)
            now = time.time()
            for s in list(self._sessions.values()):
                if s.status != "running":
                    # exited 保留超时 → 清理
                    if s.exited_at and now - s.exited_at > self._retain_exited:
                        self._close_session(s)
                    continue
                # 自然退出检测
                if s.proc.poll() is not None:
                    with s.lock:
                        s.status = "exited"
                        s.exit_code = s.proc.returncode
                        s.exited_at = now
                    continue
                # idle 超时：杀整个进程树（start_new_session + killpg，
                # 回退逐个 kill 子进程；见 executor._kill_process_tree），
                # 避免只杀 shell 留下持有管道的孤儿子进程。
                if now - s.last_active > self._idle_timeout:
                    _kill_tree(s.proc)
                    try:
                        s.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                    with s.lock:
                        s.status = "idle_killed"
                        s.exit_code = s.proc.returncode
                        s.exited_at = now
