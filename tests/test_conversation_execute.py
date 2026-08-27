# -*- coding: utf-8 -*-
"""
统一会话执行器单测（设计方案 8.5 第 8 步 / 以 Conversation/Turn 为权威）。

验证 ConversationTurnRunner：出队创建 Turn 后，runner 直接驱动执行
（mock _execute_agent，确定性验证不触碰 LLM）：
- 正确构造 InboundMessage（text / session_key / turn_id 关联）
- 事件只走统一模型（不向旧 chat.* 广播）
- 回复经 chat.done 完成 Turn；异常 → Turn=error
- 无活动 Turn / 运行中 / Turn 非 queued → 跳过
"""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from core.runtime import RuntimeStore
from gateway.webui.workspace_store import WorkspaceDatabase
from gateway.conversation import ConversationService, ConversationStore
from gateway.conversation.bridge import ConversationBridge
from gateway.conversation.runner import ConversationTurnRunner
from gateway.dispatcher import Dispatcher, SessionManager


class ConversationExecuteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = RuntimeStore(Path(self.tmp.name) / "runtime.db")
        self.db = WorkspaceDatabase(runtime_store=self.runtime)
        self.store = ConversationStore(self.db)
        self.events = []
        self.service = ConversationService(
            self.store, lambda t, p: self.events.append((t, p)),
            max_global_turns=100)
        self.bridge = ConversationBridge(self.service)
        self.sessions = SessionManager(max_sessions=5, worker_pool_size=1,
                                       persist=False)
        self.dispatcher = Dispatcher(self.sessions, agent_config={})
        self.dispatcher.set_conversation_bridge(self.bridge)
        from gateway.webui.events import EventBus
        from gateway.webui.channel import WebuiChannel
        self.dispatcher.register_channel(WebuiChannel(EventBus()))
        self.runner = ConversationTurnRunner(self.dispatcher)

    async def asyncTearDown(self):
        await self.runner.stop()
        await self.sessions.stop()
        self.tmp.cleanup()

    def _conv_and_turn(self, session_key="webui:exec-1", text="帮我执行"):
        conv = self.service.get_or_create_conversation(
            session_key, origin="webui", subtype="main")
        self.service.enqueue(conv.conversation_id, text)
        turn, _ = self.service.send_next(conv.conversation_id)
        return conv, turn

    async def test_runner_delivers_turn_user_node_to_execute(self):
        calls = []

        async def fake_execute(entry, msg, channel, **kwargs):
            calls.append(msg)
            return "回复内容"

        self.dispatcher._execute_agent = fake_execute
        conv, turn = self._conv_and_turn()

        ok = await self.runner.run_turn(conv.conversation_id)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].text, "帮我执行")
        self.assertEqual(calls[0].session_key, "webui:exec-1")
        self.assertEqual(calls[0].message_id, turn.turn_id)
        # 回复 → chat.done 完成 Turn
        turn_now = self.store.get_turn(turn.turn_id)
        self.assertEqual(turn_now.status, "done")
        self.assertIsNotNone(turn_now.final_assistant_node_id)
        # 事件只走统一模型（无旧 chat.* 广播，除 chat.done 权威终态）
        types = {t for t, _ in self.events}
        self.assertIn("chat.done", types)
        self.assertTrue(all(t.startswith("chat.") is False or t == "chat.done"
                            for t in types))

    async def test_runner_records_error_turn_on_failure(self):
        async def fail_execute(entry, msg, channel, **kwargs):
            raise RuntimeError("模拟执行失败")

        self.dispatcher._execute_agent = fail_execute
        conv, turn = self._conv_and_turn()
        await self.runner.run_turn(conv.conversation_id)
        turn_now = self.store.get_turn(turn.turn_id)
        self.assertEqual(turn_now.status, "error")
        self.assertEqual(turn_now.error_code, "agent_execution_failed")

    async def test_runner_skips_without_active_turn(self):
        conv = self.service.get_or_create_conversation(
            "webui:exec-2", origin="webui", subtype="main")
        ok = await self.runner.run_turn(conv.conversation_id)
        self.assertFalse(ok)

    async def test_runner_skips_turn_already_running(self):
        self.dispatcher._execute_agent = None  # 不应被调用
        conv, turn = self._conv_and_turn()
        self.service.set_turn_status(conv.conversation_id, turn.turn_id, "thinking")
        ok = await self.runner.run_turn(conv.conversation_id)
        self.assertFalse(ok)

    async def test_runner_reuses_agent_entry_across_turns(self):
        calls = []

        async def fake_execute(entry, msg, channel, **kwargs):
            calls.append(entry)
            return "ok"

        self.dispatcher._execute_agent = fake_execute
        conv, _ = self._conv_and_turn("webui:exec-3", "第一轮")
        await self.runner.run_turn(conv.conversation_id)
        self.service.enqueue(conv.conversation_id, "第二轮")
        self.service.send_next(conv.conversation_id)
        await self.runner.run_turn(conv.conversation_id)
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0], calls[1])  # 同一 Conversation 复用同一 Agent 入口

    async def test_runner_request_stop_interrupts_agent(self):
        """停止真正调用 agent.request_stop()（修复：仅置状态不打断任务）。"""
        stopped = []

        class FakeAgent:
            def request_stop(self):
                stopped.append(True)

        async def fake_execute(entry, msg, channel, **kwargs):
            entry.agent = FakeAgent()  # 模拟 _execute_agent 懒创建 Agent
            return "ok"

        self.dispatcher._execute_agent = fake_execute
        conv, _ = self._conv_and_turn("webui:exec-4", "跑起来")
        await self.runner.run_turn(conv.conversation_id)
        # 运行中调用 request_stop → 命中缓存的 Agent
        ok = self.runner.request_stop(conv.conversation_id)
        self.assertTrue(ok)
        self.assertEqual(stopped, [True])
        # 未运行的会话 → False，不抛异常
        self.assertFalse(self.runner.request_stop("no-such-conversation"))

    async def test_channel_turn_uses_channel_channel_and_delivers_reply(self):
        """渠道消息经统一 runner 执行：channel 取 route_metadata，回复投递渠道。"""
        delivered = []

        class FakeChannel:
            name = "feishu"
            handles_chunking = True

            async def send_reply(self, msg, text):
                delivered.append((msg.session_key, text))

        self.dispatcher.register_channel(FakeChannel())

        async def fake_execute(entry, msg, channel, **kwargs):
            self.assertEqual(channel.name, "feishu")
            return "渠道回复"

        self.dispatcher._execute_agent = fake_execute
        # 渠道会话：route_metadata 带 channel=feishu
        conv = self.service.get_or_create_conversation(
            "feishu:chat-1", origin="channel", subtype="feishu",
            route_metadata={"channel": "feishu", "user_id": "u1",
                            "user_name": "张三", "is_group": True})
        self.service.enqueue(conv.conversation_id, "渠道消息", channel="feishu",
                             sender_id="u1", sender_name="张三",
                             create_queued_node=True)
        turn, _ = self.service.send_next(conv.conversation_id)
        await self.runner.run_turn(conv.conversation_id)
        # 回复已投递到渠道
        self.assertEqual(delivered, [("feishu:chat-1", "渠道回复")])
        # Turn 终态 done
        self.assertEqual(self.store.get_turn(turn.turn_id).status, "done")


class SteeringResumeOrderingTests(ConversationExecuteTests):
    """回归（用户实测缺陷 2026-08）：运行中插队后 ok 收口不得提前 conclude。

    此前 _await_with_watchdogs 的 reply 完成分支无条件 conclude_steering，
    _execute 的 pending_steering 检查永远拿到 None——消息提示"已插入"却
    永远卡在 waiting_for_steering，从不注入。"""

    async def test_ok_path_keeps_pending_steering_for_resume(self):
        conv, turn = self._conv_and_turn("webui:steer-1", "正在执行的长任务")
        # 待插队项：运行中 Turn 之外再入队一条（_conv_and_turn 的首条已被
        # send_next 消费为当前 Turn 的 user 消息）
        self.service.enqueue(conv.conversation_id, "插入的新指令")
        # 模拟第一阶段 prepare 完成：项进入 waiting_for_steering + 注册等待
        qid = self.service.list_queue(conv.conversation_id)[0].queue_item_id
        self.service.prepare_steering(conv.conversation_id, [qid])
        self.service.register_steering_wait(conv.conversation_id, [qid])

        reply_task = asyncio.create_task(asyncio.sleep(0, result="⏹️ 已停止"))
        reply, outcome, qids = await self.runner._await_with_watchdogs(
            conv.conversation_id, reply_task, self.bridge)

        self.assertEqual(outcome, "ok")
        # 关键断言：等待记录仍存活（commit/resume 有据可依）
        self.assertEqual(self.service.pending_steering(conv.conversation_id), [qid])
        # 项仍处于 waiting_for_steering（由 commit_steering 注入后归档）
        item = self.service.store.get_queue_item(qid)
        self.assertEqual(item.status, "waiting_for_steering")
        _ = reply, qids

    async def test_ok_path_concludes_when_no_steering(self):
        conv, turn = self._conv_and_turn("webui:steer-2", "普通任务")
        reply_task = asyncio.create_task(asyncio.sleep(0, result="正常回复"))
        reply, outcome, _qids = await self.runner._await_with_watchdogs(
            conv.conversation_id, reply_task, self.bridge)
        self.assertEqual(outcome, "ok")
        self.assertEqual(reply, "正常回复")
        # 无 Steering 时维持既有清理语义（幂等，无等待记录）
        self.assertIsNone(self.service.pending_steering(conv.conversation_id))


class StopTimeoutSteeringCleanupTests(ConversationExecuteTests):
    """回归（用户实测 2026-08-26：队列消息"没有自动发送/手动发送也报错"）。

    运行中插队（prepare + register + request_stop）后停止看门先于 Steering
    窗口到点：stop_timeout 分支此前只清停止标志，泄漏 waiting_for_steering
    队列项与已过期的内存等待记录——①error 终态使前端按 §8.4 暂停自动分派；
    ②手动分派后陈旧记录在看门首轮被误判 steering_timeout，新 Turn 5ms 内
    error(steering_interrupt_timeout)，消息等于没发出去。"""

    async def test_stop_timeout_cleans_pending_steering(self):
        conv, turn = self._conv_and_turn("webui:steer-stop-1", "长任务")
        self.service.enqueue(conv.conversation_id, "插队消息")
        qid = self.service.list_queue(conv.conversation_id)[0].queue_item_id
        self.service.prepare_steering(conv.conversation_id, [qid])
        self.service.register_steering_wait(conv.conversation_id, [qid])
        cid = conv.conversation_id

        class DummyAgent:
            def request_stop(self):
                pass

        async def slow_execute(entry, msg, channel, **kwargs):
            # 模拟真实时序：Steering prepare 的 request_stop 在执行中触发
            # 停止看门，且停止窗口先于 Steering 窗口到点（回拨时间戳）
            entry.agent = DummyAgent()
            self.assertTrue(self.runner.request_stop(cid))
            self.runner._stop_requested_at[cid] -= (
                self.runner._stop_watchdog + 1.0)
            await asyncio.sleep(0.05)
            return "迟到回复"  # 看门收口后的迟到回复仅诊断

        self.dispatcher._execute_agent = slow_execute
        ok = await self.runner.run_turn(cid)
        self.assertTrue(ok)
        # Turn 以 stop_timeout 终态收口
        turn_now = self.store.get_turn(turn.turn_id)
        self.assertEqual(turn_now.status, "error")
        self.assertEqual(turn_now.error_code, "stop_timeout")
        # 关键断言①：内存等待记录已收口（不泄漏到下一 Turn）
        self.assertIsNone(self.service.pending_steering(cid))
        # 关键断言②：waiting_for_steering 项恢复为普通等待项
        item = self.service.store.get_queue_item(qid)
        self.assertEqual(item.status, "waiting")

    async def test_stale_steering_record_does_not_kill_new_turn(self):
        """陈旧等待记录（deadline 已过）不再让新 Turn 在看门首轮死亡。"""
        conv, turn = self._conv_and_turn("webui:steer-stop-2", "新任务")
        # 伪造上一轮泄漏：等待记录的窗口已过期
        self.service.register_steering_wait(conv.conversation_id, ["q_stale"])
        with self.service._steering_lock:
            self.service._steering_pending[conv.conversation_id]["deadline"] = (
                time.time() - 1.0)
        reply_task = asyncio.create_task(asyncio.sleep(0, result="正常回复"))
        try:
            reply, outcome, _qids = await self.runner._await_with_watchdogs(
                conv.conversation_id, reply_task, self.bridge)
        finally:
            if not reply_task.done():
                reply_task.cancel()
        # 修复前：outcome == "steering_timeout"（5ms 误杀）；修复后正常 ok
        self.assertEqual(outcome, "ok")
        self.assertEqual(reply, "正常回复")
        self.assertIsNone(self.service.pending_steering(conv.conversation_id))


class StaleSteeringSelfHealTests(ConversationExecuteTests):
    """自愈：无活动 Turn 时 waiting_for_steering 属陈旧态，send_next 应照常
    分派（修复存量卡队项；steering 等待本就是进程内存态，重启即失）。"""

    async def test_send_next_dispatches_stale_waiting_for_steering(self):
        conv = self.service.get_or_create_conversation(
            "webui:steal-3", origin="webui", subtype="main")
        self.service.enqueue(conv.conversation_id, "卡住的插队消息")
        qid = self.service.list_queue(conv.conversation_id)[0].queue_item_id
        # prepare 需要活动 Turn：先起一个再置 waiting_for_steering
        turn = self.service.start_turn(conv.conversation_id)
        self.service.prepare_steering(conv.conversation_id, [qid])
        # 伪造：等待记录丢失（conclude），Turn 已终结——陈旧态成立
        self.service.conclude_steering(conv.conversation_id)
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "stopped")
        self.assertIsNone(self.service.store.get_active_turn(conv.conversation_id))

        result = self.service.send_next(conv.conversation_id)
        self.assertIsNotNone(result)
        turn, node = result
        item = self.service.store.get_queue_item(qid)
        self.assertEqual(item.status, "sent")
        self.assertEqual(node.text, "卡住的插队消息")
        _ = turn


if __name__ == "__main__":
    unittest.main()


class BusyRejectionTurnHandlingTests(ConversationExecuteTests):
    """回归（用户反馈 2026-08）：忙拒绝文案不得落成会话答复节点。

    此前 SESSION_BUSY_REPLY 作为 reply 经 on_reply 存成 assistant 节点，
    渲染成"系统告警"气泡；现在应 Turn=error(agent_session_busy) 收口，
    队列项保持 waiting 等待下一轮分派。"""

    async def test_busy_reply_finalizes_error_without_reply_node(self):
        from gateway.dispatcher import SESSION_BUSY_REPLY
        conv, turn = self._conv_and_turn("webui:busy-1", "第一条消息")
        qid = None  # 再入队一条：忙拒绝场景的"第二条"
        self.service.enqueue(conv.conversation_id, "第二条消息")

        async def fake_execute(entry, msg, channel, **kwargs):
            return SESSION_BUSY_REPLY

        self.dispatcher._execute_agent = fake_execute
        await self.runner.run_turn(conv.conversation_id)

        fresh = self.store.get_turn(turn.turn_id)
        self.assertEqual(fresh.status, "error")
        self.assertEqual(fresh.error_code, "agent_session_busy")
        # 不落答复节点：turn_nodes 无 assistant 节点
        nodes = self.store.get_turn_nodes(turn.turn_id)
        self.assertFalse([n for n in nodes if n.type == "assistant"])
        # 队列项仍 waiting（等当前忙轮释放后推进）
        items = self.service.list_queue(conv.conversation_id)
        self.assertEqual([i.status for i in items], ["waiting"])
