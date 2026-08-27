# -*- coding: utf-8 -*-
"""
OpenAI 协议适配器

包装 openai.OpenAI SDK，支持 OpenAI / DeepSeek / Ollama / vLLM 等兼容服务。
消息格式不变，直接透传（内部格式即 OpenAI 格式）。
"""

import json
import logging
import os
from typing import Dict, Iterator, List, Optional

from openai import OpenAI

from .base import ChatResponse, ProtocolAdapter, ProviderStreamEvent, ProviderToolCall

logger = logging.getLogger('jk_agent')

try:
    import httpx
except ImportError:   # pragma: no cover - openai SDK 硬依赖 httpx，实际不可达
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

    openai SDK 的 per-request timeout 接受 httpx.Timeout（运行时透传给
    httpx build_request，经 MockTransport 实测），connect/write/pool 保持
    标量语义不变，仅 read（chunk 间隔）放宽为空闲上限。httpx 缺失时退回
    原值。非流式方法不得使用本函数。
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


class OpenAIAdapter(ProtocolAdapter):
    """
    OpenAI Chat Completions 协议适配器

    直接包装 openai.OpenAI SDK，内部消息格式与 API 格式一致，
    无需翻译。usage 兼容 prompt_tokens（OpenAI）和 input_tokens（DeepSeek）。
    """

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 timeout: int = 60, reasoning_effort: str = "provider_default"):
        super().__init__(api_key, base_url, timeout)
        self.reasoning_effort = reasoning_effort
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            # 部分中转站 WAF 会拦截 SDK 默认的 "OpenAI/Python" User-Agent，
            # 返回 403 "Your request was blocked."；改用自定义 UA 规避。
            default_headers={"User-Agent": "jkagent"},
        )

    def _build_request_kwargs(self, model: str, messages: List[Dict], *,
                              temperature: float, timeout: int, stream: bool,
                              tools: Optional[List[Dict]] = None,
                              tool_choice=None) -> dict:
        """Build every Chat Completions request from one compatibility policy."""
        kwargs = {
            "model": model,
            "messages": self._prepare_messages(messages),
            "stream": stream,
            "timeout": timeout,
        }
        if tools is not None:
            kwargs["tools"] = tools
            # False explicitly omits tool_choice for older compatible gateways
            # that reject both the named object and the "auto" value.
            if tool_choice is not False:
                kwargs["tool_choice"] = tool_choice or "auto"
        # Reasoning models commonly reject temperature.  Omit it only when a
        # caller explicitly opted into reasoning; legacy calls stay byte-for-byte
        # compatible with their previous sampling parameter.
        if self.reasoning_effort != "provider_default":
            kwargs["reasoning_effort"] = self.reasoning_effort
        else:
            kwargs["temperature"] = temperature
        return kwargs

    # ============================================================
    # 消息翻译（OpenAI 格式无需翻译，直接透传）
    # ============================================================

    def _prepare_messages(self, messages: List[Dict]) -> List[Dict]:
        """OpenAI 格式：content 为 list 时转 vision 格式，str 透传"""
        from core.protocols.vision import content_to_openai
        from core.tool_schema import sanitize_message_name

        result = []
        # 内部字段白名单外一律剥离：kind/runtime/plan_id 等是本地持久化与
        # UI-only 运行期元数据，不属于 OpenAI Chat Completions 字段；
        # tool_calls / tool_call_id 是原生工具调用必需字段，保留。
        _INTERNAL_FIELDS = frozenset({
            "kind", "content_text", "tokens", "is_error", "internal",
            "runtime", "plan_id", "plan_task_id", "goal_id", "goal_round",
        })
        for msg in messages:
            content = msg.get("content", "")
            new_msg = {k: v for k, v in msg.items() if k not in _INTERNAL_FIELDS}
            # role=tool 消息的 name（以及 user 作者名）必须匹配
            # ^[a-zA-Z0-9_-]+$，否则 400 invalid_value。历史/旧会话里可能
            # 残留模型回显的原始工具名（含斜杠/点/空格/中文），统一兜底净化。
            if "name" in new_msg:
                new_msg["name"] = sanitize_message_name(new_msg["name"])
            if isinstance(content, list):
                new_msg["content"] = content_to_openai(content)
                result.append(new_msg)
            else:
                result.append(new_msg)
        return result

    # ============================================================
    # 核心方法
    # ============================================================

    def generate(self, model: str, messages: List[Dict],
                 temperature: float = 0, timeout: int = 60) -> ChatResponse:
        """非流式调用"""
        response = self.client.chat.completions.create(
            **self._build_request_kwargs(model, messages, temperature=temperature,
                                         timeout=timeout, stream=False))
        text = response.choices[0].message.content or ""
        usage = self._extract_usage(response.usage)
        self.last_usage = usage
        return ChatResponse(text=text, usage=usage,
                            finish_reason=str(response.choices[0].finish_reason or "unknown"))

    def generate_with_tools(self, model: str, messages: List[Dict], tools: List[Dict],
                            temperature: float = 0, timeout: int = 60,
                            tool_choice=None) -> ChatResponse:
        """Non-streaming native tool calls (stream deltas are intentionally deferred)."""
        response = self.client.chat.completions.create(
            **self._build_request_kwargs(model, messages, tools=tools,
                                         tool_choice=tool_choice,
                                         temperature=temperature, timeout=timeout, stream=False))
        message = response.choices[0].message
        calls = []
        for order, call in enumerate(message.tool_calls or []):
            # 部分服务端直接返回 dict/对象形式的 arguments，归一为 JSON 字符串
            raw = call.function.arguments
            if not isinstance(raw, str):
                raw = json.dumps(raw, ensure_ascii=False) if raw is not None else "{}"
            raw_arguments = raw or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = None
            # Preserve malformed arguments for the Runtime to reject; do not
            # silently turn them into an empty object.
            if not isinstance(arguments, dict):
                arguments = {"__invalid_raw_arguments__": raw_arguments}
            calls.append(ProviderToolCall(call.id, call.function.name, arguments,
                                          raw_arguments, order))
        usage = self._extract_usage(response.usage)
        self.last_usage = usage
        return ChatResponse(text=message.content or "", tool_calls=calls,
                            finish_reason=str(response.choices[0].finish_reason or "unknown"),
                            usage=usage)

    def generate_stream(self, model: str, messages: List[Dict],
                        temperature: float = 0, timeout: int = 60) -> Iterator[str]:
        """流式调用，yield 增量文本。

        请求携带 stream_options={"include_usage": True} 以在流式响应中获取 usage；
        部分兼容服务端不支持该参数（首帧前报错）时，去掉后重试一次。
        """
        collected = []
        for attempt, with_usage in enumerate((True, False)):
            kwargs = self._build_request_kwargs(model, messages, temperature=temperature,
                                                timeout=timeout, stream=True)
            if with_usage:
                kwargs["stream_options"] = {"include_usage": True}
            try:
                response = self.client.chat.completions.create(**kwargs)
                for chunk in response:
                    if chunk.usage:
                        self.last_usage = self._extract_usage(chunk.usage)
                    if not chunk.choices:
                        continue
                    content = chunk.choices[0].delta.content or ""
                    collected.append(content)
                    yield content
                return
            except Exception as e:
                # 仅在尚未输出任何内容、且错误特征明确指向 stream_options
                # 不被支持时回退（401/参数错等直接抛出，避免重复请求）。
                if attempt == 0 and not collected \
                        and self._stream_options_unsupported(e):
                    logger.warning("服务端不支持 stream_options，回退为不带 usage 的流式请求: %s", e)
                    continue
                raise

    def generate_stream_with_tools(self, model: str, messages: List[Dict],
                                   tools: List[Dict], temperature: float = 0,
                                   timeout: int = 60) -> Iterator[ProviderStreamEvent]:
        """Normalize OpenAI-compatible text and tool-call deltas.

        请求携带 stream_options={"include_usage": True}；部分兼容服务端不支持时
        去掉后重试一次（仅在未转发任何事件时回退，避免重复输出）。
        """
        # 流式专用：标量超时升级为 httpx.Timeout（read 空闲上限放宽），
        # 非流式路径（generate/generate_with_tools）保持标量不动。
        timeout = _as_stream_timeout(timeout)
        yielded_any = False
        for attempt, with_usage in enumerate((True, False)):
            kwargs = self._build_request_kwargs(model, messages, tools=tools,
                                                temperature=temperature, timeout=timeout, stream=True)
            if with_usage:
                kwargs["stream_options"] = {"include_usage": True}
            calls: dict[int, dict] = {}
            try:
                response = self.client.chat.completions.create(**kwargs)
                for chunk in response:
                    if getattr(chunk, "usage", None):
                        self.last_usage = self._extract_usage(chunk.usage)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    # Providers expose hidden reasoning under several field names.
                    # Keep the normalized event stable so the WebUI can render it.
                    reasoning = (
                        getattr(delta, "reasoning_content", None)
                        or getattr(delta, "reasoning", None)
                        or getattr(delta, "thinking", None)
                        or getattr(delta, "thought", None)
                    )
                    if reasoning:
                        yielded_any = True
                        yield ProviderStreamEvent(type="reasoning_delta", text=str(reasoning))
                    if delta.content:
                        yielded_any = True
                        yield ProviderStreamEvent(type="text_delta", text=delta.content)
                    for partial in delta.tool_calls or []:
                        index = partial.index
                        item = calls.setdefault(index, {
                            "id": partial.id or "", "name": "",
                            "arguments": "", "started": False,
                        })
                        if partial.id:
                            item["id"] = partial.id
                        function = partial.function
                        if function and function.name:
                            item["name"] = function.name
                        # 首帧即使只有 arguments（id/name 尚未到达）也补发
                        # tool_call_start，保证事件顺序（start 先于 delta）。
                        if not item["started"]:
                            item["started"] = True
                            yielded_any = True
                            yield ProviderStreamEvent(type="tool_call_start",
                                                      call_id=item["id"],
                                                      name=item["name"], order=index)
                        if function and function.arguments:
                            raw_arg = function.arguments
                            if not isinstance(raw_arg, str):
                                raw_arg = json.dumps(raw_arg, ensure_ascii=False)
                            item["arguments"] += raw_arg
                            yielded_any = True
                            yield ProviderStreamEvent(type="tool_call_delta",
                                                      call_id=item["id"], name=item["name"],
                                                      arguments_delta=raw_arg, order=index)
                for index, item in sorted(calls.items()):
                    raw = item["arguments"] or "{}"
                    try:
                        arguments = json.loads(raw)
                    except json.JSONDecodeError:
                        arguments = {"__invalid_raw_arguments__": raw}
                    if not isinstance(arguments, dict):
                        arguments = {"__invalid_raw_arguments__": raw}
                    yield ProviderStreamEvent(type="tool_call_end", call_id=item["id"],
                                              name=item["name"], arguments=arguments, order=index)
                return
            except Exception as e:
                # 仅在尚未转发任何事件、且错误特征明确指向 stream_options
                # 不被支持时回退（401/参数错等直接抛出，避免重复请求）。
                if attempt == 0 and not yielded_any \
                        and self._stream_options_unsupported(e):
                    logger.warning("服务端不支持 stream_options，回退为不带 usage 的流式请求: %s", e)
                    continue
                raise

    # ============================================================
    # stream_options 兼容性判定
    # ============================================================

    # 错误消息中出现这些特征串才视为“服务端不支持 stream_options”。
    _STREAM_OPTIONS_ERROR_MARKERS = (
        "stream_options",               # 主流网关直接点名参数
        "unknown parameter",            # OpenAI 风格: Unknown parameter: 'stream_options'
        "unrecognized request argument",  # vLLM 等风格
    )

    @classmethod
    def _stream_options_unsupported(cls, exc: BaseException) -> bool:
        """首帧前异常是否表现为「服务端不支持 stream_options」。

        仅当异常链（含 __cause__/__context__，连同原始异常最多检查 6 层）
        的消息明确命中特征串时才允许去掉该参数重发一次；鉴权失败（401）、
        其他参数校验错误等一律原样抛出——此前把任意首帧前异常都当成
        stream_options 问题重发，会掩盖真实错误并造成重复请求。
        """
        current: Optional[BaseException] = exc
        for _ in range(6):
            if current is None:
                break
            text = str(current).lower()
            if any(marker in text for marker in cls._STREAM_OPTIONS_ERROR_MARKERS):
                return True
            nxt = current.__cause__ or current.__context__
            if nxt is current:
                break
            current = nxt
        return False

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

        契约③：DeepSeek 等提供商返回的 prompt_cache_hit_tokens /
        prompt_cache_miss_tokens（prompt 缓存命中/未命中）存在时透传进
        last_usage（经 llm_client 原样进入 llm.last_usage 与 stats 链路）。
        """
        if not usage:
            return None
        result = {
            "input_tokens": (getattr(usage, "input_tokens", None)
                             or getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": (getattr(usage, "output_tokens", None)
                              or getattr(usage, "completion_tokens", 0) or 0),
        }
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        if hit is not None:
            result["prompt_cache_hit_tokens"] = int(hit)
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        if miss is not None:
            result["prompt_cache_miss_tokens"] = int(miss)
        return result
