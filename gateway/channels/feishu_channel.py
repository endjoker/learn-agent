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
import time
from typing import Optional

from core.protocols.vision import _REJECT_SIZE
from gateway.channels.base import Channel, InboundMessage
from gateway.textutil import split_text, md_to_feishu_card

logger = logging.getLogger("jk_agent.gateway.feishu")


class FeishuChannel(Channel):
    """飞书消息通道（WebSocket 长连接）"""

    name = "feishu"
    handles_chunking = True   # 飞书内部用卡片构建 + _send_text_split 自行处理分片

    _PENDING_TTL = 1800            # 待认领图片缓存有效期（秒）
    _PENDING_MAX = 50              # 最多缓存多少个会话的待认领图片
    _TMP_KEEP_SECONDS = 7 * 86400  # workspace/tmp 下载图片保留 7 天

    def __init__(self, config: dict, dispatcher):
        self.config = config
        self.dispatcher = dispatcher
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.encrypt_key = config.get("encrypt_key", "")
        self.verification_token = config.get("verification_token", "")
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._api_client = None  # 懒加载的 lark API client（回复/下载各调用点共享）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._pending_images: dict[str, tuple[float, list]] = {}  # pending_key → (时间戳, 图片块列表)

    async def start(self):
        """启动飞书 WS 长连接（daemon 线程）"""
        if not self.app_id or not self.app_secret:
            raise ValueError("飞书 app_id/app_secret 未配置")

        self._loop = asyncio.get_event_loop()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_ws_client,
            name="jkagent-feishu",
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

    def _get_api_client(self):
        """懒加载并缓存 lark API client（所有回复/下载调用点共享同一实例）"""
        if self._api_client is None:
            import lark_oapi as lark
            self._api_client = (
                lark.Client.builder()
                .app_id(self.app_id)
                .app_secret(self.app_secret)
                .build()
            )
        return self._api_client

    @staticmethod
    def _pending_key(chat_id: str, user_id: str) -> str:
        """图片待领取缓存键：精确到用户，避免群聊中 A 的图被附到 B 的消息上"""
        return f"feishu:{chat_id}:{user_id}"

    def _evict_pending(self) -> None:
        """清理过期/超量的待领取图片缓存"""
        now = time.time()
        expired = [k for k, (ts, _) in self._pending_images.items()
                   if now - ts > self._PENDING_TTL]
        for k in expired:
            self._pending_images.pop(k, None)
        while len(self._pending_images) > self._PENDING_MAX:
            oldest = min(self._pending_images,
                         key=lambda k: self._pending_images[k][0])
            self._pending_images.pop(oldest, None)

    def _run_ws_client(self):
        """daemon 线程入口：lazy import + WS 长连接 + 断线重连（#3）

        lark SDK 的 receive 循环在 keepalive ping 超时时会退出（1011），
        start() 返回后线程即结束、连接永久丢失。这里在 start() 返回/异常后
        按退避间隔重建 client 重启，保证长连接自愈。
        """
        try:
            import lark_oapi as lark
        except ImportError:
            logger.error("缺少 lark-oapi 依赖: pip install lark-oapi")
            return

        backoff = 5
        while self._running:
            try:
                # 构建事件处理器
                handler = lark.EventDispatcherHandler.builder(
                    self.encrypt_key, self.verification_token
                ).register_p2_im_message_receive_v1(
                    self._on_message
                ).register_p2_im_chat_member_bot_added_v1(
                    self._on_bot_added
                ).register_p2_im_chat_member_bot_deleted_v1(
                    self._on_bot_removed
                ).build()

                # WS 长连接客户端（阻塞）
                self._client = lark.ws.Client(
                    self.app_id,
                    self.app_secret,
                    event_handler=handler,
                    log_level=lark.LogLevel.INFO,
                )
                logger.info("飞书 WS 客户端启动中…")
                self._client.start()  # 阻塞；receive 循环退出即返回
                if self._running:
                    logger.warning("飞书 WS 连接断开（keepalive/内部错误），%ss 后重连", backoff)
            except Exception as e:
                logger.error("飞书 WS 客户端异常: %s", e, exc_info=True)
            if not self._running:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    @staticmethod
    def _on_bot_added(data) -> None:
        """机器人被加入群聊（空处理，防止 SDK 报 processor not found）"""
        logger.debug("飞书: 机器人被加入群聊")

    @staticmethod
    def _on_bot_removed(data) -> None:
        """机器人被移出群聊（空处理，防止 SDK 报 processor not found）"""
        logger.debug("飞书: 机器人被移出群聊")

    def _parse_post_content(self, msg_obj) -> tuple[str, list]:
        """解析飞书富文本（post）消息，提取文字和图片。

        返回 (text, image_blocks)。
        兼容三种段落列表 JSON 结构：
          格式 C（实际）: {"title": "", "content": [[...]]}
          格式 A: {"post": {"zh_cn": {"content": [...]}}}
          格式 B: {"zh_cn": {"content": [...]}}
        """
        text_parts = []
        image_blocks = []
        try:
            content = json.loads(msg_obj.content)
            logger.info(f"飞书 post content keys: {list(content.keys())}")

            # 定位段落列表（三种格式依次尝试）
            paragraphs = None
            if isinstance(content.get("content"), list):
                # 格式 C（实际）：顶层直接 {"title": "", "content": [[...]]}
                paragraphs = content["content"]
            else:
                # 格式 A：嵌套在 "post" 下；格式 B：顶层即语言字典 —— 依次遍历候选字典
                for src in (content.get("post"), content):
                    if not isinstance(src, dict):
                        continue
                    for lang_data in src.values():
                        if isinstance(lang_data, dict) and isinstance(lang_data.get("content"), list):
                            paragraphs = lang_data["content"]
                            break
                    if paragraphs:
                        break

            if not paragraphs:
                logger.warning(f"飞书 post 无法定位段落: {json.dumps(content, ensure_ascii=False)[:500]}")
                return "", []

            for paragraph in paragraphs:
                for element in paragraph:
                    if not isinstance(element, dict):
                        continue
                    tag = element.get("tag", "")
                    if tag == "text":
                        text_parts.append(element.get("text", ""))
                    elif tag == "a":
                        text_parts.append(element.get("text", element.get("href", "")))
                    elif tag == "img":
                        image_key = element.get("image_key", "")
                        if image_key:
                            img_block = self._download_feishu_image(
                                msg_obj.message_id, image_key
                            )
                            if img_block:
                                image_blocks.append(img_block)
                    elif tag == "at":
                        pass  # @ 提及，跳过

            text = "".join(text_parts).strip()
            logger.info(f"飞书 post 解析结果: text={text[:100]!r}, images={len(image_blocks)}")
            return text, image_blocks
        except Exception as e:
            logger.error(f"解析富文本失败: {e}", exc_info=True)
            return "", []

    def _download_feishu_image(self, message_id: str, image_key: str) -> dict | None:
        """下载飞书图片，返回 image block 或 None"""
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            client = self._get_api_client()
            req = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()
            resp = client.im.v1.message_resource.get(req)
            if not resp.success() or not resp.file:
                logger.warning(f"飞书图片下载失败: image_key={image_key}")
                return None

            data = resp.file.read()
            if len(data) > _REJECT_SIZE:
                logger.warning(
                    f"飞书图片过大（{len(data) / 1024 / 1024:.1f}MB > 20MB），已跳过: "
                    f"image_key={image_key}"
                )
                return None

            from pathlib import Path
            tmp_dir = Path("workspace/tmp").resolve()
            tmp_dir.mkdir(parents=True, exist_ok=True)
            self._prune_tmp_images(tmp_dir)
            img_path = tmp_dir / f"feishu_{message_id}_{image_key[:8]}.png"
            img_path.write_bytes(data)
            logger.info(f"飞书图片已下载: {img_path}")
            return {"type": "image", "source": "file", "path": str(img_path.resolve())}
        except Exception as e:
            logger.error(f"飞书图片下载异常: {e}", exc_info=True)
            return None

    def _prune_tmp_images(self, tmp_dir) -> None:
        """清理 workspace/tmp 下超过保留期的下载图片，防止磁盘无限增长"""
        now = time.time()
        for old in tmp_dir.glob("feishu_*.png"):
            try:
                if now - old.stat().st_mtime > self._TMP_KEEP_SECONDS:
                    old.unlink()
            except OSError:
                pass

    def _handle_image_message(self, msg_obj, pending_key: str) -> None:
        """处理飞书纯图片消息：下载 → 缓存 → 询问用户意图"""
        try:
            content = json.loads(msg_obj.content)
            image_key = content.get("image_key", "")
            if not image_key:
                self._reply_sync(msg_obj.message_id, "🙏 无法获取图片")
                return

            img_block = self._download_feishu_image(msg_obj.message_id, image_key)
            if not img_block:
                self._reply_sync(msg_obj.message_id, "🙏 图片下载失败")
                return

            self._evict_pending()
            _, blocks = self._pending_images.get(pending_key) or (time.time(), [])
            blocks.append(img_block)
            self._pending_images[pending_key] = (time.time(), blocks)
            self._reply_sync(msg_obj.message_id, "📷 收到图片，你想让我做什么？")
        except Exception as e:
            logger.error(f"飞书图片处理异常: {e}", exc_info=True)
            self._reply_sync(msg_obj.message_id, "🙏 图片处理失败")

    def _on_message(self, data) -> None:
        """飞书消息回调（在 feishu daemon 线程中执行）"""
        try:
            event = data.event
            msg_obj = event.message
            sender = event.sender

            # 忽略 bot 自己的消息（防自环）
            if sender and sender.sender_id and sender.sender_type == "app":
                return

            chat_type = msg_obj.chat_type or ""
            is_group = chat_type == "group"
            user_id = ""
            user_name = "飞书用户"
            if sender and sender.sender_id:
                user_id = sender.sender_id.open_id or ""
            chat_id = msg_obj.chat_id or ""
            session_key = f"feishu:{chat_id}"

            msg_type = (msg_obj.message_type or "").lower()
            logger.info(f"飞书消息类型: {msg_type} (原始: {msg_obj.message_type})")

            # ---- 图片消息：下载 + 缓存 + 询问意图 ----
            pending_key = self._pending_key(chat_id, user_id)
            if msg_type == "image":
                self._handle_image_message(msg_obj, pending_key)
                return

            # ---- 富文本（post）：提取文字 + 图片 ----
            images_from_post = []
            if msg_type == "post":
                text, images_from_post = self._parse_post_content(msg_obj)
                if not text and not images_from_post:
                    self._reply_sync(msg_obj.message_id, "🙏 无法解析富文本内容")
                    return
            elif msg_type == "text":
                # 解析纯文本
                content = json.loads(msg_obj.content)
                text = content.get("text", "").strip()
                if not text:
                    return
            else:
                self._reply_sync(msg_obj.message_id, "🙏 暂时只能看懂文字、图片和富文本消息")
                return

            # 群聊：剥 mentions 占位，纯 @ 忽略
            if is_group:
                import re
                # 飞书群 @bot 消息格式: "@_user_1 实际内容"
                text = re.sub(r'@_user_\d+\s*', '', text).strip()
                if not text and not images_from_post:
                    return  # 纯 @ 无内容

            # 附加缓存的图片（同一用户先发图后发文字的场景）+ post 中的图片
            pending = []
            entry = self._pending_images.pop(pending_key, None)
            if entry:
                ts, blocks = entry
                if time.time() - ts <= self._PENDING_TTL:
                    pending = blocks
            all_images = pending + images_from_post

            inbound = InboundMessage(
                channel="feishu",
                session_key=session_key,
                user_id=user_id,
                user_name=user_name,
                text=text,
                message_id=msg_obj.message_id or "",
                raw=msg_obj,
                is_group=is_group,
                images=all_images,
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
            from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

            client = self._get_api_client()

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
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        client = self._get_api_client()
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
        chat_id = msg.raw.chat_id if msg.raw else ""
        self.send_to_chat(chat_id, text)

    def send_to_chat(self, chat_id: str, text: str):
        """主动向指定会话推送文本。

        定时任务 announce 投递用——合成消息没有 msg.raw，
        走不通 reply API 与 _do_create_message 的 raw 取值路径。
        """
        if not chat_id:
            logger.warning("飞书主动发送失败: chat_id 为空")
            return
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            client = self._get_api_client()

            body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type("text") \
                .content(json.dumps({"text": text})) \
                .build()

            req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body) \
                .build()

            resp = client.im.v1.message.create(req)
            if not resp.success():
                logger.warning("飞书主动发送失败: code=%d", resp.code)
        except Exception as e:
            logger.error("飞书主动发送异常: %s", e)

    def _reply_sync(self, message_id: str, text: str):
        """同步快捷回复（用于非文本消息提示等）"""
        try:
            from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

            client = self._get_api_client()

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
