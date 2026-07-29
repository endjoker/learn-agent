# -*- coding: utf-8 -*-
"""
OpenAI 协议适配器

包装 openai.OpenAI SDK，支持 OpenAI / DeepSeek / Ollama / vLLM 等兼容服务。
消息格式不变，直接透传（内部格式即 OpenAI 格式）。
"""

import logging
from typing import Dict, Iterator, List, Optional

from openai import OpenAI

from .base import ChatResponse, ProtocolAdapter

logger = logging.getLogger('hello_agent')


class OpenAIAdapter(ProtocolAdapter):
    """
    OpenAI Chat Completions 协议适配器

    直接包装 openai.OpenAI SDK，内部消息格式与 API 格式一致，
    无需翻译。usage 兼容 prompt_tokens（OpenAI）和 input_tokens（DeepSeek）。
    """

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 timeout: int = 60):
        super().__init__(api_key, base_url, timeout)
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

    # ============================================================
    # 消息翻译（OpenAI 格式无需翻译，直接透传）
    # ============================================================

    def _prepare_messages(self, messages: List[Dict]) -> List[Dict]:
        """OpenAI 格式：content 为 list 时转 vision 格式，str 透传"""
        from core.protocols.vision import content_to_openai
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                new_msg = dict(msg)
                new_msg["content"] = content_to_openai(content)
                result.append(new_msg)
            else:
                result.append(msg)
        return result

    # ============================================================
    # 核心方法
    # ============================================================

    def generate(self, model: str, messages: List[Dict],
                 temperature: float = 0, timeout: int = 60) -> ChatResponse:
        """非流式调用"""
        response = self.client.chat.completions.create(
            model=model,
            messages=self._prepare_messages(messages),
            temperature=temperature,
            stream=False,
            timeout=timeout,
        )
        text = response.choices[0].message.content or ""
        usage = self._extract_usage(response.usage)
        self.last_usage = usage
        return ChatResponse(text=text, usage=usage)

    def generate_stream(self, model: str, messages: List[Dict],
                        temperature: float = 0, timeout: int = 60) -> Iterator[str]:
        """流式调用，yield 增量文本"""
        response = self.client.chat.completions.create(
            model=model,
            messages=self._prepare_messages(messages),
            temperature=temperature,
            stream=True,
            timeout=timeout,
        )

        collected = []
        for chunk in response:
            if chunk.usage:
                self.last_usage = self._extract_usage(chunk.usage)
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content or ""
            collected.append(content)
            yield content

    # ============================================================
    # Usage 提取
    # ============================================================

    @staticmethod
    def _extract_usage(usage) -> Optional[Dict[str, int]]:
        """
        从 API 返回的 usage 对象中提取 token 数。

        兼容两种字段命名：
          - OpenAI 格式:  prompt_tokens / completion_tokens
          - DeepSeek 格式: input_tokens / output_tokens
        """
        if not usage:
            return None
        return {
            "input_tokens": (getattr(usage, "input_tokens", None)
                             or getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": (getattr(usage, "output_tokens", None)
                              or getattr(usage, "completion_tokens", 0) or 0),
        }
