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
from typing import TYPE_CHECKING, Any, Dict, Optional

from .base_tool import BaseTool

if TYPE_CHECKING:
    # 仅供注解；运行期经构造参数传入，避免与 core.mcp_client 循环导入
    from core.mcp_client import MCPConnection

logger = logging.getLogger('jk_agent')


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
    # MCP 工具是远程调用，SecurityGate 据此走 remote:call 策略
    capabilities = ("remote:call",)
    # 外层事件循环等待在协议层超时之上加少量宽限：保证内层
    # _send_request 先到期并抛出带方法名/服务器名的精确 TimeoutError，
    # 而不是外层先触发笼统的 future 超时。
    OUTER_TIMEOUT_GRACE_SECONDS = 5.0
    # 连接未暴露 default_call_timeout 时的兜底值（与协议层旧默认一致）。
    FALLBACK_CALL_TIMEOUT = 30.0

    def _call_timeout(self) -> float:
        """实际生效的工具调用超时（秒）。

        取连接级 default_call_timeout——由 MCPClientManager 按服务器配置
        tool_call_timeout > timeout 注入，默认 30s（P2-3）。外层事件循环与
        内层 tools/call 请求共用同一配置口径，不再出现"内层 30s / 外层
        硬编码 60s / 文案写死 60秒"三方分叉。
        """
        try:
            timeout = float(getattr(self._connection, "default_call_timeout", None))
        except (TypeError, ValueError):
            return self.FALLBACK_CALL_TIMEOUT
        return timeout if timeout > 0 else self.FALLBACK_CALL_TIMEOUT

    def __init__(self, connection: 'MCPConnection', tool_desc: dict, trust: bool = False):
        """
        参数:
            connection: MCPConnection 实例（来自 core/mcp_client.py）
            tool_desc: 工具描述字典，包含:
                - name: str — 工具名称（已加 {server_name}/ 前缀，用于注册表和 LLM）
                - description: str — 工具描述
                - inputSchema: dict — 参数定义的 JSON Schema
            trust: 是否受信任（来自 config.json 的 mcp.servers 中的 trust 标志）。
                   True 时 SecurityGate 直接放行，False 时每次调用需确认。
        """
        self.name = tool_desc["name"]
        # 协议调用用原始名（去掉注册用的 {server}/ 前缀），如 'web-search/search' → 'search'
        self._mcp_tool_name = self.name.split("/", 1)[1] if "/" in self.name else self.name
        self.description = tool_desc.get("description", "")
        # MCP 使用 inputSchema，BaseTool 使用 parameters —— 兼容转换
        self.parameters = tool_desc.get("inputSchema", {
            "type": "object",
            "properties": {},
            "required": [],
        })
        self._connection = connection
        self._tool_desc = tool_desc  # 保留原始描述，供调试使用
        self.trust = trust

    def execute(self, **kwargs) -> str:
        """
        同步调用 MCP 工具

        在 MCP 专用常驻事件循环中执行底层异步调用——该循环与初始化时
        使用的同一循环，保证连接状态与后台接收循环跨调用复用。

        超时口径（P2-3）：内层 tools/call 与外层事件循环等待都由服务器配置
        的 tool_call_timeout（回退 timeout，默认 30s）决定；外层额外加少量
        宽限，让协议层的精确超时错误先浮出。
        """
        timeout = self._call_timeout()
        try:
            from core.mcp_client import run_in_mcp_loop
            result = run_in_mcp_loop(
                self._execute_async(dict(kwargs), timeout=timeout),
                timeout=timeout + self.OUTER_TIMEOUT_GRACE_SECONDS)
            return self._format_result(result)
        except ConnectionError as e:
            logger.error(f"MCP 工具 '{self.name}' 连接失败: {e}")
            return (
                f"❌ MCP 服务器 '{self._connection.name}' 连接失败: {e}\n"
                f"请检查该服务是否正常运行。"
            )
        except TimeoutError:
            # Py3.11+ concurrent.futures.TimeoutError 与内置 TimeoutError 同义，
            # 内外两层超时都会落到这里。
            logger.error(
                f"MCP 工具 '{self.name}' 调用超时（>{timeout:g}s）")
            return (
                f"❌ MCP 工具 '{self.name}' 调用超时（>{timeout:g}秒）\n"
                f"可能是网络延迟或服务繁忙；如需更长等待窗口，可在 mcp 配置中"
                f"增大该服务器的 tool_call_timeout 后重试。"
            )
        except Exception as e:
            logger.error(
                f"MCP 工具 '{self.name}' 执行失败: {e}", exc_info=True
            )
            return f"❌ MCP 工具执行出错: {type(e).__name__}: {e}"

    async def _execute_async(self, arguments: dict,
                             timeout: Optional[float] = None) -> dict:
        """异步底层调用（协议层面用原始名，不含注册前缀）。

        timeout 透传到 call_tool/_send_request，与外层事件循环等待保持一致。
        注意 arguments 必须是独立 dict 参数而非 **kwargs 展开——工具参数里
        可能恰好有名为 ``timeout`` 的键，展开会与本参数冲突。
        """
        return await self._connection.call_tool(
            self._mcp_tool_name, arguments, timeout=timeout)

    def _format_result(self, result: dict) -> str:
        """
        将 MCP 工具返回的 content 数组格式化为纯文本

        支持多种内容类型:
            - text: 纯文本
            - resource: 资源引用（文件等）
            - json: JSON 数据

        结果统一截断到 20k 字符并做基础脱敏（API Key / 私钥 → ****）。
        """
        from core.sandbox.guard import sanitize_output

        if result.get("isError"):
            error_texts = [
                item["text"]
                for item in result.get("content", [])
                if item.get("type") == "text"
            ]
            error_msg = "\n".join(error_texts) if error_texts else "未知错误"
            if len(error_msg) > 20000:
                error_msg = error_msg[:20000] + "\n……（MCP 错误过长，已截断）"
            return sanitize_output(f"❌ MCP 工具返回错误: {error_msg}")

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

        text = "\n".join(parts)
        if len(text) > 20000:
            text = text[:20000] + "\n\n……（MCP 结果过长，已截断）"
        return sanitize_output(text)
