# -*- coding: utf-8 -*-
"""
统一会话桥单元测试 —— Dispatcher/Agent 事件旁路持久化。

覆盖：入站入队、渠道持久化去重、运行时事件 → TurnNode、chat.done 权威终态、
停止 → stopped、Plan/Goal 系统 Conversation + 父会话投影。
"""

import tempfile
import unittest
from pathlib import Path

from core.runtime import RuntimeStore
from gateway.webui.workspace_store import WorkspaceDatabase
from gateway.conversation import ConversationService, ConversationStore, gen_node_id_from_call
from gateway.conversation.bridge import ConversationBridge
from gateway.channels.base import InboundMessage


class BridgeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = RuntimeStore(Path(self.tmp.name) / "runtime.db")
        self.db = WorkspaceDatabase(runtime_store=self.runtime)
        self.store = ConversationStore(self.db)
        self.events = []
        self.service = ConversationService(
            self.store, lambda t, p: self.events.append((t, p)),
            max_global_turns=100)
        self.bridge = ConversationBridge(self.service)

    def tearDown(self):
        self.tmp.cleanup()

    def msg(self, channel="webui", session_key="webui:default", text="hi",
            message_id="m1", metadata=None):
        return InboundMessage(
            channel=channel, session_key=session_key, user_id="u1",
            user_name="用户", text=text, message_id=message_id,
            metadata=dict(metadata or {}))

    def node_types(self, conversation_id):
        snap = self.service.snapshot(conversation_id)
        if snap["live_turn"]:
            return [(n["type"], n["status"]) for n in snap["nodes"]]
        # 已终态：从历史取
        hist = self.service.history(conversation_id)
        if hist["items"]:
            return [(n["type"], n["status"])
                    for n in hist["items"][0]["nodes"]]
        return []


class BridgeInboundTests(BridgeBase):
    def test_webui_inbound_enqueues_and_creates_conversation(self):
        cid = self.bridge.on_inbound(self.msg())
        self.assertIsNotNone(cid)
        conv = self.store.get_conversation(cid)
        self.assertEqual(conv.subtype, "main")
        self.assertEqual(conv.origin, "webui")
        queue = self.service.list_queue(cid)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].text, "hi")

    def test_channel_inbound_creates_queued_node(self):
        cid = self.bridge.on_inbound(
            self.msg(channel="feishu", session_key="feishu:chat1",
                     message_id="m1"))
        snap = self.service.snapshot(cid)
        self.assertEqual(len(snap["queued_nodes"]), 1)
        self.assertEqual(snap["queued_nodes"][0]["source_channel"], "feishu")
        self.assertEqual(snap["queued_nodes"][0]["sender_name"], "用户")

    def test_channel_dedup_persistent(self):
        cid1 = self.bridge.on_inbound(
            self.msg(channel="feishu", session_key="feishu:chat1",
                     message_id="dup-1"))
        cid2 = self.bridge.on_inbound(
            self.msg(channel="feishu", session_key="feishu:chat1",
                     message_id="dup-1"))
        # P2 修复：去重前置——第二次重复消息直接返回 None（不再重复入队）
        self.assertIsNone(cid2)
        # 第二次为 None（重复跳过，不重复入队）
        self.assertIsNone(self.bridge.on_inbound(
            self.msg(channel="feishu", session_key="feishu:chat1",
                     message_id="dup-1")))
        # 不同消息 ID 正常入队
        self.assertIsNotNone(self.bridge.on_inbound(
            self.msg(channel="feishu", session_key="feishu:chat1",
                     message_id="new-2")))

    def test_workspace_subtype_and_scope(self):
        cid = self.bridge.on_inbound(self.msg(
            session_key="workspace:w1:s1", message_id="w1"))
        conv = self.store.get_conversation(cid)
        self.assertEqual(conv.subtype, "workspace")
        self.assertEqual(conv.workspace_id, "w1")
        self.assertEqual(conv.execution_scope, "workspace:w1")


class BridgeEventTests(BridgeBase):
    def test_agent_events_become_nodes(self):
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "reasoning_delta", "data": {"text": "思考中"}})
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "tool_call_start",
            "data": {"tool_call_id": "call1", "tool": "bash"}})
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "tool_call_end",
            "data": {"tool_call_id": "call1", "tool": "bash",
                     "arguments": {"command": "pwd"}}})
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "message_start",
            "data": {"role": "assistant", "message_id": "asst1"}})
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "text_delta", "data": {"text": "回答"}})
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "message_end", "data": {"role": "assistant"}})
        self.bridge.on_reply(self.msg(message_id="m1"), "回答")
        types = [t for t, _ in self.node_types(cid)]
        self.assertEqual(types, ["user", "reasoning", "tool", "assistant"])

    def test_reasoning_delta_merges_into_one_node(self):
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "reasoning_delta", "data": {"text": "第一段"}})
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "reasoning_delta", "data": {"text": "第二段"}})
        # Turn 结束（或 100ms 节流到期）时合并落库
        self.bridge._merger.flush()
        snap = self.service.snapshot(cid)
        reasoning = [n for n in snap["nodes"] if n["type"] == "reasoning"]
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(reasoning[0]["text"], "第一段第二段")

    def test_reasoning_splits_per_round_on_message_start(self):
        """多轮工具调用间的思考各自成卡：message_start（新轮开始）收敛上一
        reasoning 节点，下一轮 reasoning_delta 新建节点而非追加进首张卡。"""
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        msg = self.msg(message_id="m1")
        # 第 1 轮：思考 → 工具
        self.bridge.on_agent_event(msg, {"type": "reasoning_delta",
                                         "data": {"text": "第一轮思考"}})
        self.bridge.on_agent_event(msg, {"type": "tool_call_start",
                                         "data": {"tool_call_id": "c1", "tool": "bash"}})
        self.bridge.on_agent_event(msg, {"type": "tool_call_end",
                                         "data": {"tool_call_id": "c1", "tool": "bash",
                                                  "arguments": {}}})
        # 第 2 轮：message_start（新段）→ 思考 → 文本
        self.bridge.on_agent_event(msg, {"type": "message_start",
                                         "data": {"role": "assistant"}})
        self.bridge.on_agent_event(msg, {"type": "reasoning_delta",
                                         "data": {"text": "第二轮思考"}})
        self.bridge.on_agent_event(msg, {"type": "text_delta",
                                         "data": {"text": "回答"}})
        self.bridge.on_reply(msg, "回答")
        # Turn 终态后 snapshot 不再返回该 turn 节点，读 history
        hist = self.service.history(cid)
        reasoning = [n for item in hist["items"] for n in item["nodes"]
                     if n["type"] == "reasoning"]
        self.assertEqual(len(reasoning), 2, "两轮思考应是两个独立节点")
        self.assertEqual(reasoning[0]["text"], "第一轮思考")
        self.assertEqual(reasoning[1]["text"], "第二轮思考")
        # 顺序：第一轮思考在前（position 严格递增）
        self.assertLess(reasoning[0]["position"], reasoning[1]["position"])

    def test_runtime_reasoning_splits_per_tool_call(self):
        """plan/goal runtime 轮同规则：工具调用后的思考新建节点（写入父会话）。"""
        meta = {"task_source": "goal", "goal_id": "goal_x", "goal_round": 1}
        cid = self.bridge.on_inbound(self.msg(message_id="m1", metadata=meta))
        msg = self.msg(message_id="m1", metadata=meta)
        # 第 1 步：思考 → 工具 → 第 2 步思考
        self.bridge.on_agent_event(msg, {"type": "reasoning_delta",
                                         "data": {"text": "步骤一思考"}})
        self.bridge.on_agent_event(msg, {"type": "tool_call_start",
                                         "data": {"tool_call_id": "g1", "tool": "bash"}})
        self.bridge.on_agent_event(msg, {"type": "tool_call_end",
                                         "data": {"tool_call_id": "g1", "tool": "bash",
                                                  "arguments": {}}})
        self.bridge.on_agent_event(msg, {"type": "reasoning_delta",
                                         "data": {"text": "步骤二思考"}})
        self.bridge._merger.flush()
        snap = self.service.snapshot(cid)
        reasoning = [n for n in snap["nodes"] if n["type"] == "reasoning"]
        self.assertEqual(len(reasoning), 2, "runtime 轮两步思考应是两个独立节点")
        self.assertEqual(reasoning[0]["text"], "步骤一思考")
        self.assertEqual(reasoning[1]["text"], "步骤二思考")

    def test_node_delta_event_carries_delta_and_seq(self):
        """契约①：node.delta 事件携带增量 delta+seq（终态由 chat.done 权威全量）。"""
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "reasoning_delta", "data": {"text": "思考内容"}})
        self.bridge._merger.flush()
        events = [p for t, p in self.events if t == "node.delta"]
        self.assertTrue(events)
        data = events[-1]["data"]
        self.assertEqual(data["delta"], "思考内容")
        self.assertGreaterEqual(data["seq"], 1)
        self.assertIn("turn_id", events[-1])
        self.assertGreater(events[-1]["version"], 0)

    def test_merger_interval_batching(self):
        """设计方案 16.4：100ms 内的多个 delta 合并为一次提交。"""
        from gateway.conversation.bridge import _DeltaMerger
        import time
        calls = []

        def flush(conversation_id, turn_id, node_type, text):
            calls.append((turn_id, node_type, text))

        merger = _DeltaMerger(flush, interval_seconds=10.0)
        merger.accumulate(("c1", "t1", "assistant"), "a")
        merger.accumulate(("c1", "t1", "assistant"), "b")
        merger.accumulate(("c1", "t1", "reasoning"), "r")
        self.assertEqual(calls, [])  # 未到 10s，不提交
        merger.flush()  # 强制提交
        self.assertEqual(len(calls), 2)
        self.assertIn(("t1", "assistant", "ab"), calls)
        self.assertIn(("t1", "reasoning", "r"), calls)

    def test_chat_done_full_text_overrides_streamed(self):
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "message_start",
            "data": {"role": "assistant", "message_id": "asst1"}})
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "text_delta", "data": {"text": "流式草稿"}})
        self.bridge.on_reply(self.msg(message_id="m1"), "权威最终回复")
        hist = self.service.history(cid)
        assistant = [n for n in hist["items"][0]["nodes"]
                     if n["type"] == "assistant"][0]
        self.assertEqual(assistant["text"], "权威最终回复")
        turn = hist["items"][0]["turn"]
        self.assertEqual(turn["status"], "done")
        self.assertEqual(turn["final_assistant_node_id"], assistant["node_id"])

    def test_tool_result_tiered_storage(self):
        """设计方案 17：大工具结果（>64KB）独立 tool_results 表 + result_ref。"""
        cid = self.bridge.on_inbound(self.msg(message_id="m-big"))
        self.bridge.on_agent_event(self.msg(message_id="m-big"), {
            "type": "tool_call_start",
            "data": {"tool_call_id": "bigcall1", "tool": "bash"}})
        self.bridge.on_agent_event(self.msg(message_id="m-big"), {
            "type": "tool_call_end",
            "data": {"tool_call_id": "bigcall1", "tool": "bash",
                     "arguments": {"cmd": "cat big.log"}}})
        big_content = "行内容\n" * 8000  # ≈ 24KB 文本 × 行尾，确保 >64KB
        self.bridge.on_agent_event(self.msg(message_id="m-big"), {
            "type": "message_start",
            "data": {"role": "tool", "message_id": "result_bigcall1",
                     "tool_call_id": "bigcall1", "tool": "bash"}})
        self.bridge.on_agent_event(self.msg(message_id="m-big"), {
            "type": "message_end",
            "data": {"role": "tool", "message_id": "result_bigcall1",
                     "tool_call_id": "bigcall1", "tool": "bash",
                     "content": big_content}})
        turn_id = self.store.get_active_turn(cid).turn_id
        node = self.store.get_node(
            gen_node_id_from_call("bigcall1", cid))
        self.assertIsNotNone(node.metadata.get("result_ref"))
        self.assertGreaterEqual(node.metadata.get("result_size_bytes", 0),
                                64 * 1024)
        result = self.store.get_tool_result(node.metadata["result_ref"])
        self.assertEqual(result.conversation_id, cid)
        self.assertEqual(result.turn_id, turn_id)
        self.assertIn("head", result.summary)
        self.assertIn("size_bytes", result.summary)
        # 小结果（≤64KB）内嵌，无 result_ref
        cid2 = self.bridge.on_inbound(self.msg(message_id="m-small",
                                               session_key="webui:small"))
        self.bridge.on_agent_event(self.msg(message_id="m-small",
                                            session_key="webui:small"), {
            "type": "tool_call_start",
            "data": {"tool_call_id": "small1", "tool": "bash"}})
        self.bridge.on_agent_event(self.msg(message_id="m-small",
                                            session_key="webui:small"), {
            "type": "message_end",
            "data": {"role": "tool", "message_id": "result_small1",
                     "tool_call_id": "small1", "tool": "bash",
                     "content": "ok"}})
        node2 = self.store.get_node(gen_node_id_from_call("small1", cid2))
        self.assertEqual(node2.metadata.get("result_summary"), "ok")
        self.assertIsNone(node2.metadata.get("result_ref"))

    def test_stop_marks_turn_stopped(self):
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "reasoning_delta", "data": {"text": "想"}})
        self.bridge.request_stop("webui:default")
        self.bridge.on_reply(self.msg(message_id="m1"), "已停止")
        hist = self.service.history(cid)
        self.assertEqual(hist["items"][0]["turn"]["status"], "stopped")

    def test_plan_goal_writes_runtime_nodes_to_parent(self):
        """对齐 dsh：plan/goal 轮次的 reasoning/tool/assistant 节点写入父会话，
        assistant 节点带 runtime 标记（內联卡片载体），而非另建 system 会话 + 投影。"""
        cid = self.bridge.on_inbound(self.msg(message_id="parent1"))
        meta = {"task_source": "plan", "plan_id": "plan-rt"}
        self.bridge.on_agent_event(self.msg(message_id="parent1", metadata=meta), {
            "type": "reasoning_delta", "data": {"text": "分析"}})
        self.bridge.on_agent_event(self.msg(message_id="parent1", metadata=meta), {
            "type": "tool_call_start",
            "data": {"tool_call_id": "rc1", "tool": "read"}})
        self.bridge.on_agent_event(self.msg(message_id="parent1", metadata=meta), {
            "type": "tool_call_end",
            "data": {"tool_call_id": "rc1", "tool": "read",
                     "arguments": {"path": "a.txt"}}})
        self.bridge.on_reply(self.msg(message_id="parent1", metadata=meta),
                             "规划结果")
        # 不再新建 system:{type}:{id} 会话
        self.assertIsNone(self.store.get_conversation_by_key("system:plan:plan-rt"))
        # 父会话出现带 runtime 标记的 assistant 节点
        hist = self.service.history(cid)
        runtime_nodes = []
        for item in hist["items"]:
            for n in item["nodes"]:
                meta = n["metadata"] or {}
                if meta.get("runtime_type") == "plan":
                    runtime_nodes.append(n)
        self.assertTrue(runtime_nodes)
        final = [n for n in runtime_nodes if n["type"] == "assistant"]
        self.assertEqual(final[-1]["text"], "规划结果")
        self.assertEqual(final[-1]["metadata"]["runtime_type"], "plan")
        self.assertEqual(final[-1]["metadata"]["runtime_id"], "plan-rt")
        self.assertEqual(final[-1]["metadata"]["runtime_status"], "done")
        # 工具节点也带 runtime 标记（供卡片折叠明细识别）
        tools = [n for n in runtime_nodes if n["type"] == "tool"]
        self.assertEqual(tools[0]["metadata"]["runtime_type"], "plan")

    def test_goal_round_writes_runtime_node_to_parent(self):
        """goal 每轮的最终回复也写在父会话 assistant 节点（runtime_type=goal）。"""
        cid = self.bridge.on_inbound(self.msg(message_id="gparent"))
        meta = {"task_source": "goal", "goal_id": "goal-1", "goal_round": 2}
        self.bridge.on_agent_event(self.msg(message_id="gparent", metadata=meta), {
            "type": "tool_call_start",
            "data": {"tool_call_id": "gc1", "tool": "bash"}})
        self.bridge.on_reply(self.msg(message_id="gparent", metadata=meta),
                             "第 2 轮结果")
        hist = self.service.history(cid)
        runtime_nodes = []
        for item in hist["items"]:
            for n in item["nodes"]:
                if (n["metadata"] or {}).get("runtime_type") == "goal":
                    runtime_nodes.append(n)
        self.assertTrue(runtime_nodes)
        final = [n for n in runtime_nodes if n["type"] == "assistant"]
        self.assertEqual(final[-1]["text"], "第 2 轮结果")
        self.assertEqual(final[-1]["metadata"]["runtime_id"], "goal-1")
        self.assertEqual(final[-1]["metadata"]["runtime_status"], "done")

    def test_final_response_marks_runtime_final_on_parent_node(self):
        """plan 最终 step / goal 每轮（meta.final_response）→ 节点带
        runtime_final 标记；中间 step 回复不带。前端据此把终答平铺为
        正式渲染消息（持久可见），中间回复保持折叠卡片。"""
        cid = self.bridge.on_inbound(self.msg(message_id="fparent"))
        # 中间 step（无 final_response）
        step_meta = {"task_source": "plan", "plan_id": "plan-f",
                     "plan_task_id": "step_1"}
        self.bridge.on_reply(self.msg(message_id="fparent", metadata=step_meta),
                             "step_1 完成")
        # 最终 step（final_response=True，plan_runtime 对最后一步注入）
        final_meta = {"task_source": "plan", "plan_id": "plan-f",
                      "plan_task_id": "step_2", "final_response": True}
        self.bridge.on_reply(self.msg(message_id="fparent", metadata=final_meta),
                             "全部步骤完成")
        hist = self.service.history(cid)
        replies = [n for item in hist["items"] for n in item["nodes"]
                   if n["type"] == "assistant"
                   and (n["metadata"] or {}).get("runtime_type") == "plan"]
        self.assertEqual(len(replies), 2)
        self.assertNotIn("runtime_final", replies[0]["metadata"])
        self.assertTrue(replies[1]["metadata"].get("runtime_final"))

    def test_goal_round_always_marks_runtime_final(self):
        """goal 每轮都是"当前最后一步"（dispatcher 注入 final_response=True）
        → 每轮终答节点均带 runtime_final 标记。"""
        cid = self.bridge.on_inbound(self.msg(message_id="gfinal"))
        meta = {"task_source": "goal", "goal_id": "goal-9",
                "goal_round": 1, "final_response": True}
        self.bridge.on_reply(self.msg(message_id="gfinal", metadata=meta),
                             "第 1 轮结果")
        hist = self.service.history(cid)
        replies = [n for item in hist["items"] for n in item["nodes"]
                   if n["type"] == "assistant"
                   and (n["metadata"] or {}).get("runtime_type") == "goal"]
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0]["metadata"].get("runtime_final"))

    def test_reasoning_node_precedes_assistant_node(self):
        """修复：message_start 先于 reasoning_delta 到达时，assistant 空节点
        不再抢占更小的 position——思考节点在前，前端思考卡不沉底。"""
        cid = self.bridge.on_inbound(self.msg(message_id="ord1"))
        msg = self.msg(message_id="ord1")
        # 真实事件顺序：message_start（新段）→ reasoning_delta → text_delta
        self.bridge.on_agent_event(msg, {"type": "message_start",
                                         "data": {"role": "assistant"}})
        self.bridge.on_agent_event(msg, {"type": "reasoning_delta",
                                         "data": {"text": "思考中"}})
        self.bridge.on_agent_event(msg, {"type": "text_delta",
                                         "data": {"text": "最终回复"}})
        self.bridge.on_reply(msg, "最终回复")
        hist = self.service.history(cid)
        nodes = hist["items"][0]["nodes"]
        ordered = [(n["type"], n["position"]) for n in nodes
                   if n["type"] in {"reasoning", "assistant"}]
        self.assertEqual(ordered[0][0], "reasoning")
        self.assertEqual(ordered[-1][0], "assistant")
        self.assertLess(ordered[0][1], ordered[-1][1])


class BridgeRestartTests(BridgeBase):
    def test_restart_recovers_turns(self):
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        self.bridge.on_agent_event(self.msg(message_id="m1"), {
            "type": "reasoning_delta", "data": {"text": "运行中"}})
        result = self.service.recover_after_restart()
        self.assertEqual(result["interrupted_turns"], 1)
        active = self.store.get_active_turn(cid)
        self.assertIsNone(active)


class BridgeParentStopHookTests(BridgeBase):
    def test_parent_stop_hook_fires_on_stopped_turn(self):
        import asyncio
        calls = []

        async def flow():
            async def hook(session_key):
                calls.append(session_key)
            self.bridge.set_parent_stop_hook(hook)
            cid = self.bridge.on_inbound(self.msg(message_id="m1"))
            self.bridge.on_agent_event(self.msg(message_id="m1"), {
                "type": "reasoning_delta", "data": {"text": "运行中"}})
            self.bridge.request_stop("webui:default")
            self.bridge.on_reply(self.msg(message_id="m1"), "已停止")
            await asyncio.sleep(0.01)  # 让 fire-and-forget 协程执行

        asyncio.run(flow())
        self.assertEqual(calls, ["webui:default"])

    def test_parent_stop_hook_not_fired_on_done_turn(self):
        import asyncio
        calls = []

        async def flow():
            async def hook(session_key):
                calls.append(session_key)
            self.bridge.set_parent_stop_hook(hook)
            self.bridge.on_inbound(self.msg(message_id="m1"))
            self.bridge.on_reply(self.msg(message_id="m1"), "正常完成")
            await asyncio.sleep(0.01)

        asyncio.run(flow())
        self.assertEqual(calls, [])


class BridgeTurnStateLockTests(BridgeBase):
    """跨线程字段锁保护冒烟（_turn_state_lock 统一守护五个字段）。"""

    def test_turn_state_lock_exists_and_unlocked(self):
        import threading
        self.assertIsInstance(self.bridge._turn_state_lock, threading.Lock)
        self.assertFalse(self.bridge._turn_state_lock.locked())

    def test_delivery_seq_increment_is_atomic_under_contention(self):
        """并发 record_channel_delivery 不丢更新：版本号严格连续递增。"""
        import threading
        cid = self.bridge.on_inbound(self.msg())
        self.assertIsNotNone(cid)
        msg = self.msg()
        errors = []

        def worker():
            try:
                for _ in range(20):
                    self.bridge.record_channel_delivery(msg, "delivered")
            except Exception as exc:  # pragma: no cover - 诊断用
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 8 线程 × 20 次 = 160 次自增，最终序号恰为 160（读-改-写互斥）
        with self.bridge._turn_state_lock:
            final_seq = self.bridge._delivery_seq[cid]
        self.assertEqual(final_seq, 160)

    def test_stop_flag_roundtrip_via_lock(self):
        """停止标志置位/消费经同一把锁：消费后不可重复消费。"""
        self.bridge.mark_stop_requested("conv-1")
        with self.bridge._turn_state_lock:
            self.assertTrue(self.bridge._stop_requested.get("conv-1"))
        self.assertTrue(self.bridge.consume_stop_requested("conv-1"))
        self.assertFalse(self.bridge.consume_stop_requested("conv-1"))


if __name__ == "__main__":
    unittest.main()


class BridgeCardClassificationTests(BridgeBase):
    """方案 B：后端权威卡片分类标记（metadata.intermediate / metadata.final）。"""

    def test_tool_call_marks_preceding_assistant_intermediate(self):
        """assistant 后跟工具调用 → 后端标记 intermediate=True（前端据此渲染条卡）。"""
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        msg = self.msg(message_id="m1")
        self.bridge.on_agent_event(msg, {"type": "message_start",
                                         "data": {"role": "assistant"}})
        self.bridge.on_agent_event(msg, {"type": "text_delta",
                                         "data": {"text": "先看目录"}})
        self.bridge.on_agent_event(msg, {"type": "tool_call_start",
                                         "data": {"tool_call_id": "c1", "tool": "bash"}})
        self.bridge._merger.flush()
        snap = self.service.snapshot(cid)
        assistants = [n for n in snap["nodes"] if n["type"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertTrue(assistants[0]["metadata"].get("intermediate"),
                        "被工具打断的 assistant 应带 intermediate 标记")

    def test_new_round_marks_previous_assistant_intermediate(self):
        """新一轮 message_start → 上一轮 assistant 标 intermediate。"""
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        msg = self.msg(message_id="m1")
        # 第 1 轮：文本
        self.bridge.on_agent_event(msg, {"type": "message_start",
                                         "data": {"role": "assistant"}})
        self.bridge.on_agent_event(msg, {"type": "text_delta",
                                         "data": {"text": "第一轮回复"}})
        self.bridge.on_agent_event(msg, {"type": "message_end",
                                         "data": {"role": "assistant"}})
        # 第 2 轮开始
        self.bridge.on_agent_event(msg, {"type": "message_start",
                                         "data": {"role": "assistant"}})
        self.bridge._merger.flush()
        snap = self.service.snapshot(cid)
        assistants = [n for n in snap["nodes"] if n["type"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertTrue(assistants[0]["metadata"].get("intermediate"))

    def test_complete_turn_marks_final_assistant(self):
        """done 终态：最后一条 assistant 权威标记 final=True、intermediate=False。"""
        cid = self.bridge.on_inbound(self.msg(message_id="m1"))
        msg = self.msg(message_id="m1")
        self.bridge.on_agent_event(msg, {"type": "message_start",
                                         "data": {"role": "assistant"}})
        self.bridge.on_agent_event(msg, {"type": "text_delta",
                                         "data": {"text": "最终回答"}})
        self.bridge.on_agent_event(msg, {"type": "message_end",
                                         "data": {"role": "assistant"}})
        self.bridge.on_reply(msg, "最终回答")
        hist = self.service.history(cid)
        nodes = hist["items"][0]["nodes"]
        assistants = [n for n in nodes if n["type"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertTrue(assistants[0]["metadata"].get("final"))
        self.assertFalse(assistants[0]["metadata"].get("intermediate"))

    def test_runtime_step_replies_mark_intermediate_and_final(self):
        """plan/goal 步回复分类：非 final 步 → intermediate=True；final 步 →
        final=True + runtime_final。中间步骤文本不平铺（投影卡承载）。"""
        meta = {"task_source": "plan", "plan_id": "plan-cls"}
        cid = self.bridge.on_inbound(self.msg(message_id="p1", metadata=meta))
        msg = self.msg(message_id="p1", metadata=meta)
        # step1 回复（非 final）
        self.bridge.on_agent_event(msg, {"type": "reasoning_delta",
                                         "data": {"text": "step1 思考"}})
        self.bridge._on_system_reply(msg, "plan", meta, "step1 完成")
        # step2 回复（final）
        final_meta = {**meta, "final_response": True}
        self.bridge.on_agent_event(self.msg(message_id="p1", metadata=final_meta),
                                   {"type": "reasoning_delta",
                                    "data": {"text": "step2 思考"}})
        self.bridge._on_system_reply(
            self.msg(message_id="p1", metadata=final_meta), "plan", final_meta,
            "全部步骤完成")
        hist = self.service.history(cid)
        assistants = []
        for item in hist["items"]:
            for n in item["nodes"]:
                if n["type"] == "assistant" and (n["metadata"] or {}).get("runtime_type") == "plan":
                    assistants.append(n)
        self.assertEqual(len(assistants), 2)
        by_final = {bool(n["metadata"].get("runtime_final")): n for n in assistants}
        step1 = by_final[False]
        step2 = by_final[True]
        self.assertTrue(step1["metadata"].get("intermediate"),
                        "非 final 步回复应带 intermediate 标记")
        self.assertNotIn("runtime_final", step1["metadata"])
        self.assertTrue(step2["metadata"].get("final"))
        self.assertTrue(step2["metadata"].get("runtime_final"))
