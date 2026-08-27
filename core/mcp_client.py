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
import threading
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any

from core.orphan_processes import record as record_orphan_process

logger = logging.getLogger('jk_agent.mcp')


def _mask_sensitive(text: str) -> str:
    """对疑似密钥/令牌片段打码（调试日志打印请求体前调用）。"""
    import re
    # 常见密钥形态：sk-xxx / Bearer token / JSON 键值
    masked = re.sub(r'sk-[A-Za-z0-9_-]{6,}', 'sk-***', text)
    masked = re.sub(
        r'("(?:api[_-]?key|token|secret|authorization|password)"\s*:\s*")[^"]*(")',
        r'\1***\2', masked, flags=re.IGNORECASE)
    return masked


# ============================================================
# MCP 专用事件循环
#
# MCP 连接是长生命周期有状态对象（后台接收循环 _recv_task、
# 子进程 / aiohttp session、响应队列），必须在同一个事件循环上
# 创建并跨多次调用复用。若用 asyncio.run() 每次新建并关闭循环，
# 接收循环会在 init 结束时被取消，后续工具调用永远收不到响应。
#
# 本单例在后台 daemon 线程跑一个常驻事件循环；同步代码（ReAct 循环）
# 通过 run_coroutine_threadsafe 线程安全地把协程调度到该循环执行。

class _MCPLoopRunner:
    """后台常驻事件循环（daemon 线程），供 MCP 全部异步操作复用。"""

    _instance: Optional["_MCPLoopRunner"] = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "_MCPLoopRunner":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="jkagent-mcp-loop", daemon=True
        )
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def run(self, coro, timeout: Optional[float] = None):
        """把协程调度到后台循环执行，阻塞等待结果（线程安全）。"""
        if self._loop is None or self._loop.is_closed():
            raise RuntimeError("MCP 事件循环已关闭")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)


def run_in_mcp_loop(coro, timeout: Optional[float] = None):
    """在 MCP 专用事件循环中运行协程（供同步代码调用）。"""
    return _MCPLoopRunner.get().run(coro, timeout=timeout)


def fire_and_forget_in_mcp_loop(coro):
    """把协程投到 MCP 专用事件循环后台执行，不阻塞当前线程（B6/L6#1 预热用）。

    与 run_in_mcp_loop 的区别：调度后立即返回，不等待协程完成；协程内异常
    由守护包装记录，不外抛（预热失败只影响首轮可用性，不阻断 agent 创建/
    返回）。注意协程对象只能被调度一次，调用方须保证不重复使用。
    """
    runner = _MCPLoopRunner.get()
    if runner._loop is None or runner._loop.is_closed():
        raise RuntimeError("MCP 事件循环已关闭")

    async def _guard():
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP 后台任务（预热）执行失败")

    def _schedule():
        try:
            runner._loop.create_task(_guard())
        except Exception:
            logger.exception("MCP 后台任务（预热）调度失败")

    runner._loop.call_soon_threadsafe(_schedule)


def _sse_data_payload(line: str) -> Optional[str]:
    """解析一行 SSE data 字段。

    兼容规范要求的 ``data: x`` 与部分服务端实现的 ``data:x``（无空格）两种形式。
    返回 payload（已去掉一个可选前导空格）；非 data 行返回 None。
    """
    if line.startswith("data:"):
        return line[5:].lstrip(" ")
    return None


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
        self._send_lock = asyncio.Lock()  # 串行化 write+drain，避免并发写交错

    async def connect(self):
        """启动子进程并建立管道"""
        # 断线重连时先清理旧进程，避免子进程泄漏
        if self._process is not None and self._process.returncode is None:
            old_pid = self._process.pid
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                pass
            # P2-2：被替换的旧子进程已终止，孤儿日志同步收口（active=False）。
            record_orphan_process(old_pid, False)
            self._process = None
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

        # P2-2：spawn 成功即登记孤儿日志（对齐 sandbox/executor 与
        # process_manager 的 record(pid, True) 先例）。asyncio 子进程在
        # create_subprocess_exec 返回后 pid 即可用；后续 initialize 握手
        # 失败也会走 close() 收口为 active=False，崩溃遗留则由启动回收
        # （reap_stale_orphans）按日志清理。
        record_orphan_process(self._process.pid, True)

        # 启动 stderr 读取（日志输出）
        self._stderr_task = asyncio.create_task(self._read_stderr())

        logger.info(
            f"StdioTransport 已启动: {self._command} (pid={self._process.pid})"
        )

    async def send(self, message: str):
        """发送 JSON-RPC 消息（每行一条，以换行结尾）"""
        async with self._send_lock:
            if self._process is None or self._process.stdin.is_closing():
                raise ConnectionError("stdin 已关闭，无法发送消息")
            data = (message + "\n").encode("utf-8")
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def receive(self) -> Optional[str]:
        """读取一行 JSON-RPC 响应；返回 None 仅表示连接已关闭（EOF）。

        不设空闲超时——空闲时阻塞等待即可，子进程退出或 close() 取消
        本任务时会自然解除阻塞。原 30s 超时会让空闲连接被误判为断开。
        """
        if self._closed:
            return None
        if self._process is None or self._process.returncode is not None:
            return None
        try:
            line = await self._process.stdout.readline()
            if not line:
                # EOF：子进程已关闭 stdout
                return None
            return line.decode("utf-8").rstrip("\n\r")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"StdioTransport 接收异常: {e}")
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
        pid = self._process.pid if self._process else None
        try:
            if self._process and self._process.returncode is None:
                try:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
        finally:
            # P2-2：无论正常关闭、超时强杀还是清理路径抛错，都把孤儿日志
            # 收口为 active=False（record 自身永不抛错），避免崩溃恢复逻辑
            # 之后误杀已被正常回收的 pid。
            if pid is not None:
                record_orphan_process(pid, False)
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

    # SSE GET 不设 total 超时（流可能长期存活），用 sock_read 心跳超时兜底
    DEFAULT_SOCK_READ_TIMEOUT = 60.0

    def __init__(self, url: str, headers: dict = None, timeout: float = 30.0,
                 sock_read_timeout: float = DEFAULT_SOCK_READ_TIMEOUT):
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout
        self._sock_read_timeout = sock_read_timeout
        self._session = None
        self._sse_stream = None
        self._endpoint: Optional[str] = None  # 服务端宣告的 JSON-RPC POST 地址
        self._closed = False

    async def connect(self):
        """建立 SSE 连接"""
        import aiohttp

        # 重连时先清理旧会话/旧流，避免跨连接句柄泄漏
        if self._sse_stream is not None:
            try:
                await self._sse_stream.release()
            except Exception:
                pass
            self._sse_stream = None
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._endpoint = None

        self._session = aiohttp.ClientSession(headers=self._headers)
        try:
            self._sse_stream = await self._session.get(
                self._url,
                headers={"Accept": "text/event-stream"},
                # total=None：不限制流总时长；sock_read 心跳保证断流/静默可被感知
                timeout=aiohttp.ClientTimeout(total=None,
                                              sock_read=self._sock_read_timeout),
            )
            logger.info(f"SSEHttpTransport 已连接: {self._url}")
        except Exception:
            await self._session.close()
            self._session = None
            raise

    async def send(self, message: str):
        """通过 HTTP POST 发送 JSON-RPC 消息（优先使用服务端宣告的 endpoint）"""
        if self._session is None:
            raise ConnectionError("会话未建立")
        target = self._endpoint or self._url
        async with self._session.post(
            target,
            data=message,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status not in (200, 202):
                raise RuntimeError(f"发送失败: HTTP {resp.status} (POST {target})")

    async def receive(self) -> Optional[str]:
        """从 SSE 流中读取下一条 data: 消息。

        兼容：
          - 同一事件的多行 data 按规范用换行拼接为一条消息
          - ``data: x`` 与无空格 ``data:x`` 两种形式
          - event: endpoint 事件：按 MCP SSE 规范，用服务端宣告的地址
            作为后续 JSON-RPC POST 目标（不把 endpoint 当响应返回）
        返回 None 仅表示流已关闭（EOF）。
        """
        from urllib.parse import urljoin

        data_lines: list = []
        pending_event = None  # 当前事件的 event: 名（若有）
        while True:
            if self._closed or self._sse_stream is None:
                return None
            try:
                line = await self._sse_stream.content.readline()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"SSEHttpTransport 接收异常: {e}")
                return None
            if not line:
                # EOF：流已关闭；若已累积 data 行，先交付
                if data_lines:
                    return "\n".join(data_lines)
                return None
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not text:
                # 空行 = SSE 事件边界：交付累积的 data 行
                if data_lines:
                    message = "\n".join(data_lines)
                    data_lines = []
                    return message
                continue
            if text.startswith("event:"):
                pending_event = text[6:].strip()
                continue
            payload = _sse_data_payload(text)
            if payload is not None:
                if pending_event == "endpoint":
                    # 按 MCP SSE 规范：endpoint 事件的值是后续 JSON-RPC POST 地址
                    self._endpoint = urljoin(self._url, payload.strip())
                    logger.info(f"SSE endpoint 已更新: {self._endpoint}")
                    pending_event = None
                    continue
                data_lines.append(payload)
                continue
            if text.startswith(":") or text.startswith("id:") \
                    or text.startswith("retry:"):
                continue  # 注释 / 元数据字段，不终止当前事件
            # 其他未知字段行 → 事件边界兜底
            if data_lines:
                message = "\n".join(data_lines)
                data_lines = []
                return message

    async def close(self):
        """关闭连接"""
        self._closed = True
        if self._sse_stream:
            try:
                await self._sse_stream.release()
            except Exception:
                pass
            self._sse_stream = None
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
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
        self._reader_tasks: set = set()  # 后台响应读取任务
        self._closed = False

    async def connect(self):
        """建立 HTTP 会话"""
        import aiohttp

        # 重连时先清理旧会话，避免句柄泄漏
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = aiohttp.ClientSession(headers=self._headers)
        self._response_queue = asyncio.Queue()
        logger.info(f"StreamableHttpTransport 就绪: {self._url}")

    async def send(self, message: str):
        """通过 HTTP POST 发送 JSON-RPC 消息。

        响应由后台任务 _read_response 流式解析入队供 receive() 消费，
        send() 不内联读取响应体——否则遇到 text/event-stream 长流会
        阻塞到流关闭，导致 _send_request 无法 await 响应 Future。
        """
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
        logger.debug(f"[Streamable] Request: {_mask_sensitive(message[:500])}")

        try:
            resp = await self._session.post(
                self._url,
                data=message,
                headers=headers,
                # total=None：长 SSE 流不被 total 截断；sock_read 心跳兜底
                timeout=aiohttp.ClientTimeout(total=None,
                                              sock_read=self._timeout),
            )
        except Exception as e:
            raise ConnectionError(f"发送失败: 网络错误: {e}")

        logger.debug(f"[Streamable] Response: HTTP {resp.status}")
        logger.debug(f"[Streamable] Response headers: {dict(resp.headers)}")

        if resp.status not in (200, 202):
            body = await resp.text()
            logger.error(f"[Streamable] Error body: {_mask_sensitive(body[:500])}")
            resp.release()
            raise RuntimeError(
                f"发送失败: HTTP {resp.status} — {body[:200].strip()}"
            )

        # 记录服务端返回的 Session ID（后续请求需带上）
        sess_id = resp.headers.get("Mcp-Session-Id")
        if sess_id:
            self._session_id = sess_id

        # 后台流式读取响应并解析帧入队，send() 立即返回
        task = asyncio.create_task(self._read_response(resp))
        self._reader_tasks.add(task)
        task.add_done_callback(self._reader_tasks.discard)

    async def _read_response(self, resp):
        """读取 HTTP 响应体，剥离 SSE 帧后入队；支持纯 JSON 与 text/event-stream。

        读取异常（连接中断/心跳超时等）会把真实错误写入响应队列，
        receive() 将其抛给调用方，避免挂起的 Future 永久等待。
        """
        try:
            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                # 流式逐行解析，避免在长 SSE 流上阻塞
                data_lines = []
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    payload = _sse_data_payload(line)
                    if payload is not None:
                        data_lines.append(payload)
                        continue
                    if not line.strip() and data_lines:
                        # 事件边界：多行 data 拼接为一条消息
                        await self._response_queue.put("\n".join(data_lines))
                        data_lines = []
                if data_lines:
                    await self._response_queue.put("\n".join(data_lines))
            else:
                text = (await resp.text()).strip()
                if text:
                    found = False
                    data_lines = []
                    for line in text.split("\n"):
                        payload = _sse_data_payload(line.strip())
                        if payload is not None:
                            data_lines.append(payload)
                            found = True
                            continue
                        if data_lines:
                            await self._response_queue.put("\n".join(data_lines))
                            data_lines = []
                    if data_lines:
                        await self._response_queue.put("\n".join(data_lines))
                    # 服务端可能直接返回纯 JSON（无 SSE 帧）
                    if not found and text.startswith("{"):
                        await self._response_queue.put(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[Streamable] 读取响应失败: {e}")
            try:
                # 把真实错误传给等待方（receive 会抛出），而不是静默丢弃
                await self._response_queue.put(e)
            except Exception:
                pass
        finally:
            # 确保响应体被释放（包括取消路径），不留挂起 reader
            try:
                resp.release()
            except Exception:
                pass

    async def receive(self) -> Optional[str]:
        """从响应队列中读取下一条消息（阻塞直到有响应或连接关闭）。"""
        if self._closed:
            return None
        try:
            # 阻塞等待；close() 取消 _recv_task 时会解除阻塞
            item = await self._response_queue.get()
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if isinstance(item, Exception):
            # 读取阶段失败：把真实错误抛给调用方（_receive_loop 收口并复位连接）
            raise item
        return item

    async def close(self):
        """关闭连接"""
        self._closed = True
        # 取消所有后台响应读取任务
        for task in list(self._reader_tasks):
            task.cancel()
        for task in list(self._reader_tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._reader_tasks.clear()
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

    # 单次 JSON-RPC 请求的默认等待窗口（秒）。工具调用（tools/call）可被
    # 服务器配置 tool_call_timeout（或沿用 timeout 键）覆盖，见 add_server。
    DEFAULT_CALL_TIMEOUT = 30.0

    def __init__(self, name: str, transport: MCPTransport,
                 call_timeout: Optional[float] = None):
        self.name = name                       # Server 名称（唯一标识）
        self._transport = transport             # 传输层实例
        self._initialized = False               # 是否已完成初始化和能力协商
        self._ever_initialized = False          # 是否曾成功初始化（断线后据此自动重连）
        self._capabilities: dict = {}           # 服务端声明的能力
        self._server_info: dict = {}            # 服务端信息
        self._pending: Dict[int, asyncio.Future] = {}  # 挂起的请求 id → Future
        self._recv_task: Optional[asyncio.Task] = None  # 接收循环任务
        self._request_id = 0                    # 自增请求 ID
        # P2-3：连接级默认调用超时。旧实现 tools/call 硬编码 30s，服务器配置
        # 的 timeout 只作用于 HTTP sock_read——慢工具必然假超时。此处由管理器
        # 按服务器配置注入；未注入时保持原默认 30s。
        try:
            resolved = float(call_timeout) if call_timeout is not None else None
        except (TypeError, ValueError):
            resolved = None
        self.default_call_timeout: float = (
            resolved if resolved and resolved > 0 else self.DEFAULT_CALL_TIMEOUT)

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
                    "name": "jkagent",
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
            self._ever_initialized = True
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
        self._initialized = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None
        try:
            await self._transport.close()
        finally:
            # 通知所有挂起的请求连接已关闭（即使 transport.close 抛错）
            self._fail_pending(ConnectionError("连接已关闭"))

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # ============================================================
    # 工具发现与调用

    async def discover_tools(self) -> List[dict]:
        """
        获取 MCP Server 暴露的所有工具描述

        返回:
            [{"name": ..., "description": ..., "inputSchema": ...}, ...]
        """
        if not self._initialized:
            if self._ever_initialized:
                await self.initialize()  # 断线后自动重连
            else:
                raise RuntimeError("连接未初始化")

        response = await self._send_request("tools/list", {})
        if "error" in response:
            err = response["error"]
            raise RuntimeError(f"MCP tools/list 返回错误: {err.get('message', err)}")
        result = response.get("result", {})
        tools = result.get("tools", [])
        logger.info(f"从 {self.name} 发现 {len(tools)} 个工具")
        return tools

    async def call_tool(self, name: str, arguments: dict,
                        timeout: Optional[float] = None) -> dict:
        """
        调用 MCP 工具

        参数:
            name: 工具名称
            arguments: 工具参数字典
            timeout: 本次调用等待响应的超时（秒）。None 时使用连接级默认
                default_call_timeout（服务器配置 tool_call_timeout > timeout
                > 默认 30s），与 MCPTool 外层事件循环等待保持同一口径（P2-3）。

        返回:
            {"content": [...], "isError": False}
        """
        if not self._initialized:
            if self._ever_initialized:
                await self.initialize()  # 断线后自动重连
            else:
                raise RuntimeError("连接未初始化")

        response = await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        }, timeout=self._resolve_timeout(timeout))

        if "error" in response:
            err = response["error"]
            return {
                "content": [{"type": "text", "text": f"[MCP Error] {err['message']}"}],
                "isError": True,
            }

        return response.get("result", {"content": [], "isError": False})

    # ============================================================
    # 内部方法

    def _resolve_timeout(self, timeout: Optional[float]) -> float:
        """解析单次请求超时：显式传入优先，否则回落连接级默认（保证正值）。"""
        value = self.default_call_timeout if timeout is None else timeout
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = self.DEFAULT_CALL_TIMEOUT
        return value if value > 0 else self.DEFAULT_CALL_TIMEOUT

    async def _send_request(self, method: str, params: dict,
                            timeout: Optional[float] = None) -> dict:
        """发送 JSON-RPC 请求并等待响应。

        timeout=None 时使用类默认 DEFAULT_CALL_TIMEOUT（30s）；tools/call
        由 call_tool 按服务器配置解析后显式传入（P2-3）。
        """
        if timeout is None:
            timeout = self.DEFAULT_CALL_TIMEOUT
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

    def _fail_pending(self, exc: Exception) -> None:
        """把异常置给所有挂起请求的 Future 并清空挂起表。"""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def _receive_loop(self):
        """后台接收循环：持续从传输层读取消息并分发到对应的 Future。

        循环意外退出（EOF / 读取异常）时对所有挂起 Future 置 ConnectionError
        并复位 _initialized，使下一次请求自动重连，避免请求永久挂死。
        """
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
            raise  # 主动关闭：close() 统一清理挂起请求
        except Exception as e:
            logger.error(f"MCP 接收循环异常 ({self.name}): {e}")
        finally:
            # 意外退出（EOF / 异常）：挂起请求置 ConnectionError，
            # 复位 _initialized，下次请求自动重连。
            if self._pending:
                self._fail_pending(ConnectionError(f"MCP 连接已断开 ({self.name})"))
            self._initialized = False

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


def _resolve_server_call_timeout(config: dict) -> float:
    """解析服务器级工具调用超时（P2-3）：tool_call_timeout > timeout > 默认 30s。

    ``timeout`` 键历史上只作用于 HTTP sock_read/请求超时；新键
    ``tool_call_timeout`` 专门约束 tools/call 的响应等待窗口。两键都可配，
    取第一个合法正值。
    """
    for key in ("tool_call_timeout", "timeout"):
        value = config.get(key)
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return MCPConnection.DEFAULT_CALL_TIMEOUT


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
            "timeout": 30,              # 超时秒数（可选；HTTP 传输作用于 sock_read）
            "tool_call_timeout": 120,   # 工具调用响应超时秒数（可选，P2-3；
                                        #   未配置时回退 timeout，再回退默认 30s）
        }

        返回:
            MCPConnection 实例
        """
        name = config["name"]
        if name in self._connections:
            raise ValueError(f"MCP 服务器 '{name}' 已存在")

        transport = self._create_transport(config)
        connection = MCPConnection(
            name, transport, call_timeout=_resolve_server_call_timeout(config))
        self._connections[name] = connection
        self._configs[name] = config
        logger.info(f"添加 MCP 服务器: {name}")
        return connection

    def remove_server(self, name: str) -> bool:
        """移除并关闭一个服务器连接。

        关闭统一在 MCP 专用事件循环上进行（连接的全部状态——接收循环、
        传输层 session/子进程——都创建在该循环上），避免 asyncio.run 另起
        新循环关闭导致的跨循环对象泄漏（双循环不匹配）。
        """
        conn = self._connections.pop(name, None)
        self._configs.pop(name, None)
        if conn:
            self._close_connection(conn, name)
            logger.info(f"移除 MCP 服务器: {name}")
            return True
        return False

    def _close_connection(self, conn: MCPConnection, name: str) -> None:
        """在 MCP 事件循环上关闭连接；已在该循环内时直接调度避免自锁。"""
        runner = _MCPLoopRunner.get()
        if threading.current_thread() is runner._thread:
            # 已在 MCP 事件循环线程内（如 reload/reconnect 流程）：
            # 直接调度 close 任务，不能 run_coroutine_threadsafe 阻塞等待
            try:
                runner._loop.create_task(conn.close())
            except Exception as e:
                logger.warning(f"关闭 MCP 连接 '{name}' 出错: {e}")
        else:
            try:
                run_in_mcp_loop(conn.close(), timeout=10)
            except Exception as e:
                logger.warning(f"关闭 MCP 连接 '{name}' 出错: {e}")

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
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"MCP 初始化总超时 ({timeout}s)，部分服务器可能未完成初始化"
                )

    @staticmethod
    def _is_retryable_init_error(e: Exception) -> bool:
        """仅网络/连接类异常值得重试；参数/协议错误重试无意义。"""
        retryable_names = frozenset({
            "ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
            "TimeoutError", "RemoteProtocolError", "ConnectError", "ReadError",
            "ClientConnectionError", "ServerConnectionError",
            "ServerDisconnectedError", "ClientConnectorError", "ClientOSError",
        })
        if type(e).__name__ in retryable_names:
            return True
        if isinstance(e, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return True
        return False

    async def _safe_initialize(self, conn: MCPConnection, name: str, max_retries: int = 2):
        """带重试的安全初始化（仅对网络/连接类异常重试）。"""
        for attempt in range(max_retries):
            try:
                await conn.initialize()
                logger.info(f"MCP 服务器 '{name}' 初始化成功")
                return
            except Exception as e:
                if attempt < max_retries - 1 and self._is_retryable_init_error(e):
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
                    return

    async def discover_all_tools(self) -> Dict[str, List[dict]]:
        """
        从所有已初始化连接中并行收集工具列表（L3-C9：asyncio.gather 并行，
        return_exceptions=True 使单点失败不影响其他服务器）

        返回:
            {"server_name": [tool_desc, ...], ...}
        """
        targets = [(name, conn) for name, conn in self._connections.items()
                   if conn.is_initialized]
        if not targets:
            return {}
        outcomes = await asyncio.gather(
            *(conn.discover_tools() for _, conn in targets),
            return_exceptions=True,
        )
        result: Dict[str, List[dict]] = {}
        for (name, _conn), outcome in zip(targets, outcomes):
            if isinstance(outcome, Exception):
                logger.error(f"从 '{name}' 发现工具失败: {outcome}")
                result[name] = []
            else:
                tools = outcome or []
                result[name] = tools
                logger.info(f"从 '{name}' 发现 {len(tools)} 个工具")
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
