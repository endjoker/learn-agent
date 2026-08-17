# -*- coding: utf-8 -*-
"""
微信 Channel —— 基于 iLink 协议（weixin-ilink SDK）

依赖: pip install weixin-ilink[qr]>=0.3.5
登录: python agent.py gateway login weixin
"""

import asyncio
import logging
import os
import threading
from typing import Optional

from gateway.channels.base import Channel, InboundMessage
from gateway.textutil import split_text, md_to_plain

logger = logging.getLogger("jk_agent.gateway.weixin")


class WeixinChannel(Channel):
    """微信消息通道（iLink 协议长轮询）"""

    name = "weixin"

    def __init__(self, config: dict, dispatcher):
        self.config = config
        self.dispatcher = dispatcher
        self.credentials_file = config.get(
            "credentials_file", "gateway/creds/weixin.json"
        )
        self.allow_from = config.get("allow_from", [])
        self.reply_format = config.get("reply_format", "markdown")
        self._thread: Optional[threading.Thread] = None
        self._bot = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

    async def start(self):
        """启动微信 channel（daemon 线程）"""
        # 检查凭据
        if not os.path.exists(self.credentials_file):
            raise FileNotFoundError(
                f"微信凭据文件不存在: {self.credentials_file}\n"
                f"请先运行: python agent.py gateway login weixin"
            )

        self._loop = asyncio.get_event_loop()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_bot,
            name="jkagent-weixin",
            daemon=True,
        )
        self._thread.start()

    async def stop(self):
        self._running = False
        if self._bot:
            try:
                self._bot.stop()
            except Exception:
                pass

    async def send_reply(self, msg: InboundMessage, text: str):
        """通过微信发送回复"""
        if not self._bot:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._do_send, msg, text)

    def status(self) -> dict:
        return {
            "name": self.name,
            "status": "running" if self._running else "stopped",
            "credentials": os.path.exists(self.credentials_file),
            "thread_alive": self._thread.is_alive() if self._thread else False,
        }

    def _run_bot(self):
        """daemon 线程入口：初始化 SDK + 长轮询"""
        try:
            from weixin_ilink import WeixinBot

            self._bot = WeixinBot(credentials_file=self.credentials_file)
            self._bot.on_message(self._on_message)
            logger.info("微信 Bot 启动中（凭据: %s）", self.credentials_file)
            # WeixinBot.run() installs SIGINT/SIGTERM handlers. This channel
            # deliberately runs in a gateway worker thread, where CPython
            # forbids signal.signal(). Use the SDK's public blocking iterator
            # instead; stop() still terminates the iterator through bot.stop().
            self._run_poll_loop()
        except ImportError:
            logger.error("缺少 weixin-ilink 依赖: pip install weixin-ilink[qr]")
        except Exception as e:
            logger.error("微信 Bot 异常: %s", e, exc_info=True)
            self._running = False

    def _run_poll_loop(self):
        """Run iLink long polling without process-global signal registration."""
        if not self._bot:
            return
        for message in self._bot.messages():
            if not self._running:
                break
            self._on_message(message)

    def _on_message(self, msg) -> None:
        """微信消息回调（在 weixin daemon 线程中执行）"""
        try:
            # ACL 检查
            if self.allow_from:
                from_user = getattr(msg, 'from_user', '') or ''
                if from_user not in self.allow_from:
                    logger.debug("微信 ACL 拒绝: %s", from_user)
                    return

            # 只处理文本和语音（ASR 转写）
            is_text = getattr(msg, 'is_text', False)
            is_voice = getattr(msg, 'is_voice', False)

            if not is_text and not is_voice:
                # 图片/文件/视频等礼貌拒绝
                from_user = getattr(msg, 'from_user', '')
                if from_user and self._bot:
                    self._bot.send_text(from_user, "🙏 暂时只能处理文字消息")
                return

            text = getattr(msg, 'text', '') or ''
            text = text.strip()
            if not text:
                return

            from_user = getattr(msg, 'from_user', '') or ''
            from_name = getattr(msg, 'from_name', '') or '微信用户'
            msg_id = getattr(msg, 'msg_id', '') or ''

            # 发送"正在输入"状态
            if self._bot and from_user:
                try:
                    self._bot.send_typing(from_user)
                except Exception:
                    pass

            inbound = InboundMessage(
                channel="weixin",
                session_key=f"weixin:{from_user}",
                user_id=from_user,
                user_name=from_name,
                text=text,
                message_id=msg_id,
                raw=msg,
                is_group=False,  # iLink 不支持群聊
            )

            # 桥接回主循环
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.dispatcher.on_inbound(inbound), self._loop
                )
        except Exception as e:
            logger.error("微信消息处理异常: %s", e, exc_info=True)

    def _do_send(self, msg: InboundMessage, text: str):
        """同步发送（在线程池中执行）"""
        if not self._bot:
            return
        try:
            user_id = msg.user_id
            if not user_id:
                return
            # Markdown → 纯文本（微信不支持富文本）
            plain = md_to_plain(text)
            # 分片发送
            max_len = 1500
            chunks = split_text(plain, max_len)
            for chunk in chunks:
                if self.reply_format == "markdown":
                    # SDK 内置的微信兼容 markdown 过滤
                    try:
                        self._bot.send_markdown(user_id, chunk)
                    except (AttributeError, Exception):
                        self._bot.send_text(user_id, chunk)
                else:
                    self._bot.send_text(user_id, chunk)
        except Exception as e:
            logger.error("微信发送失败: %s", e)
            # errcode=-14 通常意味着 token 过期
            if "-14" in str(e):
                logger.warning("微信 token 可能已过期，请重新扫码: python agent.py gateway login weixin")
