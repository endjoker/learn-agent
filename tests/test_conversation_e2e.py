# -*- coding: utf-8 -*-
"""
统一会话 E2E 测试 —— 设计方案第 27 节"阶段五：E2E（Mock Runtime）"。

用 Mock Runtime（直接向 ConversationBridge 灌脚本化 Agent 事件，等价于真实
运行时事件回流）驱动真实 GatewayServer，覆盖：

- 正常流式完成（reasoning → tool → assistant → chat.done 校正）
- Reasoning 合并 / 多工具节点
- Steering（prepare → commit → user_steering 节点）
- Stop（stopping → stopped）
- 页面刷新恢复（Snapshot 重建）与 SSE 断线（Last-Event-ID 重放）
- 写操作无控制租约校验（控制租约已废弃）
- Execution Scope 并发限制（5/域）
- 飞书/微信排队与持久化去重（queued node 原位升级）
- Plan/Goal 投影（系统 Conversation + 父会话投影）
- 历史分页（完整 Turn 为单位）
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession
from gateway.server import GatewayServer


def _cfg(tmp: Path, name: str) -> dict:
    return {
        'host': '127.0.0.1', 'port': 0,
        'channels': {'debug': {'enabled': False}, 'webui': {'enabled': True}},
        'webui': {'allow_non_loopback': False,
                  'conversation': {'auto_execute_on_send_next': False,
                                   'max_global_turns': 100}},
        'scheduler': {'enabled': False}, 'heartbeat': {'enabled': False},
        'sessions': {'max_sessions': 20, 'idle_timeout_minutes': 1,
                     'persist': False, 'worker_pool_size': 1,
                     'soft_timeout_seconds': 1, 'hard_timeout_seconds': 2},
        'agent': {'max_steps': 5, 'quiet': True},
        'runtime_store': {'path': str(tmp / f'{name}.db'), 'wal': False,
                          'busy_timeout_ms': 1000},
        'task_runtime': {'enabled': True, 'max_global_concurrency': 1,
                         'max_attempts': 1, 'cancel_grace_seconds': 0,
                         'zombie_max_seconds': 1},
        'artifacts': {'root': str(tmp / f'{name}-artifacts'),
                      'max_file_bytes': 1024},
        'retention': {'enabled': True, 'interval_seconds': 60},
    }


class MockRuntime:
    """脚本化 Mock Runtime：按序列灌事件到 ConversationBridge。"""

    def __init__(self, bridge, session_key, message_id="e2e-1"):
        self.bridge = bridge
        self.session_key = session_key
        self.message_id = message_id
        from gateway.channels.base import InboundMessage
        self.msg = InboundMessage(
            channel="webui", session_key=session_key, user_id="e2e",
            user_name="E2E", text="脚本任务", message_id=message_id)

    def reasoning(self, text):
        self.bridge.on_agent_event(self.msg, {"type": "reasoning_delta",
                                              "data": {"text": text}})

    def tool(self, call_id, tool, arguments=None, result=None):
        self.bridge.on_agent_event(self.msg, {"type": "tool_call_start", "data": {
            "tool_call_id": call_id, "tool": tool}})
        self.bridge.on_agent_event(self.msg, {"type": "tool_call_end", "data": {
            "tool_call_id": call_id, "tool": tool, "arguments": arguments or {}}})
        if result is not None:
            self.bridge.on_agent_event(self.msg, {"type": "message_start", "data": {
                "role": "tool", "message_id": f"result_{call_id}", "content": result}})

    def assistant(self, text):
        self.bridge.on_agent_event(self.msg, {"type": "message_start", "data": {
            "role": "assistant", "message_id": "asst-1"}})
        self.bridge.on_agent_event(self.msg, {"type": "text_delta", "data": {"text": text}})
        self.bridge.on_agent_event(self.msg, {"type": "message_end", "data": {"role": "assistant"}})

    def done(self, text):
        self.bridge.on_reply(self.msg, text)


class ConversationE2ETests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = GatewayServer(_cfg(Path(self.tmp.name), "e2e"))
        await self.server.start()
        self.port = self.server._site._server.sockets[0].getsockname()[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.bridge = self.server.webui.conversation_bridge

    async def asyncTearDown(self):
        await self.server.stop()
        self.tmp.cleanup()

    async def _create(self, client, session_key):
        async with client.post(f'{self.base}/api/conversations',
                               json={'session_key': session_key}) as resp:
            return (await resp.json())['conversation']

    async def _post(self, client, path, body=None, expected=200):
        async with client.post(f'{self.base}{path}', json=body or {}) as resp:
            self.assertEqual(resp.status, expected, await resp.text())
            return await resp.json() if resp.content_type == "application/json" else None

    async def _get(self, client, path, expected=200):
        async with client.get(f'{self.base}{path}') as resp:
            self.assertEqual(resp.status, expected, await resp.text())
            return await resp.json()

    # ------------------------------------------------------------
    # 1. 正常流式完成 + chat.done 校正
    # ------------------------------------------------------------

    async def test_streaming_completion_and_chat_done_authority(self):
        async with ClientSession() as client:
            conv = await self._create(client, "webui:e2e-1")
            cid = conv["conversation_id"]
            await self._post(client, f'/api/conversations/{cid}/queue',
                             {"text": "帮我写脚本"})
            await self._post(client, f'/api/conversations/{cid}/queue/send-next')
            runtime = MockRuntime(self.bridge, "webui:e2e-1")
            runtime.reasoning("先分析需求")
            runtime.reasoning("再设计步骤")   # 合并到同一 reasoning 节点
            runtime.tool("c1", "bash", {"command": "ls"}, "a.py\nb.py")
            runtime.assistant("草稿回答")
            runtime.done("权威最终回答")       # chat.done 覆盖
            snap = await self._get(client, f'/api/conversations/{cid}/snapshot')
            self.assertIsNone(snap["live_turn"])  # 已终态
            hist = await self._get(client, f'/api/conversations/{cid}/turns')
            turn = hist["items"][0]
            self.assertEqual(turn["turn"]["status"], "done")
            types = [n["type"] for n in turn["nodes"]]
            self.assertEqual(types, ["user", "reasoning", "tool", "assistant"])
            reasoning = next(n for n in turn["nodes"] if n["type"] == "reasoning")
            self.assertEqual(reasoning["text"], "先分析需求再设计步骤")
            assistant = next(n for n in turn["nodes"] if n["type"] == "assistant")
            self.assertEqual(assistant["text"], "权威最终回答")
            self.assertEqual(turn["turn"]["final_assistant_node_id"],
                             assistant["node_id"])

    # ------------------------------------------------------------
    # 2. Steering
    # ------------------------------------------------------------

    async def test_steering_injects_user_steering_node(self):
        async with ClientSession() as client:
            conv = await self._create(client, "webui:e2e-steer")
            cid = conv["conversation_id"]
            await self._post(client, f'/api/conversations/{cid}/queue', {"text": "原始任务"})
            await self._post(client, f'/api/conversations/{cid}/queue/send-next')
            runtime = MockRuntime(self.bridge, "webui:e2e-steer")
            runtime.reasoning("执行中")
            # 用户选新消息插入当前 Turn
            queued = await self._post(client, f'/api/conversations/{cid}/queue',
                                      {"text": "改为做另一件事"})
            qid = queued["queue_item"]["queue_item_id"]
            await self._post(client, f'/api/conversations/{cid}/steering',
                             {"queue_item_ids": [qid]})
            await self._post(client, f'/api/conversations/{cid}/steering/commit',
                             {"queue_item_ids": [qid]})
            runtime.assistant("按新方向回答")
            runtime.done("完成")
            hist = await self._get(client, f'/api/conversations/{cid}/turns')
            types = [n["type"] for n in hist["items"][0]["nodes"]]
            self.assertIn("user_steering", types)
            steering = next(n for n in hist["items"][0]["nodes"]
                            if n["type"] == "user_steering")
            self.assertEqual(steering["text"], "改为做另一件事")

    # ------------------------------------------------------------
    # 3. Stop
    # ------------------------------------------------------------

    async def test_stop_flow(self):
        async with ClientSession() as client:
            conv = await self._create(client, "webui:e2e-stop")
            cid = conv["conversation_id"]
            await self._post(client, f'/api/conversations/{cid}/queue', {"text": "长任务"})
            await self._post(client, f'/api/conversations/{cid}/queue/send-next')
            runtime = MockRuntime(self.bridge, "webui:e2e-stop")
            runtime.reasoning("开始")
            await self._post(client, f'/api/conversations/{cid}/stop',
                             {"operation_id": "stop-e2e"})
            runtime.done("已停止")
            hist = await self._get(client, f'/api/conversations/{cid}/turns')
            self.assertEqual(hist["items"][0]["turn"]["status"], "stopped")

    # ------------------------------------------------------------
    # 4. 页面刷新恢复 + SSE 断线重放
    # ------------------------------------------------------------

    async def test_refresh_restores_snapshot_and_sse_replays(self):
        async with ClientSession() as client:
            conv = await self._create(client, "webui:e2e-refresh")
            cid = conv["conversation_id"]
            await self._post(client, f'/api/conversations/{cid}/queue', {"text": "q"})
            await self._post(client, f'/api/conversations/{cid}/queue/send-next')
            runtime = MockRuntime(self.bridge, "webui:e2e-refresh")
            runtime.reasoning("中途")
            # 等待后台 delta 刷盘（约 100ms 周期）
            await asyncio.sleep(0.2)
            # 页面刷新：重新拉快照（模拟新客户端）
            snap = await self._get(client, f'/api/conversations/{cid}/snapshot')
            # 设计方案 7.4：reasoning 事件后 Turn 阶段为 thinking（非 queued）
            self.assertIn(snap["live_turn"]["status"], {"queued", "thinking"})
            self.assertEqual(len(snap["nodes"]), 2)  # user + reasoning
            self.assertGreater(snap["turn_version"], 0)
            # SSE 断线重放：Last-Event-ID（>0）拿到历史事件（0 表示不重放）
            stream = await client.get(f'{self.base}/api/events'
                                      f'?session_key=webui:e2e-refresh&last_event_id=1')
            await stream.content.readuntil(b'\n\n')
            first = json.loads((await asyncio.wait_for(
                stream.content.readuntil(b'\n\n'), timeout=2))
                .decode("utf-8").split("data: ", 1)[1])
            self.assertIn(first["type"],
                          {"conversation.upserted", "queue.updated",
                           "turn.status", "node.delta"})
            stream.close()

    # ------------------------------------------------------------
    # 5. 多标签页控制租约
    # ------------------------------------------------------------

    async def test_execution_scope_limit(self):
        async with ClientSession() as client:
            convs = []
            for i in range(5):
                conv = await self._create(client, f"webui:e2e-scope{i}")
                cid = conv["conversation_id"]
                await self._post(client, f'/api/conversations/{cid}/queue', {"text": "q"})
                await self._post(client, f'/api/conversations/{cid}/queue/send-next')
                convs.append(cid)
            extra = await self._create(client, "webui:e2e-scope-extra")
            eid = extra["conversation_id"]
            await self._post(client, f'/api/conversations/{eid}/queue', {"text": "q"})
            resp = await self._post(client, f'/api/conversations/{eid}/queue/send-next',
                                    expected=409)
            self.assertEqual(resp["error"], "execution_scope_concurrency_limit")

    # ------------------------------------------------------------
    # 7. 飞书/微信排队与持久化去重
    # ------------------------------------------------------------

    async def test_channel_fifo_queued_node_and_dedup(self):
        from gateway.channels.base import InboundMessage

        def feishu_msg(text, message_id):
            return InboundMessage(channel="feishu", session_key="feishu:chat-1",
                                  user_id="u1", user_name="张三", text=text,
                                  message_id=message_id)

        async with ClientSession() as client:
            conv = await self._create(client, "feishu:chat-1")
            cid = conv["conversation_id"]
            # 第一条：真实渠道入站路径（Dispatcher → bridge.on_inbound）
            self.assertEqual(self.bridge.on_inbound(feishu_msg("群消息1", "m-1")), cid)
            snap = await self._get(client, f'/api/conversations/{cid}/snapshot')
            self.assertEqual(len(snap["queued_nodes"]), 1)
            self.assertEqual(snap["queued_nodes"][0]["source_channel"], "feishu")
            queued_node_id = snap["queued_nodes"][0]["node_id"]
            # 空闲直发：出队原位升级 queued node → Turn User Node
            await self._post(client, f'/api/conversations/{cid}/queue/send-next',
                             {"channel_node_id": queued_node_id})
            snap = await self._get(client, f'/api/conversations/{cid}/snapshot')
            self.assertEqual(snap["live_turn"]["status"], "queued")
            self.assertEqual(snap["nodes"][0]["source_channel"], "feishu")
            self.assertEqual(snap["nodes"][0]["sender_name"], "张三")
            # 忙碌时第二条进入 FIFO（queued node）
            self.assertEqual(self.bridge.on_inbound(feishu_msg("群消息2", "m-2")), cid)
            snap = await self._get(client, f'/api/conversations/{cid}/snapshot')
            self.assertEqual(len(snap["queue"]), 1)
            self.assertEqual(len(snap["queued_nodes"]), 1)
            # 持久化去重：重复 m-1 再次投递 → 收据命中返回 None（不重复入队/执行/回复）
            self.assertIsNone(self.bridge.on_inbound(feishu_msg("重复", "m-1")))
            snap = await self._get(client, f'/api/conversations/{cid}/snapshot')
            self.assertEqual(len(snap["queue"]), 1)  # 未重复入队

    async def test_channel_stop_and_delivery_status(self):
        """渠道 /stop（§11.7）+ 投递状态（§11.5/30.2 delivery.status）。"""
        from gateway.channels.base import InboundMessage

        def feishu_msg(text, message_id, channel="feishu", session_key="feishu:stop-1"):
            return InboundMessage(channel=channel, session_key=session_key,
                                  user_id="u1", user_name="张三", text=text,
                                  message_id=message_id)

        async with ClientSession() as client:
            conv = await self._create(client, "feishu:stop-1")
            cid = conv["conversation_id"]
            self.assertEqual(self.bridge.on_inbound(feishu_msg("跑任务", "m-1")), cid)
            await self._post(client, f'/api/conversations/{cid}/queue/send-next', {})
            snap = await self._get(client, f'/api/conversations/{cid}/snapshot')
            self.assertEqual(snap["live_turn"]["status"], "queued")
            # 渠道 /stop：无活动 Turn 前不消费、不报错
            self.bridge.on_inbound_stop(feishu_msg("/stop", "m-stop"))
            # 手动推进 turn 为 running（模拟执行中），再 /stop → stopped
            self.server.webui.conversation_service.set_turn_status(
                cid, snap["live_turn"]["turn_id"], "thinking")
            stopped = self.bridge.on_inbound_stop(feishu_msg("/stop", "m-stop2"))
            self.assertEqual(stopped, cid)
            turn = self.server.webui.conversation_service.store.get_turn(
                snap["live_turn"]["turn_id"])
            self.assertEqual(turn.status, "stopping")
            # 投递状态记录（模拟渠道回复成功/失败）：广播 delivery.status 事件
            self.bridge.record_channel_delivery(
                feishu_msg("回复", "m-reply"), "pending_delivery")
            self.bridge.record_channel_delivery(
                feishu_msg("回复", "m-reply"), "delivered")
            deliveries = self.server.webui.bus.replay(
                after_event_id=0, session_key="feishu:stop-1")
            delivery_events = [e for e in deliveries
                               if e["type"] == "delivery.status"]
            self.assertGreaterEqual(len(delivery_events), 1)
            self.assertEqual(
                delivery_events[-1]["data"]["data"]["state"], "delivered")

    # ------------------------------------------------------------
    # 8. Plan/Goal 投影（Mock 运行时）
    # ------------------------------------------------------------

    async def test_plan_goal_projection(self):
        async with ClientSession() as client:
            parent = await self._create(client, "webui:e2e-plan")
            pcid = parent["conversation_id"]
            meta = {"task_source": "plan", "plan_id": "plan-e2e"}
            msg = self._plan_msg(meta)
            self.bridge.on_agent_event(msg, {"type": "reasoning_delta",
                                             "data": {"text": "规划步骤"}})
            self.bridge.on_agent_event(msg, {"type": "tool_call_start", "data": {
                "tool_call_id": "p1", "tool": "read"}})
            self.bridge.on_agent_event(msg, {"type": "tool_call_end", "data": {
                "tool_call_id": "p1", "tool": "read", "arguments": {}}})
            self.bridge.on_reply(msg, "方案完成")
            # 对齐 dsh：不再建 system 会话，plan 轮次直接落在父会话 runtime 节点
            self.assertIsNone(self.server.webui.conversation_service.store
                              .get_conversation_by_key("system:plan:plan-e2e"))
            hist = await self._get(client, f'/api/conversations/{pcid}/turns?limit=50')
            runtime_nodes = []
            for item in hist["items"]:
                for n in item["nodes"]:
                    if (n["metadata"] or {}).get("runtime_type") == "plan":
                        runtime_nodes.append(n)
            self.assertTrue(runtime_nodes)
            final = [n for n in runtime_nodes if n["type"] == "assistant"]
            self.assertTrue(final)
            self.assertEqual(final[-1]["text"], "方案完成")
            self.assertEqual(final[-1]["metadata"]["runtime_type"], "plan")
            self.assertEqual(final[-1]["metadata"]["runtime_status"], "done")

    def _plan_msg(self, meta):
        from gateway.channels.base import InboundMessage
        return InboundMessage(channel="webui", session_key="webui:e2e-plan",
                              user_id="system", user_name="PlanRuntime",
                              text="plan", message_id="plan-msg",
                              metadata=meta)

    # ------------------------------------------------------------
    # 9.5 Plan/Goal 继承父会话模型/权限/推理（对齐 dsh：与父会话同一套）
    # ------------------------------------------------------------

    async def test_plan_goal_inherits_parent_session_prefs(self):
        """plan/goal 后台任务在使用与父会话同一 agent 时，Agent 延迟创建应
        继承父会话持久化的模型/权限/推理（而非回落到全局 agent_config 默认）。"""
        session_key = "webui:e2e-inherit"
        svc = self.server.webui.conversation_service
        bridge = self.server.webui.conversation_bridge
        conv = bridge.resolve(session_key)
        conv_id = conv.conversation_id
        self.assertEqual(svc.conversation_prefs(conv_id), {})
        svc.update_prefs(conv_id, model="gpt-5.6-luna",
                         permission_mode="ask", reasoning_level="high")
        prefs = self.server.dispatcher._session_prefs(session_key)
        self.assertEqual(prefs.get("model"), "gpt-5.6-luna")
        self.assertEqual(prefs.get("permission_mode"), "ask")
        self.assertEqual(prefs.get("reasoning_level"), "high")
        # 未设置偏好的新会话回落为空（默认能力子集/继承全部）
        conv2 = bridge.resolve("webui:e2e-inherit-b")
        self.assertEqual(svc.conversation_prefs(conv2.conversation_id), {})
        self.assertEqual(self.server.dispatcher._session_prefs("webui:e2e-inherit-b"), {})

    # ------------------------------------------------------------
    # 10. 父 Turn 停止联动（设计方案 14.4）
    # ------------------------------------------------------------

    async def test_parent_stop_pauses_plan_and_goal(self):
        from gateway.dispatcher import Dispatcher
        session_key = "webui:e2e-14"
        session_id = Dispatcher._runtime_session_id(session_key)
        runtime_store = self.server.dispatcher._runtime_store
        runtime_store.upsert_session(session_id, session_key, channel="webui",
                                     status="active")
        # 创建活动 Goal 与活动 Plan
        goal = self.server.webui.goal_runtime.create(
            session_id, "长期目标", title="目标A")
        self.assertEqual(goal.status.value, "active")
        plan = self.server.webui.plan_runtime.manager.create_preview(
            session_id, {"steps": [{"description": "步骤1"}]}, source_prompt="x")
        self.server.webui.plan_runtime.manager.approve(plan.plan_id)
        self.server.webui.plan_runtime.manager.activate(plan.plan_id)
        self.assertEqual(self.server.webui.plan_runtime.manager.get(
            plan.plan_id).status.value, "active")

        async with ClientSession() as client:
            conv = await self._create(client, session_key)
            cid = conv["conversation_id"]
            await self._post(client, f'/api/conversations/{cid}/queue',
                             {"text": "运行"})
            await self._post(client, f'/api/conversations/{cid}/queue/send-next')
            runtime = MockRuntime(self.bridge, session_key, message_id="e2e-14-1")
            runtime.reasoning("开始")
            await self._post(client, f'/api/conversations/{cid}/stop', {})
            runtime.done("已停止")

        # 等待 fire-and-forget 联动协程执行
        for _ in range(20):
            await asyncio.sleep(0.05)
            goal_now = self.server.webui.goal_runtime.get(goal.goal_id)
            plan_now = self.server.webui.plan_runtime.manager.get(plan.plan_id)
            if goal_now.status.value == "paused" and plan_now.status.value == "paused":
                break
        self.assertEqual(goal_now.status.value, "paused")
        self.assertEqual(plan_now.status.value, "paused")
        # 父 Turn 终态为 stopped
        async with ClientSession() as client:
            hist = await self._get(client, f'/api/conversations/{cid}/turns')
        self.assertEqual(hist["items"][0]["turn"]["status"], "stopped")

    # ------------------------------------------------------------
    # 11. 新流式管道：客户端经 node.delta 收到正文 + chat.done 权威终态
    # ------------------------------------------------------------

    async def test_sse_stream_delivers_node_delta_text(self):
        """验证"流式处理逻辑已更新"：前端数据源是带正文的 node.delta 事件，
        chat.done 携带权威 full_text，而非旧页面临时拼接。"""
        import time
        async with ClientSession() as client:
            conv = await self._create(client, "webui:e2e-stream")
            cid = conv["conversation_id"]
            await self._post(client, f'/api/conversations/{cid}/queue', {"text": "q"})
            await self._post(client, f'/api/conversations/{cid}/queue/send-next')
            stream = await client.get(f'{self.base}/api/events?session_key=webui:e2e-stream')
            await stream.content.readuntil(b'\n\n')
            runtime = MockRuntime(self.bridge, "webui:e2e-stream")
            runtime.reasoning("先思考")
            runtime.assistant("**加粗**回答")
            runtime.done("权威最终")
            seen_text = None
            seen_done = None
            seen_turn_id = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (seen_text is None or seen_done is None):
                try:
                    chunk = await asyncio.wait_for(
                        stream.content.readuntil(b'\n\n'), timeout=1)
                except asyncio.TimeoutError:
                    break
                evt = json.loads(chunk.decode("utf-8").split("data: ", 1)[1])
                biz = evt.get("data") or {}
                if evt["type"] == "node.delta":
                    delta_data = biz.get("data") or {}
                    # 契约①：增量 delta 按 seq 追加
                    if delta_data.get("delta"):
                        seen_text = (seen_text or "") + delta_data["delta"]
                    seen_turn_id = biz.get("turn_id")
                    self.assertIn("version", biz)
                elif evt["type"] == "chat.done":
                    seen_done = (biz.get("data") or {}).get("full_text")
            # 客户端收到了带正文的流式节点（非仅 text_len）
            self.assertIsNotNone(seen_text)
            self.assertIn("加粗", seen_text)
            # chat.done 权威终态（覆盖草稿）
            self.assertEqual(seen_done, "权威最终")
            self.assertIsNotNone(seen_turn_id)
            stream.close()

    # ------------------------------------------------------------
    # 9. 历史分页
    # ------------------------------------------------------------

    async def test_history_paging(self):
        async with ClientSession() as client:
            conv = await self._create(client, "webui:e2e-paging")
            cid = conv["conversation_id"]
            runtime = MockRuntime(self.bridge, "webui:e2e-paging")
            for i in range(4):
                await self._post(client, f'/api/conversations/{cid}/queue',
                                 {"text": f"问{i}"})
                await self._post(client, f'/api/conversations/{cid}/queue/send-next')
                runtime.assistant(f"答{i}")
                runtime.done(f"答{i}")
            page1 = await self._get(client, f'/api/conversations/{cid}/turns?limit=2')
            self.assertEqual(len(page1["items"]), 2)
            self.assertIsNotNone(page1["next_cursor"])
            page2 = await self._get(client, f'/api/conversations/{cid}/turns'
                                           f'?limit=2&cursor={page1["next_cursor"]}')
            self.assertEqual(len(page2["items"]), 2)
            self.assertIsNone(page2["next_cursor"])
            # 完整 Turn 不拆分
            self.assertEqual(len(page2["items"][0]["nodes"]), 2)  # user + assistant


if __name__ == "__main__":
    unittest.main()
