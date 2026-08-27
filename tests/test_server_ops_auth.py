# -*- coding: utf-8 -*-
"""P3 安全修复回归测试：/health 与 /metrics 配置 auth_token 时要求 Bearer。

未配置 token 时维持免鉴权现状（环回部署场景，探活不中断）。
"""
import asyncio
import unittest
from types import SimpleNamespace

from aiohttp.test_utils import make_mocked_request

from gateway.server import GatewayServer


def _server(token):
    s = object.__new__(GatewayServer)
    s._ops_auth_token = token
    s.dispatcher = SimpleNamespace(
        _channels={},
        metrics=lambda: {},
        metrics_prometheus=lambda: "# HELP jk_up up\n# TYPE jk_up gauge\njk_up 1\n")
    s.session_mgr = SimpleNamespace(
        active_count=lambda: 0,
        max_sessions=2,
        executor_stats=lambda: {})
    s.scheduler = None
    s.heartbeat = None
    return s


class OpsEndpointAuthTests(unittest.TestCase):
    def _run(self, coro_factory):
        return asyncio.run(coro_factory())

    def test_no_token_keeps_open_access(self):
        """未配置 token：维持现状免鉴权（回归红线）。"""
        s = _server("")
        for handler in (s._handle_health, s._handle_metrics):
            resp = self._run(lambda h=handler: h(make_mocked_request("GET", "/x")))
            self.assertEqual(resp.status, 200)

    def test_token_configured_requires_bearer(self):
        s = _server("ops-token")
        # 无 Authorization → 401
        for handler in (s._handle_health, s._handle_metrics):
            resp = self._run(lambda h=handler: h(
                make_mocked_request("GET", "/x",
                                    headers={"Host": "127.0.0.1"})))
            self.assertEqual(resp.status, 401)
        # 错误 token → 401
        headers = {"Authorization": "Bearer wrong"}
        for handler in (s._handle_health, s._handle_metrics):
            resp = self._run(lambda h=handler: h(
                make_mocked_request("GET", "/x", headers=headers)))
            self.assertEqual(resp.status, 401)

    def test_token_configured_correct_bearer_passes(self):
        s = _server("ops-token")
        headers = {"Authorization": "Bearer ops-token"}
        health = self._run(lambda: s._handle_health(
            make_mocked_request("GET", "/health", headers=headers)))
        self.assertEqual(health.status, 200)
        metrics = self._run(lambda: s._handle_metrics(
            make_mocked_request("GET", "/metrics", headers=headers)))
        self.assertEqual(metrics.status, 200)
        self.assertIn("jk_up", metrics.text)


if __name__ == "__main__":
    unittest.main()
