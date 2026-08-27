# -*- coding: utf-8 -*-
"""
Gemini generateContent API 协议适配器

包装 google.genai.Client SDK，将内部 OpenAI 格式消息翻译为 Gemini 格式。

翻译规则：
  1. system 消息提取为 GenerateContentConfig.system_instruction
  2. content → parts: [{"text": content}]
  3. role: "assistant" → role: "model"
  4. 连续同 role 消息合并（Gemini 要求 user/model 交替）
  5. assistant.tool_calls → model content 的 functionCall part；
     role:"tool" 结果 → user content 的 functionResponse part
     （原生 function calling 往返，不再走文本协议）
  6. name: "tool_result" 被合并为 user 消息的一部分（遗留文本协议兼容）

SDK / REST 形状说明（google-genai==2.18.1，经 .venv 内 inspect 确认）：
  - 工具定义 types.Tool(function_declarations=[FunctionDeclaration(
    name, description, parameters=Schema 子集)])
  - Schema 只接受官方子集字段；OpenAI JSON Schema 的 $schema /
    additionalProperties 等键会被服务端以 INVALID_ARGUMENT 拒绝，
    因此 _sanitize_json_schema 按白名单递归裁剪，type 统一大写枚举串。
    假设：v1beta 对 OBJECT 类型要求 properties 非空——无参工具直接省略
    parameters 字段。
  - 响应 parts: Part.function_call = FunctionCall(name, args=dict[, id])；
    回传用 Part(function_response=FunctionResponse(name,
    response=dict))，且 functionResponse 必须放在 role="user" 的
    Content 中、name 与被调用的函数名一致（内部 name 字段即注册时下发的
    provider 名，满足该约束）。
"""

import json
import logging
from typing import Dict, Iterator, List, Optional

from google import genai
from google.genai import types

from .base import (ChatResponse, ProtocolAdapter, ProviderStreamEvent,
                   ProviderToolCall)

logger = logging.getLogger('jk_agent')


class GeminiAdapter(ProtocolAdapter):
    """
    Gemini generateContent API 协议适配器

    包装 google.genai.Client SDK。
    消息翻译：
      - system → config.system_instruction
      - content → parts: [Part.from_text(text=content)]
      - assistant → model
      - 连续同 role → 合并
      - assistant.tool_calls → functionCall part；tool 结果 → user content
        的 functionResponse part（原生 function calling 往返）
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

    # Gemini Schema 白名单字段（camelCase，与 REST 形状一致）
    _SCHEMA_KEYS = frozenset({
        "type", "format", "description", "nullable", "enum",
        "maxItems", "minItems", "minProperties", "maxProperties",
        "minimum", "maximum", "minLength", "maxLength",
        "pattern", "example", "anyOf", "properties", "required",
        "items", "propertyOrdering", "default",
    })

    @classmethod
    def _sanitize_json_schema(cls, schema) -> Optional[dict]:
        """OpenAI JSON Schema → Gemini Schema 子集（递归白名单裁剪）。

        假设（以官方 REST 形状为准）：Gemini 只接受上述子集字段；type 取值
        需为 OBJECT/STRING/... 大写枚举。OpenAI 工具常见的 $schema、
        additionalProperties、exclusiveMinimum 等键一律丢弃。
        """
        if not isinstance(schema, dict):
            return None
        out: dict = {}
        stype = schema.get("type")
        if isinstance(stype, str) and stype:
            out["type"] = stype.upper()
        elif isinstance(stype, list) and stype:
            out["type"] = str(stype[0]).upper()  # OpenAI type 数组取首个
        for key in cls._SCHEMA_KEYS:
            if key == "type" or key not in schema:
                continue
            value = schema[key]
            if key == "properties" and isinstance(value, dict):
                props = {str(k): cls._sanitize_json_schema(v)
                         for k, v in value.items()}
                out[key] = {k: v for k, v in props.items() if v}
            elif key == "items":
                sanitized = cls._sanitize_json_schema(value)
                if sanitized:
                    out[key] = sanitized
            elif key == "anyOf" and isinstance(value, list):
                variants = [cls._sanitize_json_schema(v) for v in value]
                out[key] = [v for v in variants if v]
            elif key == "required" and isinstance(value, list):
                out[key] = [str(k) for k in value]
            else:
                out[key] = value
        return out or None

    @classmethod
    def _translate_tools(cls, tools: Optional[List[Dict]]) -> List[types.Tool]:
        """OpenAI 格式 tools → [types.Tool(function_declarations=[...])]。

        无参工具省略 parameters（OBJECT 空 properties 会被服务端拒绝）；
        全部声明为空时返回 []，调用方据此不下发 tools 参数。
        """
        declarations: List[dict] = []
        for fn in ProtocolAdapter._openai_function_defs(tools):
            declaration: dict = {
                "name": fn["name"],
                "description": fn["description"],
            }
            parameters = cls._sanitize_json_schema(fn["parameters"]) \
                if fn["parameters"] else None
            if parameters and parameters.get("properties"):
                declaration["parameters"] = parameters
            declarations.append(declaration)
        return [types.Tool(function_declarations=declarations)] \
            if declarations else []

    @staticmethod
    def _apply_tool_choice(config_kwargs: dict, tool_choice) -> None:
        """OpenAI 风格 tool_choice → ToolConfig(function_calling_config)。"""
        mode = None
        allowed = None
        if tool_choice == "none":
            mode = "NONE"
        elif tool_choice in ("required", "any"):
            mode = "ANY"
        elif isinstance(tool_choice, dict):
            name = ((tool_choice.get("function") or {}).get("name")
                    or tool_choice.get("name"))
            if name:
                mode = "ANY"
                allowed = [str(name)]
        if not mode:
            return  # None / "auto" / 未知值：不传 config（API 默认 AUTO）
        function_calling_config = {"mode": mode}
        if allowed:
            function_calling_config["allowed_function_names"] = allowed
        config_kwargs["tool_config"] = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                **function_calling_config))

    def _content_to_gemini_parts(self, content) -> list:
        """内部 content → Gemini Part 列表。

        中性工具块在此映射为 functionCall / functionResponse part；其余
        （text/image）交给 vision.content_to_gemini_parts——后者只认
        text/image，工具块必须先行拦截，否则会被静默丢弃。
        """
        from core.protocols.vision import content_to_gemini_parts
        if not isinstance(content, list):
            return content_to_gemini_parts(content)
        parts: list = []
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "tool_call":
                args = block.get("arguments")
                parts.append(types.Part(function_call=types.FunctionCall(
                    name=str(block.get("name") or ""),
                    args=args if isinstance(args, dict) else {},
                )))
            elif btype == "tool_result":
                text = str(block.get("content") or "")
                # response 必须是 JSON object：能解析成 dict 就原样透传，
                # 否则包一层 {"result": ...}；错误结果附加 error 标记。
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = None
                if not isinstance(payload, dict):
                    payload = {"result": text}
                if block.get("is_error"):
                    payload["error"] = True
                name = str(block.get("name") or "")
                parts.append(types.Part(function_response=(
                    types.FunctionResponse(name=name, response=payload))))
            else:
                parts.extend(content_to_gemini_parts([block]))
        return parts

    def _prepare_request(self, model: str, messages: List[Dict],
                         temperature: float = 0, tools: Optional[List[Dict]] = None,
                         tool_choice=None) -> tuple[str, List[types.Content], types.GenerateContentConfig]:
        """
        将内部格式消息翻译为 Gemini 格式。

        返回:
            (model, contents, config)
        """
        # 0. 展开 assistant.tool_calls / role:"tool" 为中性块（原生 FC）
        messages = self._normalize_tool_roles(messages)

        # 1. 提取 system 消息
        system_text, non_system = self._split_system_messages(messages)

        # 2. 合并连续同 role（Gemini 要求 user/model 交替）
        merged = self._merge_consecutive_same_role(non_system)

        # 3. 翻译为 Gemini Content 格式
        contents = []
        for msg in merged:
            role = msg.get("role", "user")
            content = msg.get("content")

            # Gemini role: user → user, assistant → model
            if role == "assistant":
                gemini_role = "model"
            else:
                gemini_role = "user"

            # Gemini 要求至少有一条 user 消息非空（工具往返的 parts 不受影响）
            if not content and gemini_role == "user":
                content = " "  # 空格占位，防止 API 拒绝

            parts = self._content_to_gemini_parts(content)
            contents.append(
                types.Content(
                    role=gemini_role,
                    parts=parts,
                )
            )

        # 4. 构建配置
        config_kwargs = {
            "temperature": temperature,
        }
        if system_text:
            config_kwargs["system_instruction"] = system_text
        translated_tools = self._translate_tools(tools)
        if translated_tools:
            config_kwargs["tools"] = translated_tools
            self._apply_tool_choice(config_kwargs, tool_choice)

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

        try:
            text = response.text or ""
        except ValueError:
            # safety-block：内容被安全过滤（无候选），与流式处理一致，返回空文本
            logger.warning("Gemini 非流式生成被安全过滤（无候选），返回空文本")
            text = ""
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
    # 原生工具调用（native function calling）
    # ============================================================

    @staticmethod
    def _candidate_parts(response) -> list:
        """取首个 candidate 的 parts（安全过滤无候选时返回 []）"""
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        content = getattr(candidates[0], "content", None)
        return getattr(content, "parts", None) or []

    @classmethod
    def _function_call_fields(cls, part):
        """part → (call_id, name, arguments)；非 functionCall 返回 None"""
        fc = getattr(part, "function_call", None)
        name = str(getattr(fc, "name", "") or "") if fc is not None else ""
        if fc is None or not name:
            return None
        args = getattr(fc, "args", None)
        if not isinstance(args, dict):
            args = {"__invalid_raw_arguments__": args}
        call_id = str(getattr(fc, "id", "") or "")
        return call_id, name, args

    def generate_with_tools(self, model: str, messages: List[Dict],
                            tools: List[Dict], temperature: float = 0,
                            timeout: int = 60, tool_choice=None) -> ChatResponse:
        """非流式原生工具调用：解析 parts 中的 text + functionCall。"""
        _model, contents, config = self._prepare_request(
            model, messages, temperature, tools=tools, tool_choice=tool_choice)
        response = self.client.models.generate_content(
            model=_model,
            contents=contents,
            config=config,
        )

        text_parts: List[str] = []
        calls: List[ProviderToolCall] = []
        for part in self._candidate_parts(response):
            fields = self._function_call_fields(part)
            if fields is not None:
                call_id, name, args = fields
                calls.append(ProviderToolCall(
                    call_id or f"call_{len(calls)}_{name}", name, args,
                    json.dumps(args, ensure_ascii=False), len(calls)))
            else:
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(text)

        if not getattr(response, "candidates", None):
            logger.warning("Gemini 非流式生成被安全过滤（无候选），返回空响应")
        usage = self._extract_usage(response)
        self.last_usage = usage
        finish_reason = "tool_calls" if calls else "stop"
        return ChatResponse(text="".join(text_parts), tool_calls=calls,
                            finish_reason=finish_reason, usage=usage)

    def generate_stream_with_tools(self, model: str, messages: List[Dict],
                                   tools: List[Dict], temperature: float = 0,
                                   timeout: int = 60
                                   ) -> Iterator[ProviderStreamEvent]:
        """流式原生工具调用。

        沿用本文件现有 ``generate_content_stream`` 迭代风格；Gemini 不做
        functionCall 参数增量下发（args 在单个 chunk 中一次性完整到达，
        REST 形状假设），故每个 functionCall 直接补发 start + end 一对事件。
        """
        _model, contents, config = self._prepare_request(
            model, messages, temperature, tools=tools)

        response = self.client.models.generate_content_stream(
            model=_model,
            contents=contents,
            config=config,
        )

        order = 0
        last_chunk = None
        for chunk in response:
            last_chunk = chunk
            for part in self._candidate_parts(chunk):
                fields = self._function_call_fields(part)
                if fields is not None:
                    call_id, name, args = fields
                    yield ProviderStreamEvent(type="tool_call_start",
                                              call_id=call_id, name=name,
                                              order=order)
                    yield ProviderStreamEvent(type="tool_call_end",
                                              call_id=call_id, name=name,
                                              order=order, arguments=args)
                    order += 1
                else:
                    try:
                        text = part.text
                    except (ValueError, IndexError, AttributeError):
                        continue
                    if text:
                        # 思考模型的 thought part 不进正文，走 reasoning 通道
                        if getattr(part, "thought", False):
                            yield ProviderStreamEvent(type="reasoning_delta",
                                                      text=text)
                        else:
                            yield ProviderStreamEvent(type="text_delta",
                                                      text=text)

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
