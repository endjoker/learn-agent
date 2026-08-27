# -*- coding: utf-8 -*-
"""
Outbox Publisher —— 未发布事件的异步补发器（设计方案 16.3 / 18.4）。

- 业务事务把事件写入 outbox_events 后提交；实时路径立即 publish 并标记
  published。若进程在 publish 前崩溃，本发布器在重启后按 created_at 顺序
  补发未发布事件（仅向新建立的 SSE 订阅重放）。
- 按 conversation 串行：单会话事件顺序由 created_at 保证；不同会话可并发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from gateway.conversation.models import OutboxEvent, utc_now

logger = logging.getLogger("jk_agent.gateway")

_DEFAULT_INTERVAL_SECONDS = 5.0
# 补发认领租约 TTL：认领后未发布完成（崩溃/停机）的行，其他实例在该 TTL
# 之后可重新认领补发（防重复广播与死信滞留之间的折中）。
_DEFAULT_CLAIM_TTL_SECONDS = 30.0
# 认领时间下限（秒）：只认领 created_at 早于 now - 下限 的行。业务事务提交
# 后、实时路径 publish 并标记 published 之间存在窗口；若补发器立刻抢认领
# 刚落库的事件，会与实时路径重复广播。下限取 flusher 周期（默认 5s）的
# 3 倍——正常情况实时路径远快于该窗口，只有崩溃残留事件才会被补发。
_CLAIM_MIN_AGE_SECONDS = _DEFAULT_INTERVAL_SECONDS * 3
# 死信终态标记（无约束方案，不改 schema）：outbox_events 没有 status 列，
# 未发布语义是 published_at IS NULL。为把"连败放弃"与"成功发布"区分开，
# 死信用保留认领哨兵表达——published_at 保持 NULL、claimed_by 置为本哨兵、
# claim_expires_at 清空。_claim_unpublished 对该哨兵显式跳过，死信行永不
# 再被认领投递；积压裁剪（trim_outbox_backlog 按 published_at IS NULL 计数）
# 作为最终溢出阀仍可回收最老的死信行。
DEAD_LETTER_CLAIMANT = "dead_letter"


class OutboxPublisher:
    """周期扫描未发布 Outbox 事件并补发。``publish(event_type, payload)``
    与 ConversationService 使用同一回调（业务事件已含完整 GatewayEvent 数据）。"""

    def __init__(self, store, publish: Callable[[str, dict], None],
                 *, interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
                 batch_size: int = 200, max_backlog: int = 5000,
                 max_attempts: int = 3,
                 claimant: str | None = None,
                 claim_ttl_seconds: float = _DEFAULT_CLAIM_TTL_SECONDS):
        self._store = store
        self._publish = publish
        self._interval = max(0.05, float(interval_seconds))
        self._batch_size = max(1, int(batch_size))
        self._max_backlog = max(100, int(max_backlog))
        self._max_attempts = max(1, int(max_attempts))
        # 多实例补发认领：每个进程用独立 claimant（缺省自生成 uuid），
        # 扫描未发布行时先原子认领、再只补发自己认领的行，防止多实例
        # 对同一事件重复广播（见 docs/multi-instance.md）。
        self._claimant = claimant or uuid.uuid4().hex
        try:
            self._claim_ttl = max(1.0, float(claim_ttl_seconds))
        except (TypeError, ValueError):
            self._claim_ttl = _DEFAULT_CLAIM_TTL_SECONDS
        # 单事件已重试次数（进程内记数；跨重启由重启后补发重新计数）
        self._attempts: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def flush_once(self) -> int:
        """补发一批未发布事件（测试与启动恢复用）。返回成功发布数。

        同步 DB/回调工作在 asyncio.to_thread 中执行，避免阻塞事件循环。"""
        return await asyncio.to_thread(self._flush_once_sync)

    def _flush_once_sync(self) -> int:
        """同步补发一批未发布事件（在线程池中执行）。

        - 发送失败的事件保持 pending 供后续轮次重试；单事件重试达上限后
          置入独立终态 dead_letter（不再重试，也不冒充 published，可区分
          排查）。
        - 未发布事件超过 max_backlog 时丢弃最老一批并告警，防止无限堆积。"""
        # 防无限堆积：未发布事件超过上限 → 丢弃最老一批并告警
        # 补发认领：同一事务内先原子标记候选行归本实例（claimed_by/
        # claim_expires_at），再只处理自己认领的行——多实例下互不重叠。
        with self._store.transaction() as conn:
            trimmed = self._store.trim_outbox_backlog(conn, self._max_backlog)
            claimed = self._claim_unpublished(conn)
        if trimmed:
            logger.warning(
                "Outbox 未发布事件超过上限 %d，丢弃最老 %d 条（防无限堆积）",
                self._max_backlog, trimmed)
        if claimed:
            logger.debug("Outbox 补发认领 %d 条（claimant=%s）", claimed, self._claimant)
        events = self._list_claimed(limit=self._batch_size)
        published = 0
        for event in events:
            try:
                self._publish(event.event_type, dict(event.payload))
            except Exception as exc:
                attempts = self._attempts.get(event.outbox_id, 0) + 1
                if attempts >= self._max_attempts:
                    # 死信终态：不再重试。published_at 保持 NULL（与成功发布
                    # 可区分），以保留认领哨兵进入 dead_letter，认领扫描永久
                    # 跳过；由运维排查或积压裁剪兜底回收。
                    logger.error(
                        "Outbox 事件进入死信终态 dead_letter"
                        "（连续 %d 次失败，放弃重试；最后错误: %r）: "
                        "outbox_id=%s event_type=%s conv=%s",
                        attempts, exc, event.outbox_id, event.event_type,
                        event.conversation_id)
                    try:
                        with self._store.transaction() as conn:
                            self._mark_dead_letter(conn, event.outbox_id)
                    except Exception:
                        # 标记失败：事件仍带本实例租约，TTL 过后可被重新认领
                        # 重试（宁可多试也不静默吞掉）。
                        logger.warning("Outbox 死信标记失败（下轮可重试）: %s",
                                       event.outbox_id)
                    self._attempts.pop(event.outbox_id, None)
                else:
                    # 失败保持 pending：下次循环按 created_at 顺序重试
                    self._attempts[event.outbox_id] = attempts
                    logger.warning(
                        "Outbox 补发失败（第 %d/%d 次，稍后重试）: %s %s",
                        attempts, self._max_attempts, event.outbox_id,
                        event.event_type)
                continue
            self._attempts.pop(event.outbox_id, None)
            try:
                with self._store.transaction() as conn:
                    self._store.mark_outbox_published(conn, event.outbox_id)
            except Exception:
                # 发送成功但标记失败：事件保持 pending，下轮重发（幂等可接受）
                logger.warning("Outbox 发布标记失败（下轮重发）: %s", event.outbox_id)
                continue
            published += 1
        return published

    @staticmethod
    def _mark_dead_letter(conn, outbox_id: str) -> None:
        """把事件置入独立终态 dead_letter（事务内调用）。

        刻意不复用 mark_outbox_published：死信与成功发布必须可区分。仅改
        认领列——published_at 保持 NULL，claimed_by 置为保留哨兵
        DEAD_LETTER_CLAIMANT 并清空租约；_claim_unpublished 对该哨兵显式
        跳过，事件不会再被投递。只影响仍未发布的行（防御性条件）。"""
        conn.execute(
            "UPDATE outbox_events SET claimed_by=?, claim_expires_at=NULL "
            "WHERE outbox_id=? AND published_at IS NULL",
            (DEAD_LETTER_CLAIMANT, outbox_id),
        )

    def _claim_unpublished(self, conn) -> int:
        """原子认领未发布事件（事务内调用）。

        WHERE 覆盖：未认领（claimed_by IS NULL）、认领已过期（claim_expires_at
        早于 now）的行归本 claimant；已认领且未过期的行（其他实例正在补发或
        刚崩溃）保持不动，防止重复广播。dead_letter 终态行（保留哨兵
        claimed_by）显式排除，永不再次投递。另加 created_at 时间下限（
        _CLAIM_MIN_AGE_SECONDS）：刚落库、实时路径尚未 publish 的事件不抢认领。
        返回本次新认领行数。"""
        now = utc_now()
        claim_expires = (datetime.now(timezone.utc) + timedelta(
            seconds=self._claim_ttl)).isoformat(timespec="milliseconds")
        claim_cutoff = (datetime.now(timezone.utc) - timedelta(
            seconds=_CLAIM_MIN_AGE_SECONDS)).isoformat(timespec="milliseconds")
        cursor = conn.execute(
            """
            UPDATE outbox_events
            SET claimed_by=?, claim_expires_at=?
            WHERE published_at IS NULL
              AND COALESCE(claimed_by, '') != ?
              AND created_at <= ?
              AND (claimed_by IS NULL OR claim_expires_at IS NULL
                   OR claim_expires_at < ?)
            """,
            (self._claimant, claim_expires, DEAD_LETTER_CLAIMANT,
             claim_cutoff, now),
        )
        return int(cursor.rowcount or 0)

    def _list_claimed(self, limit: int) -> list[OutboxEvent]:
        """只列出本 claimant 认领且尚未发布的待补发事件（按 created_at 顺序）。"""
        with self._store.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox_events "
                "WHERE published_at IS NULL AND claimed_by=? "
                "ORDER BY created_at LIMIT ?",
                (self._claimant, max(1, int(limit))),
            ).fetchall()
        return [
            OutboxEvent(
                outbox_id=row["outbox_id"],
                conversation_id=row["conversation_id"],
                event_type=row["event_type"],
                scope=row["scope"],
                version=int(row["version"] or 0),
                payload=dict(json.loads(row["payload"] or "{}")),
                created_at=row["created_at"],
                published_at=row["published_at"],
            )
            for row in rows
        ]

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Outbox 发布循环异常")
