# -*- coding: utf-8 -*-
"""
Anthropic Messages API 协议适配器

包装 anthropic.Anthropic SDK，将内部 OpenAI 格式消息翻译为 Anthropic 格式。

翻译规则：
  1. system 消息从 messages 中提取，作为 API 的 system 参数
  2. 连续同 role 消息合并（Anthropic 要求 user/assistant 严格交替）
  3. assistant.tool_calls → content 中的 tool_use block；
     role:"tool" 结果 → 下一条 user 消息中的 tool_result block
     （原生 function calling 往返，不再走文本协议）
  4. name: "tool_result" 被合并为 user 消息的一部分（遗留文本协议兼容）
  5. 需要 max_tokens 参数（Anthropic 必填）

SDK 形状说明（anthropic==0.122.0，经 .venv 内 inspect 确认）：
  - 工具定义 ToolParam: {"name", "description", "input_schema": JSON Schema}
  - 响应 content block: ToolUseBlock(type="tool_use", id, name, input=dict)
  - 流式 raw event: content_block_start / content_block_delta
    (TextDelta | InputJSONDelta(partial_json)) / content_block_stop，
    MessageStream 支持 __iter__ 遍历原始事件与 get_final_message()
"""

import json
import logging
import os
from typing import Dict, Iterator, List, Optional

from anthropic import Anthropic

from .base import (ChatResponse, ProtocolAdapter, ProviderStreamEvent,
                   ProviderToolCall)

logger = logging.getLogger('jk_agent')

try:
    import httpx
except ImportError:   # pragma: no cover - anthropic SDK 硬依赖 httpx，实际不可达
    httpx = None

# 流式 read（chunk 间隔）空闲上限：推理模型首帧前长思考可远超标量 timeout，
# 与 llm_client.stream_httpx_timeout 同一策略（环境变量解析失败回退 300）。
_STREAM_READ_TIMEOUT_ENV = "JKAGENT_STREAM_READ_TIMEOUT"
_STREAM_READ_IDLE_DEFAULT = 300


def _stream_read_idle_seconds(req_timeout) -> int:
    try:
        env_idle = int(os.getenv(
            _STREAM_READ_TIMEOUT_ENV, str(_STREAM_READ_IDLE_DEFAULT)))
    except (TypeError, ValueError):
        env_idle = _STREAM_READ_IDLE_DEFAULT
    try:
        base = int(req_timeout)
    except (TypeError, ValueError):
        base = 0
    return max(base, env_idle, 1)


def _as_stream_timeout(timeout):
    """流式请求超时归一：标量 → httpx.Timeout；已是 httpx.Timeout 原样返回。

    anthropic SDK 的 Timeout 即 httpx.Timeout（anthropic.Timeout is
    httpx.Timeout，经 .venv 内 anthropic==0.122.0 确认），per-request timeout
    原生接受。connect/write/pool 保持标量语义不变，仅 read（chunk 间隔）
    放宽为空闲上限。httpx 缺失时退回原值。非流式方法不得使用本函数。
    """
    if httpx is None:
        return timeout
    if isinstance(timeout, httpx.Timeout):
        return timeout
    idle = _stream_read_idle_seconds(timeout)
    try:
        base = max(int(timeout), 1)
    except (TypeError, ValueError):
        base = idle
    return httpx.Timeout(connect=base, read=idle, write=base, pool=base)

# Anthropic 要求必传 max_tokens，默认值（足够覆盖大多数 ReAct 回复）
DEFAULT_MAX_TOKENS = 8192


class AnthropicAdapter(ProtocolAdapter):
    """
    Anthropic Messages API 协议适配器

    包装 anthropic.Anthropic SDK。
    消息翻译：
      - system → API system 参数（不在 messages 数组中）
      - 连续同 role → 合并
      - assistant.tool_calls → tool_use block；tool 结果 → user 消息中的
        tool_result block（原生 function calling 往返）
      - 遗留 name: "tool_result" 文本协议 → content 前缀标记（兼容旧会话）
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

    def _content_to_anthropic(self, content):
        """内部 content → Anthropic content（str 原样；list 逐块翻译）。

        中性工具块在此映射为 Anthropic 专属块，其余（text/image）交给
        vision.content_to_anthropic——后者只认 text/image，工具块必须先行
        拦截，否则会被静默丢弃。
        """
        if not isinstance(content, list):
            return content
        from core.protocols.vision import content_to_anthropic
        blocks = []
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "tool_call":
                blocks.append({
                    "type": "tool_use",
                    "id": block.get("id") or "",
                    "name": block.get("name") or "",
                    # input 必须是 JSON object；畸形参数已在 base 归一。
                    "input": block.get("arguments") or {},
                })
            elif btype == "tool_result":
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_call_id") or "",
                    # content 接受 str 或块数组；观察文本恒为 str。
                    "content": str(block.get("content") or ""),
                }
                if block.get("is_error"):
                    tool_result["is_error"] = True
                blocks.append(tool_result)
            else:
                blocks.extend(content_to_anthropic([block]))
        return blocks

    def _prepare_messages(self, messages: List[Dict]) -> tuple[str, List[Dict]]:
        """
        将内部格式消息翻译为 Anthropic 格式。

        返回:
            (system_text, api_messages)

        其中 api_messages 仅包含 user/assistant 角色，且严格交替。
        """
        # 0. 展开 assistant.tool_calls / role:"tool" 为中性块（原生 FC）
        messages = self._normalize_tool_roles(messages)

        # 1. 提取 system 消息
        system_text, non_system = self._split_system_messages(messages)

        # 2. 合并连续同 role（Anthropic 要求交替）
        merged = self._merge_consecutive_same_role(non_system)

        # 3. Anthropic 只接受 "user" 和 "assistant"
        api_messages = []
        for msg in merged:
            role = msg.get("role", "user")
            if role not in ("user", "assistant"):
                role = "user"
            content = msg.get("content")
            if isinstance(content, list):
                content = self._content_to_anthropic(content)
            elif content is None:
                content = ""
            api_messages.append({"role": role, "content": content})

        return system_text, api_messages

    # ============================================================
    # 工具定义 / tool_choice 翻译
    # ============================================================

    @staticmethod
    def _translate_tools(tools: Optional[List[Dict]]) -> List[Dict]:
        """OpenAI 格式 tools → Anthropic ToolParam 列表。

        {"type": "function", "function": {name, description, parameters}}
        → {"name", "description", "input_schema": parameters}；
        input_schema 缺省补 {"type": "object"}（Anthropic 要求 object schema）。
        """
        translated = []
        for fn in ProtocolAdapter._openai_function_defs(tools):
            input_schema = fn["parameters"] or {"type": "object"}
            translated.append({
                "name": fn["name"],
                "description": fn["description"],
                "input_schema": input_schema,
            })
        return translated

    @staticmethod
    def _translate_tool_choice(tool_choice) -> Optional[Dict]:
        """OpenAI 风格 tool_choice → Anthropic tool_choice。

        None/"auto" 不传参（API 默认 auto）；"required" → any；
        OpenAI 具名对象 → {"type": "tool", "name": ...}。未知值原样忽略，
        由服务端校验兜底。
        """
        if tool_choice is None or tool_choice == "auto":
            return None
        if tool_choice == "none":
            return {"type": "auto"}  # Anthropic 无 none；退化为 auto
        if tool_choice in ("required", "any"):
            return {"type": "any"}
        if isinstance(tool_choice, dict):
            name = ((tool_choice.get("function") or {}).get("name")
                    or tool_choice.get("name"))
            if name:
                return {"type": "tool", "name": str(name)}
        return None

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
        # A1-3：为 system 与最后一条 user 消息追加 prompt 缓存断点
        # （cache_control={"type": "ephemeral"}），提高前缀缓存命中率。
        # 旧版 anthropic SDK 可能不认识该字段，try/except 静默跳过。
        self._apply_cache_control(kwargs, api_messages)
        return kwargs

    @staticmethod
    def _apply_cache_control(kwargs: dict, api_messages: List[Dict]) -> None:
        """为 system 与最后一条 user 消息追加 cache_control 断点（A1-3）。

        system 为字符串时升级为带缓存断点的单块文本；最后一条 user 消息在
        文本内容末尾（或最后一个 content block）放置断点，使缓存覆盖
        system + 此前全部对话前缀。任何异常（旧 SDK/异常内容结构）都静默
        跳过，不影响主调用。
        """
        try:
            if kwargs.get("system") and isinstance(kwargs["system"], str):
                kwargs["system"] = [{
                    "type": "text",
                    "text": kwargs["system"],
                    "cache_control": {"type": "ephemeral"},
                }]
            for msg in reversed(api_messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    msg["content"] = [{
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }]
                elif isinstance(content, list) and content:
                    blocks = list(content)
                    last = dict(blocks[-1])
                    last["cache_control"] = {"type": "ephemeral"}
                    blocks[-1] = last
                    msg["content"] = blocks
                break  # 只处理最后一条 user 消息
        except Exception as exc:
            logger.debug("追加 cache_control 失败（旧 SDK 兼容性保护）: %s", exc)

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
            # 流结束后获取 usage（在 with 块内，确保流已完整关闭）
            try:
                final_message = stream.get_final_message()
                self.last_usage = self._extract_usage(final_message.usage)
            except Exception as e:
                logger.warning(f"Anthropic 流式 usage 提取失败: {e}")
                self.last_usage = None

    # ============================================================
    # 原生工具调用（native function calling）
    # ============================================================

    def _build_tool_kwargs(self, model: str, messages: List[Dict],
                           tools: List[Dict], temperature: float,
                           timeout: int, tool_choice=None) -> dict:
        """构建带 tools 的 API 调用参数（generate / stream 共用）"""
        system_text, api_messages = self._prepare_messages(messages)
        kwargs = self._build_kwargs(model, system_text, api_messages,
                                    temperature, timeout)
        translated = self._translate_tools(tools)
        if translated:
            kwargs["tools"] = translated
            choice = self._translate_tool_choice(tool_choice)
            if choice:
                kwargs["tool_choice"] = choice
        return kwargs

    @staticmethod
    def _parse_arguments(raw) -> dict:
        """tool_use.input / 累积 partial_json → arguments dict（畸形保留）"""
        if isinstance(raw, str):
            arguments, _ = ProtocolAdapter._parse_json_arguments(raw)
            return arguments
        if not isinstance(raw, dict):
            return {"__invalid_raw_arguments__": raw}
        return raw

    def generate_with_tools(self, model: str, messages: List[Dict],
                            tools: List[Dict], temperature: float = 0,
                            timeout: int = 60, tool_choice=None) -> ChatResponse:
        """非流式原生工具调用：解析 content 中的 text + tool_use blocks。"""
        kwargs = self._build_tool_kwargs(model, messages, tools, temperature,
                                         timeout, tool_choice)
        response = self.client.messages.create(**kwargs)

        text_parts: List[str] = []
        calls: List[ProviderToolCall] = []
        for block in getattr(response, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use":
                call_id = str(getattr(block, "id", "") or "")
                name = str(getattr(block, "name", "") or "")
                arguments = self._parse_arguments(getattr(block, "input", None))
                calls.append(ProviderToolCall(
                    call_id, name, arguments,
                    json.dumps(arguments, ensure_ascii=False), len(calls)))

        usage = self._extract_usage(getattr(response, "usage", None))
        self.last_usage = usage
        stop_reason = str(getattr(response, "stop_reason", "") or "")
        finish_reason = "tool_calls" if calls else (stop_reason or "unknown")
        return ChatResponse(text="".join(text_parts), tool_calls=calls,
                            finish_reason=finish_reason, usage=usage)

    def generate_stream_with_tools(self, model: str, messages: List[Dict],
                                   tools: List[Dict], temperature: float = 0,
                                   timeout: int = 60
                                   ) -> Iterator[ProviderStreamEvent]:
        """流式原生工具调用。

        沿用本文件现有 ``client.messages.stream(...)`` 风格，但遍历原始事件
        （text_stream 只吐文本，拿不到 tool_use）：content_block_start 声明
        tool_use → InputJSONDelta 累积参数增量 → content_block_stop 解析出
        完整 arguments 后补发 tool_call_end。
        """
        # 流式专用：标量超时升级为 httpx.Timeout（read 空闲上限放宽），
        # 非流式路径（generate/generate_with_tools）保持标量不动。
        timeout = _as_stream_timeout(timeout)
        kwargs = self._build_tool_kwargs(model, messages, tools, temperature,
                                         timeout)
        with self.client.messages.stream(**kwargs) as stream:
            pending: Dict[int, Dict] = {}  # block index → {id,name,json}
            for event in stream:
                etype = getattr(event, "type", None)
                index = getattr(event, "index", 0)
                if etype == "content_block_start":
                    cb = getattr(event, "content_block", None)
                    if getattr(cb, "type", None) == "tool_use":
                        state = {
                            "id": str(getattr(cb, "id", "") or ""),
                            "name": str(getattr(cb, "name", "") or ""),
                            "json": "",
                        }
                        pending[index] = state
                        yield ProviderStreamEvent(
                            type="tool_call_start", call_id=state["id"],
                            name=state["name"], order=index)
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    if dtype == "text_delta":
                        text = getattr(delta, "text", "") or ""
                        if text:
                            yield ProviderStreamEvent(type="text_delta",
                                                      text=text)
                    elif dtype == "thinking_delta":
                        thinking = getattr(delta, "thinking", "") or ""
                        if thinking:
                            yield ProviderStreamEvent(type="reasoning_delta",
                                                      text=thinking)
                    elif dtype == "input_json_delta":
                        state = pending.get(index)
                        partial = getattr(delta, "partial_json", "") or ""
                        if state is not None and partial:
                            state["json"] += partial
                            yield ProviderStreamEvent(
                                type="tool_call_delta",
                                call_id=state["id"], name=state["name"],
                                arguments_delta=partial, order=index)
                elif etype == "content_block_stop":
                    state = pending.pop(index, None)
                    if state is not None:
                        yield ProviderStreamEvent(
                            type="tool_call_end", call_id=state["id"],
                            name=state["name"], order=index,
                            arguments=self._parse_arguments(state["json"]))
            # 流结束后获取 usage（在 with 块内，确保流已完整关闭）
            try:
                final_message = stream.get_final_message()
                self.last_usage = self._extract_usage(final_message.usage)
            except Exception as e:
                logger.warning(f"Anthropic 流式 usage 提取失败: {e}")
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
