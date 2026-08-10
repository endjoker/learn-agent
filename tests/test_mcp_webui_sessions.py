import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.mcp_client import MCPConnection, MCPTransport
from gateway.channels.base import InboundMessage
from gateway.session import SessionManager
from gateway.webui.api_chat import _make_plan, _make_plan_approve
from gateway.webui.channel import WebuiChannel
from gateway.webui.events import EventBus


class FakeTransport(MCPTransport):
    def __init__(self):
        self.connected = False
        self.sent = []
        self.incoming = asyncio.Queue()

    @property
    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def close(self):
        self.connected = False
        await self.incoming.put(None)

    async def send(self, message):
        self.sent.append(json.loads(message))
        sent = self.sent[-1]
        if sent.get("method") == "initialize":
            await self.incoming.put(json.dumps({"jsonrpc": "2.0", "id": sent["id"], "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"},
            }}))
        elif sent.get("method") == "tools/list":
            await self.incoming.put(json.dumps({"jsonrpc": "2.0", "id": sent["id"], "result": {"tools": [{"name": "search"}]}}))
        elif sent.get("method") == "tools/call":
            await self.incoming.put(json.dumps({"jsonrpc": "2.0", "id": sent["id"], "error": {"message": "bad args"}}))

    async def receive(self):
        return await self.incoming.get()


class FakeRequest:
    def __init__(self, body=None, plan_id=""):
        self._body = body or {}
        self.match_info = {"plan_id": plan_id}

    async def json(self):
        return self._body


class McpWebUiAndSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_initialize_discovery_and_error_result(self):
        transport = FakeTransport()
        connection = MCPConnection("fake", transport)
        capabilities = await connection.initialize()
        tools = await connection.discover_tools()
        failure = await connection.call_tool("search", {"q": 1})
        await connection.close()

        self.assertEqual(capabilities, {"tools": {}})
        self.assertEqual(tools, [{"name": "search"}])
        self.assertTrue(failure["isError"])
        self.assertEqual([m["method"] for m in transport.sent], ["initialize", "initialized", "tools/list", "tools/call"])

    async def test_webui_channel_stream_events_and_future_reply(self):
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        _, queue = bus.subscribe()
        channel = WebuiChannel(bus)
        message = InboundMessage(channel="webui", session_key="s", user_id="u", user_name="U", text="hi", message_id="m")
        future = channel.register_future("s", "m")
        channel.publish_agent_event(message, {"type": "text_delta", "data": {"text": "Hel"}})
        event = await asyncio.wait_for(queue.get(), timeout=1)
        await channel.send_reply(message, "Hello")

        self.assertEqual(event["type"], "chat.text_delta")
        self.assertEqual(event["data"], {"text": "Hel", "session_key": "s", "message_id": "m"})
        self.assertEqual(await future, "Hello")

    async def test_plan_preview_then_approval_enqueues_apply(self):
        enqueued = []
        glue = SimpleNamespace(
            create_plan=lambda key, text, preview: "plan-1",
            take_plan=lambda plan_id: {"session_key": "s", "text": "build", "plan_text": "1. test"},
        )
        entry = SimpleNamespace(pending_plan=None)
        module = SimpleNamespace(
            glue=glue,
            session_mgr=SimpleNamespace(get_or_create=lambda key: entry),
            dispatcher=SimpleNamespace(on_inbound=lambda msg: enqueued.append(msg) or asyncio.sleep(0)),
        )
        preview_handler = _make_plan(module)
        with patch("gateway.webui.api_chat.send_and_wait", return_value={
            "ok": True, "reply": json.dumps({"ok": True, "plan_text": "1. test", "tasks": [{"id": 1}]})
        }) as send:
            response = await preview_handler(FakeRequest({"text": "build", "session_key": "s"}))
        payload = json.loads(response.text)
        approval = await _make_plan_approve(module)(FakeRequest(plan_id="plan-1"))

        self.assertEqual(send.call_args.args[1:3], ("s", "/plan-preview build"))
        self.assertEqual(payload["plan_id"], "plan-1")
        self.assertEqual(approval.status, 202)
        self.assertEqual(entry.pending_plan["plan_text"], "1. test")
        self.assertEqual(enqueued[0].text, "/plan-apply")

    async def test_session_capacity_and_eviction_callbacks(self):
        events = []
        manager = SessionManager(max_sessions=1, persist=False, worker_pool_size=1)
        manager.on_created.append(lambda key, reason="": events.append(("created", key)))
        manager.on_evicted.append(lambda key, reason="": events.append((reason, key)))
        entry = manager.get_or_create("one")
        self.assertIs(manager.get_or_create("two"), None)
        self.assertTrue(await manager.evict("one", save=False))
        manager._executor.shutdown(wait=True)

        self.assertEqual(entry.session_key, "one")
        self.assertEqual(events, [("created", "one"), ("evict", "one")])
