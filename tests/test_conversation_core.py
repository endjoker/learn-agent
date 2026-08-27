# -*- coding: utf-8 -*-
"""
统一会话核心单元测试 —— 覆盖设计方案 P0 全量后端行为。

覆盖：Schema / Conversation / Turn / Node / Queue / Steering / Stop /
Approval / Lease / Idempotency / Execution Scope / Outbox / Snapshot /
History / 渠道去重 / 工具结果归属 / 重启恢复 / 数据保留清理。
"""

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.runtime import RuntimeStore
from gateway.webui.workspace_store import WorkspaceDatabase
from gateway.conversation import (
    ConversationService,
    ConversationStore,
    OutboxPublisher,
    gen_node_id_from_call,
)
from gateway.conversation.errors import (
    ApprovalConflict,
    ExecutionScopeLimit,
    IdempotencyConflict,
    QueueConflict,
    QueueLimit,
    ResultNotOwned,
    SteeringLimit,
    TurnNotFound,
    UndoExpired,
)
from gateway.conversation.models import (
    ApprovalStatus,
    QueueItemStatus,
    TurnStatus,
)


class ConversationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = RuntimeStore(Path(self.tmp.name) / "runtime.db")
        self.db = WorkspaceDatabase(runtime_store=self.runtime)
        self.store = ConversationStore(self.db)
        self.events = []
        self.service = ConversationService(
            self.store, lambda t, p: self.events.append((t, p)),
            max_global_turns=100)  # 单元测试默认放开全局上限，聚焦执行域/行为语义

    def tearDown(self):
        self.tmp.cleanup()

    def new_conv(self, session_key="webui:default", origin="webui",
                 subtype="main", workspace_id=None):
        return self.service.get_or_create_conversation(
            session_key, origin=origin, subtype=subtype, workspace_id=workspace_id)

    def events_of(self, event_type):
        return [p for t, p in self.events if t == event_type]


class ConversationSchemaTests(ConversationBase):
    def test_v12_tables_exist(self):
        with self.db.connection() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("conversation_sessions", "turns", "turn_nodes", "queue_items",
                     "idempotency_records", "outbox_events",
                     "tool_results",
                     "channel_message_receipts", "approvals"):
            self.assertIn(name, tables)

    def test_migration_idempotent_on_existing_db(self):
        # 重新打开同一文件不应报错（schema_migrations 幂等）
        RuntimeStore(Path(self.tmp.name) / "runtime.db")
        RuntimeStore(Path(self.tmp.name) / "runtime.db")
        with self.db.connection() as conn:
            version = conn.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        # v14：artifacts 关联列回填 + tasks(status)/sessions(parent_session_id) 索引
        # v16：queue_items 增 images_json（图片信封列）
        self.assertEqual(version, 16)


class ConversationCreationTests(ConversationBase):
    def test_same_session_key_dedup(self):
        c1 = self.new_conv()
        c2 = self.new_conv()
        self.assertEqual(c1.conversation_id, c2.conversation_id)

    def test_execution_scope_mapping(self):
        self.assertEqual(self.new_conv().execution_scope, "gateway:default")
        ws = self.new_conv("workspace:w1:s1", subtype="workspace", workspace_id="w1")
        self.assertEqual(ws.execution_scope, "workspace:w1")
        sys = self.new_conv("plan:abc", origin="system", subtype="plan")
        self.assertEqual(sys.execution_scope, "system:plan")

    def test_upserted_event_emitted(self):
        self.new_conv()
        self.assertTrue(self.events_of("conversation.upserted"))


class TurnLifecycleTests(ConversationBase):
    def test_single_active_turn_per_conversation(self):
        conv = self.new_conv()
        # 无队列项时 send_next 为 no-op
        self.assertIsNone(self.service.send_next(conv.conversation_id))
        self.service.enqueue(conv.conversation_id, "a")
        self.service.enqueue(conv.conversation_id, "b")
        turn1, _ = self.service.send_next(conv.conversation_id)
        # 已有活动 Turn，再次 send_next 为 no-op
        self.assertIsNone(self.service.send_next(conv.conversation_id))
        self.assertEqual(self.store.get_active_turn(conv.conversation_id).turn_id,
                         turn1.turn_id)

    def test_node_order_and_positions(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "hello")
        turn, user_node = self.service.send_next(conv.conversation_id)
        self.assertEqual(user_node.position, 1)
        self.service.upsert_node_delta(
            conv.conversation_id, turn.turn_id, "reasoning", "r1",
            continue_existing=True)
        self.service.upsert_tool_node(
            conv.conversation_id, turn.turn_id, "call-1", status="done")
        self.service.upsert_node_delta(
            conv.conversation_id, turn.turn_id, "reasoning", "r2",
            continue_existing=False)  # 被打断后新建
        self.service.upsert_node_delta(
            conv.conversation_id, turn.turn_id, "assistant", "final",
            continue_existing=False)
        nodes = self.store.get_turn_nodes(turn.turn_id)
        types = [n.type for n in nodes]
        self.assertEqual(types, ["user", "reasoning", "tool", "reasoning", "assistant"])
        positions = [n.position for n in nodes]
        self.assertEqual(positions, sorted(positions))
        # 工具节点按 call_id 幂等（同一 call_id 复用同一 node）
        tool_node = next(n for n in nodes if n.type == "tool")
        self.assertEqual(tool_node.metadata.get("call_id"), "call-1")

    def test_tool_node_metadata_merges_across_events(self):
        """工具节点多次事件合并 metadata（修复：整体覆盖导致卡片为空）。"""
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        # 事件顺序：start（工具名）→ end（参数）→ result（返回）
        self.service.upsert_tool_node(
            conv.conversation_id, turn.turn_id, "call-1", status="running",
            tool_name="bash")
        self.service.upsert_tool_node(
            conv.conversation_id, turn.turn_id, "call-1", status="done",
            tool_name="bash", params_summary='{"cmd":"ls"}')
        self.service.upsert_tool_node(
            conv.conversation_id, turn.turn_id, "call-1", status="done",
            result_summary="total 8")
        node = self.store.get_node(
            gen_node_id_from_call("call-1", conv.conversation_id))
        meta = node.metadata
        self.assertEqual(meta.get("tool"), "bash")
        self.assertEqual(meta.get("params_summary"), '{"cmd":"ls"}')
        self.assertEqual(meta.get("result_summary"), "total 8")
        self.assertEqual(node.status, "done")

    def test_clear_history_wipes_turns_and_nodes(self):
        """/clear 与"清空聊天"统一入口：删除历史但保留会话行。"""
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "hello")
        turn, _ = self.service.send_next(conv.conversation_id)
        self.service.upsert_node_delta(
            conv.conversation_id, turn.turn_id, "assistant", "hi",
            continue_existing=True)
        self.service.complete_turn(conv.conversation_id, turn.turn_id,
                                   "done", full_text="hi")
        self.service.enqueue(conv.conversation_id, "again")
        turn2, _ = self.service.send_next(conv.conversation_id)
        self.service.upsert_node_delta(
            conv.conversation_id, turn2.turn_id, "assistant", "again!",
            continue_existing=True)
        self.service.complete_turn(conv.conversation_id, turn2.turn_id,
                                   "done", full_text="again!")
        self.assertEqual(len(self.store.list_turns(conv.conversation_id)), 2)
        counts = self.service.clear_history(conv.conversation_id)
        self.assertGreaterEqual(counts.get("turns", 0), 2)
        self.assertEqual(self.store.list_turns(conv.conversation_id), [])
        # 会话行保留，可继续发送
        self.assertIsNotNone(
            self.store.get_conversation_by_key(conv.session_key))
        self.service.enqueue(conv.conversation_id, "after-clear")
        turn3, _ = self.service.send_next(conv.conversation_id)
        self.assertIsNotNone(turn3)
        # conversation.cleared 事件已广播
        self.assertTrue(self.events_of("conversation.cleared"))

    def test_complete_turn_terminal_and_chat_done(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        self.service.upsert_node_delta(
            conv.conversation_id, turn.turn_id, "assistant", "流式",
            continue_existing=True)
        self.service.complete_turn(conv.conversation_id, turn.turn_id,
                                   "done", full_text="权威最终回复")
        turn = self.store.get_turn(turn.turn_id)
        self.assertEqual(turn.status, TurnStatus.DONE.value)
        self.assertIsNotNone(turn.finished_at)
        # chat.done.full_text 覆盖流式文本
        node = self.store.get_node(turn.final_assistant_node_id)
        self.assertEqual(node.text, "权威最终回复")
        # 终态幂等：重复 complete 不报错不产生新事件
        before = len(self.events_of("chat.done"))
        self.service.complete_turn(conv.conversation_id, turn.turn_id,
                                   "done", full_text="再次")
        self.assertEqual(len(self.events_of("chat.done")), before)

    def test_restart_interrupts_active_turns(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        self.service.set_turn_status(conv.conversation_id, turn.turn_id, "thinking")
        result = self.service.recover_after_restart()
        self.assertEqual(result["interrupted_turns"], 1)
        self.assertEqual(self.store.get_turn(turn.turn_id).status,
                         TurnStatus.INTERRUPTED.value)
        self.assertEqual(self.store.get_turn(turn.turn_id).error_code,
                         "gateway_restart")
        # interrupted 后队列可继续（新 Turn）
        self.service.enqueue(conv.conversation_id, "再问")
        turn2, _ = self.service.send_next(conv.conversation_id)
        self.assertNotEqual(turn2.turn_id, turn.turn_id)


class QueueTests(ConversationBase):
    def test_queue_limit_20(self):
        conv = self.new_conv()
        for i in range(20):
            self.service.enqueue(conv.conversation_id, f"msg-{i}")
        with self.assertRaises(QueueLimit):
            self.service.enqueue(conv.conversation_id, "overflow")

    def test_queue_text_length_limit(self):
        conv = self.new_conv()
        with self.assertRaises(QueueLimit):
            self.service.enqueue(conv.conversation_id, "x" * 10001)

    def test_revision_optimistic_lock(self):
        conv = self.new_conv()
        item = self.service.enqueue(conv.conversation_id, "v1")
        with self.assertRaises(QueueConflict):
            self.service.edit_queue_item(
                conv.conversation_id, item.queue_item_id,
                expected_revision=99, text="stale")
        updated = self.service.edit_queue_item(
            conv.conversation_id, item.queue_item_id,
            expected_revision=item.revision, text="v2")
        self.assertEqual(updated.text, "v2")
        self.assertEqual(updated.revision, item.revision + 1)

    def test_move_up_down(self):
        conv = self.new_conv()
        items = [self.service.enqueue(conv.conversation_id, f"m{i}") for i in range(3)]
        ids = [it.queue_item_id for it in items]
        after = self.service.move_queue_item(
            conv.conversation_id, ids[2], "up")
        self.assertEqual([it.queue_item_id for it in after], [ids[0], ids[2], ids[1]])
        after = self.service.move_queue_item(
            conv.conversation_id, ids[0], "down")
        self.assertEqual([it.queue_item_id for it in after], [ids[2], ids[0], ids[1]])
        # 上移回到原位
        after = self.service.move_queue_item(conv.conversation_id, ids[0], "up")
        self.assertEqual([it.queue_item_id for it in after], [ids[0], ids[2], ids[1]])
        # 队首上移 / 队尾下移为边界无操作
        after = self.service.move_queue_item(conv.conversation_id, ids[0], "up")
        self.assertEqual([it.queue_item_id for it in after], [ids[0], ids[2], ids[1]])
        after = self.service.move_queue_item(conv.conversation_id, ids[1], "down")
        self.assertEqual([it.queue_item_id for it in after], [ids[0], ids[2], ids[1]])

    def test_delete_undo_clear(self):
        conv = self.new_conv()
        item = self.service.enqueue(conv.conversation_id, "del-me")
        deleted = self.service.delete_queue_item(
            conv.conversation_id, item.queue_item_id,
            expected_revision=item.revision)
        self.assertEqual(deleted.status, QueueItemStatus.PENDING_DELETE.value)
        # pending_delete 不参与队列数量/排序
        self.assertEqual(len(self.service.list_queue(conv.conversation_id)), 0)
        restored = self.service.undo_delete(conv.conversation_id, item.queue_item_id)
        self.assertEqual(restored.status, QueueItemStatus.WAITING.value)
        # 撤销窗口过期：删除后人为老化 updated_at
        self.service.delete_queue_item(
            conv.conversation_id, item.queue_item_id, expected_revision=restored.revision)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE queue_items SET updated_at=? WHERE queue_item_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=10))
                 .isoformat(timespec="milliseconds"), item.queue_item_id))
        with self.assertRaises(UndoExpired):
            self.service.undo_delete(conv.conversation_id, item.queue_item_id)
        # 清空等待项
        self.service.enqueue(conv.conversation_id, "x1")
        self.service.enqueue(conv.conversation_id, "x2")
        cleared = self.service.clear_waiting(conv.conversation_id)
        self.assertEqual(cleared, 2)

    def test_pending_delete_auto_archives_after_window(self):
        """超过 5 秒撤销窗口的 pending_delete → deleted 自动归档（设计方案 8.6）。"""
        conv = self.new_conv()
        item = self.service.enqueue(conv.conversation_id, "auto-archive")
        self.service.delete_queue_item(
            conv.conversation_id, item.queue_item_id,
            expected_revision=item.revision)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE queue_items SET updated_at=? WHERE queue_item_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=10))
                 .isoformat(timespec="milliseconds"), item.queue_item_id))
        result = self.service.cleanup()
        self.assertEqual(result["pending_deletes"], 1)
        archived = self.store.get_queue_item(item.queue_item_id)
        self.assertEqual(archived.status, QueueItemStatus.DELETED.value)
        # 归档后再次 cleanup 不重复处理
        again = self.service.cleanup()
        self.assertEqual(again["pending_deletes"], 0)

    def test_dequeue_archives_sent_and_creates_turn(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "go")
        turn, node = self.service.send_next(conv.conversation_id)
        self.assertEqual(node.type, "user")
        self.assertEqual(node.status, "dispatched")
        # 队列为空
        self.assertEqual(len(self.service.list_queue(conv.conversation_id)), 0)


class SteeringTests(ConversationBase):
    def test_steering_flow(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "第一问")
        turn, _ = self.service.send_next(conv.conversation_id)
        self.service.set_turn_status(conv.conversation_id, turn.turn_id, "tool")
        # 用户选中队列项注入
        item = self.service.enqueue(conv.conversation_id, "改为做别的事")
        active, items = self.service.prepare_steering(
            conv.conversation_id, [item.queue_item_id])
        self.assertEqual(items[0].status, QueueItemStatus.WAITING_FOR_STEERING.value)
        # 工具结束后提交注入
        nodes = self.service.commit_steering(
            conv.conversation_id, [item.queue_item_id])
        self.assertEqual(nodes[0].type, "user_steering")
        self.assertEqual(self.store.get_queue_item(item.queue_item_id).status,
                         QueueItemStatus.INJECTED.value)
        # 每 Turn 最多 10 次（首次 Steering 后还剩 9 次）
        for i in range(9):
            it = self.service.enqueue(conv.conversation_id, f"s{i}")
            self.service.prepare_steering(conv.conversation_id, [it.queue_item_id])
            self.service.commit_steering(conv.conversation_id, [it.queue_item_id])
        it = self.service.enqueue(conv.conversation_id, "over-limit")
        with self.assertRaises(SteeringLimit):
            self.service.prepare_steering(conv.conversation_id, [it.queue_item_id])

    def test_steering_abort_restores_queue(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        item = self.service.enqueue(conv.conversation_id, "steer")
        self.service.prepare_steering(conv.conversation_id, [item.queue_item_id])
        self.service.abort_steering(conv.conversation_id, [item.queue_item_id])
        self.assertEqual(self.store.get_queue_item(item.queue_item_id).status,
                         QueueItemStatus.WAITING.value)

    def test_restart_resets_pending_steering(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        item = self.service.enqueue(conv.conversation_id, "steer")
        self.service.prepare_steering(conv.conversation_id, [item.queue_item_id])
        result = self.service.recover_after_restart()
        self.assertEqual(result["reset_queue_items"], 1)
        self.assertEqual(self.store.get_queue_item(item.queue_item_id).status,
                         QueueItemStatus.WAITING.value)

    def test_steering_wait_register_pending_and_timeout(self):
        """Steering 运行时等待（设计方案 9.1/9.3）：prepare 后 pending，
        超时检测返回待恢复项并清除。"""
        import time as _time
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        self.service.send_next(conv.conversation_id)
        item = self.service.enqueue(conv.conversation_id, "steer")
        self.service.prepare_steering(conv.conversation_id, [item.queue_item_id])
        self.service.register_steering_wait(
            conv.conversation_id, [item.queue_item_id])
        self.assertEqual(self.service.pending_steering(conv.conversation_id),
                         [item.queue_item_id])
        # 未超时：剩余时间 > 0
        self.assertGreater(self.service.steering_remaining(conv.conversation_id) or 0, 0)
        # 老化 deadline → 超时
        with self.service._steering_lock:
            self.service._steering_pending[conv.conversation_id]["deadline"] = \
                _time.time() - 1
        restored = self.service.check_steering_timeout(conv.conversation_id)
        self.assertEqual(restored, [item.queue_item_id])
        self.assertIsNone(self.service.pending_steering(conv.conversation_id))


class PrefsTests(ConversationBase):
    def test_update_prefs_persists_and_roundtrips(self):
        """会话偏好（模型/推理/权限）统一持久化（设计方案：管理操作统一化）。"""
        conv = self.new_conv()
        self.service.update_prefs(conv.conversation_id, model="gpt-x",
                                  reasoning_level="medium",
                                  permission_mode="ask")
        prefs = self.service.conversation_prefs(conv.conversation_id)
        self.assertEqual(prefs["model"], "gpt-x")
        self.assertEqual(prefs["reasoning_level"], "medium")
        self.assertEqual(prefs["permission_mode"], "ask")
        # 合并更新不丢旧值
        self.service.update_prefs(conv.conversation_id, model="gpt-y")
        prefs = self.service.conversation_prefs(conv.conversation_id)
        self.assertEqual(prefs["model"], "gpt-y")
        self.assertEqual(prefs["reasoning_level"], "medium")
        # 广播 conversation.upserted（含 prefs）
        upserted = self.events_of("conversation.upserted")
        self.assertTrue(upserted)
        self.assertIn("prefs", upserted[-1]["data"])

    def test_clear_history_and_delete_via_unified(self):
        """会话清空/删除走统一模型（设计方案：会话管理统一化）。"""
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "hello")
        turn, _ = self.service.send_next(conv.conversation_id)
        self.service.upsert_node_delta(
            conv.conversation_id, turn.turn_id, "assistant", "hi",
            continue_existing=True)
        self.service.complete_turn(conv.conversation_id, turn.turn_id,
                                   "done", full_text="hi")
        self.assertGreater(len(self.store.list_turns(conv.conversation_id)), 0)
        self.service.clear_history(conv.conversation_id)
        self.assertEqual(self.store.list_turns(conv.conversation_id), [])


class StopTests(ConversationBase):
    def test_stop_flow(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        stopped = self.service.request_stop(conv.conversation_id)
        self.assertEqual(stopped.status, TurnStatus.STOPPING.value)
        # 运行时确认停止完成
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "stopped")
        self.assertEqual(self.store.get_turn(turn.turn_id).status,
                         TurnStatus.STOPPED.value)
        self.assertEqual(self.store.get_turn(turn.turn_id).final_assistant_node_id, None)

class ApprovalTests(ConversationBase):
    def test_approval_lifecycle(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        approval = self.service.request_approval(
            conv.conversation_id, turn.turn_id, tool_name="bash",
            params_summary="rm -rf /tmp/x")
        self.assertEqual(approval.status, ApprovalStatus.PENDING.value)
        self.assertEqual(self.store.get_turn(turn.turn_id).status,
                         TurnStatus.APPROVAL.value)
        approved = self.service.resolve_approval(
            conv.conversation_id, approval.approval_id, "approved")
        self.assertEqual(approved.status, ApprovalStatus.APPROVED.value)
        with self.assertRaises(ApprovalConflict):
            self.service.resolve_approval(
                conv.conversation_id, approval.approval_id, "denied")

    def test_approval_timeout_expiry(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        self.service.request_approval(conv.conversation_id, turn.turn_id,
                                      tool_name="bash", params_summary="x")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE approvals SET created_at=? WHERE conversation_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=301))
                 .isoformat(timespec="milliseconds"), conv.conversation_id))
        result = self.service.cleanup()
        self.assertEqual(result["approvals"], 1)
        pending = self.store.list_pending_approvals(conv.conversation_id)
        self.assertEqual(pending, [])

    def test_approval_resolve_and_turn_advance(self):
        """桥接路径：resolve（无租约校验）；审批后事件推进 Turn（§7.7）。"""
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        approval = self.service.request_approval(
            conv.conversation_id, turn.turn_id, tool_name="bash",
            params_summary="rm x")
        self.assertEqual(self.store.get_turn(turn.turn_id).status,
                         TurnStatus.APPROVAL.value)
        # 旧审批桥路径：resolve 无租约校验（控制租约已废弃）
        resolved = self.service.resolve_approval(
            conv.conversation_id, approval.approval_id, "approved")
        self.assertEqual(resolved.status, ApprovalStatus.APPROVED.value)
        # 审批后事件推进：tool_call_end → Turn=tool
        self.service.set_turn_status(conv.conversation_id, turn.turn_id, "tool")
        self.assertEqual(self.store.get_turn(turn.turn_id).status,
                         TurnStatus.TOOL.value)
        # 拒绝路径
        approval2 = self.service.request_approval(
            conv.conversation_id, turn.turn_id, tool_name="bash",
            params_summary="rm y")
        denied = self.service.resolve_approval(
            conv.conversation_id, approval2.approval_id, "denied")
        self.assertEqual(denied.status, ApprovalStatus.DENIED.value)


class IdempotencyTests(ConversationBase):
    def test_same_operation_id_replays(self):
        conv = self.new_conv()
        item = self.service.enqueue(conv.conversation_id, "op1",
                                    operation_id="op-1")
        item2 = self.service.enqueue(conv.conversation_id, "op1",
                                     operation_id="op-1")
        self.assertEqual(item.queue_item_id, item2.queue_item_id)

    def test_same_id_different_request_conflicts(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "a", operation_id="op-1")
        with self.assertRaises(IdempotencyConflict):
            self.service.enqueue(conv.conversation_id, "b", operation_id="op-1")

    def test_stop_idempotent(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        self.service.send_next(conv.conversation_id)
        t1 = self.service.request_stop(conv.conversation_id,
                                       operation_id="stop-1")
        self.assertEqual(t1.status, TurnStatus.STOPPING.value)
        # 同 ID 同请求 → 幂等重放，返回同一 Turn，不重复写
        before = len(self.events_of("turn.status"))
        t2 = self.service.request_stop(conv.conversation_id,
                                       operation_id="stop-1")
        self.assertEqual(t1.turn_id, t2.turn_id)
        self.assertEqual(len(self.events_of("turn.status")), before)


class ExecutionScopeTests(ConversationBase):
    def test_scope_limit_5(self):
        # 同一执行域（gateway:default）下最多 5 个非终态 Turn，跨会话共享名额
        convs = [self.new_conv(f"webui:c{i}") for i in range(5)]
        for conv in convs:
            self.service.enqueue(conv.conversation_id, "q")
            self.service.send_next(conv.conversation_id)
        self.assertEqual(self.service.scope_usage("gateway:default"), 5)
        extra = self.new_conv("webui:c6")
        self.service.enqueue(extra.conversation_id, "q6")
        with self.assertRaises(ExecutionScopeLimit):
            self.service.send_next(extra.conversation_id)
        # 终态释放名额
        for conv in convs:
            for turn in self.store.list_turns(conv.conversation_id):
                self.service.complete_turn(conv.conversation_id, turn.turn_id, "done",
                                           full_text="ok")
        self.assertEqual(self.service.scope_usage("gateway:default"), 0)
        # 名额释放后可继续
        self.service.send_next(extra.conversation_id)

    def test_system_scope_isolated(self):
        user_conv = self.new_conv()
        sys_conv = self.new_conv("plan:p1", origin="system", subtype="plan")
        self.assertEqual(user_conv.execution_scope, "gateway:default")
        self.assertEqual(sys_conv.execution_scope, "system:plan")
        self.assertNotEqual(user_conv.execution_scope, sys_conv.execution_scope)


class OutboxTests(ConversationBase):
    def test_outbox_written_in_same_tx(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox_events WHERE conversation_id=?",
                (conv.conversation_id,),
            ).fetchall()
        self.assertTrue(rows)
        types = {r["event_type"] for r in rows}
        self.assertIn("queue.updated", types)
        # B2：published 标记改为批量刷盘——先触发 flush 再断言
        self.service.flush_outbox_published()
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox_events WHERE conversation_id=?",
                (conv.conversation_id,),
            ).fetchall()
        # 实时发布成功后已标记 published（OutboxPublisher 不会重复补发）
        self.assertTrue(all(r["published_at"] for r in rows))
        for r in rows:
            payload = json.loads(r["payload"])
            self.assertIn("conversation_id", payload)
            self.assertIn("scope", payload)
            self.assertIn("version", payload)

    def _simulate_crash_residue(self, age_seconds: int = 60) -> None:
        """把未发布事件回拨为"足够老"的崩溃残留并清空发布标记。

        补发认领带 created_at 时间下限（_CLAIM_MIN_AGE_SECONDS）：刚落库、
        实时路径尚未 publish 的事件不会被抢认领。真实补发场景中事件早已
        存在超过下限；测试里时间被压缩为 0，需要显式回拨 created_at。
        """
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE outbox_events SET published_at=NULL, "
                "claimed_by=NULL, claim_expires_at=NULL, created_at=?",
                ((datetime.now(timezone.utc)
                  - timedelta(seconds=age_seconds))
                 .isoformat(timespec="milliseconds"),),
            )

    def test_outbox_replays_unpublished_after_real_time_publish_fails(self):
        """发布失败（未标记 published）的残留事件可由 OutboxPublisher 补发。"""
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        # 模拟实时发布失败：把已发布事件恢复为未发布的旧残留（补发路径）
        self._simulate_crash_residue()
        loop = asyncio.new_event_loop()
        try:
            publisher = OutboxPublisher(self.store, self._publish_capture,
                                        interval_seconds=0.05)
            published = loop.run_until_complete(publisher.flush_once())
            self.assertGreaterEqual(published, 1)
            self.assertEqual(self.store.list_unpublished_outbox(), [])
        finally:
            loop.close()

    def test_publisher_flushes_outbox(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        # 模拟实时广播失败：清空已发布标记（保留未发布事件）
        self._simulate_crash_residue()
        loop = asyncio.new_event_loop()
        try:
            publisher = OutboxPublisher(self.store, self._publish_capture,
                                        interval_seconds=0.05)
            published = loop.run_until_complete(publisher.flush_once())
            self.assertGreaterEqual(published, 1)
            self.assertEqual(self.store.list_unpublished_outbox(), [])
        finally:
            loop.close()

    def test_claim_skips_fresh_unpublished_events(self):
        """认领时间下限：刚落库的事件不抢认领（避免与实时路径重复广播）。

        事件在 created_at 下限窗口内时补发器应跳过；超过下限后才可认领。
        """
        from gateway.conversation.outbox import _CLAIM_MIN_AGE_SECONDS
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        # 模拟实时发布失败但事件很"新"（刚落库，仍在下限窗口内）
        with self.store.transaction() as conn:
            conn.execute("UPDATE outbox_events SET published_at=NULL")
        loop = asyncio.new_event_loop()
        try:
            publisher = OutboxPublisher(self.store, self._publish_capture,
                                        interval_seconds=0.05)
            published = loop.run_until_complete(publisher.flush_once())
            self.assertEqual(published, 0)   # 新事件不被补发
            claimed = loop.run_until_complete(publisher.flush_once())
            self.assertEqual(claimed, 0)
            self.assertGreater(len(self.store.list_unpublished_outbox()), 0)
            # 回拨超过下限 → 正常认领并补发
            self._simulate_crash_residue(
                age_seconds=int(_CLAIM_MIN_AGE_SECONDS) + 5)
            published = loop.run_until_complete(publisher.flush_once())
            self.assertGreaterEqual(published, 1)
            self.assertEqual(self.store.list_unpublished_outbox(), [])
        finally:
            loop.close()

    def _publish_capture(self, event_type, payload):
        self.events.append((event_type, payload))


class SnapshotHistoryTests(ConversationBase):
    def test_snapshot_consistency(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q1")
        self.service.enqueue(conv.conversation_id, "q2")
        turn, node = self.service.send_next(conv.conversation_id)
        self.service.set_turn_status(conv.conversation_id, turn.turn_id, "thinking")
        self.service.upsert_node_delta(conv.conversation_id, turn.turn_id,
                                       "reasoning", "想", continue_existing=True)
        snap = self.service.snapshot(conv.conversation_id)
        self.assertEqual(snap["conversation"]["session_key"], "webui:default")
        self.assertEqual(snap["live_turn"]["turn_id"], turn.turn_id)
        self.assertEqual(len(snap["nodes"]), 2)  # user + reasoning
        self.assertGreater(snap["turn_version"], 0)
        self.assertIn("server_time", snap)
        # 队列剩 1 条
        self.assertEqual(len(snap["queue"]), 1)

    def test_history_paging_by_turn(self):
        conv = self.new_conv()
        for i in range(3):
            self.service.enqueue(conv.conversation_id, f"q{i}")
            turn, _ = self.service.send_next(conv.conversation_id)
            self.service.complete_turn(conv.conversation_id, turn.turn_id, "done",
                                       full_text=f"a{i}")
        page1 = self.service.history(conv.conversation_id, limit=2)
        self.assertEqual(len(page1["items"]), 2)
        self.assertIsNotNone(page1["next_cursor"])
        page2 = self.service.history(conv.conversation_id,
                                     before=page1["next_cursor"], limit=2)
        self.assertEqual(len(page2["items"]), 1)
        self.assertIsNone(page2["next_cursor"])

    def test_history_chronological_order(self):
        """历史必须按时间正序返回（旧→新），最新 Turn 在末尾，避免
        最新消息显示在时间线最上方。"""
        conv = self.new_conv()
        for i in range(3):
            self.service.enqueue(conv.conversation_id, f"q{i}")
            turn, _ = self.service.send_next(conv.conversation_id)
            self.service.complete_turn(conv.conversation_id, turn.turn_id, "done",
                                       full_text=f"a{i}")
        page = self.service.history(conv.conversation_id, limit=10)
        assistant_texts = []
        user_texts = []
        for item in page["items"]:
            for node in item["nodes"]:
                if node["type"] == "assistant":
                    assistant_texts.append(node["text"])
                if node["type"] == "user":
                    user_texts.append(node["text"])
        self.assertEqual(user_texts, ["q0", "q1", "q2"])       # 旧→新
        self.assertEqual(assistant_texts, ["a0", "a1", "a2"])  # 旧→新


class ChannelDedupTests(ConversationBase):
    def test_receipt_dedup_window(self):
        conv = self.new_conv()
        dup = self.service.check_and_record_receipt(
            conv.conversation_id, "feishu", "msg-1")
        self.assertFalse(dup)
        dup = self.service.check_and_record_receipt(
            conv.conversation_id, "feishu", "msg-1")
        self.assertTrue(dup)
        # 不同 channel 不冲突
        dup = self.service.check_and_record_receipt(
            conv.conversation_id, "weixin", "msg-1")
        self.assertFalse(dup)
        # 过期清理
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE channel_message_receipts SET created_at=?",
                ((datetime.now(timezone.utc) - timedelta(hours=73))
                 .isoformat(timespec="milliseconds"),))
        result = self.service.cleanup()
        self.assertEqual(result["receipts"], 2)
        dup = self.service.check_and_record_receipt(
            conv.conversation_id, "feishu", "msg-1")
        self.assertFalse(dup)

    def test_channel_queued_node_upgrade(self):
        conv = self.new_conv()
        item = self.service.enqueue(
            conv.conversation_id, "群消息", channel="feishu",
            message_id="m1", sender_id="u1", sender_name="张三",
            create_queued_node=True)
        # 出队前快照里有 queued user 节点（turn_id 为空）
        snap = self.service.snapshot(conv.conversation_id)
        self.assertIsNone(snap["live_turn"])
        self.assertEqual(len(snap["queued_nodes"]), 1)
        turn, node = self.service.send_next(
            conv.conversation_id, channel_node_id=snap["queued_nodes"][0]["node_id"])
        self.assertEqual(node.turn_id, turn.turn_id)
        self.assertEqual(node.position, 1)
        self.assertEqual(node.source_channel, "feishu")


class ToolResultTests(ConversationBase):
    def test_result_ownership(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "q")
        turn, _ = self.service.send_next(conv.conversation_id)
        self.service.save_tool_result(
            conv.conversation_id, turn.turn_id, result_ref="res-1", kind="text",
            size_bytes=123, lines=3, content_type="text/plain",
            summary={"head": "..."})
        result = self.service.get_result(conv.conversation_id, turn.turn_id, "res-1")
        self.assertEqual(result["result_ref"], "res-1")
        other = self.new_conv("webui:other")
        with self.assertRaises(ResultNotOwned):
            self.service.get_result(other.conversation_id, turn.turn_id, "res-1")
        with self.assertRaises(ResultNotOwned):
            self.service.get_result(conv.conversation_id, "wrong-turn", "res-1")


class CleanupTests(ConversationBase):
    def test_idempotency_cleanup(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "a", operation_id="op-old")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE idempotency_records SET created_at=?",
                ((datetime.now(timezone.utc) - timedelta(hours=25))
                 .isoformat(timespec="milliseconds"),))
        result = self.service.cleanup()
        self.assertEqual(result["idempotency"], 1)

    def test_outbox_cleanup(self):
        conv = self.new_conv()
        self.service.enqueue(conv.conversation_id, "a")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE outbox_events SET published_at=created_at, created_at=?",
                ((datetime.now(timezone.utc) - timedelta(hours=25))
                 .isoformat(timespec="milliseconds"),))
        result = self.service.cleanup()
        # conversation.upserted + queue.updated 两条均已发布且过期
        self.assertEqual(result["outbox"], 2)


# ================================================================
# 工具节点终态兜底收口（2026-08：修复"计划已完成仍卡执行中"）
# ================================================================

class ToolNodeTerminalSweepTests(ConversationBase):
    def _turn_with_running_tool(self, sid: str):
        conv = self.new_conv(session_key=f"webui:{sid}")
        turn = self.service.start_turn(conv.conversation_id)
        node = self.service.upsert_tool_node(
            conv.conversation_id, turn.turn_id, "call-stuck",
            status="running", tool_name="read")
        return conv, turn, node

    def test_error_turn_sweeps_running_tool_node(self):
        conv, turn, node = self._turn_with_running_tool("s1")
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "error",
                                   error_code="stop_timeout")
        fresh = self.service.store.get_node(node.node_id)
        self.assertEqual(fresh.status, "error")
        meta = fresh.metadata or {}
        self.assertEqual(meta.get("error_code"), "tool_no_return")
        self.assertIn("未返回", meta.get("result_summary") or "")
        # 前端"失败"徽章依据 meta.error !== undefined
        self.assertIsNotNone(meta.get("error"))
        # 广播了收口事件，前端立即翻转（事件载荷为 GatewayEvent，业务数据
        # 在 data 字段）
        swept_events = [e for e in self.events_of("node.tool")
                        if (e.get("data") or {}).get("node_id") == node.node_id
                        and e["data"].get("status") == "error"]
        self.assertTrue(swept_events)
        self.assertEqual(swept_events[0]["data"]["error_code"], "tool_no_return")

    def test_done_turn_also_sweeps(self):
        """done 终态同样兜底：轮次正常结束但某工具事件缺失时也应收口。"""
        conv, turn, node = self._turn_with_running_tool("s2")
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "done",
                                   full_text="完成")
        fresh = self.service.store.get_node(node.node_id)
        self.assertEqual(fresh.status, "error")
        self.assertEqual(fresh.metadata.get("error_code"), "tool_no_return")

    def test_completed_tool_node_untouched(self):
        conv, turn, _node = self._turn_with_running_tool("s3")
        self.service.upsert_tool_node(
            conv.conversation_id, turn.turn_id, "call-stuck",
            status="done", params_summary='{"path": "a.txt"}',
            result_summary="ok")
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "done",
                                   full_text="ok")
        done_node = self.service.store.get_node(
            gen_node_id_from_call("call-stuck", conv.conversation_id))
        self.assertEqual(done_node.status, "done")
        self.assertNotIn("turn_terminal_sweep", done_node.metadata or {})

    def test_idempotent_complete_does_not_double_emit(self):
        conv, turn, node = self._turn_with_running_tool("s4")
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "error")
        before = len(self.events_of("node.tool"))
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "error")
        self.assertEqual(len(self.events_of("node.tool")), before)
        fresh = self.service.store.get_node(node.node_id)
        self.assertEqual(fresh.status, "error")

    def test_runtime_projection_nodes_not_swept(self):
        """Plan/Goal/Subagent 的运行时活动投影（metadata.runtime_type）生命周期
        跟随后台任务：complete_turn 后仍保持 running，不被误标为失败。"""
        conv = self.new_conv(session_key="webui:s6")
        turn = self.service.start_turn(conv.conversation_id)
        proj = self.service.upsert_tool_node(
            conv.conversation_id, turn.turn_id, "call-proj",
            status="running", tool_name="bash",
            extra_metadata={"runtime_type": "plan", "runtime_id": "plan_1"})
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "done",
                                   full_text="")
        fresh = self.service.store.get_node(proj.node_id)
        self.assertEqual(fresh.status, "running")
        self.assertNotIn("turn_terminal_sweep", fresh.metadata or {})

    def test_sweep_limited_to_own_conversation(self):
        """隔离性：收口只作用于本会话本 Turn；另一会话的 running 节点不受
        牵连（同会话单活动 Turn 语义下，"另一 Turn"不存在）。"""
        conv, turn, node = self._turn_with_running_tool("s5")
        other_conv = self.new_conv(session_key="webui:s5-other")
        other_turn = self.service.start_turn(other_conv.conversation_id)
        other_node = self.service.upsert_tool_node(
            other_conv.conversation_id, other_turn.turn_id, "call-other",
            status="running", tool_name="bash")
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "stopped")
        stuck = self.service.store.get_node(other_node.node_id)
        self.assertEqual(stuck.status, "running")
        swept = self.service.store.get_node(node.node_id)
        self.assertEqual(swept.status, "error")


if __name__ == "__main__":
    unittest.main()
