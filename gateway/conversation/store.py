# -*- coding: utf-8 -*-
"""
统一会话存储层 —— Conversation/Turn/Node/Queue/Lease/Outbox/Results/
Projection/Receipts/Approvals 的 SQLite CRUD 与状态机事务。

- 复用 RuntimeStore 的 SQLite 文件与连接配置（WAL / busy_timeout / FK）。
- 所有写操作通过 ``transaction()`` 单事务完成；版本（turn_version /
  session_version）与 Outbox 在同一事务内递增/写入（设计方案 16.3）。
- ``position`` 语义：Turn 内稳定序号（1..N，创建后不变）；
  队列中尚未出队的渠道 User Node 的 position 为 NULL（turn_id 为空）。
"""

from __future__ import annotations

import json

import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from gateway.conversation.errors import (
    ApprovalConflict,
    ConversationError,
    QueueConflict,
    QueueLimit,
    ResourceNotFound,
    TurnNotFound,
    UndoExpired,
    ValidationFailed,
)
from gateway.conversation.models import (
    Approval,
    ApprovalStatus,
    ChannelMessageReceipt,
    ConversationSession,
    IdempotencyRecord,
    OutboxEvent,
    QueueItem,
    QueueItemStatus,
    ToolResult,
    Turn,
    TurnNode,
    TurnNodeType,
    TurnStatus,
    _dumps_json,
    _loads_json,
    gen_approval_id,
    gen_conversation_id,
    gen_node_id,
    gen_outbox_id,
    gen_queue_item_id,
    gen_turn_id,
    utc_now,
)

# 设计方案 8.2：每会话最多 20 条活动队列消息，每条最多 10,000 字符
MAX_ACTIVE_QUEUE_ITEMS = 20
MAX_QUEUE_TEXT_LENGTH = 10_000
# 设计方案 9.4：每个 Turn 最多 10 次 Steering
MAX_STEERING_PER_TURN = 10
# 设计方案 9.3：Steering 中断超时默认 10 秒
STEERING_TIMEOUT_SECONDS = 10
# 设计方案 10：幂等记录保留 24 小时
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
# 设计方案 11.4：去重保留窗口 72 小时
RECEIPT_TTL_SECONDS = 72 * 3600
# 设计方案 11.5：投递退避重试 3 次
DELIVERY_MAX_ATTEMPTS = 3
# 设计方案 16.5：Outbox 发布成功后 24 小时清理
OUTBOX_TTL_SECONDS = 24 * 3600
# 设计方案 9.6：删除撤销窗口 5 秒
UNDO_WINDOW_SECONDS = 5
# 设计方案 7.6：停止确认超时默认 10 秒
STOP_TIMEOUT_SECONDS = 10
# 设计方案 7.7：审批超时默认 300 秒
APPROVAL_TIMEOUT_SECONDS = 300


def _is_expired(expires_at: str, now: str | None = None) -> bool:
    now = now or utc_now()
    return bool(expires_at) and expires_at <= now


class ConversationStore:
    """统一会话存储。``db`` 需提供 connection() / transaction() 上下文管理器
    （复用 WorkspaceDatabase / RuntimeStore 的 SQLite 连接配置）。"""

    def __init__(self, db):
        self._db = db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._db.transaction() as connection:
            yield connection

    # ============================================================
    # Conversation
    # ============================================================

    def get_conversation(self, conversation_id: str) -> ConversationSession:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_sessions WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise ResourceNotFound(f"会话不存在: {conversation_id}")
        return _conversation_from_row(row)

    def get_conversation_by_key(self, session_key: str) -> Optional[ConversationSession]:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_sessions WHERE session_key=?",
                (session_key,),
            ).fetchone()
        return _conversation_from_row(row) if row else None

    def list_conversations(self, *, limit: int = 100, offset: int = 0,
                           origin: Optional[str] = None) -> list[ConversationSession]:
        """会话导航列表（设计方案 21.1），按最近更新倒序。"""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        sql = "SELECT * FROM conversation_sessions"
        params: list = []
        if origin:
            sql += " WHERE origin=?"
            params.append(origin)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_conversation_from_row(r) for r in rows]

    def create_conversation(
        self,
        session_key: str,
        *,
        origin: str,
        subtype: str,
        execution_scope: str,
        workspace_id: Optional[str] = None,
        route_metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[ConversationSession, bool]:
        """创建（或取得）Conversation。返回 (conversation, created)。"""
        existing = self.get_conversation_by_key(session_key)
        if existing is not None:
            return existing, False
        now = utc_now()
        conversation = ConversationSession(
            conversation_id=gen_conversation_id(),
            session_key=session_key,
            origin=origin,
            subtype=subtype,
            workspace_id=workspace_id,
            execution_scope=execution_scope,
            route_metadata=dict(route_metadata or {}),
            session_version=0,
            created_at=now,
            updated_at=now,
        )
        try:
            with self.transaction() as conn:
                conn.execute(
                    """INSERT INTO conversation_sessions (
                        conversation_id, session_key, origin, subtype, workspace_id,
                        execution_scope, route_metadata, session_version, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        conversation.conversation_id, session_key, origin, subtype,
                        workspace_id, execution_scope,
                        _dumps_json(conversation.route_metadata),
                        conversation.session_version, now, now,
                    ),
                )
        except sqlite3.IntegrityError:
            # 并发创建同一 session_key：取回既有行
            existing = self.get_conversation_by_key(session_key)
            if existing is not None:
                return existing, False
            raise
        return conversation, True

    def update_conversation_metadata(
        self, conn: sqlite3.Connection, conversation_id: str,
        metadata: Dict[str, Any]) -> ConversationSession:
        """合并更新会话元数据（模型/推理/权限等会话偏好，设计方案：管理操作统一化）。"""
        row = conn.execute(
            "SELECT * FROM conversation_sessions WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise ResourceNotFound(f"会话不存在: {conversation_id}")
        merged = {**(_loads_json(row["route_metadata"]) or {}), **metadata}
        conn.execute(
            "UPDATE conversation_sessions SET route_metadata=?, updated_at=? "
            "WHERE conversation_id=?",
            (_dumps_json(merged), utc_now(), conversation_id),
        )
        updated = conn.execute(
            "SELECT * FROM conversation_sessions WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return _conversation_from_row(updated)

    def bump_session_version(self, conn: sqlite3.Connection, conversation_id: str) -> int:
        """会话级单调序列递增（在写事务内调用），返回新值。"""
        conn.execute(
            "UPDATE conversation_sessions SET session_version=session_version+1, "
            "updated_at=? WHERE conversation_id=?",
            (utc_now(), conversation_id),
        )
        row = conn.execute(
            "SELECT session_version FROM conversation_sessions WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return int(row["session_version"]) if row else 0

    # ============================================================
    # Turn
    # ============================================================

    def create_turn(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        *,
        status: str = TurnStatus.QUEUED.value,
        runtime_snapshot_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        parent_turn_id: Optional[str] = None,
    ) -> Turn:
        now = utc_now()
        turn = Turn(
            turn_id=gen_turn_id(),
            conversation_id=conversation_id,
            status=status,
            turn_version=1,
            runtime_snapshot_id=runtime_snapshot_id,
            started_at=now,
            parent_conversation_id=parent_conversation_id,
            parent_turn_id=parent_turn_id,
        )
        conn.execute(
            """INSERT INTO turns (
                turn_id, conversation_id, status, turn_version, runtime_snapshot_id,
                started_at, finished_at, final_assistant_node_id, error_code,
                parent_conversation_id, parent_turn_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                turn.turn_id, conversation_id, turn.status, turn.turn_version,
                runtime_snapshot_id, now, None, None, None,
                parent_conversation_id, parent_turn_id,
            ),
        )
        return turn

    def create_turn_if_no_active(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        *,
        status: str = TurnStatus.QUEUED.value,
        runtime_snapshot_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        parent_turn_id: Optional[str] = None,
    ) -> Optional[Turn]:
        """条件创建活动 Turn（设计方案 5.2）：同一 Conversation 已有非终态
        Turn 时不创建。

        用 INSERT ... SELECT ... WHERE NOT EXISTS 在写事务内原子判定，修复
        并发 send_next/start_turn 事务外检查导致的"双活动 Turn → 队列死锁"
        （TOCTOU）。返回新 Turn；已有活动 Turn 时返回 None。"""
        now = utc_now()
        turn = Turn(
            turn_id=gen_turn_id(),
            conversation_id=conversation_id,
            status=status,
            turn_version=1,
            runtime_snapshot_id=runtime_snapshot_id,
            started_at=now,
            parent_conversation_id=parent_conversation_id,
            parent_turn_id=parent_turn_id,
        )
        cursor = conn.execute(
            """INSERT INTO turns (
                turn_id, conversation_id, status, turn_version, runtime_snapshot_id,
                started_at, finished_at, final_assistant_node_id, error_code,
                parent_conversation_id, parent_turn_id
            )
            SELECT ?,?,?,?,?,?,?,?,?,?,?
            WHERE NOT EXISTS (
                SELECT 1 FROM turns
                WHERE conversation_id=? AND status NOT IN
                ('done','stopped','error','interrupted'))""",
            (
                turn.turn_id, conversation_id, turn.status, turn.turn_version,
                runtime_snapshot_id, now, None, None, None,
                parent_conversation_id, parent_turn_id, conversation_id,
            ),
        )
        if int(cursor.rowcount or 0) == 0:
            return None
        return turn

    def get_turn(self, turn_id: str) -> Turn:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM turns WHERE turn_id=?", (turn_id,),
            ).fetchone()
        if row is None:
            raise TurnNotFound(f"Turn 不存在: {turn_id}")
        return _turn_from_row(row)

    def get_active_turn(self, conversation_id: str) -> Optional[Turn]:
        """同一 Conversation 最多一个非终态 Turn（设计方案 5.2）。"""
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM turns WHERE conversation_id=? AND status NOT IN "
                "('done','stopped','error','interrupted') "
                "ORDER BY started_at DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return _turn_from_row(row) if row else None

    def update_turn_status(
        self,
        conn: sqlite3.Connection,
        turn_id: str,
        status: str,
        *,
        error_code: Optional[str] = None,
        finished_at: Optional[str] = None,
        final_assistant_node_id: Optional[str] = None,
    ) -> Turn:
        now = utc_now()
        conn.execute(
            """UPDATE turns SET status=?, turn_version=turn_version+1,
               finished_at=COALESCE(?, finished_at), error_code=COALESCE(?, error_code),
               final_assistant_node_id=COALESCE(?, final_assistant_node_id)
               WHERE turn_id=?""",
            (status, finished_at, error_code, final_assistant_node_id, turn_id),
        )
        row = conn.execute(
            "SELECT * FROM turns WHERE turn_id=?", (turn_id,),
        ).fetchone()
        return _turn_from_row(row)

    def bump_turn_version(self, conn: sqlite3.Connection, turn_id: str) -> int:
        """节点/运行事件提交时递增 turn_version（设计方案 18.2）。"""
        conn.execute(
            "UPDATE turns SET turn_version=turn_version+1 WHERE turn_id=?",
            (turn_id,),
        )
        row = conn.execute(
            "SELECT turn_version FROM turns WHERE turn_id=?", (turn_id,),
        ).fetchone()
        return int(row["turn_version"]) if row else 0

    def finalize_turn_nodes(self, conn: sqlite3.Connection, turn_id: str) -> int:
        """Turn 终态：把残留的流式/排队节点统一置为 done（设计方案 7.2，
        避免历史/快照中出现"正在生成…"占位）。返回受影响节点数。"""
        cursor = conn.execute(
            "UPDATE turn_nodes SET status='done', updated_at=? "
            "WHERE turn_id=? AND status IN ('streaming','queued')",
            (utc_now(), turn_id),
        )
        return int(cursor.rowcount or 0)

    def sweep_running_tool_nodes(self, conn: sqlite3.Connection, turn_id: str, *,
                                 note: str, error_code: str) -> list[dict]:
        """Turn 终态兜底：把仍处于 running 的工具节点收口为 error。

        正常链路由 tool_call_end / 结果事件收口；但轮次以停止/超时/中断
        终态时（zombie 事件隔离会按设计丢弃迟到事件、stop_timeout 等待
        30s 即放弃），end 事件永远不来，工具卡会永久停在"执行中"。
        此处按轮次归属兜底收口：status='error' + metadata 写入人读说明
        （error/error_message/result_summary，前端徽章翻转为"失败"、返回
        区显示说明）。返回被收口节点的事件载荷列表（供 service 发
        node.tool 事件），无收口时返回空表。"""
        now = utc_now()
        rows = conn.execute(
            "SELECT node_id, position, metadata FROM turn_nodes "
            "WHERE turn_id=? AND type='tool' AND status='running'",
            (turn_id,),
        ).fetchall()
        swept: list[dict] = []
        for row in rows:
            metadata = dict(_loads_json(row["metadata"]) or {})
            # Plan/Goal/Subagent 的运行时活动投影节点（metadata.runtime_type）
            # 生命周期跟随后台任务而非本 Turn：running 是其合法持久态，
            # 不在收口范围（否则后台步骤会被终态误标为失败）。
            if metadata.get("runtime_type"):
                continue
            metadata.setdefault("result_summary", note)
            metadata["error"] = note
            metadata["error_code"] = error_code
            metadata["error_message"] = note
            metadata["turn_terminal_sweep"] = True
            conn.execute(
                "UPDATE turn_nodes SET status='error', metadata=?, updated_at=? "
                "WHERE node_id=? AND status='running'",
                (_dumps_json(metadata), now, row["node_id"]),
            )
            swept.append({
                "node_id": row["node_id"],
                "call_id": metadata.get("call_id") or "",
                "tool": metadata.get("tool") or "",
                "params_summary": metadata.get("params_summary") or "",
                "result_summary": note,
                "position": row["position"],
            })
        return swept

    def count_active_turns_in_scope(self, execution_scope: str) -> int:
        """执行域内非终态 Turn 数量（设计方案 13）。"""
        with self._db.connection() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS c FROM turns t
                   JOIN conversation_sessions c ON c.conversation_id = t.conversation_id
                   WHERE c.execution_scope=? AND t.status NOT IN
                   ('done','stopped','error','interrupted')""",
                (execution_scope,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def count_active_turns(self) -> int:
        """进程级全局非终态 Turn 数量（设计方案 13/30.3：503 上限）。"""
        with self._db.connection() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS c FROM turns
                   WHERE status NOT IN
                   ('done','stopped','error','interrupted')""",
            ).fetchone()
        return int(row["c"]) if row else 0

    def list_turns(self, conversation_id: str, *, before: Optional[str] = None,
                   limit: int = 30) -> list[Turn]:
        limit = max(1, min(int(limit), 200))
        sql = ("SELECT * FROM turns WHERE conversation_id=? "
               + ("AND started_at < ? " if before else "")
               + "ORDER BY started_at DESC LIMIT ?")
        params: list = [conversation_id]
        if before:
            params.append(before)
        params.append(limit)
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_turn_from_row(r) for r in rows]

    def interrupt_active_turns(self, conn: sqlite3.Connection) -> int:
        """后端重启：将一切非终态 Turn 置为 interrupted（设计方案 5.2）。"""
        now = utc_now()
        cursor = conn.execute(
            """UPDATE turns SET status='interrupted', turn_version=turn_version+1,
               error_code=COALESCE(error_code,'gateway_restart'),
               finished_at=COALESCE(finished_at, ?)
               WHERE status NOT IN ('done','stopped','error','interrupted')""",
            (now,),
        )
        return int(cursor.rowcount or 0)

    # ============================================================
    # TurnNode
    # ============================================================

    def _next_position(self, conn: sqlite3.Connection, turn_id: str) -> int:
        row = conn.execute(
            "SELECT MAX(position) AS m FROM turn_nodes WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        return int(row["m"] or 0) + 1 if row and row["m"] is not None else 1

    def create_node(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        type: str,
        status: str,
        turn_id: Optional[str] = None,
        position: Optional[int] = None,
        text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source_channel: Optional[str] = None,
        source_message_id: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_name: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> TurnNode:
        now = utc_now()
        if position is None:
            position = self._next_position(conn, turn_id) if turn_id else None
        node = TurnNode(
            node_id=node_id or gen_node_id(),
            conversation_id=conversation_id,
            turn_id=turn_id,
            type=type,
            position=position,
            status=status,
            text=text,
            metadata=dict(metadata or {}),
            source_channel=source_channel,
            source_message_id=source_message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            created_at=now,
            updated_at=now,
        )
        conn.execute(
            """INSERT INTO turn_nodes (
                node_id, conversation_id, turn_id, type, position, status, text,
                metadata, source_channel, source_message_id, sender_id, sender_name,
                created_at, updated_at, text_seq
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                node.node_id, conversation_id, turn_id, type, position, status, text,
                _dumps_json(node.metadata), source_channel, source_message_id,
                sender_id, sender_name, now, now,
                # B1 契约①：首段文本即 seq=1（流式 delta 的单调序号从 1 起）
                1 if text is not None else 0,
            ),
        )
        return node

    def assign_node_to_turn(
        self, conn: sqlite3.Connection, node_id: str, turn_id: str, position: int,
    ) -> TurnNode:
        """排队中的渠道 User Node 出队时原位升级为 Turn User Node（设计方案 11.3）。"""
        conn.execute(
            "UPDATE turn_nodes SET turn_id=?, position=?, updated_at=? WHERE node_id=?",
            (turn_id, position, utc_now(), node_id),
        )
        row = conn.execute(
            "SELECT * FROM turn_nodes WHERE node_id=?", (node_id,),
        ).fetchone()
        return _node_from_row(row)

    def set_node_status(self, conn: sqlite3.Connection, node_id: str,
                        status: str) -> TurnNode:
        """更新节点状态并返回更新后的节点（同连接读，避免未提交写不可见）。

        修复：原实现返回 None，service.finalize_node 读取返回值构造事件时
        抛 AttributeError（被 bridge 的 try/except 吞掉，节点停留在 streaming）。"""
        conn.execute(
            "UPDATE turn_nodes SET status=?, updated_at=? WHERE node_id=?",
            (status, utc_now(), node_id),
        )
        row = conn.execute(
            "SELECT * FROM turn_nodes WHERE node_id=?", (node_id,),
        ).fetchone()
        node = _node_from_row(row)
        node.text_seq = int(row["text_seq"] or 0)
        return node

    def get_node(self, node_id: str) -> TurnNode:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM turn_nodes WHERE node_id=?", (node_id,),
            ).fetchone()
        if row is None:
            raise ResourceNotFound(f"节点不存在: {node_id}")
        return _node_from_row(row)

    def get_turn_nodes(self, turn_id: str) -> list[TurnNode]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM turn_nodes WHERE turn_id=? ORDER BY position",
                (turn_id,),
            ).fetchall()
        return [_node_from_row(r) for r in rows]

    def get_last_turn_node(self, conn: sqlite3.Connection, turn_id: str,
                           type: str) -> Optional[TurnNode]:
        """Turn 内指定类型的最后一个节点（连续 delta 合并用，同连接读）。"""
        row = conn.execute(
            "SELECT * FROM turn_nodes WHERE turn_id=? AND type=? "
            "ORDER BY position DESC LIMIT 1",
            (turn_id, type),
        ).fetchone()
        return _node_from_row(row) if row else None

    def get_turn_user_node(self, turn_id: str) -> Optional[TurnNode]:
        """Turn 的第一个 User 节点（出队执行时构造 Agent 输入，设计方案 8.5）。"""
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM turn_nodes WHERE turn_id=? AND type='user' "
                "ORDER BY position LIMIT 1",
                (turn_id,),
            ).fetchone()
        return _node_from_row(row) if row else None

    def find_queued_node(self, conversation_id: str,
                         source_message_id: str) -> Optional[TurnNode]:
        """按渠道消息 ID 查找尚未出队的 queued User Node（设计方案 11.3）。"""
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM turn_nodes WHERE conversation_id=? AND turn_id IS NULL "
                "AND source_message_id=? ORDER BY created_at DESC LIMIT 1",
                (conversation_id, source_message_id),
            ).fetchone()
        return _node_from_row(row) if row else None

    def find_last_node_id(self, conn: sqlite3.Connection, turn_id: str,
                           node_type: str) -> Optional[str]:
        """Turn 内指定类型的最后一个节点 ID（连续 delta 合并定位用）。

        只 SELECT 主键列，避免流式路径每 delta 全行读取（含累计全文）的
        O(n^2) 文本拷贝；配合 append_node_text 的 SQL 追加实现 B1。"""
        row = conn.execute(
            "SELECT node_id FROM turn_nodes WHERE turn_id=? AND type=? "
            "ORDER BY position DESC LIMIT 1",
            (turn_id, node_type),
        ).fetchone()
        return row["node_id"] if row else None

    def append_node_text(self, conn: sqlite3.Connection, node_id: str,
                         delta: str) -> TurnNode:
        """流式 delta 的 SQL 追加（B1）：text = COALESCE(text,'') || ?。

        追加与 text_seq 递增在同一 UPDATE 内原子完成（契约① seq 落库），
        不再 Python 侧读全文拼接，把每次 delta 的拷贝从 O(n) 降为 O(1)。
        返回更新后的节点（text_seq 以动态属性附在 TurnNode 上供事件使用）。"""
        conn.execute(
            "UPDATE turn_nodes SET text = COALESCE(text, '') || ?, "
            "text_seq = text_seq + 1, updated_at = ? WHERE node_id = ?",
            (delta, utc_now(), node_id),
        )
        row = conn.execute(
            "SELECT * FROM turn_nodes WHERE node_id=?", (node_id,),
        ).fetchone()
        node = _node_from_row(row)
        node.text_seq = int(row["text_seq"] or 0)
        return node

    def update_node(self, conn: sqlite3.Connection, node_id: str, *,
                    text: Optional[str] = None, status: Optional[str] = None,
                    metadata: Optional[Dict[str, Any]] = None) -> TurnNode:
        """单事务更新节点字段并返回（同连接读，避免未提交写不可见）。

        text 为**整值替换**语义（终态权威全文用，如 complete_turn 的
        chat.done 覆盖）；流式增量追加请用 append_node_text（B1）。"""
        sets, params = [], []
        if text is not None:
            sets.append("text=?")
            params.append(text)
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if metadata is not None:
            sets.append("metadata=?")
            params.append(_dumps_json(metadata))
        if not sets:
            return self.get_node(node_id)
        sets.append("updated_at=?")
        params.append(utc_now())
        params.append(node_id)
        conn.execute(
            f"UPDATE turn_nodes SET {', '.join(sets)} WHERE node_id=?", params)
        row = conn.execute(
            "SELECT * FROM turn_nodes WHERE node_id=?", (node_id,),
        ).fetchone()
        return _node_from_row(row)

    # ============================================================
    # Queue
    # ============================================================

    def _active_queue_count(self, conn: sqlite3.Connection, conversation_id: str) -> int:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM queue_items
               WHERE conversation_id=? AND status NOT IN
               ('sent','injected','deleted','failed') AND status <> 'pending_delete'""",
            (conversation_id,),
        ).fetchone()
        return int(row["c"]) if row else 0

    def enqueue_item(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        text: str,
        *,
        operation_id: Optional[str] = None,
        target_turn_id: Optional[str] = None,
        images: Optional[list] = None,
    ) -> QueueItem:
        text = str(text or "")
        if not text.strip():
            raise ValidationFailed("消息不能为空")
        if len(text) > MAX_QUEUE_TEXT_LENGTH:
            raise QueueLimit(f"消息超过 {MAX_QUEUE_TEXT_LENGTH} 字符上限")
        if self._active_queue_count(conn, conversation_id) >= MAX_ACTIVE_QUEUE_ITEMS:
            raise QueueLimit(f"队列已满（最多 {MAX_ACTIVE_QUEUE_ITEMS} 条）")
        row = conn.execute(
            "SELECT MAX(position) AS m FROM queue_items WHERE conversation_id=? "
            "AND status NOT IN ('sent','injected','deleted','failed')",
            (conversation_id,),
        ).fetchone()
        position = (int(row["m"] or 0) + 1) if row and row["m"] is not None else 1
        now = utc_now()
        item = QueueItem(
            queue_item_id=gen_queue_item_id(),
            conversation_id=conversation_id,
            position=position,
            revision=1,
            status=QueueItemStatus.WAITING.value,
            text=text,
            target_turn_id=target_turn_id,
            operation_id=operation_id,
            created_at=now,
            updated_at=now,
            images=images or None,
        )
        conn.execute(
            """INSERT INTO queue_items (
                queue_item_id, conversation_id, position, revision, status, text,
                target_turn_id, created_turn_id, created_node_id, operation_id,
                created_at, updated_at, images_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.queue_item_id, conversation_id, position, item.revision,
                item.status, text, target_turn_id, None, None, operation_id, now,
                now, json.dumps(images, ensure_ascii=False) if images else None,
            ),
        )
        return item

    def get_queue_item(self, queue_item_id: str) -> QueueItem:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM queue_items WHERE queue_item_id=?", (queue_item_id,),
            ).fetchone()
        if row is None:
            raise ResourceNotFound(f"队列项不存在: {queue_item_id}")
        return _queue_item_from_row(row)

    def list_active_queue(self, conversation_id: str,
                          conn: Optional[sqlite3.Connection] = None
                          ) -> list[QueueItem]:
        """活动队列（pending_delete 不参与数量/排序/倒计时/Steering，设计方案 8.6）。"""
        if conn is not None:
            rows = conn.execute(
                """SELECT * FROM queue_items WHERE conversation_id=? AND status NOT IN
                   ('sent','injected','deleted','failed','pending_delete')
                   ORDER BY position""",
                (conversation_id,),
            ).fetchall()
            return [_queue_item_from_row(r) for r in rows]
        with self._db.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM queue_items WHERE conversation_id=? AND status NOT IN
                   ('sent','injected','deleted','failed','pending_delete')
                   ORDER BY position""",
                (conversation_id,),
            ).fetchall()
        return [_queue_item_from_row(r) for r in rows]

    def update_queue_item(
        self,
        conn: sqlite3.Connection,
        queue_item_id: str,
        *,
        expected_revision: int,
        text: Optional[str] = None,
        status: Optional[str] = None,
        target_turn_id: Optional[str] = None,
    ) -> QueueItem:
        # 读-改-写防丢双保险：连接为 sqlite3 legacy 隔离（SELECT 自动提交、
        # 不持读快照），读到的 revision 仅作前置校验；真正的并发防护靠下方
        # 带 `AND revision=?` 的条件 UPDATE——写时才隐式开事务，快照必然最新，
        # 不存在 BUSY_SNAPSHOT 升级路径；版本被并发推进时 rowcount=0 抛冲突。
        current = conn.execute(
            "SELECT revision FROM queue_items WHERE queue_item_id=?",
            (queue_item_id,),
        ).fetchone()
        if current is None:
            raise ResourceNotFound(f"队列项不存在: {queue_item_id}")
        if int(current["revision"]) != int(expected_revision):
            raise QueueConflict(
                f"队列项版本冲突: 当前 {current['revision']}，期望 {expected_revision}")
        now = utc_now()
        if text is not None:
            if len(text) > MAX_QUEUE_TEXT_LENGTH:
                raise QueueLimit(f"消息超过 {MAX_QUEUE_TEXT_LENGTH} 字符上限")
            cursor = conn.execute(
                "UPDATE queue_items SET text=?, revision=revision+1, updated_at=? "
                "WHERE queue_item_id=? AND revision=?",
                (text, now, queue_item_id, expected_revision),
            )
            if int(cursor.rowcount or 0) == 0:
                raise QueueConflict(
                    f"队列项版本冲突: 当前 {expected_revision} 已被其他操作推进")
        if status is not None:
            cursor = conn.execute(
                "UPDATE queue_items SET status=?, revision=revision+1, updated_at=? "
                "WHERE queue_item_id=? AND revision=?",
                (status, now, queue_item_id, expected_revision),
            )
            if int(cursor.rowcount or 0) == 0:
                raise QueueConflict(
                    f"队列项版本冲突: 当前 {expected_revision} 已被其他操作推进")
        if target_turn_id is not None:
            cursor = conn.execute(
                "UPDATE queue_items SET target_turn_id=?, revision=revision+1, updated_at=? "
                "WHERE queue_item_id=? AND revision=?",
                (target_turn_id, now, queue_item_id, expected_revision),
            )
            if int(cursor.rowcount or 0) == 0:
                raise QueueConflict(
                    f"队列项版本冲突: 当前 {expected_revision} 已被其他操作推进")
        row = conn.execute(
            "SELECT * FROM queue_items WHERE queue_item_id=?", (queue_item_id,),
        ).fetchone()
        return _queue_item_from_row(row)

    def set_queue_status(self, conn: sqlite3.Connection, queue_item_id: str,
                         status: str) -> QueueItem:
        conn.execute(
            "UPDATE queue_items SET status=?, revision=revision+1, updated_at=? "
            "WHERE queue_item_id=?",
            (status, utc_now(), queue_item_id),
        )
        row = conn.execute(
            "SELECT * FROM queue_items WHERE queue_item_id=?", (queue_item_id,),
        ).fetchone()
        return _queue_item_from_row(row)

    def mark_queue_sent(self, conn: sqlite3.Connection, queue_item_id: str,
                        turn_id: str, node_id: Optional[str]) -> QueueItem:
        conn.execute(
            """UPDATE queue_items SET status='sent', created_turn_id=?,
               created_node_id=?, revision=revision+1, updated_at=? WHERE queue_item_id=?""",
            (turn_id, node_id, utc_now(), queue_item_id),
        )
        row = conn.execute(
            "SELECT * FROM queue_items WHERE queue_item_id=?", (queue_item_id,),
        ).fetchone()
        return _queue_item_from_row(row)

    def mark_queue_injected(self, conn: sqlite3.Connection, queue_item_id: str,
                            node_id: str) -> QueueItem:
        conn.execute(
            """UPDATE queue_items SET status='injected', created_node_id=?,
               revision=revision+1, updated_at=? WHERE queue_item_id=?""",
            (node_id, utc_now(), queue_item_id),
        )
        row = conn.execute(
            "SELECT * FROM queue_items WHERE queue_item_id=?", (queue_item_id,),
        ).fetchone()
        return _queue_item_from_row(row)

    def move_queue_item(self, conn: sqlite3.Connection, conversation_id: str,
                        queue_item_id: str, direction: str) -> list[QueueItem]:
        """上移/下移：与相邻活动队列项交换 position（设计方案 8.2 仅上移/下移）。"""
        if direction not in ("up", "down"):
            raise ValidationFailed("direction 必须是 up 或 down")
        items = self.list_active_queue(conversation_id, conn=conn)
        idx = next((i for i, it in enumerate(items) if it.queue_item_id == queue_item_id), None)
        if idx is None:
            raise ResourceNotFound(f"队列项不存在或已归档: {queue_item_id}")
        other = idx - 1 if direction == "up" else idx + 1
        if other < 0 or other >= len(items):
            return items  # 已在边界，无操作
        a, b = items[idx], items[other]
        now = utc_now()
        conn.execute(
            "UPDATE queue_items SET position=?, revision=revision+1, updated_at=? "
            "WHERE queue_item_id=?",
            (b.position, now, a.queue_item_id),
        )
        conn.execute(
            "UPDATE queue_items SET position=?, revision=revision+1, updated_at=? "
            "WHERE queue_item_id=?",
            (a.position, now, b.queue_item_id),
        )
        return self.list_active_queue(conversation_id, conn=conn)

    def undo_delete(self, conn: sqlite3.Connection, queue_item_id: str) -> QueueItem:
        item = self.get_queue_item(queue_item_id)
        if item.status != QueueItemStatus.PENDING_DELETE.value:
            raise QueueConflict("仅 pending_delete 状态可撤销")
        from datetime import datetime, timezone
        try:
            updated = datetime.fromisoformat(item.updated_at)
            now = datetime.now(timezone.utc)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if (now - updated).total_seconds() > UNDO_WINDOW_SECONDS:
                raise UndoExpired("撤销窗口（5 秒）已过期")
        except ValueError:
            raise UndoExpired("撤销窗口（5 秒）已过期")
        conn.execute(
            "UPDATE queue_items SET status='waiting', revision=revision+1, updated_at=? "
            "WHERE queue_item_id=?",
            (utc_now(), queue_item_id),
        )
        row = conn.execute(
            "SELECT * FROM queue_items WHERE queue_item_id=?", (queue_item_id,),
        ).fetchone()
        return _queue_item_from_row(row)

    def clear_waiting_queue(self, conn: sqlite3.Connection, conversation_id: str) -> int:
        """清空普通等待项（设计方案 8.6：只处理等待项，二次确认由调用方负责）。"""
        cursor = conn.execute(
            "UPDATE queue_items SET status='deleted', revision=revision+1, updated_at=? "
            "WHERE conversation_id=? AND status IN ('waiting')",
            (utc_now(), conversation_id),
        )
        return int(cursor.rowcount or 0)

    def archive_expired_pending_deletes(self, conn: sqlite3.Connection) -> int:
        """归档超过撤销窗口（5 秒）的 pending_delete 项 → deleted（设计方案 8.6）。

        删除单项先进入 pending_delete 提供 5 秒撤销；窗口过期后没有自动归档的
        路径，导致该项永远停在 pending_delete。这里按 updated_at 判定过期并
        置为 deleted（不再参与队列数量/排序/倒计时/Steering）。"""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=UNDO_WINDOW_SECONDS)
                  ).isoformat(timespec="milliseconds")
        cursor = conn.execute(
            "UPDATE queue_items SET status='deleted', revision=revision+1, updated_at=? "
            "WHERE status='pending_delete' AND updated_at < ?",
            (utc_now(), cutoff),
        )
        return int(cursor.rowcount or 0)

    def delete_conversation(self, conn: sqlite3.Connection, conversation_id: str) -> bool:
        """删除一个会话及其全部关联数据（含 conversation_sessions 行）。

        先删所有引用 conversation_sessions 的子表，避免外键约束失败。
        返回是否删除了会话行。"""
        for table in ("turn_nodes", "turns", "queue_items", "approvals",
                      "tool_results",
                      "idempotency_records", "outbox_events",
                      "channel_message_receipts"):
            conn.execute(f"DELETE FROM {table} WHERE conversation_id=?", (conversation_id,))
        cursor = conn.execute(
            "DELETE FROM conversation_sessions WHERE conversation_id=?",
            (conversation_id,),
        )
        return int(cursor.rowcount or 0) > 0

    def delete_stale_system_conversations(self, conn: sqlite3.Connection,
                                          retention_days: int = 7,
                                          *,
                                          batch_limit: int = 500) -> list:
        """删除超过保留窗口的 system 会话（方案A：定时任务会话窗口清理）。

        覆盖 origin='system'（sched:*/heartbeat:*/system:* 等）以及历史上被
        误标为 webui 的 sched:/heartbeat:/system: 键会话（早期 _origin_subtype
        未匹配 sched: 前缀）。webui / channel / workspace 会话不受影响。
        单次最多删除 batch_limit 条（周期任务下一轮继续回收），避免单事务
        过长阻塞其他写入。返回删除的 conversation_id 列表。"""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(1, int(retention_days)))
                  ).isoformat(timespec="milliseconds")
        rows = conn.execute(
            "SELECT conversation_id FROM conversation_sessions "
            "WHERE (origin='system' OR session_key LIKE 'sched:%' "
            "OR session_key LIKE 'heartbeat:%' OR session_key LIKE 'system:%') "
            "AND updated_at < ? ORDER BY updated_at LIMIT ?",
            (cutoff, int(batch_limit)),
        ).fetchall()
        deleted = []
        for row in rows:
            if self.delete_conversation(conn, row["conversation_id"]):
                deleted.append(row["conversation_id"])
        return deleted

    def clear_history(self, conn: sqlite3.Connection, conversation_id: str) -> dict:
        """清空会话全部历史（设计方案 §30：/clear 与"清空聊天"走统一模型）。

        删除该会话的 turns / turn_nodes / queue_items / approvals /
        tool_results（保留 conversation_sessions 行，会话仍可继续发送）。
        返回删除计数供事件广播。
        """
        counts = {}
        for table, where in (
            ("turn_nodes", "conversation_id=?"),
            ("turns", "conversation_id=?"),
            ("queue_items", "conversation_id=?"),
            ("approvals", "conversation_id=?"),
            ("tool_results", "conversation_id=?"),
        ):
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {where}", (conversation_id,))
            counts[table] = int(cursor.rowcount or 0)
        return counts

    # ============================================================
    # Steering
    # ============================================================

    def count_steering_nodes(self, conn: sqlite3.Connection, turn_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM turn_nodes WHERE turn_id=? AND type='user_steering'",
            (turn_id,),
        ).fetchone()
        return int(row["c"]) if row else 0

    def reset_interrupted_queue_items(self, conn: sqlite3.Connection) -> int:
        """后端重启：waiting_for_steering / injecting 项恢复为普通等待项
        （设计方案 9.3：中断的 Steering 待插入消息恢复为普通队列项）。"""
        cursor = conn.execute(
            "UPDATE queue_items SET status='waiting', revision=revision+1, updated_at=? "
            "WHERE status IN ('waiting_for_steering','injecting')",
            (utc_now(),),
        )
        return int(cursor.rowcount or 0)

    # ============================================================
    # Idempotency
    # ============================================================

    def check_idempotency(self, conn: sqlite3.Connection, operation_id: str,
                          conversation_id: str, request_hash: str):
        """返回 (hit, result)。hit=True 表示已执行过；同 ID 异请求抛冲突。"""
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return False, None
        if row["request_hash"] != request_hash:
            from gateway.conversation.errors import IdempotencyConflict
            raise IdempotencyConflict(
                "相同 operation_id 对应不同请求，拒绝执行")
        return True, _loads_json(row["result_json"])

    def record_idempotency(self, conn: sqlite3.Connection, operation_id: str,
                           conversation_id: str, request_hash: str,
                           result: Optional[dict] = None) -> None:
        conn.execute(
            """INSERT INTO idempotency_records (
                operation_id, conversation_id, request_hash, result_json, created_at
            ) VALUES (?,?,?,?,?)""",
            (operation_id, conversation_id, request_hash,
             _dumps_json(result) if result is not None else None, utc_now()),
        )

    def cleanup_idempotency(self, conn: sqlite3.Connection) -> int:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=IDEMPOTENCY_TTL_SECONDS)) \
            .isoformat(timespec="milliseconds")
        cursor = conn.execute(
            "DELETE FROM idempotency_records WHERE created_at < ?", (cutoff,))
        return int(cursor.rowcount or 0)

    # ============================================================
    # Outbox
    # ============================================================

    def append_outbox(self, conn: sqlite3.Connection, conversation_id: str,
                      event_type: str, scope: str, version: int,
                      payload: Optional[Dict[str, Any]] = None) -> OutboxEvent:
        now = utc_now()
        event = OutboxEvent(
            outbox_id=gen_outbox_id(),
            conversation_id=conversation_id,
            event_type=event_type,
            scope=scope,
            version=version,
            payload=dict(payload or {}),
            created_at=now,
        )
        conn.execute(
            """INSERT INTO outbox_events (
                outbox_id, conversation_id, event_type, scope, version, payload,
                created_at, published_at
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                event.outbox_id, conversation_id, event_type, scope, version,
                _dumps_json(event.payload), now, None,
            ),
        )
        return event

    def list_unpublished_outbox(self, limit: int = 200) -> list[OutboxEvent]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox_events WHERE published_at IS NULL "
                "ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [_outbox_from_row(r) for r in rows]

    def mark_outbox_published(self, conn: sqlite3.Connection, outbox_id: str) -> None:
        conn.execute(
            "UPDATE outbox_events SET published_at=? WHERE outbox_id=?",
            (utc_now(), outbox_id),
        )

    def trim_outbox_backlog(self, conn: sqlite3.Connection,
                            max_unpublished: int) -> int:
        """未发布事件超过上限时丢弃最老一批（防无限堆积，设计方案 16.5）。

        返回删除条数；调用方负责告警。"""
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM outbox_events WHERE published_at IS NULL",
        ).fetchone()
        count = int(row["c"] or 0) if row else 0
        if count <= max_unpublished:
            return 0
        excess = count - max_unpublished
        cursor = conn.execute(
            """DELETE FROM outbox_events WHERE outbox_id IN (
                SELECT outbox_id FROM outbox_events WHERE published_at IS NULL
                ORDER BY created_at LIMIT ?)""",
            (excess,),
        )
        return int(cursor.rowcount or 0)

    def cleanup_outbox(self, conn: sqlite3.Connection) -> int:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=OUTBOX_TTL_SECONDS)) \
            .isoformat(timespec="milliseconds")
        cursor = conn.execute(
            "DELETE FROM outbox_events WHERE published_at IS NOT NULL AND created_at < ?",
            (cutoff,),
        )
        return int(cursor.rowcount or 0)

    # ============================================================
    # Tool results（设计方案 17）
    # ============================================================

    def save_tool_result(self, conn: sqlite3.Connection, *, conversation_id: str,
                         turn_id: str, result_ref: str, kind: str,
                         node_id: Optional[str] = None, size_bytes: int = 0,
                         lines: int = 0, content_type: Optional[str] = None,
                         summary: Optional[Dict[str, Any]] = None,
                         truncation_reason: Optional[str] = None) -> ToolResult:
        result = ToolResult(
            result_ref=result_ref,
            conversation_id=conversation_id,
            turn_id=turn_id,
            node_id=node_id,
            kind=kind,
            size_bytes=size_bytes,
            lines=lines,
            content_type=content_type,
            summary=dict(summary or {}),
            truncation_reason=truncation_reason,
            created_at=utc_now(),
        )
        conn.execute(
            """INSERT INTO tool_results (
                result_ref, conversation_id, turn_id, node_id, kind, size_bytes,
                lines, content_type, summary, truncation_reason, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result.result_ref, conversation_id, turn_id, node_id, kind,
                size_bytes, lines, content_type, _dumps_json(result.summary),
                truncation_reason, result.created_at,
            ),
        )
        return result

    def get_tool_result(self, result_ref: str) -> ToolResult:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tool_results WHERE result_ref=?", (result_ref,),
            ).fetchone()
        if row is None:
            raise ResourceNotFound(f"工具结果不存在: {result_ref}")
        return _tool_result_from_row(row)
    # ============================================================
    # Channel receipts（设计方案 11.4 去重）
    # ============================================================

    def cleanup_receipts(self, conn: sqlite3.Connection) -> int:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RECEIPT_TTL_SECONDS)) \
            .isoformat(timespec="milliseconds")
        cursor = conn.execute(
            "DELETE FROM channel_message_receipts WHERE created_at < ?", (cutoff,))
        return int(cursor.rowcount or 0)

    # ============================================================
    # Approvals（设计方案 7.7）
    # ============================================================

    def create_approval(self, conn: sqlite3.Connection, *, conversation_id: str,
                        turn_id: str, tool_name: str, params_summary: str,
                        node_id: Optional[str] = None) -> Approval:
        approval = Approval(
            approval_id=gen_approval_id(),
            conversation_id=conversation_id,
            turn_id=turn_id,
            node_id=node_id,
            tool_name=tool_name,
            params_summary=params_summary,
            status=ApprovalStatus.PENDING.value,
            created_at=utc_now(),
        )
        conn.execute(
            """INSERT INTO approvals (
                approval_id, conversation_id, turn_id, node_id, tool_name,
                params_summary, status, created_at, resolved_at, resolved_by
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                approval.approval_id, conversation_id, turn_id, node_id,
                tool_name, params_summary, approval.status, approval.created_at,
                None, None,
            ),
        )
        return approval

    def get_approval(self, approval_id: str) -> Approval:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id=?", (approval_id,),
            ).fetchone()
        if row is None:
            raise ResourceNotFound(f"审批不存在: {approval_id}")
        return _approval_from_row(row)

    def resolve_approval(self, conn: sqlite3.Connection, approval_id: str,
                         status: str, resolved_by: str) -> Approval:
        current = self.get_approval(approval_id)
        if current.status != ApprovalStatus.PENDING.value:
            raise ApprovalConflict(f"审批状态为 {current.status}，不能重复处理")
        conn.execute(
            "UPDATE approvals SET status=?, resolved_at=?, resolved_by=? WHERE approval_id=?",
            (status, utc_now(), resolved_by, approval_id),
        )
        row = conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,),
        ).fetchone()
        return _approval_from_row(row)

    def list_pending_approvals(self, conversation_id: str) -> list[Approval]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE conversation_id=? AND status='pending' "
                "ORDER BY created_at", (conversation_id,),
            ).fetchall()
        return [_approval_from_row(r) for r in rows]

    def expire_stale_approvals(self, conn: sqlite3.Connection) -> int:
        """审批超时（默认 300 秒）视为拒绝（设计方案 7.7）。"""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=APPROVAL_TIMEOUT_SECONDS)) \
            .isoformat(timespec="milliseconds")
        cursor = conn.execute(
            "UPDATE approvals SET status='timed_out', resolved_at=? "
            "WHERE status='pending' AND created_at < ?",
            (utc_now(), cutoff),
        )
        return int(cursor.rowcount or 0)

    # ============================================================
    # Snapshot / History（设计方案 19）
    # ============================================================

    def snapshot(self, conversation_id: str) -> dict:
        """同一数据库读事务返回完整会话快照（设计方案 19.1）。

        显式 BEGIN 开启读事务：默认 sqlite 连接在逐条 SELECT 之间自动提交，
        多表读取可能读到混合版本（非原子）。BEGIN 后所有 SELECT 看到同一
        快照，退出时统一 commit/rollback。"""
        with self._db.connection() as conn:
            conn.execute("BEGIN")
            conv_row = conn.execute(
                "SELECT * FROM conversation_sessions WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if conv_row is None:
                raise ResourceNotFound(f"会话不存在: {conversation_id}")
            conversation = _conversation_from_row(conv_row)
            queue_rows = conn.execute(
                """SELECT * FROM queue_items WHERE conversation_id=? AND status NOT IN
                   ('sent','injected','deleted','failed','pending_delete') ORDER BY position""",
                (conversation_id,),
            ).fetchall()
            turn_row = conn.execute(
                "SELECT * FROM turns WHERE conversation_id=? AND status NOT IN "
                "('done','stopped','error','interrupted') ORDER BY started_at DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            nodes: list = []
            turn_version = 0
            if turn_row is not None:
                turn = _turn_from_row(turn_row)
                turn_version = turn.turn_version
                node_rows = conn.execute(
                    "SELECT * FROM turn_nodes WHERE turn_id=? ORDER BY position",
                    (turn.turn_id,),
                ).fetchall()
                nodes = [_node_from_row(r) for r in node_rows]
            approval_rows = conn.execute(
                "SELECT * FROM approvals WHERE conversation_id=? AND status='pending' "
                "ORDER BY created_at", (conversation_id,),
            ).fetchall()
            queued_node_rows = conn.execute(
                "SELECT * FROM turn_nodes WHERE conversation_id=? AND turn_id IS NULL "
                "ORDER BY created_at", (conversation_id,),
            ).fetchall()
        return {
            "conversation": conversation.to_dict(),
            "session_version": conversation.session_version,
            "queue": [_queue_item_from_row(r).to_dict() for r in queue_rows],
            "live_turn": (_turn_from_row(turn_row).to_dict()
                          if turn_row is not None else None),
            "turn_version": turn_version,
            "nodes": [n.to_dict() for n in nodes],
            "queued_nodes": [_node_from_row(r).to_dict() for r in queued_node_rows],
            "pending_approvals": [_approval_from_row(r).to_dict()
                                  for r in approval_rows],
            "server_time": utc_now(),
        }

    @staticmethod
    def _encode_history_cursor(started_at: str, rowid: int) -> str:
        # B3 复合游标：毫秒时间戳可并列，需 (started_at, rowid) 双键；
        # rowid 即插入序，保证同毫秒 Turn 的时间正序稳定。
        import base64
        import json as _json
        raw = _json.dumps({"s": started_at, "r": int(rowid)},
                          ensure_ascii=False, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_history_cursor(cursor):
        # 解码复合游标；兼容旧的纯 started_at 字符串游标（无平局键）。
        import base64
        import json as _json
        if not cursor:
            return None
        try:
            raw = _json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            return str(raw["s"]), int(raw["r"])
        except Exception:
            return (cursor, None)

    def history_page(self, conversation_id: str, *, before: Optional[str] = None,
                     limit: int = 30) -> dict:
        """历史分页：完整 Turn 为最小分页单位（设计方案 19.3）。

        ``items`` 按**时间正序**返回（旧 → 新，最新 Turn 在末尾），
        ``next_cursor`` 指向更早一页的边界（本页最旧 Turn 的 started_at）。
        """
        # B3 修正：毫秒时间戳可并列，游标/探测改用 (started_at, rowid) 复合键
        # （rowid=插入序，保证同毫秒 Turn 按创建先后稳定排序）。
        keyset = self._decode_history_cursor(before)
        sql = ("SELECT turns.*, turns.rowid AS _rid FROM turns "
               "WHERE conversation_id=?")
        params = [conversation_id]
        if keyset:
            ks, kr = keyset
            if kr is not None:
                sql += " AND (started_at < ? OR (started_at = ? AND turns.rowid < ?))"
                params += [ks, ks, kr]
            else:
                sql += " AND started_at < ?"
                params.append(ks)
        sql += " ORDER BY started_at DESC, turns.rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            turns = [_turn_from_row(r) for r in rows]
            rid_by_turn = {r["turn_id"]: r["_rid"] for r in rows}
        next_cursor = None
        if turns:
            last = turns[-1]  # DESC 列表末尾 = 本页最旧
            last_rid = rid_by_turn.get(last.turn_id)
            if last_rid is not None:
                with self._db.connection() as conn:
                    row = conn.execute(
                        "SELECT 1 FROM turns WHERE conversation_id=? "
                        "AND (started_at < ? OR (started_at = ? AND rowid < ?)) "
                        "LIMIT 1",
                        (conversation_id, last.started_at, last.started_at,
                         last_rid),
                    ).fetchone()
                if row is not None:
                    next_cursor = self._encode_history_cursor(last.started_at,
                                                              last_rid)
        turns.reverse()  # 时间正序（旧 → 新），避免最新消息显示在最上方
        out = []
        if turns:
            # B3：合并 N+1（每 Turn 一次节点查询）为单条 IN 查询
            turn_ids = [t.turn_id for t in turns]
            placeholders = ",".join("?" for _ in turn_ids)
            with self._db.connection() as conn:
                node_rows = conn.execute(
                    f"SELECT * FROM turn_nodes WHERE turn_id IN ({placeholders}) "
                    "ORDER BY turn_id, position",
                    turn_ids,
                ).fetchall()
            by_turn: dict[str, list] = {}
            for row in node_rows:
                by_turn.setdefault(row["turn_id"], []).append(_node_from_row(row))
            for turn in turns:
                out.append({
                    "turn": turn.to_dict(),
                    "nodes": [n.to_dict()
                              for n in by_turn.get(turn.turn_id, [])],
                })
        return {"items": out, "next_cursor": next_cursor}


# ============================================================
# 行 → 领域对象
# ============================================================


def _conversation_from_row(row) -> ConversationSession:
    return ConversationSession(
        conversation_id=row["conversation_id"],
        session_key=row["session_key"],
        origin=row["origin"],
        subtype=row["subtype"],
        workspace_id=row["workspace_id"],
        execution_scope=row["execution_scope"],
        route_metadata=_loads_json(row["route_metadata"]),
        session_version=int(row["session_version"] or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _turn_from_row(row) -> Turn:
    return Turn(
        turn_id=row["turn_id"],
        conversation_id=row["conversation_id"],
        status=row["status"],
        turn_version=int(row["turn_version"] or 0),
        runtime_snapshot_id=row["runtime_snapshot_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        final_assistant_node_id=row["final_assistant_node_id"],
        error_code=row["error_code"],
        parent_conversation_id=row["parent_conversation_id"],
        parent_turn_id=row["parent_turn_id"],
    )


def _node_from_row(row) -> TurnNode:
    return TurnNode(
        node_id=row["node_id"],
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        type=row["type"],
        position=row["position"],
        status=row["status"],
        text=row["text"],
        metadata=_loads_json(row["metadata"]),
        source_channel=row["source_channel"],
        source_message_id=row["source_message_id"],
        sender_id=row["sender_id"],
        sender_name=row["sender_name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _queue_item_from_row(row) -> QueueItem:
    return QueueItem(
        queue_item_id=row["queue_item_id"],
        conversation_id=row["conversation_id"],
        position=int(row["position"] or 0),
        revision=int(row["revision"] or 1),
        status=row["status"],
        text=row["text"],
        target_turn_id=row["target_turn_id"],
        created_turn_id=row["created_turn_id"],
        created_node_id=row["created_node_id"],
        operation_id=row["operation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        images=(json.loads(row["images_json"]) if row["images_json"] else None),
    )


def _outbox_from_row(row) -> OutboxEvent:
    return OutboxEvent(
        outbox_id=row["outbox_id"],
        conversation_id=row["conversation_id"],
        event_type=row["event_type"],
        scope=row["scope"],
        version=int(row["version"] or 0),
        payload=_loads_json(row["payload"]),
        created_at=row["created_at"],
        published_at=row["published_at"],
    )


def _tool_result_from_row(row) -> ToolResult:
    return ToolResult(
        result_ref=row["result_ref"],
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        node_id=row["node_id"],
        kind=row["kind"],
        size_bytes=int(row["size_bytes"] or 0),
        lines=int(row["lines"] or 0),
        content_type=row["content_type"],
        summary=_loads_json(row["summary"]),
        truncation_reason=row["truncation_reason"],
        created_at=row["created_at"],
    )


def _approval_from_row(row) -> Approval:
    return Approval(
        approval_id=row["approval_id"],
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        node_id=row["node_id"],
        tool_name=row["tool_name"],
        params_summary=row["params_summary"],
        status=row["status"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
    )
