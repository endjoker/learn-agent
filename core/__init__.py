# -*- coding: utf-8 -*-
"""
core 包 —— Agent 核心组件

包含 LLM 客户端、配置管理等核心基础设施。
后续可以在此添加：
- config.py       配置管理
- session.py      会话管理
- token_counter.py Token 计数
"""

from .llm_client import JKAgentLLM
from .system_prompt import SystemPrompt
from .mcp_client import MCPClientManager, MCPConnection

__all__ = [
    "JKAgentLLM",
    "SystemPrompt",
    "MCPClientManager",
    "MCPConnection",
]
