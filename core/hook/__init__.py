# -*- coding: utf-8 -*-
"""
Hook 模块 — 事件驱动生命周期钩子

基于 code/learn/HOOK/design.md 设计，让用户在 agent 关键执行点插入自定义逻辑
（审计/通知/改写/拦截），而不改动 agent.py 主流程。

导出:
    HookManager  — 管理器（注册/分发/配置加载）
    HookEvent    — 12 个生命周期事件枚举
    HookContext  — 事件上下文数据类
    Decision     — 裁决枚举（ALLOW/BLOCK/MODIFY/CONTINUE）
    HookResult   — 裁决结果
    PythonHook   — 进程内回调
    CommandHook  — 进程外命令（对标 Claude Code hooks 协议）
"""

from .events import HookEvent, HookContext, HookResult, Decision
from .hooks import BaseHook, PythonHook, CommandHook
from .manager import HookManager
from .builtin import (audit_logger, webhook_notifier,
                      sensitive_word_filter, block_pattern_filter)

__all__ = [
    "HookManager",
    "HookEvent",
    "HookContext",
    "HookResult",
    "Decision",
    "BaseHook",
    "PythonHook",
    "CommandHook",
    "audit_logger",
    "webhook_notifier",
    "sensitive_word_filter",
    "block_pattern_filter",
]
