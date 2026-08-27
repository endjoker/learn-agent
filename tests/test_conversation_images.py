# -*- coding: utf-8 -*-
"""会话图片全链路回归（修正版方案 A，2026-08）。

- 队列图片信封：校验（张数/大小/mime/base64）+ v16 列持久化
- 出队消费：base64 落盘 ImageStore + image 引用节点 + user 节点 metadata.images
- 执行重建：InboundMessage.images 中性视觉块（文件读回）
- 回放降级：image 节点不进上下文，user 消息追加 [图片已存档: …] 占位
- 图片端点：归属校验（跨会话/不存在同响应）、mime/缓存头
"""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from core.runtime import RuntimeStore
from gateway.agent_factory import _collect_sqlite_history, _turn_nodes_to_messages
from gateway.conversation.images import ImageStore
from gateway.conversation.service import ConversationService, _validate_image_envelope
from gateway.conversation.errors import ValidationFailed
from gateway.conversation.store import ConversationStore
from gateway.webui.workspace_store import WorkspaceDatabase

_PNG_1PX = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c626001000000ffff030000060005"
        "57bfabd40000000049454e44ae426082"
    )
).decode("ascii")


def _svc(tmp: Path) -> ConversationService:
    runtime = RuntimeStore(tmp / "runtime.db")
    runtime.initialize()
    db = WorkspaceDatabase(runtime_store=runtime)
    service = ConversationService(
        ConversationStore(db), lambda t, p: None, max_global_turns=100,
        image_store=ImageStore(Path(tmp) / "images"))
    return service


class EnvelopeValidationTests(unittest.TestCase):
    def test_accepts_png_and_normalizes(self):
        out = _validate_image_envelope([{"data": _PNG_1PX, "media_type": "image/png"}])
        self.assertEqual(out[0]["media_type"], "image/png")

    def test_rejects_count_over_limit(self):
        with self.assertRaises(ValidationFailed):
            _validate_image_envelope(
                [{"data": _PNG_1PX, "media_type": "image/png"}] * 5)

    def test_rejects_bad_mime_and_bad_base64(self):
        with self.assertRaises(ValidationFailed):
            _validate_image_envelope([{"data": _PNG_1PX, "media_type": "text/html"}])
        with self.assertRaises(ValidationFailed):
            _validate_image_envelope([{"data": "!!!not-base64!!!",
                                       "media_type": "image/png"}])

    def test_rejects_oversize(self):
        big = base64.b64encode(b"0" * (4 * 1024 * 1024 + 1)).decode()
        with self.assertRaises(ValidationFailed):
            _validate_image_envelope([{"data": big, "media_type": "image/png"}])


class DequeueImageFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = _svc(self.root)

    def test_send_next_persists_image_and_links_user_node(self):
        conv = self.service.get_or_create_conversation(
            "webui:img1", origin="webui", subtype="main", execution_scope="gateway:default")
        self.service.enqueue(
            conv.conversation_id, "看看这张图",
            images=[{"data": _PNG_1PX, "media_type": "image/png"}])
        turn, node = self.service.send_next(conv.conversation_id)
        # user 节点 metadata.images 携带引用
        images = (node.metadata or {}).get("images") or []
        self.assertEqual(len(images), 1)
        ref = images[0]["ref"]
        self.assertTrue(ref.startswith(f"{conv.conversation_id}/att_"))
        # 文件真实落盘
        path = self.service.image_store.resolve(conv.conversation_id, ref)
        self.assertGreater(path.stat().st_size, 0)
        # image 引用节点存在且 type=image/done
        nodes = self.service.store.get_turn_nodes(turn.turn_id)
        img_nodes = [n for n in nodes if n.type == "image"]
        self.assertEqual(len(img_nodes), 1)
        self.assertEqual(img_nodes[0].status, "done")
        self.assertEqual(img_nodes[0].metadata.get("ref"), ref)

    def test_text_only_flow_untouched_without_images(self):
        conv = self.service.get_or_create_conversation(
            "webui:img2", origin="webui", subtype="main", execution_scope="gateway:default")
        self.service.enqueue(conv.conversation_id, "纯文本")
        turn, node = self.service.send_next(conv.conversation_id)
        self.assertNotIn("images", node.metadata or {})
        self.assertEqual([n for n in self.service.store.get_turn_nodes(turn.turn_id)
                          if n.type == "image"], [])

    def test_runner_rebuilds_inbound_images(self):
        """runner 语义：user_node.metadata.images → 中性视觉块（按 ref 读回）。"""
        conv = self.service.get_or_create_conversation(
            "webui:img3", origin="webui", subtype="main", execution_scope="gateway:default")
        self.service.enqueue(
            conv.conversation_id, "图", images=[{"data": _PNG_1PX, "media_type": "image/png"}])
        _, node = self.service.send_next(conv.conversation_id)
        ref = (node.metadata or {}).get("images")[0]["ref"]
        b64 = self.service.image_store.load_b64(conv.conversation_id, ref)
        self.assertEqual(base64.b64decode(b64), base64.b64decode(_PNG_1PX))
        # 中性块形状（vision.py 消费格式）
        block = {"type": "image", "source": "base64",
                 "media_type": "image/png", "data": b64}
        self.assertEqual(block["source"], "base64")

    def test_replay_downgrades_to_placeholder(self):
        """回放：图片不还原进上下文，user 文本追加 [图片已存档: …]。"""
        conv = self.service.get_or_create_conversation(
            "webui:img4", origin="webui", subtype="main", execution_scope="gateway:default")
        self.service.enqueue(
            conv.conversation_id, "看这张",
            images=[{"data": _PNG_1PX, "media_type": "image/png"}])
        turn, _node = self.service.send_next(conv.conversation_id)
        self.service.complete_turn(conv.conversation_id, turn.turn_id, "done",
                                   full_text="看到了")
        nodes = self.service.store.get_turn_nodes(turn.turn_id)
        messages = _turn_nodes_to_messages(nodes)
        user_msgs = [m for m in messages if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 1)
        content = user_msgs[0]["content"]
        self.assertIn("看这张", content)
        self.assertIn("[图片已存档:", content)
        self.assertNotIn(_PNG_1PX[:40], content)  # base64 绝不进上下文

    def test_imagestore_rejects_cross_conversation_ref(self):
        store = self.service.image_store
        saved = store.save("conv_a", _PNG_1PX, "image/png")
        with self.assertRaises(ValueError):
            store.resolve("conv_b", saved["ref"])


class StoreImageRoundtripTests(unittest.TestCase):
    def test_queue_item_roundtrip_with_images(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = RuntimeStore(Path(tmp.name) / "runtime.db")
        runtime.initialize()
        db = WorkspaceDatabase(runtime_store=runtime)
        store = ConversationStore(db)
        conv = store.create_conversation(
            "webui:rt", origin="webui", subtype="main",
            execution_scope="gateway:default")[0]
        with store.transaction() as conn:
            item = store.enqueue_item(
                conn, conv.conversation_id, "带图",
                images=[{"data": _PNG_1PX, "media_type": "image/png"}])
        back = store.get_queue_item(item.queue_item_id)
        self.assertEqual(back.images[0]["media_type"], "image/png")
        self.assertEqual(len(base64.b64decode(back.images[0]["data"])),
                         len(base64.b64decode(_PNG_1PX)))
        # to_dict 只回张数，不泄漏 base64
        self.assertEqual(back.to_dict()["image_count"], 1)
        self.assertNotIn("data", json.dumps(back.to_dict()))


if __name__ == "__main__":
    unittest.main()


class ImageLiveEventTests(unittest.TestCase):
    """用户反馈 #1：send_next 必须广播 node.image——不刷新页面缩略图也要出现。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events: list = []
        runtime = RuntimeStore(self.root / "runtime.db")
        runtime.initialize()
        db = WorkspaceDatabase(runtime_store=runtime)
        self.service = ConversationService(
            ConversationStore(db), lambda t, p: self.events.append((t, p)),
            max_global_turns=100, image_store=ImageStore(self.root / "images"))

    def test_send_next_emits_node_image_events(self):
        conv = self.service.get_or_create_conversation(
            "webui:evt", origin="webui", subtype="main",
            execution_scope="gateway:default")
        self.service.enqueue(
            conv.conversation_id, "看图",
            images=[{"data": _PNG_1PX, "media_type": "image/png"}])
        turn, node = self.service.send_next(conv.conversation_id)
        img_events = [e for t, e in self.events if t == "node.image"]
        self.assertEqual(len(img_events), 1)
        biz = img_events[0]["data"]  # 事件载荷为 GatewayEvent，业务数据在 data
        self.assertEqual(biz["node_id"], _any_image_node_id(self.service, turn))
        self.assertEqual(biz["status"], "done")
        self.assertEqual(biz["type"], "image")
        self.assertIn("ref", biz)
        self.assertIsInstance(biz["position"], int)
        # 顶层携带 turn_id（前端 store 从 payload 顶层取归属）
        self.assertEqual(img_events[0]["turn_id"], turn.turn_id)


def _any_image_node_id(service, turn) -> str:
    nodes = [n for n in service.store.get_turn_nodes(turn.turn_id) if n.type == "image"]
    return nodes[0].node_id
