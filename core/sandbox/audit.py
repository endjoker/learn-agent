# -*- coding: utf-8 -*-
"""
审计日志模块 —— 记录沙箱拦截 / 绕过 / 异常事件到 log/sandbox-audit.log
"""

import os
import logging
from datetime import datetime
from pathlib import Path

# 审计日志独立文件，不与主 logger 共用
_AUDIT_LOGGER: logging.Logger | None = None
_AUDIT_DIR = Path("log")
_AUDIT_FILE = _AUDIT_DIR / "sandbox-audit.log"


def _ensure_logger():
    """延迟初始化审计日志 logger（避免 import 时创建目录）"""
    global _AUDIT_LOGGER
    if _AUDIT_LOGGER is not None:
        return

    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(str(_AUDIT_FILE), encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    _AUDIT_LOGGER = logging.getLogger("sandbox_audit")
    _AUDIT_LOGGER.setLevel(logging.INFO)
    _AUDIT_LOGGER.propagate = False  # 不输出到控制台
    _AUDIT_LOGGER.addHandler(handler)


def log_interception(
    tool_name: str,
    command_snippet: str,
    rule: str,
    action: str = "BLOCKED",
):
    """
    记录拦截事件

    参数:
        tool_name:     触发拦截的工具名（bash / write / http ...）
        command_snippet: 被拦截的命令或内容片段（前 200 字符）
        rule:           触发的规则名（SENSITIVE_FILE / DATA_LEAK / EXEC_INJECTION ...）
        action:         执行的动作（BLOCKED / SANITIZED / BYPASSED）
    """
    _ensure_logger()

    snippet = command_snippet[:200].replace("\n", "\\n")
    _AUDIT_LOGGER.info(
        "%s | %s | %s | %s",
        tool_name,
        snippet,
        rule,
        action,
    )


def log_bypass(tool_name: str, reason: str = "user_bypass"):
    """记录沙箱绕过事件"""
    _ensure_logger()
    _AUDIT_LOGGER.info(
        "BYPASS | %s | %s | BYPASSED",
        tool_name,
        reason,
    )


def log_error(tool_name: str, message: str):
    """记录沙箱内部异常"""
    _ensure_logger()
    _AUDIT_LOGGER.info(
        "ERROR | %s | %s | ERROR",
        tool_name,
        message[:200],
    )
