# -*- coding: utf-8 -*-
"""
飞书 Channel —— lark-oapi WS 长连接模式

依赖: pip install lark-oapi>=1.7.0
注意: 必须在 daemon 线程内 lazy import lark_oapi，
      因为其 WS 客户端 import 时会捕获事件循环，
      与主线程的 aiohttp 循环冲突。
"""

import asyncio
import json
import logging
import threading
from typing import Optional

from gateway.channels.base import Channel, InboundMessage
from gateway.textutil import split_text, md_to_feishu_card

logger = logging.getLogger("hello_agent.gateway.feishu")


class FeishuChannel(Channel):
    """飞书消息通道（WebSocket 长连接）"""

    name = "feishu"
    handles_chunking = True   # 飞书内部用卡片构建 + _send_text_split 自行处理分片

    def __init__(self, config: dict, dispatcher):
        self.config = config
        self.dispatcher = dispatcher
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.encrypt_key = config.get("encrypt_key", "")
        self.verification_token = config.get("verification_token", "")
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

    async def start(self):
        """启动飞书 WS 长连接（daemon 线程）"""
        if not self.app_id or not self.app_secret:
            raise ValueError("飞书 app_id/app_secret 未配置")

        self._loop = asyncio.get_event_loop()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_ws_client,
            name="hello-agent-feishu",
            daemon=True,
        )
        self._thread.start()

    async def stop(self):
        self._running = False
        # lark WS client 没有显式 stop，daemon 线程随主进程退出

    async def send_reply(self, msg: InboundMessage, text: str):
        """通过飞书 API 回复消息"""
        if not self._client:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._do_reply, msg, text)

    def status(self) -> dict:
        return {
            "name": self.name,
            "status": "running" if self._running else "stopped",
            "mode": "websocket",
            "thread_alive": self._thread.is_alive() if self._thread else False,
        }

    def _run_ws_client(self):
        """daemon 线程入口：lazy import + WS 长连接"""
        try:
            # 必须在线程内 lazy import，避免抢主循环
            import lark_oapi as lark

            # 构建事件处理器
            handler = lark.EventDispatcherHandler.builder(
                self.encrypt_key, self.verification_token
            ).register_p2_im_message_receive_v1(
                self._on_message
            ).build()

            # WS 长连接客户端（阻塞）
            self._client = lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
            )
            logger.info("飞书 WS 客户端启动中…")
            self._client.start()  # 阻塞，SDK 自带 auto_reconnect
        except ImportError:
            logger.error("缺少 lark-oapi 依赖: pip install lark-oapi")
        except Exception as e:
            logger.error("飞书 WS 客户端异常: %s", e, exc_info=True)

    def _on_message(self, data) -> None:
        """飞书消息回调（在 feishu daemon 线程中执行）"""
        try:
            event = data.event
            msg_obj = event.message
            sender = event.sender

            # 忽略 bot 自己的消息（防自环）
            if sender and sender.sender_id and sender.sender_type == "app":
                return

            # 只处理文本消息
            if msg_obj.message_type != "text":
                self._reply_sync(msg_obj.message_id, "🙏 暂时只能看懂文字消息")
                return

            # 解析文本
            content = json.loads(msg_obj.content)
            text = content.get("text", "").strip()
            if not text:
                return

            # 群聊：剥 mentions 占位，纯 @ 忽略
            chat_type = msg_obj.chat_type or ""
            is_group = chat_type == "group"
            if is_group:
                # 飞书群 @bot 消息格式: "@_user_1 实际内容"
                import re
                text = re.sub(r'@_user_\d+\s*', '', text).strip()
                if not text:
                    return  # 纯 @ 无内容

            user_id = ""
            user_name = "飞书用户"
            if sender and sender.sender_id:
                user_id = sender.sender_id.open_id or ""

            chat_id = msg_obj.chat_id or ""
            session_key = f"feishu:{chat_id}"

            inbound = InboundMessage(
                channel="feishu",
                session_key=session_key,
                user_id=user_id,
                user_name=user_name,
                text=text,
                message_id=msg_obj.message_id or "",
                raw=msg_obj,
                is_group=is_group,
            )

            # 桥接回主循环
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.dispatcher.on_inbound(inbound), self._loop
                )
        except Exception as e:
            logger.error("飞书消息处理异常: %s", e, exc_info=True)

    def _do_reply(self, msg: InboundMessage, text: str):
        """同步回复（在线程池中执行）—— 优先 Card 2.0，降级 text"""
        if not self._client:
            return

        # 提取 meta 信息（dispatcher 通过 InboundMessage.raw 传递）
        meta = getattr(msg, '_reply_meta', {}) or {}
        model = meta.get("model", "")
        elapsed = meta.get("elapsed", 0)

        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

            client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()

            # 尝试 Card 2.0
            card = md_to_feishu_card(text, model=model, elapsed=elapsed)
            card_json = json.dumps(card, ensure_ascii=False)

            # 飞书卡片 30KB 限制，超限降级 text 分片
            if len(card_json.encode("utf-8")) < 28000:
                body = ReplyMessageRequestBody.builder() \
                    .msg_type("interactive") \
                    .content(card_json) \
                    .build()
                req = ReplyMessageRequest.builder() \
                    .message_id(msg.message_id) \
                    .request_body(body) \
                    .build()
                resp = client.im.v1.message.reply(req)
                if not resp.success():
                    logger.warning("飞书卡片回复失败: code=%d, msg=%s，降级为纯文本",
                                   resp.code, resp.msg)
                    self._send_text_split(client, msg, text)
            else:
                # 卡片超限，降级为纯文本分片
                self._send_text_split(client, msg, text)
        except Exception as e:
            logger.error("飞书回复异常: %s", e)
            # 降级为纯文本
            try:
                self._do_reply_text_fallback(msg, text)
            except Exception:
                pass

    def _send_text_split(self, client, msg: InboundMessage, text: str):
        """纯文本分片发送（卡片降级时使用）"""
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        chunks = split_text(text, 3500)
        for i, chunk in enumerate(chunks):
            body = ReplyMessageRequestBody.builder() \
                .msg_type("text") \
                .content(json.dumps({"text": chunk})) \
                .build()
            req = ReplyMessageRequest.builder() \
                .message_id(msg.message_id) \
                .request_body(body) \
                .build()
            resp = client.im.v1.message.reply(req)
            if not resp.success():
                if i == 0 and resp.code == 230002 and msg.raw:
                    # 首片 message_id 过期，降级为主动发送
                    self._do_create_message(msg, chunk)
                else:
                    logger.warning("飞书文本分片发送失败: code=%d, chunk=%d",
                                   resp.code, i)

    def _do_reply_text_fallback(self, msg: InboundMessage, text: str):
        """纯文本降级回复"""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        body = ReplyMessageRequestBody.builder() \
            .msg_type("text") \
            .content(json.dumps({"text": text})) \
            .build()
        req = ReplyMessageRequest.builder() \
            .message_id(msg.message_id) \
            .request_body(body) \
            .build()
        client.im.v1.message.reply(req)

    def _do_create_message(self, msg: InboundMessage, text: str):
        """降级：主动发送消息（message_id 过期时）"""
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()

            receive_id_type = "chat_id"
            receive_id = msg.raw.chat_id if msg.raw else ""

            body = CreateMessageRequestBody.builder() \
                .receive_id(receive_id) \
                .msg_type("text") \
                .content(json.dumps({"text": text})) \
                .build()

            req = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(body) \
                .build()

            resp = client.im.v1.message.create(req)
            if not resp.success():
                logger.warning("飞书主动发送也失败: code=%d", resp.code)
        except Exception as e:
            logger.error("飞书主动发送异常: %s", e)

    def _reply_sync(self, message_id: str, text: str):
        """同步快捷回复（用于非文本消息提示等）"""
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

            client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()

            body = ReplyMessageRequestBody.builder() \
                .msg_type("text") \
                .content(json.dumps({"text": text})) \
                .build()

            req = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body) \
                .build()

            client.im.v1.message.reply(req)
        except Exception:
            pass
