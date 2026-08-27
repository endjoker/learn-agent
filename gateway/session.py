# -*- coding: utf-8 -*-
"""
会话管理器 —— Agent 实例池 + 过期清理

（历史 FIFO worker 漏斗已由统一 runner / TaskRuntime 接管，队列字段退役。）
"""

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("jk_agent.gateway")


@dataclass
class SessionEntry:
    """单个会话条目"""
    session_key: str
    agent: object = None           # Agent 实例（延迟创建）
    created_at: float = 0.0
    last_active: float = 0.0
    is_busy: bool = False          # 是否正在执行 agent.run()
    # P1-2：跨路径执行互斥（runner 与 TaskRuntime/dispatcher 共享同一 entry）。
    # 非阻塞 try-acquire：拿不到锁说明该会话正在另一路径执行，直接拒绝重入。
    exec_lock: threading.Lock = field(default_factory=threading.Lock)
    # ---- Phase 0/4：工作区运行上下文（预留字段，必须有默认值）----
    runtime_context: object = None         # WorkspaceRuntimeContext（工作区会话）
    runtime_snapshot_id: str = ""          # 当前消息使用的 RuntimeSnapshot id
    config_stale: bool = False             # Profile/Workspace 更新后标记 stale

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()
        self.last_active = self.created_at


class SessionManager:
    """
    管理 gateway 的所有 Agent 会话。
    - 每会话独立 Agent 实例（同 session_key 全局唯一）
    - 执行串行由 entry.exec_lock / 统一 runner 保证（旧 asyncio.Queue
      FIFO worker 已退役）
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
        # 会话生命周期回调（WebUI session.created/.evicted 事件用）
        self.on_created: list = []
        self.on_evicted: list = []
        # 驱逐豁免谓词（P2：armed Goal 所在会话豁免 janitor 空闲回收——Goal 轮
        # 间隙/pause 期间 last_active 不更新，回收会丢掉 proc 子进程会话等纯
        # 内存态。装配方注入：session_key -> True 表示本轮跳过回收）。
        self.evict_guard = None

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
        self._notify(self.on_created, session_key)
        return entry

    def get_executor(self) -> ThreadPoolExecutor:
        return self._executor

    def active_count(self) -> int:
        return len(self._sessions)

    def is_busy(self, session_key: str) -> bool:
        """指定会话是否正在执行（心跳 defer_when_busy 判定用）"""
        entry = self._sessions.get(session_key)
        return bool(entry is not None and entry.is_busy)

    def list_entries(self) -> list[dict]:
        """内存会话快照（状态面板 / 会话页用）。不含磁盘未加载项。"""
        out = []
        for key, e in self._sessions.items():
            agent = e.agent
            model = ""
            msg_count = 0
            if agent is not None:
                try:
                    model = agent.llm.model if agent.llm else ""
                    msg_count = len(agent.messages)
                except Exception:
                    pass
            out.append({
                "session_key": key,
                "session_id": getattr(getattr(agent, "store", None),
                                      "session_id", ""),
                "model": model,
                "message_count": msg_count,
                "is_busy": e.is_busy,
                "created_at": e.created_at,
                "last_active": e.last_active,
                "loaded": agent is not None,
            })
        return out

    @staticmethod
    def _notify(callbacks: list, session_key: str, reason: str = ""):
        for cb in callbacks:
            try:
                cb(session_key, reason)
            except Exception as ex:  # 回调异常不影响主流程
                logger.warning("会话回调异常: %s", ex)

    def executor_stats(self) -> dict:
        """线程池统计（/health 与状态面板用）"""
        ex = self._executor
        return {
            "workers": getattr(ex, "_max_workers", 0),
            "pending": ex._work_queue.qsize()
            if hasattr(ex, "_work_queue") else -1,
            "busy_sessions": sum(
                1 for e in self._sessions.values() if e.is_busy),
        }

    async def evict(self, session_key: str, save: bool = False,
                    force: bool = False) -> bool:
        """从内存移除会话（可选保存）。返回是否移除成功。

        供 WebUI DELETE / 定时任务 isolated 会话投递后清理使用。
        防护：entry 正忙（is_busy）时拒绝驱逐，避免运行中清掉进程/MCP
        资源造成半死会话；确属"卡死自愈"场景请显式传 force=True
        （heartbeat 连续 busy 自愈是唯一预期调用方）。
        """
        entry = self._sessions.get(session_key)
        if entry is None:
            return False
        if entry.is_busy and not force:
            logger.warning("拒绝驱逐正忙会话（如确认卡死请 force=True）: %s",
                           session_key)
            return False
        # P2：armed Goal 豁免（非 force 时）——Goal 轮间隙的会话被回收会丢失
        # proc 子进程会话等纯内存态，下一轮重建实例后无法恢复。
        if not force and self.evict_guard and self.evict_guard(session_key):
            logger.info("跳过驱逐（驱逐豁免谓词命中，如 armed Goal 所在会话）: %s",
                        session_key)
            return False
        self._sessions.pop(session_key, None)
        await self._cleanup_entry(session_key, entry, save=save)
        self._notify(self.on_evicted, session_key, "evict")
        return True

    async def _janitor_loop(self):
        """每 60s 扫描过期会话"""
        while True:
            await asyncio.sleep(60)
            try:
                now = time.time()
                # P1-1：先收集过期 key 快照再统一弹出。此前在 items() 迭代中
                # 直接 pop 会触发 RuntimeError（dictionary changed size），
                # 被下方 except 吞掉后，已摘出条目既不 save_session 也不释放
                # MCP 连接/进程池（永久泄漏）。
                expired_keys = [
                    key
                    for key, entry in self._sessions.items()
                    if not entry.is_busy and (now - entry.last_active) > self.idle_timeout
                    and not (self.evict_guard and self.evict_guard(key))
                ]
                pairs = []
                for key in expired_keys:
                    entry = self._sessions.pop(key, None)
                    if entry is not None:
                        pairs.append((key, entry))
                for key, entry in pairs:
                    logger.info("回收过期会话: %s (空闲 %.0fs)",
                                key, now - entry.last_active)
                    self._notify(self.on_evicted, key, "idle_timeout")

                # 并发化清理：gather + 单条目超时 30s，避免慢会话阻塞整轮
                async def _cleanup(key, entry):
                    try:
                        await asyncio.wait_for(
                            self._cleanup_entry(key, entry, save=self.persist),
                            timeout=30)
                    except asyncio.TimeoutError:
                        logger.warning("清理会话超时(30s): %s", key)
                    except Exception as e:
                        logger.error("清理会话失败 %s: %s", key, e)

                if pairs:
                    await asyncio.gather(*(
                        _cleanup(key, entry) for key, entry in pairs))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("janitor 异常: %s", e)

            # 临时文件 TTL 清理（每 60s 扫描一次，代价低）
            try:
                from core.temp_cleanup import cleanup_old_files, cleanup_agent_output_logs
                import tempfile
                from core.config_loader import _find_project_root
                _removed = 0
                _removed += cleanup_old_files([str(_find_project_root() / "workspace" / "tmp")], 7 * 86400)
                _removed += cleanup_agent_output_logs(tempfile.gettempdir(), 7 * 86400)
                if _removed:
                    logger.info("临时文件清理: %d", _removed)
            except Exception as _e:
                logger.warning("临时文件清理失败: %s", _e)

    async def _cleanup_entry(self, key: str, entry: SessionEntry, save: bool = True):
        """清理单个会话：保存 → 释放资源"""
        if entry.agent and save:
            # 持久化退役：save_session 为 no-op，保留调用仅为兼容自定义 store；
            # 提交到本管理器的 executor，不用默认池（无界，易被慢清理占满）。
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    self._executor, entry.agent.store.save_session)
            except Exception as e:
                logger.error("保存会话失败 %s: %s", key, e)
        # 清理 MCP / 进程（移入线程执行，避免阻塞事件循环）
        if entry.agent:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    self._executor, self._cleanup_agent_resources, entry.agent)
            except Exception as e:
                logger.error("清理资源失败 %s: %s", key, e)

    @staticmethod
    def _cleanup_agent_resources(agent) -> None:
        """同步清理 Agent 的进程/MCP 资源（线程中执行）。"""
        try:
            if getattr(agent, 'process_manager', None):
                agent.process_manager.cleanup_all()
        except Exception as e:
            logger.warning("清理进程资源失败: %s", e)
        try:
            if getattr(agent, 'mcp_manager', None):
                from core.mcp_client import run_in_mcp_loop
                run_in_mcp_loop(agent.mcp_manager.close_all(), timeout=10)
        except Exception as e:
            logger.warning("清理 MCP 资源失败: %s", e)
