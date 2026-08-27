# -*- coding: utf-8 -*-
"""P2-7：native tool-call 路径（complete / stream_with_tools）共享统一重试。"""
import unittest

from core.llm_client import JKAgentLLM


class _RateLimited(Exception):
    status_code = 429
    retry_after = 0  # 避免测试 sleep


class _BadRequest(Exception):
    status_code = 400


class _ChatResponse:
    def __init__(self, text="", tool_calls=None, usage=None):
        self.text = text
        self.tool_calls = tool_calls or []
        self.usage = usage or {}
        self.finish_reason = "stop"


class _ToolCall:
    def __init__(self, call_id="c1", name="read", arguments=None, order=0):
        self.call_id, self.name, self.arguments, self.order = call_id, name, arguments or {}, order


class _Adapter:
    """可配置次数的可重试故障（429），到点后成功；或单发一个不可重试错误。"""
    def __init__(self, failures=0, *, always_bad_request=False):
        self._failures = failures
        self._always_bad = always_bad_request
        self._calls = 0
        self.last_usage = {"total_tokens": 7}
    def _maybe_fail(self):
        self._calls += 1
        if self._always_bad:
            raise _BadRequest()
        if self._calls <= self._failures:
            raise _RateLimited()
    def generate_with_tools(self, *a, **k):
        self._maybe_fail()
        return _ChatResponse(tool_calls=[_ToolCall()])
    def generate_stream_with_tools(self, *a, **k):
        self._maybe_fail()
        yield type("E", (), {"type": "text_delta", "text": "hi", "call_id": None,
                             "name": None, "arguments_delta": None,
                             "arguments": None, "order": 0})()
    def generate(self, *a, **k):
        self._calls += 1
        return _ChatResponse(text="fallback")


def _llm(adapter):
    llm = object.__new__(JKAgentLLM)
    llm._adapter = adapter
    llm._config_timeout = 60
    llm._protocol = "openai"
    llm.model = "gpt-4o"
    llm.last_usage = None
    return llm


class LLPromptRetryTests(unittest.TestCase):

    def test_complete_retries_then_succeeds(self):
        adapter = _Adapter(failures=2)
        llm = _llm(adapter)
        resp = llm.complete([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
        self.assertIsNotNone(resp.tool_calls)
        self.assertEqual(adapter._calls, 3)  # 2 次可重试失败 (429) + 1 次成功

    def test_complete_raises_on_non_retryable_error(self):
        adapter = _Adapter(always_bad_request=True)
        llm = _llm(adapter)
        with self.assertRaises(_BadRequest):
            llm.complete([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])

    def test_stream_with_tools_retries_before_forwarding(self):
        adapter = _Adapter(failures=2)
        llm = _llm(adapter)
        events = []
        resp = llm.stream_with_tools(
            [{"role": "user", "content": "hi"}], [{"type": "function"}],
            on_event=lambda evt: events.append(evt))
        self.assertEqual(resp.text, "hi")
        self.assertEqual(len(events), 1)

    def test_retry_delay_respects_retry_after(self):
        delay = JKAgentLLM._retry_delay(1, _RateLimited())
        self.assertTrue(0 <= delay <= 60)


if __name__ == "__main__":
    unittest.main()

