# -*- coding: utf-8 -*-
"""
协议适配器抽象基类

定义所有协议适配器的统一接口。
内部消息格式保持 OpenAI 风格：
    [{"role": "system/user/assistant", "content": "...",
      "name"?: "tool_result"},
     {"role": "assistant", "content": str|None,
      "tool_calls": [{"id", "type": "function",
                      "function": {"name", "arguments"}}]},
     {"role": "tool", "tool_call_id": "...", "name": "...",
      "content": "..."}]

各适配器负责：
  1. 将内部消息格式翻译为目标协议格式
  2. 调用目标 API（流式/非流式）
  3. 将响应转回统一的 ChatResponse / text chunks
"""

from abc import ABC, abstractmethod
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


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


@dataclass
class ProviderStreamEvent:
    """A normalized provider event; control data never shares the text channel."""
    type: str
    text: str = ""
    call_id: str = ""
    name: str = ""
    arguments_delta: str = ""
    arguments: Optional[dict] = None
    order: int = 0


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

    def generate_stream_with_tools(self, model: str, messages: List[Dict],
                                   tools: List[Dict], temperature: float = 0,
                                   timeout: int = 60) -> Iterator[ProviderStreamEvent]:
        """Optional native tool-call stream.

        Adapters that do not implement this are handled by the non-streaming
        native fallback in ``JKAgentLLM``; they must not fall back to a
        textual tool protocol.
        """
        raise NotImplementedError

    # ============================================================
    # 消息翻译辅助
    # ============================================================

    @staticmethod
    def _parse_json_arguments(raw: Any) -> tuple:
        """把工具参数原始值解析为 (arguments_dict, raw_str)。

        OpenAI 风格的 ``function.arguments`` 约定为 JSON 字符串；部分服务端
        会直接返回 dict/对象，这里统一归一为 JSON 字符串再解析。解析失败或
        结果不是 object 时回退为 ``{"__invalid_raw_arguments__": raw}``，
        与 openai_adapter 保持同一约定：保留畸形参数交给 Runtime 拒绝，
        不静默吞成空对象。
        """
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False) if raw is not None else "{}"
        raw_arguments = raw or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict):
            arguments = {"__invalid_raw_arguments__": raw_arguments}
        return arguments, raw_arguments

    @staticmethod
    def _normalize_tool_roles(messages: List[Dict]) -> List[Dict]:
        """把 assistant.tool_calls / role:"tool" 展开为中性 content 块。

        内部格式沿用 OpenAI 风格（assistant.tool_calls / role:"tool"），但
        Anthropic（tool_use / tool_result）与 Gemini（functionCall /
        functionResponse）的原生 function calling 往返都需要把这些角色翻译
        成 provider 专属块。这里在消息合并前统一展开为中性块，各适配器再把
        中性块映射到目标协议：

          - assistant 且带 tool_calls → content 变为块列表：原文本（str 或
            多模态 list）在前，每个 tool call 追加一个
            {"type": "tool_call", "id", "name", "arguments"} 块；
          - role == "tool" → 改写为 role "user"，content 为单个
            {"type": "tool_result", "tool_call_id", "name", "content",
             "is_error"} 块。连续多条 tool 结果随后由
            _merge_consecutive_same_role 合并为同一条 user 消息——恰好满足
            Anthropic「tool_result 必须紧随对应 assistant 轮且可批量回传」
            与 Gemini「functionResponse 与 user 同 role」的形状要求。

        健壮性：content 为 None（agent 在纯工具轮会写入 None）、缺失字段、
        arguments 非字符串等均兜底处理，不抛异常。
        """
        normalized: List[Dict] = []
        for msg in messages:
            role = msg.get("role")
            tool_calls = msg.get("tool_calls")
            if role == "assistant" and tool_calls:
                content = msg.get("content")
                blocks: List[Dict] = []
                if isinstance(content, str):
                    if content.strip():
                        blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    blocks.extend(b for b in content if isinstance(b, dict))
                for tc in tool_calls or []:
                    fn = tc.get("function") if isinstance(tc, dict) else None
                    if not isinstance(fn, dict):
                        continue
                    arguments, raw_arguments = ProtocolAdapter._parse_json_arguments(
                        fn.get("arguments"))
                    blocks.append({
                        "type": "tool_call",
                        "id": str(tc.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "arguments": arguments,
                    })
                new_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                new_msg["content"] = blocks
                normalized.append(new_msg)
            elif role == "tool":
                content = msg.get("content")
                if not isinstance(content, str):
                    # None / 多模态 list 等统一序列化为文本，避免下游崩溃。
                    content = ("" if content is None
                               else json.dumps(content, ensure_ascii=False))
                normalized.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_call_id": str(msg.get("tool_call_id") or ""),
                        "name": str(msg.get("name") or ""),
                        "content": content,
                        "is_error": bool(msg.get("is_error")),
                    }],
                })
            else:
                normalized.append(dict(msg))
        return normalized

    @staticmethod
    def _openai_function_defs(tools: Optional[List[Dict]]) -> List[Dict]:
        """provider tools 参数 → 中性 (name/description/parameters) 定义列表。

        工具注册表下发的形状是 OpenAI 格式
        ``{"type": "function", "function": {...}}``；兼容直接给扁平定义
        ``{"name", ...}`` 的第三方调用方。
        """
        defs: List[Dict] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            if not isinstance(fn, dict):
                fn = tool if tool.get("name") else None
            if not isinstance(fn, dict) or not fn.get("name"):
                continue
            params = fn.get("parameters")
            defs.append({
                "name": str(fn["name"]),
                "description": str(fn.get("description") or ""),
                "parameters": params if isinstance(params, dict) else {},
            })
        return defs

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
            content = msg.get("content")
            if content is None:
                # 纯工具轮的 assistant 消息 content 为 None，兜底为空串。
                content = ""
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
