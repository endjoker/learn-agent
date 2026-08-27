# -*- coding: utf-8 -*-
"""飞书统一 runner 回复路径：重建的 InboundMessage 无 raw，必须仍能回复。

回归：ConversationTurnRunner 重建渠道回复 msg 时不带 lark 原始对象（raw）也不带
route_chat_id，FeishuChannel._do_reply 因 raw 缺失直接返回 False → 飞书收不到回复。
修复：无 raw 时优先用 message_id 在线程内回复，失败再按 session_key 解析 chat_id
主动发送。
"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from gateway.channels.base import InboundMessage
from gateway.channels.feishu_channel import FeishuChannel


class _ReplyResp:
    code = 0
    msg = "ok"
    def success(self):
        return True


class _FailResp:
    code = 230002
    msg = "message expired"
    def success(self):
        return False


class _Builder:
    def __init__(self, cls_name=""):
        self._kw = {}
        self._cls_name = cls_name
    def __getattr__(self, name):
        def setter(v):
            self._kw[name] = v
            return self
        return setter
    def build(self):
        return (self._cls_name, self._kw)


def _fake_sdk_class(cls_name):
    class C:
        @staticmethod
        def builder():
            return _Builder(cls_name)
    return C


class FakeClient:
    def __init__(self, resp=None):
        self.reply_calls = []
        self.create_calls = []
        self._resp = resp or _ReplyResp()
        self.im = SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(
            reply=self._reply, create=self._create)))
    def _reply(self, req):
        self.reply_calls.append(req)
        return self._resp
    def _create(self, req):
        self.create_calls.append(req)
        return self._resp


class FeishuReplyTests(unittest.TestCase):
    def _channel(self, client):
        ch = FeishuChannel({"app_id": "x", "app_secret": "y"}, dispatcher=None)
        ch._client = object()  # 让 send_reply 判定 _client 已建立
        ch._get_api_client = lambda: client
        return ch

    def _patch_sdk(self):
        p1 = mock.patch("lark_oapi.api.im.v1.ReplyMessageRequestBody",
                        _fake_sdk_class("ReplyMessageRequestBody"))
        p2 = mock.patch("lark_oapi.api.im.v1.ReplyMessageRequest",
                        _fake_sdk_class("ReplyMessageRequest"))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_nonraw_msg_replies_via_message_id(self):
        # 统一 runner 重建的 msg：无 raw，但有 message_id + session_key。
        # 审计修复：nonraw 路径升级为 Card 2.0 优先（Markdown 渲染），降级 text。
        client = FakeClient()
        ch = self._channel(client)
        self._patch_sdk()
        msg = InboundMessage(channel="feishu", session_key="feishu:oc_abc",
                             user_id="u1", user_name="U", text="hi", message_id="om_123", metadata={})
        ok = ch._do_reply(msg, "答复")
        self.assertTrue(ok)
        # 走 reply API（传 message_id），而非创建新消息
        self.assertEqual(len(client.reply_calls), 1)
        req = client.reply_calls[0]
        self.assertEqual(req[1]["message_id"], "om_123")
        self.assertEqual(req[1]["request_body"][1]["msg_type"], "interactive")

    def test_nonraw_card_failure_falls_back_to_push_and_text(self):
        # 卡片线程回复失败 → chat_id 卡片主动推送（降级链第一层）
        client = FakeClient(resp=_FailResp())
        ch = self._channel(client)
        self._patch_sdk()
        p3 = mock.patch("lark_oapi.api.im.v1.CreateMessageRequestBody",
                        _fake_sdk_class("CreateMessageRequestBody"))
        p4 = mock.patch("lark_oapi.api.im.v1.CreateMessageRequest",
                        _fake_sdk_class("CreateMessageRequest"))
        p3.start(); p4.start()
        self.addCleanup(p3.stop); self.addCleanup(p4.stop)
        msg = InboundMessage(channel="feishu", session_key="feishu:oc_abc",
                             user_id="u1", user_name="U", text="hi", message_id="om_123", metadata={})
        ch._do_reply(msg, "答复")  # 全链路失败的构造：断言降级链按序尝试
        # 线程内 interactive 回复尝试过（失败）
        types = [req[1]["request_body"][1]["msg_type"] for req in client.reply_calls]
        self.assertIn("interactive", types)
        # chat_id 卡片主动推送也尝试过（降级链第一层，Create 调用）
        self.assertTrue(client.create_calls)

    def test_nonraw_msg_without_message_id_falls_back_to_send_to_chat(self):
        client = FakeClient()
        ch = self._channel(client)
        # nonraw 分支现在走 send_card_to_chat（卡片优先）；mock 实例方法拦截
        sent = []
        ch.send_card_to_chat = lambda cid, text: sent.append((cid, text)) or True
        # 无 message_id，但 session_key 可解析 chat_id
        msg = InboundMessage(channel="feishu", session_key="feishu:oc_group1",
                             user_id="u1", user_name="U", text="hi", message_id="", metadata={})
        ok = ch._do_reply(msg, "答复")
        self.assertTrue(ok)
        self.assertEqual(sent, [("oc_group1", "答复")])

    def test_nonraw_msg_with_no_identity_returns_false(self):
        client = FakeClient()
        ch = self._channel(client)
        msg = InboundMessage(channel="feishu", session_key="", user_id="u1", user_name="U", text="hi",
                             message_id="", metadata={})
        ok = ch._do_reply(msg, "答复")
        self.assertFalse(ok)

    def test_raw_path_still_sends_card(self):
        # raw 存在时走原卡片路径（md_to_feishu_card 需要 mock）
        import gateway.channels.feishu_channel as fc
        client = FakeClient()
        ch = self._channel(client)
        self._patch_sdk()
        msg = InboundMessage(channel="feishu", session_key="feishu:oc_abc",
                             user_id="u1", user_name="U", text="hi", message_id="om_123", raw=object())
        with mock.patch.object(fc, "md_to_feishu_card",
                               return_value={"config": {}, "i18n": {}}):
            ok = ch._do_reply(msg, "答复")
        self.assertTrue(ok)
        self.assertEqual(len(client.reply_calls), 1)


if __name__ == "__main__":
    unittest.main()
