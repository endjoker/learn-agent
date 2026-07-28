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

# 安全剥离残留的 ReAct 标签（模型偶尔不遵循格式时兜底）
_REACT_TAG_RE = __import__("re").compile(
    r"^\s*(?:FINAL_ANSWER|最终回答|THOUGHT|思考)[：:]\s*",
    __import__("re").IGNORECASE,
)


def _strip_react_tags(text: str) -> str:
    """剥掉回复中残留的 ReAct 标签前缀"""
    return _REACT_TAG_RE.sub("", text).strip()


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

    def register_channel(self, channel: Channel):
        self._channels[channel.name] = channel

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
                    reply = _strip_react_tags(reply)
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

        # 延迟创建 Agent
        if entry.agent is None:
            cfg = self.agent_config
            entry.agent = await loop.run_in_executor(
                executor,
                lambda: create_gateway_agent(
                    session_key=entry.session_key,
                    model=cfg.get("model", ""),
                    max_steps=cfg.get("max_steps", 30),
                    permission_mode=cfg.get("permission_mode", "allow"),
                    quiet=cfg.get("quiet", True),
                    auto_approve_plan=cfg.get("auto_approve_plan", True),
                ),
            )

        agent = entry.agent

        # ---- / 命令拦截（不经过 LLM） ----
        cmd_reply = await self._handle_gateway_command(agent, msg.text, loop, executor)
        if cmd_reply is not None:
            return cmd_reply

        # ---- agent.run() 含 soft/hard 超时 ----
        # 注意：必须保存 afuture 引用，软超时后继续等同一个 future
        # 而不能起第二次 agent.run()——线程池里的旧调用不会因 asyncio 取消而停止
        afuture = loop.run_in_executor(executor, agent.run, msg.text, False)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(afuture), timeout=self._soft_timeout,
            )
            return result
        except asyncio.TimeoutError:
            try:
                await channel.send_reply(msg, "⏳ 还在处理中，请稍候…")
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
        self, agent, text: str, loop, executor
    ) -> Optional[str]:
        """拦截 / 前缀命令，直接处理不经过 LLM。返回回复文本或 None（非命令）。"""
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/compact":
            ok = await loop.run_in_executor(executor, agent._full_compress, False)
            return "✅ 上下文压缩完成" if ok else "ℹ️ 上下文较短，无需压缩"

        if cmd == "/clear":
            await loop.run_in_executor(executor, agent.clear_history)
            return "✅ 会话已清空"

        if cmd == "/stats":
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

        if cmd == "/model":
            if not arg:
                return f"🤖 当前模型: {agent.llm.model}"
            try:
                await loop.run_in_executor(
                    executor, lambda m=arg: agent.switch_llm(model=m)
                )
                return f"✅ 已切换到模型: {arg}"
            except Exception as e:
                return f"❌ 切换到 {arg} 失败: {e}\n请检查 config.json 的 llm.models 中是否有该模型的配置（api_key / base_url）"

        if cmd == "/session":
            sid = agent.store.session_id
            msg_count = len(agent.messages)
            return f"💾 会话 ID: {sid}\n  消息数: {msg_count}"

        if cmd == "/help":
            return (
                "📋 可用命令:\n"
                "/compact — 压缩上下文，释放 token 空间\n"
                "/clear — 清空会话历史\n"
                "/stats — 查看上下文占用\n"
                "/model [名称] — 查看/切换模型\n"
                "/session — 查看会话信息\n"
                "/help — 显示此帮助"
            )

        # 非 gateway 命令，交给 agent 处理（可能是 /plan 等需要 LLM 参与的命令）
        return None

    async def _send_chunked(self, channel: Channel, msg: InboundMessage, text: str):
        """分片发送长文本。平台内部自行分片的 channel 跳过预切。"""
        if channel.handles_chunking:
            await channel.send_reply(msg, text)
        else:
            max_len = 1500
            chunks = split_text(text, max_len)
            for chunk in chunks:
                await channel.send_reply(msg, chunk)
