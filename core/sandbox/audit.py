# -*- coding: utf-8 -*-
"""
审计日志模块 —— 记录沙箱拦截 / 绕过 / 异常事件到 log/sandbox-audit.log

L6#9 链路优化：
- 写入前统一使用 core.sandbox.guard 的 SECRET_PATTERNS 规则脱敏
  （import 复用 guard.sanitize_output，本模块不复制实现）。
- 文件使用 RotatingFileHandler（maxBytes=10MB, backupCount=5），
  与 gateway-run.log 同策略；文件权限收紧为 0600。
- 支持 run_id / turn_id 上下文（set_audit_context 或显式参数），用于链路追踪。
"""

import contextvars
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .guard import sanitize_output

# 审计日志独立文件，不与主 logger 共用
_AUDIT_LOGGER: logging.Logger | None = None
_AUDIT_DIR = Path("log")
_AUDIT_FILE = _AUDIT_DIR / "sandbox-audit.log"

# RotatingFileHandler 策略（L6#9）：10MB × 5 备份，与 gateway-run.log 一致
_FILE_MAX_BYTES = 10 * 1024 * 1024
_FILE_BACKUP_COUNT = 5

# run_id/turn_id 上下文：由调度层在每轮处理入口 set_audit_context() 设置；
# 显式传入的参数优先于上下文。
_audit_ctx: contextvars.ContextVar = contextvars.ContextVar(
    "sandbox_audit_ctx", default={})


def set_audit_context(run_id: str = None, turn_id: str = None) -> None:
    """在当前执行上下文（async task / 线程）中设置审计日志的 run_id/turn_id。"""
    cur = dict(_audit_ctx.get())
    if run_id is not None:
        cur["run_id"] = str(run_id)
    if turn_id is not None:
        cur["turn_id"] = str(turn_id)
    _audit_ctx.set(cur)


def clear_audit_context() -> None:
    """清空当前执行上下文的审计 run_id/turn_id。"""
    _audit_ctx.set({})


def _ctx_suffix(run_id: str = "", turn_id: str = "") -> str:
    """构造 [run_id/turn_id] 后缀；两者均为空时返回空串（不改变原格式）。"""
    ctx = _audit_ctx.get()
    rid = run_id or ctx.get("run_id", "") or ""
    tid = turn_id or ctx.get("turn_id", "") or ""
    if rid or tid:
        return f" [{rid or '-'}/{tid or '-'}]"
    return ""


def _ensure_logger():
    """延迟初始化审计日志 logger（避免 import 时创建目录）"""
    global _AUDIT_LOGGER
    if _AUDIT_LOGGER is not None:
        return

    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        str(_AUDIT_FILE),
        maxBytes=_FILE_MAX_BYTES,
        backupCount=_FILE_BACKUP_COUNT,
        encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # 审计日志可能包含敏感片段，收紧文件权限为 0600
    try:
        os.chmod(_AUDIT_FILE, 0o600)
    except OSError:
        pass

    _AUDIT_LOGGER = logging.getLogger("sandbox_audit")
    _AUDIT_LOGGER.setLevel(logging.INFO)
    _AUDIT_LOGGER.propagate = False  # 不输出到控制台
    _AUDIT_LOGGER.addHandler(handler)


def log_interception(
    tool_name: str,
    command_snippet: str,
    rule: str,
    action: str = "BLOCKED",
    run_id: str = "",
    turn_id: str = "",
):
    """
    记录拦截事件

    参数:
        tool_name:     触发拦截的工具名（bash / write / http ...）
        command_snippet: 被拦截的命令或内容片段（脱敏后取前 200 字符）
        rule:           触发的规则名（SENSITIVE_FILE / DATA_LEAK / EXEC_INJECTION ...）
        action:         执行的动作（BLOCKED / SANITIZED / BYPASSED）
        run_id:         可选运行 ID（不传时回退到 set_audit_context 上下文）
        turn_id:        可选轮次 ID（不传时回退到 set_audit_context 上下文）
    """
    _ensure_logger()

    # 先按 guard.SECRET_PATTERNS 统一脱敏，再转义 / 截断，避免截断产生部分密钥残留
    snippet = sanitize_output(command_snippet).replace("\n", "\\n")[:200]
    _AUDIT_LOGGER.info(
        "%s | %s | %s | %s%s",
        sanitize_output(tool_name),
        snippet,
        sanitize_output(rule),
        sanitize_output(action),
        _ctx_suffix(run_id, turn_id),
    )


def log_bypass(tool_name: str, reason: str = "user_bypass",
               run_id: str = "", turn_id: str = ""):
    """记录沙箱绕过事件"""
    _ensure_logger()
    _AUDIT_LOGGER.info(
        "BYPASS | %s | %s | BYPASSED%s",
        sanitize_output(tool_name),
        sanitize_output(reason),
        _ctx_suffix(run_id, turn_id),
    )


def log_error(tool_name: str, message: str, run_id: str = "", turn_id: str = ""):
    """记录沙箱内部异常"""
    _ensure_logger()
    _AUDIT_LOGGER.info(
        "ERROR | %s | %s | ERROR%s",
        sanitize_output(tool_name),
        sanitize_output(message)[:200],
        _ctx_suffix(run_id, turn_id),
    )
