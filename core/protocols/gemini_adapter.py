# -*- coding: utf-8 -*-
"""
Gemini generateContent API 协议适配器

包装 google.genai.Client SDK，将内部 OpenAI 格式消息翻译为 Gemini 格式。

翻译规则：
  1. system 消息提取为 GenerateContentConfig.system_instruction
  2. content → parts: [{"text": content}]
  3. role: "assistant" → role: "model"
  4. 连续同 role 消息合并（Gemini 要求 user/model 交替）
  5. name: "tool_result" 被合并为 user 消息的一部分
"""

import logging
from typing import Dict, Iterator, List, Optional

from google import genai
from google.genai import types

from .base import ChatResponse, ProtocolAdapter

logger = logging.getLogger('hello_agent')


class GeminiAdapter(ProtocolAdapter):
    """
    Gemini generateContent API 协议适配器

    包装 google.genai.Client SDK。
    消息翻译：
      - system → config.system_instruction
      - content → parts: [Part.from_text(text=content)]
      - assistant → model
      - 连续同 role → 合并
    """

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 timeout: int = 60):
        super().__init__(api_key, base_url, timeout)
        # google-genai SDK v2.x：用 http_options 配置自定义 endpoint
        client_kwargs: dict = {"api_key": api_key}
        if base_url:
            client_kwargs["http_options"] = types.HttpOptions(base_url=base_url)
        if timeout:
            if "http_options" not in client_kwargs:
                client_kwargs["http_options"] = types.HttpOptions()
            client_kwargs["http_options"].timeout = timeout * 1000  # 毫秒
        self.client = genai.Client(**client_kwargs)

    # ============================================================
    # 消息翻译
    # ============================================================

    def _prepare_request(self, model: str, messages: List[Dict],
                         temperature: float = 0) -> tuple[str, List[types.Content], types.GenerateContentConfig]:
        """
        将内部格式消息翻译为 Gemini 格式。

        返回:
            (model, contents, config)
        """
        # 1. 提取 system 消息
        system_text, non_system = self._split_system_messages(messages)

        # 2. 合并连续同 role（Gemini 要求 user/model 交替）
        merged = self._merge_consecutive_same_role(non_system)

        # 3. 翻译为 Gemini Content 格式
        contents = []
        for msg in merged:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Gemini role: user → user, assistant → model
            if role == "assistant":
                gemini_role = "model"
            else:
                gemini_role = "user"

            # Gemini 要求至少有一条 user 消息非空
            if not content and gemini_role == "user":
                content = " "  # 空格占位，防止 API 拒绝

            contents.append(
                types.Content(
                    role=gemini_role,
                    parts=[types.Part.from_text(text=content)],
                )
            )

        # 4. 构建配置
        config_kwargs = {
            "temperature": temperature,
        }
        if system_text:
            config_kwargs["system_instruction"] = system_text

        config = types.GenerateContentConfig(**config_kwargs)

        return model, contents, config

    # ============================================================
    # 核心方法
    # ============================================================

    def generate(self, model: str, messages: List[Dict],
                 temperature: float = 0, timeout: int = 60) -> ChatResponse:
        """非流式调用"""
        _model, contents, config = self._prepare_request(model, messages, temperature)

        # google-genai SDK 的 timeout 通过 client config 或 httpx timeout
        # 使用 generate_content 的 config 参数传递
        response = self.client.models.generate_content(
            model=_model,
            contents=contents,
            config=config,
        )

        text = response.text or ""
        usage = self._extract_usage(response)
        self.last_usage = usage

        return ChatResponse(text=text, usage=usage)

    def generate_stream(self, model: str, messages: List[Dict],
                        temperature: float = 0, timeout: int = 60) -> Iterator[str]:
        """流式调用，yield 增量文本"""
        _model, contents, config = self._prepare_request(model, messages, temperature)

        # generate_content_stream 返回迭代器
        response = self.client.models.generate_content_stream(
            model=_model,
            contents=contents,
            config=config,
        )

        last_chunk = None
        for chunk in response:
            last_chunk = chunk
            try:
                text = chunk.text
            except (ValueError, IndexError):
                continue
            if text:
                yield text

        # Gemini 流式结束后尝试提取 usage（不一定所有模型都提供）
        try:
            self.last_usage = self._extract_usage(last_chunk) if last_chunk else None
        except Exception:
            self.last_usage = None

    # ============================================================
    # Usage 提取
    # ============================================================

    @staticmethod
    def _extract_usage(response) -> Optional[Dict[str, int]]:
        """
        从 Gemini 响应提取 token 数

        Gemini 的 usage_metadata 在 candidate 级别或 response 级别：
          - generate_content 返回: response.usage_metadata
          - 流式返回: chunk.usage_metadata（最后一个 chunk）
        """
        try:
            um = getattr(response, "usage_metadata", None)
            if um:
                return {
                    "input_tokens": getattr(um, "prompt_token_count", 0) or 0,
                    "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
                }
        except Exception:
            pass

        return None
