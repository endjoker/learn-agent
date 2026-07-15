# -*- coding: utf-8 -*-
"""
MCP 工具桥接层 —— 将远程 MCP 工具适配为本地 BaseTool 接口

MCPTool 包装 MCP Server 暴露的工具，使 LLM 无需感知工具是本地还是远程。
工具名称自动添加 {server_name}/ 前缀以避免命名冲突。

使用方式：
    from tools.mcp_tools import MCPTool

    # 注册 MCP 工具到注册表
    conn = mcp_manager.get_connection("github")
    tool = MCPTool(connection=conn, tool_desc={
        "name": "create_issue",
        "description": "创建 Issue",
        "inputSchema": {"type": "object", "properties": {...}, "required": [...]}
    })
    registry.register_tool(tool)
"""

import json
import logging
from typing import Any, Dict, Optional

from .base_tool import BaseTool

logger = logging.getLogger('hello_agent')


class MCPTool(BaseTool):
    """
    MCP 工具桥接 —— 将 MCP Server 的工具包装为 BaseTool

    透明地连接 MCP 传输层和 Agent 工具系统，LLM 通过统一的
    ToolRegistry 接口调用 MCP 工具，无需区分本地或远程。
    """

    # BaseTool 要求的类属性（在 __init__ 中被实例属性覆盖）
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}, "required": []}

    def __init__(self, connection: 'MCPConnection', tool_desc: dict):
        """
        参数:
            connection: MCPConnection 实例（来自 core/mcp_client.py）
            tool_desc: 工具描述字典，包含:
                - name: str — 工具名称（建议加 {server_name}/ 前缀）
                - description: str — 工具描述
                - inputSchema: dict — 参数定义的 JSON Schema
        """
        self.name = tool_desc["name"]
        self.description = tool_desc.get("description", "")
        # MCP 使用 inputSchema，BaseTool 使用 parameters —— 兼容转换
        self.parameters = tool_desc.get("inputSchema", {
            "type": "object",
            "properties": {},
            "required": [],
        })
        self._connection = connection
        self._tool_desc = tool_desc  # 保留原始描述，供调试使用

    def execute(self, **kwargs) -> str:
        """
        同步调用 MCP 工具

        在 MCP 专用常驻事件循环中执行底层异步调用——该循环与初始化时
        使用的同一循环，保证连接状态与后台接收循环跨调用复用。
        """
        try:
            from core.mcp_client import run_in_mcp_loop
            result = run_in_mcp_loop(self._execute_async(**kwargs), timeout=60)
            return self._format_result(result)
        except ConnectionError as e:
            logger.error(f"MCP 工具 '{self.name}' 连接失败: {e}")
            return (
                f"❌ MCP 服务器 '{self._connection.name}' 连接失败: {e}\n"
                f"请检查该服务是否正常运行。"
            )
        except TimeoutError:
            logger.error(f"MCP 工具 '{self.name}' 调用超时")
            return (
                f"❌ MCP 工具 '{self.name}' 调用超时（60秒）\n"
                f"可能是网络延迟或服务繁忙，请重试。"
            )
        except Exception as e:
            logger.error(
                f"MCP 工具 '{self.name}' 执行失败: {e}", exc_info=True
            )
            return f"❌ MCP 工具执行出错: {type(e).__name__}: {e}"

    async def _execute_async(self, **kwargs) -> dict:
        """异步底层调用"""
        return await self._connection.call_tool(self.name, kwargs)

    def _format_result(self, result: dict) -> str:
        """
        将 MCP 工具返回的 content 数组格式化为纯文本

        支持多种内容类型:
            - text: 纯文本
            - resource: 资源引用（文件等）
            - json: JSON 数据
        """
        if result.get("isError"):
            error_texts = [
                item["text"]
                for item in result.get("content", [])
                if item.get("type") == "text"
            ]
            error_msg = "\n".join(error_texts) if error_texts else "未知错误"
            return f"❌ MCP 工具返回错误: {error_msg}"

        parts = []
        for item in result.get("content", []):
            item_type = item.get("type", "text")
            if item_type == "text":
                parts.append(item.get("text", ""))
            elif item_type == "resource":
                resource = item.get("resource", {})
                uri = resource.get("uri", "")
                text = resource.get("text", "")
                mime = resource.get("mimeType", "")
                parts.append(f"[资源] {uri} ({mime}):\n{text}")
            elif item_type == "json":
                parts.append(
                    json.dumps(item.get("json", {}), ensure_ascii=False, indent=2)
                )
            else:
                parts.append(str(item))

        return "\n".join(parts)
