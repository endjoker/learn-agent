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


def _feishu_chat_id_from_session(session_key: str) -> str:
    """从会话 key 解析飞书 chat_id（统一 runner 重建的 msg 不带 raw/metadata 路由字段）。

    会话 key 约定为 ``feishu:{chat_id}``（见 ``_on_message``）。
    """
    if session_key.startswith("feishu:"):
        return session_key[len("feishu:"):]
    return ""


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
        # L1-B4/C5：入站消息队列 + 消费 worker（主循环内创建）。
        # SDK 回调线程只做解析 + 轻量入队；重活由 worker 协程串行消费。
        self._queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None

    async def start(self):
        """启动飞书 WS 长连接（daemon 线程）"""
        if not self.app_id or not self.app_secret:
            raise ValueError("飞书 app_id/app_secret 未配置")

        self._loop = asyncio.get_event_loop()
        self._running = True
        # L1-B4/C5：队列 + worker 在主循环内创建（回调线程只入队，不阻塞等待）
        if self._worker_task is None:
            self._queue = asyncio.Queue()
            self._worker_task = asyncio.create_task(
                self._message_worker(), name="feishu-inbound-worker")
        self._thread = threading.Thread(
            target=self._run_ws_client,
            name="jkagent-feishu",
            daemon=True,
        )
        self._thread.start()

    async def stop(self):
        self._running = False
        if self._worker_task is not None:
            task = self._worker_task
            self._worker_task = None
            self._queue = None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # lark WS client 没有显式 stop，daemon 线程随主进程退出

    async def send_reply(self, msg: InboundMessage, text: str):
        """通过飞书 API 回复消息"""
        if not self._client:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._do_reply, msg, text)

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
        """解析飞书富文本（post）消息，提取文字和图片 key（不下载）。

        返回 (text, image_keys)。图片下载是重活，由主循环 worker 经
        asyncio.to_thread 执行（L1-B4/C5），回调线程只做轻量解析。
        兼容三种段落列表 JSON 结构：
          格式 C（实际）: {"title": "", "content": [[...]]}
          格式 A: {"post": {"zh_cn": {"content": [...]}}}
          格式 B: {"zh_cn": {"content": [...]}}
        """
        text_parts = []
        image_keys = []
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
                            image_keys.append(image_key)
                    elif tag == "at":
                        pass  # @ 提及，跳过

            text = "".join(text_parts).strip()
            logger.info(f"飞书 post 解析结果: text={text[:100]!r}, images={len(image_keys)}")
            return text, image_keys
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
            from core.config_loader import _find_project_root
            # 锚定项目根而非 CWD（agent 创建后会 os.chdir(workspace)）
            tmp_dir = _find_project_root() / "workspace" / "tmp"
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

    async def _handle_image_message(self, msg_obj, pending_key: str) -> None:
        """处理飞书纯图片消息：下载 → 缓存 → 询问用户意图。

        L1-B4/C5：在主循环 worker 协程中执行（阻塞的下载/快捷回复经
        asyncio.to_thread 移出 SDK 接收线程），不再 future.result()
        同步等待，也不阻塞回调线程。
        """
        try:
            content = json.loads(msg_obj.content)
            image_key = content.get("image_key", "")
            if not image_key:
                await asyncio.to_thread(
                    self._reply_sync, msg_obj.message_id, "🙏 无法获取图片")
                return

            img_block = await asyncio.to_thread(
                self._download_feishu_image, msg_obj.message_id, image_key)
            if not img_block:
                await asyncio.to_thread(
                    self._reply_sync, msg_obj.message_id, "🙏 图片下载失败")
                return

            self._evict_pending()
            _, blocks = self._pending_images.get(pending_key) or (time.time(), [])
            blocks.append(img_block)
            self._pending_images[pending_key] = (time.time(), blocks)
            await asyncio.to_thread(
                self._reply_sync, msg_obj.message_id, "📷 收到图片，你想让我做什么？")
        except Exception as e:
            logger.error(f"飞书图片处理异常: {e}", exc_info=True)
            await asyncio.to_thread(
                self._reply_sync, msg_obj.message_id, "🙏 图片处理失败")

    def _on_message(self, data) -> None:
        """飞书消息回调（在 feishu daemon 线程中执行）。

        L1-B4/C5：回调只做轻量解析 + 入队（asyncio.Queue.put_nowait，
        经 call_soon_threadsafe 投递到主循环，无界队列不会阻塞）；图片下载、
        快捷回复（_reply_sync）、_handle_image_message、dispatcher.on_inbound
        等重活全部由主循环 worker 协程（_message_worker）串行消费
        （阻塞调用经 asyncio.to_thread 执行），不再 future.result() 同步
        等待（删除 120s 等待路径），消息按到达顺序保序。
        """
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

            if self._queue is None or not (self._loop and self._loop.is_running()):
                # 队列未就绪（start 未完成 / 循环已关闭）：与旧行为一致丢弃
                logger.warning("飞书入站消息丢弃：消息队列未就绪")
                return

            pending_key = self._pending_key(chat_id, user_id)

            # ---- 图片消息：只入队，下载/缓存/询问意图在 worker 中执行 ----
            if msg_type == "image":
                self._enqueue({
                    "kind": "image",
                    "msg_obj": msg_obj,
                    "pending_key": pending_key,
                })
                return

            # ---- 富文本（post）：只解析文字 + 收集图片 key（不下载）----
            images_from_post = []
            if msg_type == "post":
                text, images_from_post = self._parse_post_content(msg_obj)
                if not text and not images_from_post:
                    self._enqueue({
                        "kind": "reply",
                        "message_id": msg_obj.message_id,
                        "text": "🙏 无法解析富文本内容",
                    })
                    return
            elif msg_type == "text":
                # 解析纯文本
                content = json.loads(msg_obj.content)
                text = content.get("text", "").strip()
                if not text:
                    return
            else:
                self._enqueue({
                    "kind": "reply",
                    "message_id": msg_obj.message_id,
                    "text": "🙏 暂时只能看懂文字、图片和富文本消息",
                })
                return

            # 群聊：剥 mentions 占位，纯 @ 忽略
            if is_group:
                import re
                # 飞书群 @bot 消息格式: "@_user_1 实际内容"
                text = re.sub(r'@_user_\d+\s*', '', text).strip()
                if not text and not images_from_post:
                    return  # 纯 @ 无内容

            self._enqueue({
                "kind": "message",
                "msg_obj": msg_obj,
                "session_key": session_key,
                "user_id": user_id,
                "user_name": user_name,
                "chat_id": chat_id,
                "is_group": is_group,
                "pending_key": pending_key,
                "text": text,
                "image_keys": images_from_post,
            })
        except Exception as e:
            logger.error("飞书消息处理异常: %s", e, exc_info=True)

    def _enqueue(self, item: dict) -> None:
        """线程安全轻量入队：call_soon_threadsafe + 无界 Queue.put_nowait。

        回调线程只做这一步（不阻塞、不等待）；主循环 worker 串行消费，
        到达顺序即处理顺序（含同一会话内消息保序）。
        """
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            # 循环关闭竞态：入队失败，与旧行为一致丢弃
            logger.error("飞书消息入队失败: 事件循环已关闭")

    async def _message_worker(self) -> None:
        """主循环内的消息消费 worker：串行处理 _on_message 入队的消息。

        全局 FIFO 队列 + 单 worker ⇒ 到达顺序即处理顺序（会话保序）。
        重活（图片下载 / _reply_sync / _handle_image_message / on_inbound）
        经 asyncio.to_thread 或直接 await 完成，SDK 接收线程永不阻塞等待。
        """
        queue = self._queue
        if queue is None:
            return
        while True:
            item = await queue.get()
            try:
                await self._process_message(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("飞书消息处理异常: %s", e, exc_info=True)
            finally:
                queue.task_done()

    async def _process_message(self, item: dict) -> None:
        """处理一条已入队的消息（worker 协程，主循环内执行）。"""
        kind = item.get("kind")

        if kind == "reply":
            # 快捷提示回复（非文本消息 / 解析失败等）
            await asyncio.to_thread(
                self._reply_sync, item["message_id"], item["text"])
            return

        if kind == "image":
            await self._handle_image_message(item["msg_obj"], item["pending_key"])
            return

        # ---- 常规文字/富文本消息：下载 post 图片 → 认领待缓存图片 → 分发 ----
        msg_obj = item["msg_obj"]
        images_from_post = []
        for image_key in item["image_keys"]:
            img_block = await asyncio.to_thread(
                self._download_feishu_image, msg_obj.message_id, image_key)
            if img_block:
                images_from_post.append(img_block)

        # 附加缓存的图片（同一用户先发图后发文字的场景）+ post 中的图片
        pending = []
        entry = self._pending_images.pop(item["pending_key"], None)
        if entry:
            ts, blocks = entry
            if time.time() - ts <= self._PENDING_TTL:
                pending = blocks
        all_images = pending + images_from_post

        inbound = InboundMessage(
            channel="feishu",
            session_key=item["session_key"],
            user_id=item["user_id"],
            user_name=item["user_name"],
            text=item["text"],
            message_id=msg_obj.message_id or "",
            raw=msg_obj,
            is_group=item["is_group"],
            images=all_images,
            metadata={"route_chat_id": item["chat_id"], "route_user_id": item["user_id"]},
        )

        # 已在主循环内：直接 await，无需 run_coroutine_threadsafe / future 等待
        if self.dispatcher is not None:
            try:
                await self.dispatcher.on_inbound(inbound)
            except Exception as e:
                logger.error("飞书入站消息处理异常: %s", e, exc_info=True)

    def _do_reply(self, msg: InboundMessage, text: str):
        """同步回复（在线程池中执行）—— 优先 Card 2.0，降级 text"""
        if not self._client:
            return False
        # 统一 runner（ConversationTurnRunner）重建的 msg：不带 lark 原始对象，
        # 也缺 route_chat_id 元数据（只有 session_key）。否则会因 raw 缺失而找不到
        # chat_id 直接返回 False → 飞书收不到任何回复。优先级：
        #   ① metadata.route_chat_id（恢复重放路径）→ 主动发送；
        #   ② message_id（正常渠道会话回复）→ 线程内 reply；
        #   ③ session_key（feishu:{chat_id}）→ 主动发送。
        if not msg.raw:
            # 统一 runner（ConversationTurnRunner）重建的 msg：优先 Card 2.0 渲染
            # （Markdown/代码块/表格在纯 text 消息里原样输出，观感差——审计反馈），
            # 卡片失败/超限时降级为纯文本分片。
            meta = getattr(msg, "metadata", {}) or {}
            chat_id = meta.get("route_chat_id", "")
            message_id = getattr(msg, "message_id", "") or ""
            if message_id and self._reply_card_by_message_id(message_id, text):
                return True
            if not chat_id:
                chat_id = _feishu_chat_id_from_session(
                    getattr(msg, "session_key", "") or "")
            if not chat_id:
                logger.warning("飞书回复失败：无法解析 chat_id（raw 缺失且无会话 key）")
                return False
            return self.send_card_to_chat(chat_id, text)

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
                return False
        return True

    def _reply_card_by_message_id(self, message_id: str, text: str) -> bool:
        """用消息 ID 在飞书线程内回复 Card 2.0（Markdown 渲染）。

        统一 runner 重建的 msg 没有 raw，此前该路径发纯 text——Markdown 原样
        输出观感差（审计反馈）。卡片构建失败/超限（30KB）/发送失败时返回
        False，由调用方降级纯文本。"""
        try:
            from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

            card = md_to_feishu_card(text)
            card_json = json.dumps(card, ensure_ascii=False)
            if len(card_json.encode("utf-8")) >= 28000:
                logger.info("飞书卡片超限（%d bytes），降级纯文本分片",
                            len(card_json.encode("utf-8")))
                return False
            body = ReplyMessageRequestBody.builder() \
                .msg_type("interactive") \
                .content(card_json) \
                .build()
            req = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body) \
                .build()
            resp = self._get_api_client().im.v1.message.reply(req)
            if resp.success():
                return True
            logger.warning("飞书卡片回复失败: code=%d msg=%s，降级纯文本",
                           resp.code, resp.msg)
            return False
        except Exception as exc:
            logger.error("飞书卡片回复异常: %s", exc)
            return False

    def send_card_to_chat(self, chat_id: str, text: str) -> bool:
        """主动推送 Card 2.0 到指定会话；失败降级纯文本主动发送。"""
        if not chat_id:
            logger.warning("飞书卡片主动发送失败: chat_id 为空")
            return False
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            card = md_to_feishu_card(text)
            card_json = json.dumps(card, ensure_ascii=False)
            if len(card_json.encode("utf-8")) >= 28000:
                return self.send_to_chat(chat_id, text)
            body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type("interactive") \
                .content(card_json) \
                .build()
            req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body) \
                .build()
            resp = self._get_api_client().im.v1.message.create(req)
            if resp.success():
                return True
            logger.warning("飞书卡片主动发送失败: code=%d，降级纯文本", resp.code)
            return self.send_to_chat(chat_id, text)
        except Exception as exc:
            logger.error("飞书卡片主动发送异常: %s", exc)
            return self.send_to_chat(chat_id, text)

    def _reply_text_by_message_id(self, client, message_id: str, text: str) -> bool:
        """用消息 ID 在飞书线程内回复一条纯文本消息。

        返回是否成功。统一 runner 重建的 msg 没有 ``raw``（只有 message_id +
        session_key），此路径保证回复能被发出。
        """
        try:
            from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

            body = ReplyMessageRequestBody.builder() \
                .msg_type("text") \
                .content(json.dumps({"text": text})) \
                .build()
            req = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body) \
                .build()
            resp = client.im.v1.message.reply(req)
            if resp.success():
                return True
            logger.warning("飞书消息ID回复失败: code=%d msg=%s", resp.code, resp.msg)
            return False
        except Exception as exc:
            logger.error("飞书消息ID回复异常: %s", exc)
            return False

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

    def send_to_chat(self, chat_id: str, text: str) -> bool:
        """主动向指定会话推送（Card 2.0 优先，失败降级纯文本）。

        定时任务 announce 投递用——合成消息没有 msg.raw，
        走不通 reply API 与 _do_create_message 的 raw 取值路径。
        """
        if not chat_id:
            logger.warning("飞书主动发送失败: chat_id 为空")
            return False
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            client = self._get_api_client()

            # 卡片优先（Markdown 渲染）；超限/失败降级纯文本
            card = md_to_feishu_card(text)
            card_json = json.dumps(card, ensure_ascii=False)
            if len(card_json.encode("utf-8")) < 28000:
                body = CreateMessageRequestBody.builder() \
                    .receive_id(chat_id) \
                    .msg_type("interactive") \
                    .content(card_json) \
                    .build()
                req = CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(body) \
                    .build()
                resp = client.im.v1.message.create(req)
                if resp.success():
                    return True
                logger.warning("飞书卡片主动发送失败: code=%d，降级纯文本", resp.code)

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
                return False
            return True
        except Exception as e:
            logger.error("飞书主动发送异常: %s", e)
            return False

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
            logger.warning("飞书快捷回复失败: message_id=%s",
                           message_id, exc_info=True)
