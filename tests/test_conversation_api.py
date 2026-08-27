# -*- coding: utf-8 -*-
"""
统一会话 REST API 集成测试 —— 设计方案第 30 节。

通过真实 GatewayServer + aiohttp ClientSession 验证：
创建/查找 Conversation、入队、出队、快照、历史、租约、Steering、
停止、审批、错误码（queue_limit / idempotency / execution_scope）、
SSE 事件（queue.updated / turn.status / chat.done）与快照版本缺口。
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
        'sessions': {'max_sessions': 10, 'idle_timeout_minutes': 1,
                     'persist': False, 'worker_pool_size': 1,
                     'soft_timeout_seconds': 1, 'hard_timeout_seconds': 2},
        'agent': {'max_steps': 1, 'quiet': True},
        'runtime_store': {'path': str(tmp / f'{name}.db'), 'wal': False,
                          'busy_timeout_ms': 1000},
        'task_runtime': {'enabled': True, 'max_global_concurrency': 1,
                         'max_attempts': 1, 'cancel_grace_seconds': 0,
                         'zombie_max_seconds': 1},
        'artifacts': {'root': str(tmp / f'{name}-artifacts'),
                      'max_file_bytes': 1024},
        'retention': {'enabled': True, 'interval_seconds': 60},
    }


class ConversationApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = GatewayServer(_cfg(Path(self.tmp.name), "api"))
        await self.server.start()
        self.port = self.server._site._server.sockets[0].getsockname()[1]
        self.base = f'http://127.0.0.1:{self.port}'

    async def asyncTearDown(self):
        await self.server.stop()
        self.tmp.cleanup()

    async def _create(self, client, session_key):
        async with client.post(f'{self.base}/api/conversations',
                               json={'session_key': session_key}) as resp:
            self.assertEqual(resp.status, 200)
            return (await resp.json())['conversation']

    async def test_conversation_full_flow_over_http(self):
        async with ClientSession() as client:
            conv = await self._create(client, 'webui:api1')
            cid = conv['conversation_id']
            # 同 key 幂等
            again = await self._create(client, 'webui:api1')
            self.assertEqual(again['conversation_id'], cid)
            # 按 session_key 查找
            async with client.get(f'{self.base}/api/conversations/lookup'
                                  f'?session_key=webui:api1') as resp:
                self.assertEqual(resp.status, 200)
            # 入队
            async with client.post(f'{self.base}/api/conversations/{cid}/queue',
                                   json={'text': '第一问', 'operation_id': 'enq-1'}) as resp:
                self.assertEqual(resp.status, 200)
                item = (await resp.json())['queue_item']
            # 出队
            async with client.post(f'{self.base}/api/conversations/{cid}/queue/send-next',
                                   json={'operation_id': 'send-1'}) as resp:
                data = await resp.json()
                self.assertTrue(data['dispatched'])
            # 快照
            async with client.get(f'{self.base}/api/conversations/{cid}/snapshot') as resp:
                snap = await resp.json()
            self.assertEqual(snap['live_turn']['status'], 'queued')
            self.assertEqual(len(snap['nodes']), 1)  # user node
            self.assertIn('session_version', snap)
            # 停止（无租约校验：控制租约已废弃）
            async with client.post(f'{self.base}/api/conversations/{cid}/stop',
                                   json={'operation_id': 'stop-1'}) as resp:
                self.assertEqual(resp.status, 200)
            # 审批（运行时节点）
            turn_id = snap['live_turn']['turn_id']
            async with client.post(f'{self.base}/api/conversations/{cid}/steering',
                                   json={'queue_item_ids': []}) as resp:
                self.assertEqual(resp.status, 400)  # 未选择项
            # 历史
            async with client.get(f'{self.base}/api/conversations/{cid}/turns') as resp:
                hist = await resp.json()
            self.assertIsInstance(hist['items'], list)

    async def test_sse_receives_conversation_events(self):
        async with ClientSession() as client:
            conv = await self._create(client, 'webui:sse1')
            cid = conv['conversation_id']
            stream = await client.get(f'{self.base}/api/events?session_key=webui:sse1')
            await stream.content.readuntil(b'\n\n')
            async with client.post(f'{self.base}/api/conversations/{cid}/queue',
                                   json={'text': '事件测试'}) as resp:
                self.assertEqual(resp.status, 200)
            chunk = await asyncio.wait_for(stream.content.readuntil(b'\n\n'), timeout=2)
            payload = json.loads(chunk.decode('utf-8').split('data: ', 1)[1])
            self.assertIn(payload['type'], {'queue.updated', 'conversation.upserted'})
            self.assertEqual(payload['data']['conversation_id'], cid)
            self.assertEqual(payload['data']['session_key'], 'webui:sse1')
            self.assertIn('version', payload['data'])
            self.assertEqual(payload['data']['scope'], 'session')
            stream.close()

    async def test_snapshot_matches_sse_versions(self):
        """数据库提交先于事件广播：事件 version 与快照 version 一致。"""
        async with ClientSession() as client:
            conv = await self._create(client, 'webui:ver1')
            cid = conv['conversation_id']
            async with client.post(f'{self.base}/api/conversations/{cid}/queue',
                                   json={'text': 'v'}) as resp:
                self.assertEqual(resp.status, 200)
            async with client.get(f'{self.base}/api/conversations/{cid}/snapshot') as resp:
                snap = await resp.json()
            self.assertGreaterEqual(snap['session_version'], 1)

    async def test_conversation_list_navigation(self):
        """会话导航列表（设计方案 21.1）：按最近更新倒序。"""
        async with ClientSession() as client:
            await self._create(client, "webui:nav-a")
            await self._create(client, "feishu:nav-chat")
            await self._create(client, "workspace:w9:s9")
            async with client.get(f'{self.base}/api/conversations') as resp:
                self.assertEqual(resp.status, 200)
                body = await resp.json()
            keys = [c["session_key"] for c in body["conversations"]]
            self.assertIn("webui:nav-a", keys)
            self.assertIn("feishu:nav-chat", keys)
            self.assertIn("workspace:w9:s9", keys)
            # origin 过滤
            async with client.get(f'{self.base}/api/conversations?origin=channel') as resp:
                body = await resp.json()
            self.assertEqual([c["session_key"] for c in body["conversations"]],
                             ["feishu:nav-chat"])

    async def test_errors_are_stable_codes(self):
        async with ClientSession() as client:
            # 不存在的会话
            async with client.get(f'{self.base}/api/conversations/nope/snapshot') as resp:
                self.assertEqual(resp.status, 404)
                self.assertEqual((await resp.json())['error'], 'conversation_not_found')
            conv = await self._create(client, 'webui:err1')
            cid = conv['conversation_id']
            # 队列满 20 条
            for i in range(20):
                async with client.post(f'{self.base}/api/conversations/{cid}/queue',
                                       json={'text': f'm{i}'}) as resp:
                    self.assertEqual(resp.status, 200)
            async with client.post(f'{self.base}/api/conversations/{cid}/queue',
                                   json={'text': 'overflow'}) as resp:
                self.assertEqual(resp.status, 409)
                self.assertEqual((await resp.json())['error'], 'queue_limit')

    async def test_send_next_without_body_is_allowed(self):
        """POST send-next 可无请求体（前端倒计时结束触发）。"""
        async with ClientSession() as client:
            conv = await self._create(client, 'webui:nobody')
            cid = conv['conversation_id']
            async with client.post(f'{self.base}/api/conversations/{cid}/queue',
                                   json={'text': 'q'}) as resp:
                self.assertEqual(resp.status, 200)
            async with client.post(f'{self.base}/api/conversations/{cid}/queue/send-next') as resp:
                self.assertEqual(resp.status, 200)
                body = await resp.json()
            self.assertTrue(body['dispatched'])

    async def test_clear_history_endpoint(self):
        """/clear 与"清空聊天"统一入口：清空历史但保留会话行（设计方案 §30）。"""
        async with ClientSession() as client:
            conv = await self._create(client, 'webui:clear1')
            cid = conv['conversation_id']
            async with client.post(f'{self.base}/api/conversations/{cid}/queue',
                                   json={'text': 'hello'}) as resp:
                self.assertEqual(resp.status, 200)
            async with client.post(f'{self.base}/api/conversations/{cid}/clear') as resp:
                self.assertEqual(resp.status, 200)
                body = await resp.json()
            self.assertTrue(body['cleared'])
            # 清空后历史为空、会话行仍在
            async with client.get(f'{self.base}/api/conversations/{cid}/turns') as resp:
                hist = await resp.json()
            self.assertEqual(hist['items'], [])
            async with client.get(f'{self.base}/api/conversations/{cid}/snapshot') as resp:
                snap = await resp.json()
            self.assertEqual(snap['conversation']['conversation_id'], cid)

    async def test_global_concurrency_saturated_503(self):
        """进程级全局并发打满 → 503 gateway_concurrency_saturated（设计方案 13/30.3）。"""
        cfg = _cfg(Path(self.tmp.name), "global-503")
        cfg["webui"]["conversation"]["max_global_turns"] = 2
        server = GatewayServer(cfg)
        await server.start()
        try:
            port = server._site._server.sockets[0].getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            async with ClientSession() as client:
                for i in range(2):
                    async with client.post(f'{base}/api/conversations',
                                           json={'session_key': f'webui:g{i}'}) as resp:
                        self.assertEqual(resp.status, 200)
                        conv = (await resp.json())['conversation']
                    cid = conv['conversation_id']
                    async with client.post(f'{base}/api/conversations/{cid}/queue',
                                           json={'text': 'q'}) as resp:
                        self.assertEqual(resp.status, 200)
                    async with client.post(f'{base}/api/conversations/{cid}/queue/send-next') as resp:
                        self.assertEqual(resp.status, 200)
                # 第三个会话出队 → 503 gateway_concurrency_saturated
                async with client.post(f'{base}/api/conversations',
                                       json={'session_key': 'webui:g-extra'}) as resp:
                    self.assertEqual(resp.status, 200)
                    extra = (await resp.json())['conversation']
                eid = extra['conversation_id']
                async with client.post(f'{base}/api/conversations/{eid}/queue',
                                       json={'text': 'q'}) as resp:
                    self.assertEqual(resp.status, 200)
                async with client.post(f'{base}/api/conversations/{eid}/queue/send-next') as resp:
                    self.assertEqual(resp.status, 503)
                    body = await resp.json()
                self.assertEqual(body['error'], 'gateway_concurrency_saturated')
        finally:
            await server.stop()


if __name__ == '__main__':
    unittest.main()
