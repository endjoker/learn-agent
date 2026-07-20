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
import subprocess
from typing import Callable

from .events import HookContext, HookResult, Decision, _coerce

logger = logging.getLogger("hello_agent.hook")


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

    def run(self, ctx: HookContext) -> HookResult:
        try:
            return _coerce(self.fn(ctx))
        except Exception:
            logger.error(f"PythonHook '{self.name}' 执行异常", exc_info=True)
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
        suppress=obj.get("suppress", False),
    )


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

    def run(self, ctx: HookContext) -> HookResult:
        try:
            proc = subprocess.run(
                self.command, shell=True, input=ctx.to_json(),
                capture_output=True, text=True, timeout=self.timeout,
                cwd=self.cwd, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"CommandHook 超时({self.timeout}s): {self.command[:120]}")
            return HookResult(Decision.CONTINUE, reason="hook 超时")
        except Exception:
            logger.error(f"CommandHook 启动失败: {self.command[:120]}", exc_info=True)
            return HookResult(Decision.CONTINUE, reason="hook 启动失败")

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
