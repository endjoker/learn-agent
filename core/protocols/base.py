# -*- coding: utf-8 -*-
"""
协议适配器抽象基类

定义所有协议适配器的统一接口。
内部消息格式保持 OpenAI 风格：
    [{"role": "system/user/assistant", "content": "...",
      "name"?: "tool_result"}]

各适配器负责：
  1. 将内部消息格式翻译为目标协议格式
  2. 调用目标 API（流式/非流式）
  3. 将响应转回统一的 ChatResponse / text chunks
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional


@dataclass
class ProviderToolCall:
    call_id: str
    name: str
    arguments: dict
    raw_arguments: str | None = None
    order: int = 0


@dataclass
class ChatResponse:
    """非流式响应的统一数据结构"""
    text: str
    tool_calls: List[ProviderToolCall] = field(default_factory=list)
    finish_reason: str = "unknown"
    usage: Optional[Dict[str, int]] = None
    raw_metadata: Optional[Dict] = None
    """{"input_tokens": N, "output_tokens": M}"""


class ProtocolAdapter(ABC):
    """
    协议适配器抽象基类

    每个具体适配器包装一个 LLM 提供商的 SDK（或 HTTP 客户端），
    实现 generate() / generate_stream() 两个核心方法。

    子类需要处理：
      - 消息格式翻译（内部 → 目标协议）
      - SSE 流式解析（目标协议 → text chunks）
      - usage 提取 / 记录
    """

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 timeout: int = 60):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.last_usage: Optional[Dict[str, int]] = None

    # ============================================================
    # 抽象接口
    # ============================================================

    @abstractmethod
    def generate(self, model: str, messages: List[Dict],
                 temperature: float = 0, timeout: int = 60) -> ChatResponse:
        """
        非流式生成。返回 (text, usage)。

        参数:
            model:       模型名称
            messages:    内部格式消息列表
            temperature: 生成温度
            timeout:     请求超时秒数

        返回:
            ChatResponse(text=完整文本, usage=token 用量)
        """
        ...

    @abstractmethod
    def generate_stream(self, model: str, messages: List[Dict],
                        temperature: float = 0, timeout: int = 60) -> Iterator[str]:
        """
        流式生成。yield 文本块，结束后 self.last_usage 已设置。

        参数:
            model:       模型名称
            messages:    内部格式消息列表
            temperature: 生成温度
            timeout:     请求超时秒数

        Yields:
            str: 增量文本块
        """
        ...

    # ============================================================
    # 消息翻译辅助
    # ============================================================

    @staticmethod
    def _split_system_messages(messages: List[Dict]) -> tuple:
        """
        从消息列表中提取 system 消息，返回 (system_text, non_system_messages)。

        多条 system 消息用 '\\n\\n---\\n\\n' 拼接。
        Anthropic / Gemini 适配器共用。
        """
        system_text = ""
        non_system = []
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content", "")
                if system_text:
                    system_text += "\n\n---\n\n" + content
                else:
                    system_text = content
            else:
                non_system.append(msg)
        return system_text, non_system

    @staticmethod
    def _join_contents(parts: list) -> str | list:
        """合并多个 content 片段。全 str → str 拼接；含 list → 合并为 list。"""
        has_list = any(isinstance(p, list) for p in parts)
        if not has_list:
            return "\n\n---\n\n".join(parts)
        # 含多模态内容 — 合并为 block 列表
        blocks = []
        for p in parts:
            if isinstance(p, str):
                if p.strip():
                    blocks.append({"type": "text", "text": p})
            elif isinstance(p, list):
                blocks.extend(p)
        return blocks

    @staticmethod
    def _merge_consecutive_same_role(messages: List[Dict]) -> List[Dict]:
        """
        合并连续同 role 的消息，满足 Anthropic / Gemini 的交替要求。

        合并策略：
          - 连续同 role → 用 '\\n\\n---\\n\\n' 分隔合并
          - 如果后一条有 name 字段，拼入标记如 '[工具结果]'
          - system 消息不参与合并（最前面，单独处理）

        参数:
            messages: 内部格式消息列表

        返回:
            合并后的消息列表（连续同 role 已合并）
        """
        if not messages:
            return []

        merged = []
        parts = []  # 当前合并组的 content 片段
        current_role = None

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            name = msg.get("name", "")

            # 添加标记（str 直接加前缀；多模态 list 在最前面插入文本块）
            if name == "tool_result":
                if isinstance(content, str):
                    content = f"[工具返回结果]\n{content}"
                elif isinstance(content, list):
                    content = [{"type": "text", "text": "[工具返回结果]"}] + list(content)

            if role == current_role:
                parts.append(content)
            else:
                # 输出上一组
                if current_role is not None:
                    merged.append({"role": current_role,
                                   "content": ProtocolAdapter._join_contents(parts)})
                current_role = role
                parts = [content]

        # 输出最后一组
        if current_role is not None:
            merged.append({"role": current_role,
                           "content": ProtocolAdapter._join_contents(parts)})

        return merged
