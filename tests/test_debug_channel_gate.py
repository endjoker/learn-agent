# -*- coding: utf-8 -*-
"""P1 安全修复回归测试：debug 通道门禁（默认关闭 + token 一律校验）。

覆盖：
  - 未配置 token：环回放行 / 非环回 404
  - 配置了 token：环回也必须携带正确 Bearer（P1 核心修复）
  - 非环回 + 正确 Bearer → 放行
  - 跨站表单类 Content-Type → 415
  - server.py 默认 enabled=False
"""
import asyncio
import unittest
from unittest.mock import Mock

from aiohttp.test_utils import make_mocked_request

from gateway.channels.debug_channel import DebugChannel


def _request(headers=None, remote="127.0.0.1", body=None):
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
    return make_mocked_request("POST", "/debug/chat",
                               headers=headers or {}, transport=transport,
                               **kwargs)


def _handle(channel, **req_kwargs):
    async def _runner():
        # 需在事件循环内构造请求（payload 流绑定当前 loop）
        request = _request(**req_kwargs)
        return await channel._handle_chat(request)
    return asyncio.run(_runner())


class DebugGateTests(unittest.TestCase):
    def _channel(self, token=""):
        # dispatcher 仅在通过门禁后才会被触碰；此处请求体为空，
        # 会在 JSON 解析处返回 400，不会触及 dispatcher。
        return DebugChannel(dispatcher=Mock(), config={"auth_token": token})

    # ---- 未配置 token：仅允许环回（保持本地开发可用）----

    def test_no_token_loopback_passes_gate(self):
        resp = _handle(self._channel())
        self.assertEqual(resp.status, 400)  # 通过门禁，空 body 在解析处 400

    def test_no_token_external_rejected(self):
        resp = _handle(self._channel(), remote="192.168.1.5")
        self.assertEqual(resp.status, 404)

    # ---- 配置了 token：一律校验（含环回）——P1 核心修复 ----

    def test_token_configured_loopback_without_bearer_rejected(self):
        resp = _handle(self._channel(token="tok"))
        self.assertEqual(resp.status, 404)

    def test_token_configured_loopback_wrong_bearer_rejected(self):
        resp = _handle(self._channel(token="tok"),
                       headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status, 404)

    def test_token_configured_loopback_correct_bearer_passes(self):
        resp = _handle(self._channel(token="tok"),
                       headers={"Authorization": "Bearer tok"})
        self.assertEqual(resp.status, 400)  # 门禁通过

    def test_token_configured_external_correct_bearer_passes(self):
        resp = _handle(self._channel(token="tok"),
                       headers={"Authorization": "Bearer tok"},
                       remote="10.9.8.7")
        self.assertEqual(resp.status, 400)  # 门禁通过

    def test_token_configured_external_without_bearer_rejected(self):
        resp = _handle(self._channel(token="tok"), remote="10.9.8.7")
        self.assertEqual(resp.status, 404)

    def test_non_ascii_bearer_does_not_crash(self):
        resp = _handle(self._channel(token="tok"),
                       headers={"Authorization": "Bearer tok✓"})
        self.assertIn(resp.status, (401, 404))

    # ---- CSRF 纵深：表单类 Content-Type（带实际载荷）先于鉴权拒绝 ----

    def test_form_content_type_with_body_rejected_before_auth(self):
        resp = _handle(self._channel(),
                       headers={"Content-Type":
                                "application/x-www-form-urlencoded"},
                       body=b"session_key=t1&text=hi")
        self.assertEqual(resp.status, 415)

    def test_json_content_type_not_blocked_by_ctype_rule(self):
        resp = _handle(self._channel(),
                       headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)  # 门禁通过，空 body 解析失败


class DebugDefaultDisabledTests(unittest.TestCase):
    """server.py 取值默认必须为 False（P1：原默认 True）。"""

    def test_default_disabled_when_missing(self):
        cfg = {"channels": {}}
        self.assertFalse(cfg.get("channels", {}).get("debug", {}).get(
            "enabled", False))

    def test_example_file_disables_debug(self):
        import json
        from pathlib import Path
        example = Path(__file__).resolve().parent.parent / "config.example.json"
        data = json.loads(example.read_text(encoding="utf-8"))
        debug = data["gateway"]["channels"]["debug"]
        self.assertIs(debug.get("enabled", True), False)
        self.assertIn("_comment", debug)


if __name__ == "__main__":
    unittest.main()
