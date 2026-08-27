# -*- coding: utf-8 -*-
"""
Hook 实现 — BaseHook / PythonHook / CommandHook

两种 hook 类型：
  PythonHook   — 进程内回调 fn(ctx) -> HookResult
  CommandHook  — 进程外命令（对标 Claude Code），JSON stdin/stdout + exit code
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from typing import Callable

from .events import HookContext, HookResult, Decision, _coerce

logger = logging.getLogger("jk_agent.hook")


# ================================================================
# 基类
# ================================================================

class BaseHook:
    """Hook 基类 — 子类实现 run(ctx) -> HookResult"""

    name: str = ""

    def run(self, ctx: HookContext) -> HookResult:
        raise NotImplementedError

    def __repr__(self):
        label = self.name or type(self).__name__
        return f"<{label}>"


# ================================================================
# PythonHook — 进程内回调
# ================================================================

class PythonHook(BaseHook):
    """进程内 hook：fn(ctx) -> HookResult，自动 _coerce 返回值"""

    def __init__(self, fn: Callable[[HookContext], object],
                 name: str = ""):
        super().__init__()
        self.fn = fn
        self.name = name or getattr(fn, "__name__", "python_hook")
        self.consecutive_errors: int = 0   # 连续异常次数（成功即清零）
        self.last_error: str = ""          # 最近一次异常信息
        self.last_error_at: float = 0.0    # 最近一次异常时间戳

    def run(self, ctx: HookContext) -> HookResult:
        try:
            result = _coerce(self.fn(ctx))
            self.consecutive_errors = 0
            return result
        except Exception as exc:
            self.consecutive_errors += 1
            self.last_error = str(exc)
            self.last_error_at = time.time()
            logger.error(
                f"PythonHook '{self.name}' 执行异常（连续 {self.consecutive_errors} 次）",
                exc_info=True,
            )
            return HookResult(Decision.CONTINUE, reason="hook 内部异常")


# ================================================================
# CommandHook — 进程外命令（对标 Claude Code）
# ================================================================

def _parse_stdout(text: str) -> HookResult:
    """从命令 stdout 解析 HookResult JSON。

    成功 → 按 decision/reason/data 构造。
    无输出 / 非 JSON → CONTINUE（命令不干预）。
    """
    text = (text or "").strip()
    if not text:
        return HookResult(Decision.CONTINUE)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        logger.debug(f"CommandHook stdout 非 JSON: {text[:200]}")
        return HookResult(Decision.CONTINUE)
    if not isinstance(obj, dict):
        return HookResult(Decision.CONTINUE)
    return HookResult(
        decision=Decision(obj.get("decision", "continue")),
        reason=obj.get("reason", ""),
        data=obj.get("data"),
    )


# 需要 shell 解释的操作符/元字符（P3 加固判定依据）：
# 管道 | 后台 & 顺序 ; 重定向 < > 命令替换 $ ` 子shell ( ) 及换行。
# 注意：空格与引号不在列——shlex.split 能正确拆分含空格/引号的简单命令。
_SHELL_METACHARS = "|&;<>()$`\n\r"


def _contains_shell_metachars(command: str) -> bool:
    """命令是否包含必须经 shell 解释的操作符/元字符。"""
    return any(ch in _SHELL_METACHARS for ch in command)


class CommandHook(BaseHook):
    """进程外命令 hook（对标 Claude Code hooks 协议）。

    协议：
      - stdin:   HookContext.to_json()
      - stdout:  可选 JSON，含 decision/reason/data
      - exit code: 0=allow, 2=block(stderr→reason), 其他=非阻塞错误→CONTINUE
      - 超时:    timeout 秒后 kill 子进程 → CONTINUE
    """

    def __init__(self, command: str, timeout: int = 30,
                 cwd: str | None = None, name: str = ""):
        super().__init__()
        self.command = command
        self.timeout = timeout
        self.cwd = cwd
        self.name = name or command[:60]
        self.consecutive_errors: int = 0   # 连续异常次数（成功即清零）
        self.last_error: str = ""          # 最近一次异常信息
        self.last_error_at: float = 0.0    # 最近一次异常时间戳

    @classmethod
    def from_config(cls, cfg: dict) -> "CommandHook":
        """从配置字典创建 CommandHook，含注册期安全预检。

        cfg 格式: {"command": "...", "timeout": 30, "cwd": "...", "name": "..."}
        危险命令抛 PermissionError（由 manager._load_from_dict 捕获）。
        """
        command = cfg.get("command", "")
        if not command:
            raise ValueError("command hook 缺少 command 字段")
        # 注册期安全预检
        from core.sandbox.guard import check_command_safety
        is_safe, reason = check_command_safety(command)
        if not is_safe:
            raise PermissionError(f"命令被安全策略拒绝: {reason}")
        return cls(
            command=command,
            timeout=cfg.get("timeout", 30),
            cwd=cfg.get("cwd"),
            name=cfg.get("name", ""),
        )

    def run(self, ctx: HookContext) -> HookResult:
        # P3 加固（shell=True → 元字符感知执行）：
        # - 命令含 shell 操作符（| & ; < > $ ` ( ) 及换行）时保留 shell=True，
        #   这些命令依赖管道/重定向/变量展开语义，强行拆分会改变既有 hooks
        #   配置的行为；
        # - 否则 shlex.split 拆成 argv 列表直接 execvp，命令不再经 /bin/sh
        #   二次解析，消除注入面；拆分结果为空或解析失败（引号不成对等
        #   shlex.ValueError）时回退 shell=True，保持旧行为不崩溃。
        if _contains_shell_metachars(self.command):
            argv: str | list[str] = self.command
            use_shell = True
        else:
            try:
                parts = shlex.split(self.command)
            except ValueError:
                parts = []
            if parts:
                argv, use_shell = parts, False
            else:
                argv, use_shell = self.command, True
        try:
            proc = subprocess.run(
                argv, shell=use_shell, input=ctx.to_json(),
                capture_output=True, text=True, timeout=self.timeout,
                cwd=self.cwd, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"CommandHook '{self.name}' 超时({self.timeout}s): {self.command[:120]}")
            return HookResult(Decision.CONTINUE, reason="hook 超时")
        except Exception as exc:
            self.consecutive_errors += 1
            self.last_error = str(exc)
            self.last_error_at = time.time()
            logger.error(
                f"CommandHook '{self.name}' 启动失败（连续 {self.consecutive_errors} 次）: {self.command[:120]}",
                exc_info=True,
            )
            return HookResult(Decision.CONTINUE, reason="hook 启动失败")

        self.consecutive_errors = 0
        if proc.returncode == 2:
            reason = (proc.stderr or "").strip() or "hook 拦截"
            return HookResult(Decision.BLOCK, reason=reason)
        if proc.returncode != 0:
            logger.warning(
                f"CommandHook 非零退出({proc.returncode}): "
                f"stderr={proc.stderr[:200]}"
            )
            return HookResult(Decision.CONTINUE)
        return _parse_stdout(proc.stdout)
