# -*- coding: utf-8 -*-
"""
MCP 客户端模块 —— Model Context Protocol 实现

提供三层架构：
1. 传输层（Transport）—— Stdio（本地子进程）和 SSE（远程 HTTP+SSE）
2. 协议层（MCPConnection）—— JSON-RPC 2.0 会话管理
3. 管理器（MCPClientManager）—— 多连接生命周期管理

使用方式：
    manager = MCPClientManager()
    conn = manager.add_server({
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "xxx"}
    })
    await manager.initialize_all()
    tools = await manager.discover_all_tools()
"""

import asyncio
import json
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any

logger = logging.getLogger('hello_agent.mcp')


# ============================================================
# 异常定义

class MCPError(Exception):
    """MCP 协议错误"""
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


# ============================================================
# 传输层（Transport Layer）

class MCPTransport(ABC):
    """MCP 传输层抽象基类"""

    @abstractmethod
    async def connect(self):
        """建立连接"""

    @abstractmethod
    async def send(self, message: str):
        """发送消息（JSON-RPC 字符串）"""

    @abstractmethod
    async def receive(self) -> Optional[str]:
        """接收消息，返回 None 表示连接关闭"""

    @abstractmethod
    async def close(self):
        """关闭连接"""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """是否已连接"""


class StdioTransport(MCPTransport):
    """
    基于子进程标准输入/输出的 MCP 传输
    适用于本地 MCP Server（如 npx 启动的服务）
    """

    def __init__(
        self,
        command: str,
        args: list = None,
        env: dict = None,
        cwd: str = None,
    ):
        self._command = command
        self._args = args or []
        self._env = env
        self._cwd = cwd
        self._process: Optional[asyncio.subprocess.Process] = None
        self._closed = False
        self._stderr_task: Optional[asyncio.Task] = None

    async def connect(self):
        """启动子进程并建立管道"""
        env = os.environ.copy()
        if self._env:
            env.update(self._env)

        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self._cwd,
            )
        except FileNotFoundError:
            raise ConnectionError(
                f"命令 '{self._command}' 未找到，请确认已安装。"
                f"提示：npx 模块需先执行 'npm install -g {self._command}' 或使用 npx -y"
            )

        # 启动 stderr 读取（日志输出）
        self._stderr_task = asyncio.create_task(self._read_stderr())

        logger.info(
            f"StdioTransport 已启动: {self._command} (pid={self._process.pid})"
        )

    async def send(self, message: str):
        """发送 JSON-RPC 消息（每行一条，以换行结尾）"""
        if self._process is None or self._process.stdin.is_closing():
            raise ConnectionError("stdin 已关闭，无法发送消息")
        data = (message + "\n").encode("utf-8")
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def receive(self) -> Optional[str]:
        """读取一行 JSON-RPC 响应"""
        if self._closed or (self._process and self._process.returncode is not None):
            return None
        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=30.0,
            )
            if not line:
                return None
            return line.decode("utf-8").rstrip("\n\r")
        except asyncio.TimeoutError:
            logger.warning("StdioTransport 接收超时")
            return None

    async def close(self):
        """关闭子进程"""
        self._closed = True
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        logger.info(f"StdioTransport 已关闭: {self._command}")

    @property
    def is_connected(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _read_stderr(self):
        """读取子进程的标准错误输出（日志）"""
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                logger.debug(f"[MCP {self._command}] {line.decode('utf-8').rstrip()}")
        except (asyncio.CancelledError, Exception):
            pass


class SSEHttpTransport(MCPTransport):
    """
    基于 HTTP POST + SSE（Server-Sent Events）的 MCP 传输
    适用于远程 MCP Server
    """

    def __init__(self, url: str, headers: dict = None, timeout: float = 30.0):
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout
        self._session = None
        self._sse_stream = None
        self._closed = False

    async def connect(self):
        """建立 SSE 连接"""
        import aiohttp

        self._session = aiohttp.ClientSession(headers=self._headers)
        try:
            self._sse_stream = await self._session.get(
                self._url,
                headers={"Accept": "text/event-stream"},
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
            logger.info(f"SSEHttpTransport 已连接: {self._url}")
        except Exception:
            await self._session.close()
            self._session = None
            raise

    async def send(self, message: str):
        """通过 HTTP POST 发送 JSON-RPC 消息"""
        if self._session is None:
            raise ConnectionError("会话未建立")
        async with self._session.post(
            self._url,
            data=message,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status not in (200, 202):
                raise RuntimeError(f"发送失败: HTTP {resp.status}")

    async def receive(self) -> Optional[str]:
        """从 SSE 流中读取下一条消息（data: 行）"""
        if self._closed or self._sse_stream is None:
            return None
        try:
            line = await asyncio.wait_for(
                self._sse_stream.content.readline(),
                timeout=self._timeout,
            )
            if not line:
                return None
            text = line.decode("utf-8").strip()
            if text.startswith("data: "):
                return text[6:]
            return None
        except asyncio.TimeoutError:
            return None

    async def close(self):
        """关闭连接"""
        self._closed = True
        if self._sse_stream:
            await self._sse_stream.release()
        if self._session:
            await self._session.close()
        logger.info(f"SSEHttpTransport 已关闭: {self._url}")

    @property
    def is_connected(self) -> bool:
        return self._session is not None and not self._session.closed


class StreamableHttpTransport(MCPTransport):
    """
    基于 Streamable HTTP 的 MCP 传输（新版 MCP 协议）

    适用于支持 Streamable HTTP 传输的 MCP Server（如 open-webSearch）。
    纯 HTTP POST 通信，无需 SSE 流，通过 Mcp-Session-Id 头维护会话。
    """

    def __init__(self, url: str, headers: dict = None, timeout: float = 30.0):
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout
        self._session = None
        self._session_id = None
        self._response_queue = None
        self._closed = False

    async def connect(self):
        """建立 HTTP 会话"""
        import aiohttp

        self._session = aiohttp.ClientSession(headers=self._headers)
        self._response_queue = asyncio.Queue()
        logger.info(f"StreamableHttpTransport 就绪: {self._url}")

    async def send(self, message: str):
        """通过 HTTP POST 发送 JSON-RPC 消息，响应入队供 receive() 读取"""
        if self._session is None:
            raise ConnectionError("会话未建立")

        import aiohttp

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        logger.debug(f"[Streamable] POST {self._url} | session={self._session_id}")
        logger.debug(f"[Streamable] Request: {message[:300]}")

        async with self._session.post(
            self._url,
            data=message,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        ) as resp:
            logger.debug(f"[Streamable] Response: HTTP {resp.status}")
            logger.debug(f"[Streamable] Response headers: {dict(resp.headers)}")

            if resp.status not in (200, 202):
                body = await resp.text()
                logger.error(f"[Streamable] Error body: {body[:500]}")
                raise RuntimeError(
                    f"发送失败: HTTP {resp.status} — {body[:200].strip()}"
                )

            # 记录服务端返回的 Session ID（后续请求需带上）
            sess_id = resp.headers.get("Mcp-Session-Id")
            if sess_id:
                self._session_id = sess_id

            # 读取响应体，剥离 SSE 帧后入队
            text = await resp.text()
            text = text.strip()
            if text:
                found = False
                for line in text.split("\n"):
                    if line.startswith("data: "):
                        await self._response_queue.put(line[6:])
                        found = True
                # 服务端可能直接返回纯 JSON（无 SSE 帧）
                if not found and text.startswith("{"):
                    await self._response_queue.put(text)

    async def receive(self) -> Optional[str]:
        """从响应队列中读取下一条消息（阻塞直到有响应）"""
        if self._closed:
            return None
        try:
            return await asyncio.wait_for(
                self._response_queue.get(),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return None

    async def close(self):
        """关闭连接"""
        self._closed = True
        if self._session:
            await self._session.close()
        logger.info(f"StreamableHttpTransport 已关闭: {self._url}")

    @property
    def is_connected(self) -> bool:
        return self._session is not None and not self._session.closed


# ============================================================
# 协议层（Protocol Layer）

class MCPConnection:
    """
    MCP 连接实例 —— 维护与单个 MCP Server 的状态化会话

    生命周期:
        initialize() → discover_tools() / call_tool() / ... → close()
    """

    # JSON-RPC 标准错误码
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    def __init__(self, name: str, transport: MCPTransport):
        self.name = name                       # Server 名称（唯一标识）
        self._transport = transport             # 传输层实例
        self._initialized = False               # 是否已完成初始化和能力协商
        self._capabilities: dict = {}           # 服务端声明的能力
        self._server_info: dict = {}            # 服务端信息
        self._pending: Dict[int, asyncio.Future] = {}  # 挂起的请求 id → Future
        self._recv_task: Optional[asyncio.Task] = None  # 接收循环任务
        self._request_id = 0                    # 自增请求 ID

    # ============================================================
    # 生命周期管理

    async def initialize(self) -> dict:
        """
        建立连接并进行初始化握手（能力协商）

        流程:
            1. 建立传输层连接
            2. 启动后台接收循环（处理所有后续响应）
            3. 发送 initialize 请求并等待响应
            4. 发送 initialized 通知

        返回:
            服务端的能力声明字典
        """
        if self._initialized:
            return self._capabilities

        # 1. 建立传输层连接
        try:
            await self._transport.connect()
        except Exception:
            await self._transport.close()
            raise

        # 2. 启动后台接收循环（必须先启动，才能处理 initialize 的响应）
        self._recv_task = asyncio.create_task(self._receive_loop())

        # 3. 发送 initialize 请求
        try:
            init_params = {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"subscribe": True, "listChanged": True},
                    "prompts": {"listChanged": True},
                },
                "clientInfo": {
                    "name": "hello-agent",
                    "version": "1.0.0",
                },
            }
            response = await self._send_request("initialize", init_params)
            result = response.get("result", {})

            self._capabilities = result.get("capabilities", {})
            self._server_info = result.get("serverInfo", {})
            self._protocol_version = result.get("protocolVersion", "unknown")

            # 4. 发送 initialized 通知
            await self._send_notification("initialized", {})

            self._initialized = True
            logger.info(
                f"MCP 连接已初始化: {self.name} "
                f"(v{self._protocol_version}), "
                f"server={self._server_info.get('name', 'unknown')}"
            )

            return self._capabilities

        except Exception:
            if self._recv_task:
                self._recv_task.cancel()
                self._recv_task = None
            await self._transport.close()
            raise

    async def close(self):
        """关闭连接，清理所有挂起请求"""
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        await self._transport.close()
        self._initialized = False
        # 通知所有挂起的请求连接已关闭
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("连接已关闭"))
        self._pending.clear()

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def capabilities(self) -> dict:
        return self._capabilities

    # ============================================================
    # 工具发现与调用

    async def discover_tools(self) -> List[dict]:
        """
        获取 MCP Server 暴露的所有工具描述

        返回:
            [{"name": ..., "description": ..., "inputSchema": ...}, ...]
        """
        if not self._initialized:
            raise RuntimeError("连接未初始化")

        response = await self._send_request("tools/list", {})
        result = response.get("result", {})
        tools = result.get("tools", [])
        logger.info(f"从 {self.name} 发现 {len(tools)} 个工具")
        return tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """
        调用 MCP 工具

        参数:
            name: 工具名称
            arguments: 工具参数字典

        返回:
            {"content": [...], "isError": False}
        """
        if not self._initialized:
            raise RuntimeError("连接未初始化")

        response = await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })

        if "error" in response:
            err = response["error"]
            return {
                "content": [{"type": "text", "text": f"[MCP Error] {err['message']}"}],
                "isError": True,
            }

        return response.get("result", {"content": [], "isError": False})

    async def discover_resources(self) -> List[dict]:
        """获取服务器资源列表（需服务端支持）"""
        if not self._capabilities.get("resources"):
            return []
        response = await self._send_request("resources/list", {})
        result = response.get("result", {})
        return result.get("resources", [])

    async def discover_prompts(self) -> List[dict]:
        """获取提示词模板列表（需服务端支持）"""
        if not self._capabilities.get("prompts"):
            return []
        response = await self._send_request("prompts/list", {})
        result = response.get("result", {})
        return result.get("prompts", [])

    # ============================================================
    # 内部方法

    async def _send_request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        """发送 JSON-RPC 请求并等待响应"""
        self._request_id += 1
        request_id = self._request_id

        message = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }, ensure_ascii=False)

        # 创建 Future 等待响应
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future

        try:
            await self._transport.send(message)
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise TimeoutError(f"MCP 请求超时 ({timeout}s): {method}")
        except Exception:
            self._pending.pop(request_id, None)
            raise

    async def _send_notification(self, method: str, params: dict):
        """发送 JSON-RPC 通知（无响应期待）"""
        message = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }, ensure_ascii=False)
        await self._transport.send(message)

    async def _receive_loop(self):
        """后台接收循环：持续从传输层读取消息并分发到对应的 Future"""
        try:
            while self._transport.is_connected:
                raw = await self._transport.receive()
                if raw is None:
                    break

                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"收到非法 JSON: {raw[:100]}")
                    continue

                # 有 id → 响应（匹配挂起的请求）；无 id → 通知
                if "id" in message:
                    request_id = message["id"]
                    future = self._pending.pop(request_id, None)
                    if future and not future.done():
                        future.set_result(message)
                else:
                    await self._handle_notification(message)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"MCP 接收循环异常 ({self.name}): {e}")

    async def _handle_notification(self, message: dict):
        """处理服务端发来的通知"""
        method = message.get("method", "")
        logger.debug(f"MCP 通知 ({self.name}): {method}")

        if method == "notifications/tools/list_changed":
            logger.info(
                f"MCP Server '{self.name}' 工具列表已变更，"
                f"建议重新 discover_tools()"
            )
        elif method == "notifications/resources/list_changed":
            logger.info(f"MCP Server '{self.name}' 资源列表已变更")


# ============================================================
# 管理器（Manager Layer）

class MCPClientManager:
    """
    MCP 客户端管理器

    管理多个 MCPConnection 实例，提供统一的连接、发现和关闭接口。
    支持优雅降级：单个服务器失败不影响其他服务器。
    """

    def __init__(self):
        self._connections: Dict[str, MCPConnection] = {}  # name → MCPConnection
        self._configs: Dict[str, dict] = {}                # name → config

    def add_server(self, config: dict) -> MCPConnection:
        """
        根据配置添加一个 MCP 服务器

        config 格式:
        {
            "name": "github",
            "transport": "stdio" 或 "http+sse",
            "command": "npx",           # stdio 模式
            "args": ["-y", "..."],     # stdio 模式（可选）
            "url": "http://...",       # http+sse 模式
            "headers": {...},           # http+sse 模式（可选）
            "env": {...},               # stdio 模式（可选）
            "timeout": 30,              # 超时秒数（可选）
        }

        返回:
            MCPConnection 实例
        """
        name = config["name"]
        if name in self._connections:
            raise ValueError(f"MCP 服务器 '{name}' 已存在")

        transport = self._create_transport(config)
        connection = MCPConnection(name, transport)
        self._connections[name] = connection
        self._configs[name] = config
        logger.info(f"添加 MCP 服务器: {name}")
        return connection

    def remove_server(self, name: str) -> bool:
        """移除并关闭一个服务器连接"""
        conn = self._connections.pop(name, None)
        self._configs.pop(name, None)
        if conn:
            # 同步方式关闭（调用方可能没有事件循环）
            try:
                asyncio.run(conn.close())
            except RuntimeError:
                # 已在事件循环中
                loop = asyncio.get_event_loop()
                loop.create_task(conn.close())
            logger.info(f"移除 MCP 服务器: {name}")
            return True
        return False

    def get_connection(self, name: str) -> Optional[MCPConnection]:
        """获取指定名称的连接"""
        return self._connections.get(name)

    def list_connections(self) -> List[str]:
        """列出所有已添加的服务器名称"""
        return list(self._connections.keys())

    async def initialize_all(self, timeout: float = 30.0):
        """
        初始化所有未初始化的连接

        单点失败不影响其他服务器（优雅降级）。
        """
        if not self._connections:
            return

        tasks = []
        for name, conn in self._connections.items():
            if not conn.is_initialized:
                tasks.append(self._safe_initialize(conn, name))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_initialize(self, conn: MCPConnection, name: str, max_retries: int = 2):
        """带重试的安全初始化"""
        for attempt in range(max_retries):
            try:
                await conn.initialize()
                logger.info(f"MCP 服务器 '{name}' 初始化成功")
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"MCP 服务器 '{name}' 初始化失败 "
                        f"(第 {attempt + 1} 次)，即将重试: {e}"
                    )
                    await asyncio.sleep(1)
                else:
                    logger.error(
                        f"MCP 服务器 '{name}' 初始化失败 "
                        f"(已重试 {max_retries} 次): {e}"
                    )

    async def discover_all_tools(self) -> Dict[str, List[dict]]:
        """
        从所有已初始化连接中收集工具列表

        返回:
            {"server_name": [tool_desc, ...], ...}
        """
        result = {}
        for name, conn in self._connections.items():
            if conn.is_initialized:
                try:
                    tools = await conn.discover_tools()
                    result[name] = tools
                    logger.info(f"从 '{name}' 发现 {len(tools)} 个工具")
                except Exception as e:
                    logger.error(f"从 '{name}' 发现工具失败: {e}")
                    result[name] = []
        return result

    async def close_all(self):
        """关闭所有连接"""
        for name, conn in list(self._connections.items()):
            try:
                await conn.close()
            except Exception as e:
                logger.error(f"关闭 MCP 连接 '{name}' 出错: {e}")
        self._connections.clear()
        self._configs.clear()
        logger.info("所有 MCP 连接已关闭")

    def _create_transport(self, config: dict) -> MCPTransport:
        """根据配置创建对应的传输层实例"""
        transport_type = config.get("transport", "stdio")
        if transport_type == "stdio":
            return StdioTransport(
                command=config["command"],
                args=config.get("args"),
                env=config.get("env"),
                cwd=config.get("cwd"),
            )
        elif transport_type == "http+sse":
            return SSEHttpTransport(
                url=config["url"],
                headers=config.get("headers"),
                timeout=config.get("timeout", 30.0),
            )
        elif transport_type == "streamable":
            return StreamableHttpTransport(
                url=config["url"],
                headers=config.get("headers"),
                timeout=config.get("timeout", 30.0),
            )
        else:
            raise ValueError(
                f"不支持的传输类型: '{transport_type}'。"
                f"可选: 'stdio', 'http+sse', 'streamable'"
            )
