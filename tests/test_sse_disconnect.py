import asyncio
import unittest
from unittest.mock import patch

from gateway.webui.events import EventBus, SSEHandler


class _Response:
    def __init__(self):
        self.writes = []

    async def prepare(self, _request):
        return self

    async def write(self, data):
        self.writes.append(data)
        if data == b": ping\n\n":
            raise ConnectionResetError("client closed")


class SSEDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_during_heartbeat_is_normal_completion(self):
        bus = EventBus()
        handler = SSEHandler(bus)
        response = _Response()

        with patch("gateway.webui.events.web.StreamResponse", return_value=response), \
             patch("gateway.webui.events._SSE_PING_INTERVAL", 0):
            returned = await handler.handle(object())

        self.assertIs(returned, response)
        self.assertEqual(response.writes, [b": connected\n\n", b": ping\n\n"])
        self.assertEqual(bus._subs, {})
