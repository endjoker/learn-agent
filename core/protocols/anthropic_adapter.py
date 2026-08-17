# -*- coding: utf-8 -*-
"""
Anthropic Messages API 协议适配器

包装 anthropic.Anthropic SDK，将内部 OpenAI 格式消息翻译为 Anthropic 格式。

翻译规则：
  1. system 消息从 messages 中提取，作为 API 的 system 参数
  2. 连续同 role 消息合并（Anthropic 要求 user/assistant 严格交替）
  3. name: "tool_result" 被合并为 user 消息的一部分
  4. 需要 max_tokens 参数（Anthropic 必填）
"""

import logging
from typing import Dict, Iterator, List, Optional

from anthropic import Anthropic

from .base import ChatResponse, ProtocolAdapter

logger = logging.getLogger('jk_agent')

# Anthropic 要求必传 max_tokens，默认值（足够覆盖大多数 ReAct 回复）
DEFAULT_MAX_TOKENS = 8192


class AnthropicAdapter(ProtocolAdapter):
    """
    Anthropic Messages API 协议适配器

    包装 anthropic.Anthropic SDK。
    消息翻译：
      - system → API system 参数（不在 messages 数组中）
      - 连续同 role → 合并
      - tool_result name → content 前缀标记
    """

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 timeout: int = 60, max_tokens: int = DEFAULT_MAX_TOKENS):
        super().__init__(api_key, base_url, timeout)
        self.max_tokens = max_tokens
        self.client = Anthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

    # ============================================================
    # 消息翻译
    # ============================================================

    def _prepare_messages(self, messages: List[Dict]) -> tuple[str, List[Dict]]:
        """
        将内部格式消息翻译为 Anthropic 格式。

        返回:
            (system_text, api_messages)

        其中 api_messages 仅包含 user/assistant 角色，且严格交替。
        """
        # 1. 提取 system 消息
        system_text, non_system = self._split_system_messages(messages)

        # 2. 合并连续同 role（Anthropic 要求交替）
        merged = self._merge_consecutive_same_role(non_system)

        # 3. Anthropic 只接受 "user" 和 "assistant"
        from core.protocols.vision import content_to_anthropic
        api_messages = []
        for msg in merged:
            role = msg.get("role", "user")
            if role not in ("user", "assistant"):
                role = "user"
            content = msg.get("content", "")
            if isinstance(content, list):
                content = content_to_anthropic(content)
            api_messages.append({"role": role, "content": content})

        return system_text, api_messages

    def _build_kwargs(self, model: str, system_text: str,
                      api_messages: List[Dict],
                      temperature: float, timeout: int) -> dict:
        """构建 API 调用参数（generate / generate_stream 共用）"""
        kwargs = {
            "model": model,
            "messages": api_messages,
            "max_tokens": self.max_tokens,
            "temperature": temperature,
            "timeout": timeout,
        }
        if system_text:
            kwargs["system"] = system_text
        return kwargs

    # ============================================================
    # 核心方法
    # ============================================================

    def generate(self, model: str, messages: List[Dict],
                 temperature: float = 0, timeout: int = 60) -> ChatResponse:
        """非流式调用"""
        system_text, api_messages = self._prepare_messages(messages)
        kwargs = self._build_kwargs(model, system_text, api_messages,
                                    temperature, timeout)

        response = self.client.messages.create(**kwargs)

        # 提取文本
        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        # 提取 usage
        usage = self._extract_usage(response.usage)
        self.last_usage = usage

        return ChatResponse(text=text, usage=usage)

    def generate_stream(self, model: str, messages: List[Dict],
                        temperature: float = 0, timeout: int = 60) -> Iterator[str]:
        """流式调用，yield 增量文本"""
        system_text, api_messages = self._prepare_messages(messages)
        kwargs = self._build_kwargs(model, system_text, api_messages,
                                    temperature, timeout)

        with self.client.messages.stream(**kwargs) as stream:
            for text_chunk in stream.text_stream:
                yield text_chunk

        # 流结束后获取 usage
        try:
            final_message = stream.get_final_message()
            self.last_usage = self._extract_usage(final_message.usage)
        except Exception:
            self.last_usage = None

    # ============================================================
    # Usage 提取
    # ============================================================

    @staticmethod
    def _extract_usage(usage) -> Optional[Dict[str, int]]:
        """从 Anthropic usage 对象提取 token 数"""
        if not usage:
            return None
        return {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        }
