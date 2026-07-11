# -*- coding: utf-8 -*-
"""
记忆系统 —— 跨会话长期记忆（情景记忆）

提供 MemoryManager 类，自动将每轮对话归档到 memory/daily/，
并支持按关键词/日期搜索历史记忆。
"""

from .manager import MemoryManager

__all__ = ["MemoryManager"]
