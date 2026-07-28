# -*- coding: utf-8 -*-
"""
会话管理器 —— Agent 实例池 + FIFO 队列 + 过期清理
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("hello_agent.gateway")


@dataclass
class SessionEntry:
    """单个会话条目"""
    session_key: str
    agent: object = None           # Agent 实例（延迟创建）
    queue: asyncio.Queue = None    # 消息 FIFO 队列
    worker_task: asyncio.Task = None  # worker 协程
    created_at: float = 0.0
    last_active: float = 0.0
    is_busy: bool = False          # 是否正在执行 agent.run()

    def __post_init__(self):
        if self.queue is None:
            self.queue = asyncio.Queue()
        if self.created_at == 0.0:
            self.created_at = time.time()
        self.last_active = self.created_at


class SessionManager:
    """
    管理 gateway 的所有 Agent 会话。
    - 每会话独立 Agent 实例
    - 同 session_key 通过 asyncio.Queue FIFO 串行
    - janitor 定期清理过期会话
    """

    def __init__(
        self,
        max_sessions: int = 50,
        idle_timeout_minutes: int = 60,
        persist: bool = True,
        worker_pool_size: int = 4,
        agent_config: dict = None,
    ):
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout_minutes * 60
        self.persist = persist
        self.agent_config = agent_config or {}
        self._sessions: dict[str, SessionEntry] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=worker_pool_size,
            thread_name_prefix="gateway-agent",
        )
        self._janitor_task: Optional[asyncio.Task] = None

    async def start(self):
        """启动 janitor 定时任务"""
        self._janitor_task = asyncio.create_task(self._janitor_loop())
        logger.info("SessionManager 启动: max=%d, idle=%ds, pool=%d",
                     self.max_sessions, self.idle_timeout,
                     self._executor._max_workers)

    async def stop(self):
        """优雅停机：保存所有会话 → 关闭线程池"""
        if self._janitor_task:
            self._janitor_task.cancel()
        # 保存所有活跃会话
        for key, entry in list(self._sessions.items()):
            await self._cleanup_entry(key, entry, save=True)
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("SessionManager 已停止")

    def get_or_create(self, session_key: str) -> Optional[SessionEntry]:
        """获取或创建会话条目（不创建 Agent，Agent 在 worker 中延迟创建）"""
        if session_key in self._sessions:
            entry = self._sessions[session_key]
            entry.last_active = time.time()
            return entry
        # 容量检查
        if len(self._sessions) >= self.max_sessions:
            logger.warning("会话池已满 (%d/%d)，拒绝新会话: %s",
                          len(self._sessions), self.max_sessions, session_key)
            return None
        entry = SessionEntry(session_key=session_key)
        self._sessions[session_key] = entry
        logger.info("新建会话: %s (活跃: %d/%d)",
                    session_key, len(self._sessions), self.max_sessions)
        return entry

    def get_executor(self) -> ThreadPoolExecutor:
        return self._executor

    def active_count(self) -> int:
        return len(self._sessions)

    async def _janitor_loop(self):
        """每 60s 扫描过期会话"""
        while True:
            await asyncio.sleep(60)
            try:
                now = time.time()
                expired = [
                    key for key, entry in self._sessions.items()
                    if not entry.is_busy and (now - entry.last_active) > self.idle_timeout
                ]
                for key in expired:
                    entry = self._sessions.pop(key, None)
                    if entry:
                        logger.info("回收过期会话: %s (空闲 %.0fs)",
                                   key, now - entry.last_active)
                        await self._cleanup_entry(key, entry, save=self.persist)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("janitor 异常: %s", e)

    async def _cleanup_entry(self, key: str, entry: SessionEntry, save: bool = True):
        """清理单个会话：保存 → 释放资源"""
        if entry.agent and save:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, entry.agent.store.save_session)
            except Exception as e:
                logger.error("保存会话失败 %s: %s", key, e)
        # 清理 MCP / 进程
        if entry.agent:
            try:
                if getattr(entry.agent, 'process_manager', None):
                    entry.agent.process_manager.cleanup_all()
                if getattr(entry.agent, 'mcp_manager', None):
                    from core.mcp_client import run_in_mcp_loop
                    run_in_mcp_loop(entry.agent.mcp_manager.close_all(), timeout=10)
            except Exception as e:
                logger.error("清理资源失败 %s: %s", key, e)
        if entry.worker_task and not entry.worker_task.done():
            entry.worker_task.cancel()
