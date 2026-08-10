import unittest

from aiohttp import web

from gateway.webui import WebUIModule, _parse_allowed_networks


class _Request:
    def __init__(self, remote, headers=None):
        self.path = "/api/status"
        self.remote = remote
        self.headers = headers or {}
        self.host = "agent.example"


class WebUiIpAllowlistTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _module(*, allowed_ips=None, auth_token=""):
        module = object.__new__(WebUIModule)
        module.config = {"allow_non_loopback": True}
        module._auth_token = auth_token
        module._allowed_networks = _parse_allowed_networks(allowed_ips or [])
        return module

    async def test_allowlist_accepts_single_ip_and_cidr(self):
        module = self._module(allowed_ips=["192.168.1.10", "10.20.0.0/16"])

        async def ok(_request):
            return web.Response(status=204)

        self.assertEqual(
            (await module._guard_middleware(_Request("192.168.1.10"), ok)).status,
            204,
        )
        self.assertEqual(
            (await module._guard_middleware(_Request("10.20.3.9"), ok)).status,
            204,
        )

    async def test_allowlist_rejects_other_client_without_bearer_fallback(self):
        module = self._module(allowed_ips=["10.20.0.0/16"], auth_token="old-token")

        async def ok(_request):
            return web.Response(status=204)

        response = await module._guard_middleware(
            _Request("10.21.3.9", {"Authorization": "Bearer old-token"}), ok)
        self.assertEqual(response.status, 403)

    async def test_token_is_retained_when_no_allowlist_is_configured(self):
        module = self._module(auth_token="secret")

        async def ok(_request):
            return web.Response(status=204)

        response = await module._guard_middleware(
            _Request("10.21.3.9", {"Authorization": "Bearer secret"}), ok)
        self.assertEqual(response.status, 204)

    def test_invalid_allowlist_entry_is_rejected_at_startup(self):
        with self.assertRaisesRegex(ValueError, "无效地址"):
            _parse_allowed_networks(["not-an-ip"])
