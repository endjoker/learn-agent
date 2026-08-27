# -*- coding: utf-8 -*-
"""P2 安全修复回归测试：WebUI guard 中间件鉴权组合 + Origin/Content-Type。

覆盖：
  - Origin "null"（沙盒 iframe / file://）不再短路放行 → 403
  - 跨源 Origin → 403；同源 Origin（含端口一致）→ 放行；无 Origin → 放行
  - allowed_ips 与 auth_token 组合矩阵（AND 语义修复）
  - 变更类方法声明非 JSON Content-Type → 415（CSRF 纵深）
  - 环回来源零配置直通（回归红线：本机 http://127.0.0.1:9120/ui/ 不受影响）
"""
import asyncio
import ipaddress
import unittest
from unittest.mock import Mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from gateway.webui import WebUIModule, _origin_mismatch


def _request(method="GET", path="/api/status", headers=None,
             remote="127.0.0.1", body=None):
    transport = Mock()
    transport.get_extra_info.side_effect = (
        lambda name, default=None:
        (remote, 55000) if name == "peername" else default)
    kwargs = {}
    if body is not None:
        from aiohttp.streams import StreamReader
        reader = StreamReader(Mock(), limit=2 ** 16)
        reader.feed_data(body)
        kwargs["payload"] = reader
    return make_mocked_request(method, path, headers=headers or {},
                               transport=transport, **kwargs)


def _module(networks=(), token="", allow_non_loopback=True):
    m = object.__new__(WebUIModule)
    m.config = {"allow_non_loopback": allow_non_loopback}
    m._allowed_networks = tuple(
        ipaddress.ip_network(n) for n in networks)
    m._auth_token = token
    return m


async def _ok_handler(request):
    return web.json_response({"ok": True})


def _guard(module, **req_kwargs):
    async def _runner():
        # 需在事件循环内构造请求（payload 流绑定当前 loop）
        request = _request(**req_kwargs)
        return await module._guard_middleware(request, _ok_handler)
    return asyncio.run(_runner())


class GuardMatrixTests(unittest.TestCase):
    """非环回请求的 allowed_ips × auth_token 组合矩阵。"""

    def test_ip_only_mode_ip_hit_passes(self):
        resp = _guard(_module(networks=("10.0.0.0/8",)),
                      remote="10.1.2.3")
        self.assertEqual(resp.status, 200)

    def test_ip_only_mode_ip_miss_403(self):
        resp = _guard(_module(networks=("10.0.0.0/8",)),
                      remote="203.0.113.9")
        self.assertEqual(resp.status, 403)

    def test_token_only_mode(self):
        m = _module(token="tok")
        # 无头 / 错 token → 401；正确 Bearer → 200
        self.assertEqual(_guard(m, remote="203.0.113.9").status, 401)
        bad = {"Authorization": "Bearer wrong"}
        self.assertEqual(
            _guard(m, remote="203.0.113.9", headers=bad).status, 401)
        good = {"Authorization": "Bearer tok"}
        self.assertEqual(
            _guard(m, remote="203.0.113.9", headers=good).status, 200)

    def test_both_configured_require_and(self):
        """P2 核心修复：两者都配置时必须同时满足（旧实现是"或"）。"""
        nets = ("10.0.0.0/8",)
        good_tok = {"Authorization": "Bearer tok"}

        # IP 命中但无 token → 401（旧实现此处直接放行）
        m = _module(networks=nets, token="tok")
        self.assertEqual(_guard(m, remote="10.1.2.3").status, 401)

        # IP 命中 + 正确 token → 放行
        self.assertEqual(
            _guard(m, remote="10.1.2.3", headers=good_tok).status, 200)

        # IP 未命中 + 正确 token → 403（旧实现此处直接放行）
        self.assertEqual(
            _guard(m, remote="203.0.113.9", headers=good_tok).status, 403)

    def test_neither_configured_fail_closed(self):
        m = _module()
        resp = _guard(m, remote="203.0.113.9")
        self.assertEqual(resp.status, 401)


class LoopbackRegressionTests(unittest.TestCase):
    """回归红线：默认本机环回访问不受影响。"""

    def test_loopback_passes_without_token_or_ips(self):
        for token, nets in (( "", () ), ("tok", ("10.0.0.0/8",))):
            m = _module(token=token, networks=nets)
            self.assertEqual(_guard(m, remote="127.0.0.1").status, 200,
                             f"token={token!r}")

    def test_ipv6_mapped_loopback_passes(self):
        m = _module(token="tok")
        self.assertEqual(_guard(m, remote="::ffff:127.0.0.1").status, 200)

    def test_same_origin_browser_request_unaffected(self):
        """同源前端请求带同源 Origin，必须照常放行。"""
        host = "127.0.0.1:9120"
        headers = {
            "Host": host,
            "Origin": f"http://{host}",
            "Content-Type": "application/json",
            "Referer": f"http://{host}/ui/",
        }
        m = _module()
        self.assertEqual(
            _guard(m, method="POST", path="/api/config/llm",
                   headers=headers).status, 200)


class OriginCheckTests(unittest.TestCase):
    def test_origin_null_rejected_even_for_get(self):
        m = _module()
        resp = _guard(m, headers={"Host": "127.0.0.1:9120",
                                  "Origin": "null"})
        self.assertEqual(resp.status, 403)

    def test_cross_origin_rejected(self):
        m = _module()
        resp = _guard(m, headers={"Host": "127.0.0.1:9120",
                                  "Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)

    def test_malformed_origin_without_netloc_rejected(self):
        m = _module()
        resp = _guard(m, headers={"Host": "127.0.0.1:9120",
                                  "Origin": "file://"})
        self.assertEqual(resp.status, 403)

    def test_missing_origin_allowed(self):
        m = _module()
        self.assertEqual(_guard(m).status, 200)

    def test_origin_helper_unit_table(self):
        self.assertFalse(_origin_mismatch("", "127.0.0.1"))
        self.assertTrue(_origin_mismatch("null", "127.0.0.1"))
        self.assertTrue(_origin_mismatch("NULL", "127.0.0.1"))
        self.assertTrue(_origin_mismatch("http://evil.example", "127.0.0.1:9120"))
        self.assertFalse(_origin_mismatch("http://127.0.0.1:9120",
                                          "127.0.0.1:9120"))
        # 大小写不敏感比较
        self.assertFalse(_origin_mismatch("http://LOCALHOST:9120",
                                          "localhost:9120"))


class ContentTypeTests(unittest.TestCase):
    def _post(self, ctype=None, body=None):
        headers = {}
        if ctype is not None:
            headers["Content-Type"] = ctype
        return _guard(_module(), method="POST", path="/api/sessions/k/stop",
                      headers=headers, body=body)

    def test_form_urlencoded_with_body_rejected(self):
        """跨站表单（urlencoded + 实际载荷）→ 415。"""
        self.assertEqual(
            self._post("application/x-www-form-urlencoded",
                       body=b"goal=archive").status, 415)

    def test_multipart_with_body_rejected(self):
        body = (b"--x\r\nContent-Disposition: form-data; name=\"a\"\r\n"
                b"\r\n1\r\n--x--\r\n")
        self.assertEqual(
            self._post("multipart/form-data; boundary=x",
                       body=body).status, 415)

    def test_text_plain_with_body_rejected(self):
        self.assertEqual(self._post("text/plain", body=b"x=1").status, 415)

    def test_json_and_bodyless_pass(self):
        self.assertEqual(
            self._post("application/json", body=b'{"ok":1}').status, 200)
        self.assertEqual(
            self._post("application/json; charset=utf-8",
                       body=b'{}').status, 200)
        # 动作型端点无 body 不带 Content-Type：合法前端行为
        self.assertEqual(self._post(None).status, 200)
        # 无实际载荷的声明头（aiohttp 客户端空请求自动补 octet-stream）
        self.assertEqual(self._post("application/octet-stream").status, 200)

    def test_get_not_affected_by_content_type_rule(self):
        m = _module()
        resp = _guard(m, method="GET", path="/api/status",
                      headers={"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 200)

    def test_static_paths_not_affected(self):
        m = _module()
        resp = _guard(m, method="POST", path="/ui/",
                      headers={"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
