# -*- coding: utf-8 -*-
"""
消息调度器 —— 入站去重 → 会话路由 → Agent 执行 → 回复下发
"""

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Optional

from gateway.channels.base import Channel, InboundMessage
from gateway.session import SessionManager, SessionEntry
from gateway.agent_factory import create_gateway_agent
from gateway.textutil import split_text, sanitize_error

logger = logging.getLogger("hello_agent.gateway")


class _LRUDedup:
    """基于 message_id 的 LRU 去重"""

    def __init__(self, capacity: int = 1000):
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._capacity = capacity

    def is_dup(self, msg_id: str) -> bool:
        if msg_id in self._seen:
            self._seen.move_to_end(msg_id)
            return True
        self._seen[msg_id] = time.time()
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False


class Dispatcher:
    """
    消息调度核心。
    channel 线程 → on_inbound() → 去重 → session queue → worker → agent.run → reply
    """

    def __init__(self, session_mgr: SessionManager, agent_config: dict = None):
        self.session_mgr = session_mgr
        self.agent_config = agent_config or {}
        self._channels: dict[str, Channel] = {}
        self._dedup = _LRUDedup()
        self._soft_timeout = self.agent_config.get("soft_timeout_seconds", 90)
        self._hard_timeout = self.agent_config.get("hard_timeout_seconds", 600)
        # 命令表：name -> {"help", "args", "handler", "client_hint"}
        # 内置 + 扩展模块注册（scheduler/heartbeat/webui）；
        # handler 签名: async def handler(arg, ctx) -> str，
        # ctx = {"agent", "entry", "loop", "executor"}
        self._commands: dict[str, dict] = {}
        # agent 初始化回调（WebUI 挂 BridgeHook / 审批桥），创建后在 executor 内调用
        self._agent_initializers: list = []
        self._register_builtin_commands()

    def register_channel(self, channel: Channel):
        self._channels[channel.name] = channel

    def channels(self) -> dict:
        """已注册通道表（公开访问器，替代 _channels 私有直捅）"""
        return dict(self._channels)

    def add_command(self, name: str, handler, help_text: str = "",
                    args_hint: str = "", client_hint: str = ""):
        """注册命令。handler: async def handler(arg, ctx) -> str"""
        self._commands[name.lower()] = {
            "help": help_text, "args": args_hint,
            "handler": handler, "client_hint": client_hint,
        }

    def add_agent_initializer(self, cb):
        """注册 agent 初始化回调：agent 在 executor 内创建后立即调用 cb(agent, entry)。

        WebUI 用它挂 BridgeHook / ask 审批桥等。
        回调在 executor 线程执行，勿做耗时操作。
        """
        self._agent_initializers.append(cb)

    def commands_table(self) -> list:
        """命令清单（GET /api/commands 数据源）"""
        return [
            {"name": name, "args": c.get("args", ""),
             "help": c.get("help", ""),
             "client_hint": c.get("client_hint", "")}
            for name, c in sorted(self._commands.items())
        ]

    # ---------- 内置命令 ----------

    def _register_builtin_commands(self):
        self._commands.update({
            "/compact": {"help": "/compact — 压缩上下文，释放 token 空间",
                          "args": "", "handler": self._cmd_compact},
            "/clear": {"help": "/clear — 清空会话历史",
                        "args": "", "handler": self._cmd_clear},
            "/stats": {"help": "/stats — 查看上下文占用",
                        "args": "", "handler": self._cmd_stats},
            "/model": {"help": "/model [名称] — 查看/切换模型",
                        "args": "[名称]", "handler": self._cmd_model},
            "/session": {"help": "/session — 查看会话信息",
                          "args": "", "handler": self._cmd_session},
            "/help": {"help": "/help — 显示此帮助",
                       "args": "", "handler": self._cmd_help},
        })

    async def _cmd_compact(self, arg, ctx):
        ok = await ctx["loop"].run_in_executor(
            ctx["executor"], ctx["agent"]._full_compress, False)
        return "✅ 上下文压缩完成" if ok else "ℹ️ 上下文较短，无需压缩"

    async def _cmd_clear(self, arg, ctx):
        await ctx["loop"].run_in_executor(
            ctx["executor"], ctx["agent"].clear_history)
        return "✅ 会话已清空"

    async def _cmd_stats(self, arg, ctx):
        agent = ctx["agent"]
        stats = agent.store.stats()
        ratio = stats.get("usage_ratio", 0) * 100
        return (
            f"📊 上下文统计\n"
            f"  模型: {agent.llm.model}\n"
            f"  消息数: {stats.get('total_messages', 0)}\n"
            f"  已用 token: {stats.get('total_tokens', 0)}\n"
            f"  上限: {stats.get('max_tokens', 0)}\n"
            f"  使用率: {ratio:.1f}%"
        )

    async def _cmd_model(self, arg, ctx):
        agent, entry = ctx["agent"], ctx["entry"]
        if not arg:
            return f"🤖 当前模型: {agent.llm.model}"
        try:
            await ctx["loop"].run_in_executor(
                ctx["executor"], lambda m=arg: agent.switch_llm(model=m))
            # 记入 sessions_map v2，重启后可回填（修模型切换不持久缺口）
            if entry is not None:
                from gateway.agent_factory import update_map_meta
                update_map_meta(entry.session_key, model=arg)
            return f"✅ 已切换到模型: {arg}"
        except Exception as e:
            return f"❌ 切换到 {arg} 失败: {e}\n请检查 config.json 的 llm.models 中是否有该模型的配置（api_key / base_url）"

    async def _cmd_session(self, arg, ctx):
        agent = ctx["agent"]
        return f"💾 会话 ID: {agent.store.session_id}\n  消息数: {len(agent.messages)}"

    async def _cmd_help(self, arg, ctx):
        lines = ["📋 可用命令:"]
        for c in sorted(self._commands.values(),
                        key=lambda c: c.get("help", "")):
            if c.get("help"):
                lines.append(c["help"])
        return "\n".join(lines)

    async def on_inbound(self, msg: InboundMessage):
        """入站消息处理（从 channel 线程通过 run_coroutine_threadsafe 调用）"""
        # 去重
        if self._dedup.is_dup(msg.message_id):
            logger.debug("重复消息，跳过: %s", msg.message_id)
            return
        # 获取/创建会话
        entry = self.session_mgr.get_or_create(msg.session_key)
        if entry is None:
            # 会话池满
            channel = self._channels.get(msg.channel)
            if channel:
                await channel.send_reply(msg, "🈵 当前会话数已达上限，请稍后再试")
            return
        # 确保 worker 在运行
        if entry.worker_task is None or entry.worker_task.done():
            entry.worker_task = asyncio.create_task(
                self._session_worker(entry)
            )
        # 入队
        await entry.queue.put(msg)

    async def _session_worker(self, entry: SessionEntry):
        """每会话的 FIFO worker 协程"""
        while True:
            try:
                msg: InboundMessage = await asyncio.wait_for(
                    entry.queue.get(), timeout=300
                )
            except asyncio.TimeoutError:
                # 5 分钟无消息，worker 自动退出（janitor 会清理 entry）
                break
            except asyncio.CancelledError:
                break

            channel = self._channels.get(msg.channel)
            if not channel:
                continue

            entry.is_busy = True
            entry.last_active = time.time()
            try:
                t0 = time.time()
                reply = await self._execute_agent(entry, msg, channel)
                elapsed = time.time() - t0
                if reply:
                    # 附加 meta 信息供 channel 格式化使用
                    a = entry.agent
                    model = a.llm.model if a and a.llm else ""
                    msg._reply_meta = {"model": model, "elapsed": elapsed}
                    await self._send_chunked(channel, msg, reply)
            except Exception as e:
                logger.error("处理消息异常 [%s]: %s", msg.session_key, e, exc_info=True)
                try:
                    err_text = f"❌ 处理出错: {sanitize_error(e)}"
                    await channel.send_reply(msg, err_text)
                except Exception:
                    pass
            finally:
                entry.is_busy = False
                entry.last_active = time.time()

    async def _execute_agent(
        self, entry: SessionEntry, msg: InboundMessage, channel: Channel
    ) -> Optional[str]:
        """在线程池中执行 agent.run()，带 soft/hard 超时"""
        loop = asyncio.get_event_loop()
        executor = self.session_mgr.get_executor()

        # 延迟创建 Agent（创建后在 executor 内运行初始化回调）
        if entry.agent is None:
            cfg = self.agent_config
            initializers = list(self._agent_initializers)

            def _create():
                agent = create_gateway_agent(
                    session_key=entry.session_key,
                    model=cfg.get("model", ""),
                    max_steps=cfg.get("max_steps", 30),
                    permission_mode=cfg.get("permission_mode", "allow"),
                    quiet=cfg.get("quiet", True),
                    auto_approve_plan=cfg.get("auto_approve_plan", True),
                )
                for cb in initializers:
                    try:
                        cb(agent, entry)
                    except Exception as e:
                        logger.warning("agent 初始化回调异常: %s", e)
                return agent

            entry.agent = await loop.run_in_executor(executor, _create)

        agent = entry.agent

        # ---- / 命令拦截（不经过 LLM） ----
        cmd_reply = await self._handle_gateway_command(
            agent, msg.text, loop, executor, entry)
        if cmd_reply is not None:
            return cmd_reply

        # ---- agent.run() 含 soft/hard 超时 ----
        # 注意：必须保存 afuture 引用，软超时后继续等同一个 future
        # 而不能起第二次 agent.run()——线程池里的旧调用不会因 asyncio 取消而停止
        _images = getattr(msg, 'images', None) or None
        event_sink = None
        publisher = getattr(channel, "publish_agent_event", None)
        if callable(publisher):
            # The sink runs in the Agent executor thread.  WebuiChannel's
            # event bus is explicitly cross-thread safe.
            event_sink = lambda event: publisher(msg, event)
        afuture = loop.run_in_executor(
            executor,
            lambda: agent.run(msg.text, False, images=_images,
                              event_sink=event_sink),
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(afuture), timeout=self._soft_timeout,
            )
            return result
        except asyncio.TimeoutError:
            try:
                # 进度提示走 send_progress，避免抢占 future 型通道的最终回复
                await channel.send_progress(msg, "⏳ 还在处理中，请稍候…")
            except Exception:
                pass
            try:
                result = await asyncio.wait_for(
                    afuture, timeout=self._hard_timeout - self._soft_timeout,
                )
                return result
            except asyncio.TimeoutError:
                return f"⏰ 处理超时（>{self._hard_timeout}s），请简化问题后重试"

    async def _handle_gateway_command(
        self, agent, text: str, loop, executor, entry=None
    ) -> Optional[str]:
        """拦截 / 前缀命令，直接处理不经过 LLM。返回回复文本或 None（非命令）。"""
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        command = self._commands.get(cmd)
        if command is None:
            # 非 gateway 命令，交给 agent 处理（可能是 /plan 等需要 LLM 参与的命令）
            return None
        ctx = {"agent": agent, "entry": entry, "loop": loop, "executor": executor}
        return await command["handler"](arg, ctx)

    async def _send_chunked(self, channel: Channel, msg: InboundMessage, text: str):
        """分片发送长文本。平台内部自行分片的 channel 跳过预切。"""
        if channel.handles_chunking:
            await channel.send_reply(msg, text)
        else:
            max_len = 1500
            chunks = split_text(text, max_len)
            for chunk in chunks:
                await channel.send_reply(msg, chunk)
