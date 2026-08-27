# -*- coding: utf-8 -*-
"""P1-1 修复回归：anthropic / gemini 原生 function calling + 兜底收窄。

覆盖（全部走 mock/fake 传输，无真实网络）：
  1. anthropic：tool 定义翻译、tool_use 解析、tool_result 回传往返、流式事件；
  2. gemini：functionDeclarations 清洗、functionCall 解析、functionResponse
     回传往返、流式事件；
  3. llm_client.stream_with_tools：仅 NotImplementedError 才降级且告警，
     其他异常原样抛出；complete() 调用前签名探测 tool_choice；
  4. openai_adapter：非 stream_options 异常不重发。

SDK 形状假设以 anthropic==0.122.0 / google-genai==2.18.1 的对象属性为准
（SimpleNamespace 模拟），详见各适配器模块 docstring。
"""
import unittest
from types import SimpleNamespace

from core.llm_client import JKAgentLLM
from core.protocols.base import ChatResponse, ProviderToolCall

# ============================================================
# 公共 fake 构造
# ============================================================

TOOLS = [{"type": "function", "function": {
    "name": "read_file__ab12", "description": "读文件",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "路径"},
    }, "required": ["path"]},
}}]

TOOL_CALL_MSG = {"role": "assistant", "content": None, "kind": "tool_calls",
                 "tool_calls": [{"id": "t1", "type": "function",
                                 "function": {"name": "read_file__ab12",
                                              "arguments": '{"path": "a.txt"}'}}]}
TOOL_RESULT_MSG = {"role": "tool", "tool_call_id": "t1",
                   "name": "read_file__ab12", "content": "file body",
                   "kind": "tool_result", "is_error": False}


def _anthropic_response(content_blocks, stop_reason="end_turn",
                        usage=(11, 7)):
    return SimpleNamespace(content=content_blocks, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=usage[0],
                                                 output_tokens=usage[1]))


class _FakeAnthropicMessages:
    def __init__(self, create_response=None, stream=None):
        self.create_response = create_response
        self._stream = stream
        self.create_kwargs = None
        self.stream_kwargs = None

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return self.create_response

    def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        return self._stream


class _FakeAnthropicStream:
    """模拟 client.messages.stream() 的上下文管理器（迭代原始事件）。"""

    def __init__(self, events, final_message):
        self._events = events
        self._final = final_message

    def __iter__(self):
        return iter(self._events)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


def _new_anthropic_adapter(messages_fake):
    from core.protocols.anthropic_adapter import AnthropicAdapter
    adapter = AnthropicAdapter(api_key="test-key")
    adapter.client = SimpleNamespace(messages=messages_fake)
    return adapter


class _FakeGeminiModels:
    def __init__(self, response=None, chunks=None):
        self.response = response
        self.chunks = chunks
        self.generate_kwargs = None
        self.stream_kwargs = None

    def generate_content(self, **kwargs):
        self.generate_kwargs = kwargs
        return self.response

    def generate_content_stream(self, **kwargs):
        self.stream_kwargs = kwargs
        return iter(self.chunks)


def _new_gemini_adapter(models_fake):
    from core.protocols.gemini_adapter import GeminiAdapter
    adapter = GeminiAdapter(api_key="test-key")
    adapter.client = SimpleNamespace(models=models_fake)
    return adapter


def _gemini_text_part(text):
    return SimpleNamespace(text=text, function_call=None)


def _gemini_fc_part(name, args, call_id=None):
    return SimpleNamespace(text=None,
                           function_call=SimpleNamespace(id=call_id,
                                                         name=name, args=args))


def _gemini_response(parts, usage=(3, 5)):
    candidates = [SimpleNamespace(
        content=SimpleNamespace(parts=list(parts)), finish_reason=None)]
    return SimpleNamespace(candidates=candidates,
                           usage_metadata=SimpleNamespace(
                               prompt_token_count=usage[0],
                               candidates_token_count=usage[1]))


def _llm(adapter, protocol="anthropic"):
    """绕过 __init__ 组装 JKAgentLLM（与 test_llm_retry 同一约定）。"""
    llm = object.__new__(JKAgentLLM)
    llm._adapter = adapter
    llm._config_timeout = 60
    llm._protocol = protocol
    llm.model = "test-model"
    llm.last_usage = None
    return llm


# ============================================================
# Anthropic 原生 function calling
# ============================================================


class AnthropicNativeFCTests(unittest.TestCase):

    def test_tool_definition_translation(self):
        captured = _FakeAnthropicMessages(
            create_response=_anthropic_response([]))
        adapter = _new_anthropic_adapter(captured)
        resp = adapter.generate_with_tools("claude-x", [
            {"role": "user", "content": "hi"}], TOOLS)
        self.assertEqual(captured.create_kwargs["tools"], [{
            "name": "read_file__ab12",
            "description": "读文件",
            "input_schema": TOOLS[0]["function"]["parameters"],
        }])
        # 默认不传 tool_choice（API 默认 auto）
        self.assertNotIn("tool_choice", captured.create_kwargs)
        self.assertIsInstance(resp, ChatResponse)

    def test_named_tool_choice_translation(self):
        captured = _FakeAnthropicMessages(
            create_response=_anthropic_response([]))
        adapter = _new_anthropic_adapter(captured)
        adapter.generate_with_tools("claude-x", [{"role": "user", "content": "h"}],
                                    TOOLS, tool_choice={
                                        "type": "function",
                                        "function": {"name": "read_file__ab12"}})
        self.assertEqual(captured.create_kwargs["tool_choice"],
                         {"type": "tool", "name": "read_file__ab12"})

    def test_tool_use_response_parsing(self):
        blocks = [
            SimpleNamespace(type="text", text="let me check"),
            SimpleNamespace(type="tool_use", id="toolu_1",
                            name="read_file__ab12",
                            input={"path": "a.txt"}),
        ]
        captured = _FakeAnthropicMessages(
            create_response=_anthropic_response(blocks, stop_reason="tool_use"))
        adapter = _new_anthropic_adapter(captured)
        resp = adapter.generate_with_tools("claude-x", [
            {"role": "user", "content": "list files"}], TOOLS)
        self.assertEqual(resp.text, "let me check")
        self.assertEqual(len(resp.tool_calls), 1)
        call = resp.tool_calls[0]
        self.assertEqual(call.call_id, "toolu_1")
        self.assertEqual(call.name, "read_file__ab12")
        self.assertEqual(call.arguments, {"path": "a.txt"})
        self.assertEqual(resp.finish_reason, "tool_calls")
        self.assertEqual(resp.usage, {"input_tokens": 11, "output_tokens": 7})
        self.assertEqual(adapter.last_usage, resp.usage)

    def test_tool_result_roundtrip(self):
        """assistant.tool_calls → tool_use block；role:"tool" → tool_result。"""
        history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "list files"},
            TOOL_CALL_MSG,
            TOOL_RESULT_MSG,
            # 连续第二条 tool 结果必须合并进同一条 user 消息
            {"role": "tool", "tool_call_id": "t2", "name": "ls__cd34",
             "content": "boom", "is_error": True},
        ]
        blocks = [SimpleNamespace(type="tool_use", id="t1",
                                  name="read_file__ab12",
                                  input={"path": "a.txt"})]
        captured = _FakeAnthropicMessages(create_response=_anthropic_response(blocks))
        adapter = _new_anthropic_adapter(captured)
        adapter.generate_with_tools("claude-x", history, TOOLS)

        messages = captured.create_kwargs["messages"]
        # system 经既有 cache_control 逻辑升级为块列表，取回纯文本断言
        system = captured.create_kwargs["system"]
        self.assertIn("sys", system if isinstance(system, str)
                      else "".join(b.get("text", "") for b in system))
        self.assertEqual([m["role"] for m in messages],
                         ["user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], [{
            "type": "tool_use", "id": "t1",
            "name": "read_file__ab12", "input": {"path": "a.txt"}}])
        results = messages[2]["content"]
        self.assertEqual(len(results), 2)  # 连续结果合并为一条 user 消息
        self.assertEqual(results[0], {"type": "tool_result",
                                      "tool_use_id": "t1",
                                      "content": "file body"})
        # 最后一条 user 消息的末块被既有 A1-3 逻辑追加 cache_control 断点
        self.assertEqual(results[1], {"type": "tool_result",
                                      "tool_use_id": "t2",
                                      "content": "boom", "is_error": True,
                                      "cache_control": {"type": "ephemeral"}})

    def test_streaming_tool_events(self):
        events = [
            SimpleNamespace(type="content_block_start", index=0,
                            content_block=SimpleNamespace(type="text")),
            SimpleNamespace(type="content_block_delta", index=0,
                            delta=SimpleNamespace(type="text_delta",
                                                  text="checking")),
            SimpleNamespace(type="content_block_start", index=1,
                            content_block=SimpleNamespace(
                                type="tool_use", id="toolu_9",
                                name="ls__cd34")),
            SimpleNamespace(type="content_block_delta", index=1,
                            delta=SimpleNamespace(type="input_json_delta",
                                                  partial_json='{"pat')),
            SimpleNamespace(type="content_block_delta", index=1,
                            delta=SimpleNamespace(type="input_json_delta",
                                                  partial_json='h": "."}')),
            SimpleNamespace(type="content_block_stop", index=1),
        ]
        stream = _FakeAnthropicStream(events, final_message=_anthropic_response(
            [], usage=(5, 6)))
        captured = _FakeAnthropicMessages(stream=stream)
        adapter = _new_anthropic_adapter(captured)
        got = list(adapter.generate_stream_with_tools(
            "claude-x", [{"role": "user", "content": "ls"}], TOOLS))

        types = [e.type for e in got]
        self.assertEqual(types, ["text_delta", "tool_call_start",
                                 "tool_call_delta", "tool_call_delta",
                                 "tool_call_end"])
        start = got[1]
        self.assertEqual((start.call_id, start.name, start.order),
                         ("toolu_9", "ls__cd34", 1))
        end = got[-1]
        self.assertEqual(end.arguments, {"path": "."})
        deltas = "".join(e.arguments_delta for e in got if e.type == "tool_call_delta")
        self.assertEqual(deltas, '{"path": "."}')
        # 流式请求确实携带 tools 定义
        self.assertIn("tools", captured.stream_kwargs)
        self.assertEqual(adapter.last_usage,
                         {"input_tokens": 5, "output_tokens": 6})


# ============================================================
# Gemini 原生 function calling
# ============================================================


class GeminiNativeFCTests(unittest.TestCase):

    def test_function_declaration_sanitization(self):
        tools = [{"type": "function", "function": {
            "name": "f", "description": "d",
            "parameters": {"type": "object", "$schema": "http://x",
                           "additionalProperties": False,
                           "properties": {"p": {"type": "string"}},
                           "required": ["p"]}}},
            {"type": "function", "function": {
                "name": "noop", "description": "no params",
                "parameters": {"type": "object"}}}]
        captured = _FakeGeminiModels(response=_gemini_response([]))
        adapter = _new_gemini_adapter(captured)
        adapter.generate_with_tools("gemini-x", [{"role": "user", "content": "h"}],
                                    tools)
        config = captured.generate_kwargs["config"]
        decls = config.tools[0].function_declarations
        # $schema / additionalProperties 被白名单裁剪；type 大写枚举串
        from google.genai import types as genai_types
        schema = decls[0].parameters
        self.assertIsNone(getattr(schema, "additional_properties", None))
        self.assertNotIn("$schema", getattr(schema, "model_extra", {}) or {})
        self.assertEqual(schema.type, genai_types.Type.OBJECT)
        self.assertEqual(list(schema.properties.keys()), ["p"])
        # 无参工具直接省略 parameters（OBJECT 空 properties 会被服务端拒绝）
        self.assertIsNone(decls[1].parameters)

    def test_function_call_parsing(self):
        response = _gemini_response([
            _gemini_text_part("checking"),
            _gemini_fc_part("read_file__ab12", {"path": "a.txt"}),
        ])
        captured = _FakeGeminiModels(response=response)
        adapter = _new_gemini_adapter(captured)
        resp = adapter.generate_with_tools("gemini-x", [
            {"role": "user", "content": "list files"}], TOOLS)
        self.assertEqual(resp.text, "checking")
        self.assertEqual(len(resp.tool_calls), 1)
        call = resp.tool_calls[0]
        self.assertEqual(call.name, "read_file__ab12")
        self.assertEqual(call.arguments, {"path": "a.txt"})
        self.assertTrue(call.call_id)  # SDK 未给 id 时合成稳定 id
        self.assertEqual(resp.finish_reason, "tool_calls")
        self.assertEqual(resp.usage, {"input_tokens": 3, "output_tokens": 5})

    def test_function_response_roundtrip(self):
        history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "list files"},
            TOOL_CALL_MSG,
            TOOL_RESULT_MSG,
            {"role": "tool", "tool_call_id": "", "name": "ls__cd34",
             "content": "boom", "is_error": True},
        ]
        captured = _FakeGeminiModels(response=_gemini_response(
            [_gemini_fc_part("read_file__ab12", {"path": "a.txt"})]))
        adapter = _new_gemini_adapter(captured)
        adapter.generate_with_tools("gemini-x", history, TOOLS)

        contents = captured.generate_kwargs["contents"]
        roles = [c.role for c in contents]
        self.assertEqual(roles, ["user", "model", "user"])
        fc = contents[1].parts[0].function_call
        self.assertEqual(fc.name, "read_file__ab12")
        self.assertEqual(dict(fc.args), {"path": "a.txt"})
        responses = [p.function_response for p in contents[2].parts]
        self.assertEqual(responses[0].name, "read_file__ab12")
        self.assertEqual(responses[0].response, {"result": "file body"})
        self.assertEqual(responses[1].name, "ls__cd34")
        self.assertEqual(responses[1].response.get("error"), True)

    def test_streaming_tool_events(self):
        chunks = [
            _gemini_response([_gemini_text_part("hello")], usage=(1, 2)),
            _gemini_response([_gemini_fc_part("ls__cd34", {"path": "."})],
                             usage=(4, 8)),
        ]
        captured = _FakeGeminiModels(chunks=chunks)
        adapter = _new_gemini_adapter(captured)
        got = list(adapter.generate_stream_with_tools(
            "gemini-x", [{"role": "user", "content": "ls"}], TOOLS))
        types = [e.type for e in got]
        # Gemini 不做参数增量下发：functionCall 直接补 start+end 一对事件
        self.assertEqual(types, ["text_delta", "tool_call_start",
                                 "tool_call_end"])
        end = got[-1]
        self.assertEqual((end.name, end.arguments), ("ls__cd34", {"path": "."}))
        self.assertEqual(adapter.last_usage,
                         {"input_tokens": 4, "output_tokens": 8})


# ============================================================
# base.py 中性块展开健壮性
# ============================================================


class BaseNormalizeToolRolesTests(unittest.TestCase):

    def test_malformed_arguments_and_none_content(self):
        from core.protocols.base import ProtocolAdapter
        normalized = ProtocolAdapter._normalize_tool_roles([
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "t1", "type": "function",
                             "function": {"name": "f",
                                          "arguments": "{bad json"}}]},
            {"role": "tool", "tool_call_id": "t1", "name": "f",
             "content": None},
        ])
        assistant = normalized[0]
        self.assertIsNone(assistant.get("tool_calls"))
        block = assistant["content"][0]
        self.assertEqual(block["type"], "tool_call")
        self.assertEqual(block["arguments"],
                         {"__invalid_raw_arguments__": "{bad json"})
        result = normalized[1]
        self.assertEqual(result["role"], "user")
        self.assertEqual(result["content"][0]["type"], "tool_result")
        self.assertEqual(result["content"][0]["content"], "")

    def test_merge_tolerates_none_content(self):
        from core.protocols.base import ProtocolAdapter
        merged = ProtocolAdapter._merge_consecutive_same_role([
            {"role": "user", "content": None},
            {"role": "user", "content": "next"},
        ])
        self.assertEqual(len(merged), 1)
        self.assertIn("next", merged[0]["content"])


# ============================================================
# llm_client 兜底收窄 + complete() 签名探测
# ============================================================


class _FallbackAdapter:
    """generate_stream_with_tools 抛 NotImplementedError 的旧版适配器。"""

    def __init__(self):
        self.generate_kwargs = None

    def generate_stream_with_tools(self, *args, **kwargs):
        raise NotImplementedError("native tool streaming unsupported")

    def generate(self, model, messages, temperature, timeout):
        self.generate_kwargs = {"model": model, "messages": messages,
                                "temperature": temperature}
        return ChatResponse(text="plain answer", tool_calls=[],
                            finish_reason="stop")


class LLMClientFallbackTests(unittest.TestCase):

    def test_not_implemented_falls_back_with_warning(self):
        adapter = _FallbackAdapter()
        llm = _llm(adapter)
        events = []
        with self.assertLogs("jk_agent", level="WARNING") as cm:
            resp = llm.stream_with_tools(
                [{"role": "user", "content": "hi"}], TOOLS,
                on_event=lambda evt: events.append(evt))
        self.assertEqual(resp.text, "plain answer")
        self.assertEqual([e["type"] for e in events], ["text_delta"])
        # 明示降级告警：包含协议名与“无法执行工具”语义
        joined = "\n".join(cm.output)
        self.assertIn("NotImplementedError", joined)
        self.assertIn("降级", joined)
        self.assertIn("anthropic", joined)
        # 降级调用不得携带 tools（generate 本身也没有该参数）
        self.assertEqual(adapter.generate_kwargs["messages"],
                         [{"role": "user", "content": "hi"}])

    def test_other_exceptions_are_raised(self):
        class _BoomAdapter:
            def generate_stream_with_tools(self, *args, **kwargs):
                raise RuntimeError("boom: invalid temperature")

        llm = _llm(_BoomAdapter())
        with self.assertRaises(RuntimeError):
            llm.stream_with_tools([{"role": "user", "content": "hi"}], TOOLS)

    def test_attribute_error_is_raised_not_swallowed(self):
        class _NoMethodAdapter:
            last_usage = None  # 缺 generate_stream_with_tools 属性

            def generate(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("不应被调用")

        llm = _llm(_NoMethodAdapter())
        with self.assertRaises(AttributeError):
            llm.stream_with_tools([{"role": "user", "content": "hi"}], TOOLS)


class _LegacySigAdapter:
    """旧签名：不接受 tool_choice 关键字。"""

    def __init__(self):
        self.calls = []

    def generate_with_tools(self, model, messages, tools, temperature, timeout):
        self.calls.append({"model": model, "temperature": temperature})
        return ChatResponse(text="", tool_calls=[], finish_reason="stop")


class _ModernSigAdapter:
    def __init__(self):
        self.calls = []

    def generate_with_tools(self, model, messages, tools, temperature,
                            timeout, tool_choice=None):
        self.calls.append({"model": model, "tool_choice": tool_choice})
        return ChatResponse(text="", tool_calls=[], finish_reason="stop")


class CompleteSignatureProbeTests(unittest.TestCase):

    def test_legacy_signature_called_once_without_tool_choice(self):
        adapter = _LegacySigAdapter()
        llm = _llm(adapter)
        resp = llm.complete([{"role": "user", "content": "hi"}],
                            tools=TOOLS, tool_choice="auto")
        self.assertIsNotNone(resp)
        self.assertEqual(len(adapter.calls), 1)  # 无 TypeError 重放二次调用
        self.assertNotIn("tool_choice", adapter.calls[0])

    def test_modern_signature_receives_tool_choice(self):
        adapter = _ModernSigAdapter()
        llm = _llm(adapter)
        llm.complete([{"role": "user", "content": "hi"}],
                     tools=TOOLS, tool_choice="required")
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0]["tool_choice"], "required")


# ============================================================
# openai_adapter stream_options 回退收窄
# ============================================================


class _FakeCompletions:
    def __init__(self, behaviors):
        self.calls = []
        self.behaviors = list(behaviors)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        behavior = self.behaviors.pop(0) if self.behaviors else "ok"
        if isinstance(behavior, BaseException):
            raise behavior
        chunk = SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=behavior, tool_calls=None))])
        return iter([chunk])


def _new_openai_adapter(behaviors, tool_stream=False):
    from core.protocols.openai_adapter import OpenAIAdapter
    adapter = OpenAIAdapter(api_key="sk-test")
    completions = _FakeCompletions(behaviors)
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return adapter, completions


class OpenAIStreamOptionsNarrowingTests(unittest.TestCase):

    def test_auth_error_not_resent(self):
        adapter, completions = _new_openai_adapter([
            Exception("Error code: 401 - {'error': {'message': "
                      "'Incorrect API key provided'}}")])
        with self.assertRaises(Exception) as ctx:
            list(adapter.generate_stream("gpt-x", [
                {"role": "user", "content": "hi"}], 0, 60))
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(len(completions.calls), 1)  # 未重发

    def test_unknown_parameter_error_not_resent(self):
        adapter, completions = _new_openai_adapter([
            Exception("Invalid value for 'temperature': 7 is out of range")])
        with self.assertRaises(Exception):
            list(adapter.generate_stream("gpt-x", [
                {"role": "user", "content": "hi"}], 0, 60))
        self.assertEqual(len(completions.calls), 1)

    def test_stream_options_error_retries_without_it(self):
        adapter, completions = _new_openai_adapter([
            Exception("Unknown parameter: 'stream_options'"), "hello"])
        got = list(adapter.generate_stream("gpt-x", [
            {"role": "user", "content": "hi"}], 0, 60))
        self.assertEqual("".join(got), "hello")
        self.assertEqual(len(completions.calls), 2)
        self.assertIn("stream_options", completions.calls[0])
        self.assertNotIn("stream_options", completions.calls[1])

    def test_with_tools_path_other_error_raises(self):
        adapter, completions = _new_openai_adapter([
            Exception("401 invalid api key")])
        with self.assertRaises(Exception):
            list(adapter.generate_stream_with_tools(
                "gpt-x", [{"role": "user", "content": "hi"}], TOOLS, 0, 60))
        self.assertEqual(len(completions.calls), 1)

    def test_with_tools_path_stream_options_error_retries(self):
        adapter, completions = _new_openai_adapter([
            Exception("unrecognized request argument: stream_options"), "ok"])
        got = list(adapter.generate_stream_with_tools(
            "gpt-x", [{"role": "user", "content": "hi"}], TOOLS, 0, 60))
        self.assertEqual([e.text for e in got], ["ok"])
        self.assertEqual(len(completions.calls), 2)
        self.assertNotIn("stream_options", completions.calls[1])


# ============================================================
# 流式读空闲超时：标量 → httpx.Timeout（read 空闲上限放宽）
# ============================================================


class StreamReadIdleTimeoutTests(unittest.TestCase):
    """流式方法（generate_stream_with_tools）传给 SDK 的 timeout 必须是
    httpx.Timeout 且 read >= 300；非流式路径保持标量不动。"""

    MESSAGES = [{"role": "user", "content": "hi"}]

    # ---- openai adapter ----

    def test_openai_stream_with_tools_passes_httpx_timeout(self):
        import httpx
        adapter, completions = _new_openai_adapter(["ok"])
        list(adapter.generate_stream_with_tools(
            "gpt-x", self.MESSAGES, TOOLS, 0, 60))
        timeout = completions.calls[0]["timeout"]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertGreaterEqual(timeout.read, 300)
        self.assertEqual(timeout.connect, 60)
        self.assertEqual(timeout.write, 60)
        self.assertEqual(timeout.pool, 60)

    def test_openai_stream_accepts_prestaged_httpx_timeout(self):
        """上游已构造 httpx.Timeout 时原样透传（幂等，不二次包装）。"""
        import httpx
        staged = httpx.Timeout(connect=5, read=999, write=5, pool=5)
        adapter, completions = _new_openai_adapter(["ok"])
        list(adapter.generate_stream_with_tools(
            "gpt-x", self.MESSAGES, TOOLS, 0, staged))
        self.assertIs(completions.calls[0]["timeout"], staged)

    def test_openai_non_stream_keeps_scalar_timeout(self):
        from types import SimpleNamespace as NS
        from core.protocols.openai_adapter import OpenAIAdapter
        adapter = OpenAIAdapter(api_key="sk-test")
        calls = []
        response = NS(choices=[NS(message=NS(content=None, tool_calls=[]),
                                finish_reason="stop")], usage=None)
        adapter.client = NS(chat=NS(completions=NS(
            create=lambda **kw: (calls.append(kw), response)[1])))
        adapter.generate_with_tools("gpt-x", self.MESSAGES, TOOLS, 0, 60)
        self.assertEqual(calls[0]["timeout"], 60)  # 标量原样

    def test_read_idle_env_override_and_garbage_fallback(self):
        import httpx
        import os
        from unittest import mock
        from core.protocols.openai_adapter import (
            _as_stream_timeout, _STREAM_READ_TIMEOUT_ENV)
        with mock.patch.dict(os.environ, {_STREAM_READ_TIMEOUT_ENV: "777"}):
            self.assertEqual(_as_stream_timeout(60).read, 777)
        with mock.patch.dict(os.environ,
                             {_STREAM_READ_TIMEOUT_ENV: "not-a-number"}):
            self.assertEqual(_as_stream_timeout(60).read, 300)  # 兜底
        with mock.patch.dict(os.environ):
            os.environ.pop(_STREAM_READ_TIMEOUT_ENV, None)
            self.assertEqual(_as_stream_timeout(60).read, 300)  # 默认

    # ---- anthropic adapter ----

    def test_anthropic_stream_with_tools_passes_httpx_timeout(self):
        import httpx
        stream = _FakeAnthropicStream(
            [], SimpleNamespace(usage=None))
        adapter = _new_anthropic_adapter(
            _FakeAnthropicMessages(stream=stream))
        list(adapter.generate_stream_with_tools(
            "claude-x", self.MESSAGES, TOOLS, 0, 60))
        timeout = adapter.client.messages.stream_kwargs["timeout"]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertGreaterEqual(timeout.read, 300)
        self.assertEqual(timeout.connect, 60)

    def test_anthropic_non_stream_keeps_scalar_timeout(self):
        response = _anthropic_response([])
        messages = _FakeAnthropicMessages(create_response=response)
        adapter = _new_anthropic_adapter(messages)
        adapter.generate_with_tools("claude-x", self.MESSAGES, TOOLS, 0, 60)
        self.assertEqual(messages.create_kwargs["timeout"], 60)  # 标量原样

    # ---- llm_client 层 ----

    @staticmethod
    def _capturing_adapter(captured):
        class _CapAdapter:
            last_usage = None

            def generate_stream_with_tools(self, model, messages, tools,
                                           temperature, timeout):
                captured["stream_timeout"] = timeout
                yield type("E", (), {
                    "type": "text_delta", "text": "hi", "call_id": None,
                    "name": None, "arguments_delta": None,
                    "arguments": None, "order": 0})()

            def generate(self, model, messages, temperature, timeout):
                captured["fallback_timeout"] = timeout
                return ChatResponse(text="plain", tool_calls=[],
                                    finish_reason="stop")

        return _CapAdapter()

    def test_llm_stream_with_tools_sends_httpx_timeout(self):
        import httpx
        captured = {}
        llm = _llm(self._capturing_adapter(captured))
        resp = llm.stream_with_tools(self.MESSAGES, [{"type": "function"}])
        self.assertEqual(resp.text, "hi")
        timeout = captured["stream_timeout"]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertGreaterEqual(timeout.read, 300)
        self.assertEqual(timeout.connect, 60)

    def test_llm_not_implemented_fallback_keeps_scalar_timeout(self):
        import httpx
        captured = {}

        class _FallbackCapture(_FallbackAdapter):
            def generate(self, model, messages, temperature, timeout):
                captured["timeout"] = timeout
                return super().generate(model, messages, temperature, timeout)

        llm = _llm(_FallbackCapture())
        with self.assertLogs("jk_agent", level="WARNING"):
            llm.stream_with_tools(self.MESSAGES, TOOLS)
        # 降级后的非流式调用保持标量（不受流式 read 放宽影响）
        self.assertEqual(captured["timeout"], 60)
        self.assertNotIsInstance(captured["timeout"], httpx.Timeout)


if __name__ == "__main__":
    unittest.main()
