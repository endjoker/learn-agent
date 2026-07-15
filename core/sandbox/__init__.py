# -*- coding: utf-8 -*-
"""
沙箱执行器模块 —— 三层防护：内容拦截 → 资源隔离 → 子进程执行

导出:
    SandboxExecutor — 沙箱执行器主类
    SandboxResult   — 执行结果
"""

from .executor import SandboxExecutor, SandboxResult

__all__ = ["SandboxExecutor", "SandboxResult"]
