# -*- coding: utf-8 -*-
"""
tools 包 —— Agent 工具系统

本包为 AI 智能体提供完整的工具支持，包含：
- BaseTool:     工具基类，定义工具的接口规范
- ToolRegistry: 工具注册表，集中管理所有可用工具
- builtin_tools: 内置工具实现（读文件、写文件、编辑、搜索、查找、执行命令）

使用方式：
    from tools import BaseTool, ToolRegistry
    from tools.builtin_tools import ReadTool, WriteTool, BashTool

    registry = ToolRegistry()
    registry.register_tool(ReadTool())
    registry.register_tool(BashTool())
"""

from .base_tool import BaseTool
from .registry import ToolRegistry
from .mcp_tools import MCPTool

# 方便外部直接导入
__all__ = [
    "BaseTool",
    "ToolRegistry",
    "MCPTool",
]
