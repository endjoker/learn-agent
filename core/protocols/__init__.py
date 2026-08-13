# -*- coding: utf-8 -*-
"""
core.protocols 包 —— LLM 协议适配器

支持的协议：
  - OpenAI Chat Completions（OpenAIAdapter）
  - Anthropic Messages API（AnthropicAdapter）
  - Gemini generateContent（GeminiAdapter）

工厂函数 create_adapter() 根据协议标识或 URL 自动选择适配器。
"""

from typing import Optional

from .base import ProtocolAdapter, ChatResponse
from .openai_adapter import OpenAIAdapter


__all__ = [
    "ProtocolAdapter",
    "ChatResponse",
    "OpenAIAdapter",
    "create_adapter",
    "detect_protocol",
]


def detect_protocol(base_url: str) -> str:
    """
    根据 base_url 自动检测协议类型（唯一真相源）。

    检测规则（按优先级）：
      - 含 api.anthropic.com  → anthropic
      - 含 generativelanguage.googleapis.com  → gemini
      - 含 googleapis + gemini → gemini
      - 其他 → openai（默认）
    """
    if not base_url:
        return "openai"
    url_lower = base_url.lower()
    if "api.anthropic.com" in url_lower:
        return "anthropic"
    if "generativelanguage.googleapis.com" in url_lower:
        return "gemini"
    if "googleapis" in url_lower and "gemini" in url_lower:
        return "gemini"
    return "openai"


def create_adapter(
    protocol: str = "",
    api_key: str = "",
    base_url: Optional[str] = None,
    timeout: int = 60,
    **kwargs,
) -> ProtocolAdapter:
    """
    根据协议标识或 base_url 创建对应的适配器。

    协议选择优先级：
      1. protocol 参数（显式指定 "openai" / "anthropic" / "gemini"）
      2. base_url 自动检测（根据域名特征推断）
      3. 默认 fallback → OpenAIAdapter

    参数:
        protocol: 协议标识 — "openai" | "anthropic" | "gemini"
        api_key:  API 密钥
        base_url: API 服务地址（用于自动检测 + 传递到适配器）
        timeout:  请求超时秒数
        **kwargs: 传递给适配器的额外参数

    返回:
        ProtocolAdapter 实例
    """
    # ---- 自动检测 ----
    if not protocol and base_url:
        protocol = detect_protocol(base_url)
    if not protocol:
        protocol = "openai"

    protocol_lower = protocol.lower()

    if protocol_lower == "anthropic":
        from .anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_tokens=kwargs.get("max_tokens", 8192),
        )
    elif protocol_lower == "gemini":
        from .gemini_adapter import GeminiAdapter
        return GeminiAdapter(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
    elif protocol_lower == "openai":
        return OpenAIAdapter(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            reasoning_effort=kwargs.get("reasoning_effort", "provider_default"),
        )
    else:
        raise ValueError(
            f"不支持的协议: '{protocol}'，可选: openai / anthropic / gemini"
        )
