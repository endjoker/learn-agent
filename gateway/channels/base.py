# -*- coding: utf-8 -*-
"""
Channel 插件基础接口 + 入站消息数据类
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    """入站消息（从各平台统一抽象）"""
    channel: str          # "feishu" / "weixin" / "debug"
    session_key: str      # "feishu:{chat_id}" / "weixin:{from_user}" / "debug:{key}"
    user_id: str          # 平台用户标识
    user_name: str        # 用户显示名
    text: str             # 消息文本
    message_id: str       # 平台消息 ID（去重用）
    raw: Any = None       # 原始平台消息对象（回复时需要）
    is_group: bool = False  # 是否群聊
    images: list = field(default_factory=list)  # 多模态图片块列表


class Channel(ABC):
    """Channel 插件抽象基类"""

    name: str = "base"
    handles_chunking: bool = False  # True = 平台内部自行处理长文本分片

    @abstractmethod
    async def start(self) -> None:
        """启动 channel（可能启动 daemon 线程）"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """优雅停止 channel"""
        ...

    @abstractmethod
    async def send_reply(self, msg: InboundMessage, text: str) -> None:
        """向用户发送回复文本"""
        ...

    def status(self) -> dict:
        """返回 channel 状态（/health 聚合用）"""
        return {"name": self.name, "status": "unknown"}
