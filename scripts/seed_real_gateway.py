# -*- coding: utf-8 -*-
"""真实网关 → 真实页面 的种子脚本。

启动一个真实 GatewayServer（webui enabled，隔离临时 DB），再用其
conversation_bridge 模拟一次 plan runtime 事件序列，把 runtime 节点写入
父会话（webui:default）真实 SQLite。随后保持服务直到被终止，供 Playwright
直连真实 /api/conversations（不 mock）验证 plan 内联卡从中渲染。
"""
import asyncio
import tempfile
from pathlib import Path

from gateway.server import GatewayServer
from gateway.channels.base import InboundMessage


def cfg(tmp: Path, name: str) -> dict:
    return {
        'host': '127.0.0.1', 'port': 9120,
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


async def seed_plan(bridge, session_key: str) -> None:
    msg = InboundMessage(
        channel="webui", session_key=session_key, user_id="e2e",
        user_name="E2E", text="规划一个方案", message_id="seed-1",
        metadata={"task_source": "plan", "plan_id": "seed-plan"})
    bridge.on_agent_event(msg, {"type": "reasoning_delta",
                                "data": {"text": "分析需求：读取文件结构"}})
    bridge.on_agent_event(msg, {"type": "tool_call_start", "data": {
        "tool_call_id": "c1", "tool": "read"}})
    bridge.on_agent_event(msg, {"type": "tool_call_end", "data": {
        "tool_call_id": "c1", "tool": "read",
        "arguments": {"path": "a.txt"}}})
    bridge.on_agent_event(msg, {"type": "message_start", "data": {
        "role": "tool", "message_id": "result_c1", "tool": "read"}})
    bridge.on_agent_event(msg, {"type": "message_end", "data": {
        "role": "tool", "message_id": "result_c1", "tool": "read",
        "content": "total 8"}})
    bridge.on_reply(msg, "方案完成：读取 a.txt")


async def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    server = GatewayServer(cfg(Path(tmp.name), "realseed"))
    await server.start()
    print("READY", flush=True)
    await seed_plan(server.webui.conversation_bridge, "webui:default")
    print("SEEDED webui:default", flush=True)
    # start() 已绑定并开始服务；保持事件循环存活即可
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
