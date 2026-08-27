"""Provider turn adapter used by the unified AgentLoop."""
from __future__ import annotations

class ProviderTurnAdapter:
    """Keep provider streaming semantics outside Agent's loop ownership."""
    def __init__(self, agent): self.agent = agent

    def _forward_provider_event(self, assistant_id, event):
        if event.get("type") == "reasoning_delta":
            self.agent._emit_event(
                "reasoning_delta", message_id=assistant_id,
                text=event.get("text", ""))
            return
        self.agent._forward_provider_event(assistant_id, event)

    def complete(self, messages, provider_tools, assistant_id):
        cfg = getattr(self.agent, "_config", None) or {}
        runtime = cfg.get("agent_runtime", {})
        native_stream = bool(runtime.get("native_tool_streaming", True))
        if native_stream:
            return self.agent.llm.stream_with_tools(
                messages, provider_tools, temperature=0,
                on_event=lambda event: self._forward_provider_event(assistant_id, event))
        response = self.agent.llm.complete(messages, tools=provider_tools, temperature=0)
        if response is None:
            # P3：非流式回退防御——provider 返回 None（自定义适配器吞掉异常）
            # 时构造空响应，避免下游 AttributeError 破坏循环收口。
            response = self._empty_response()
        # P3：缺字段防御——tool_calls/text 可能为 None/缺失，逐个 getattr 兜底。
        for call in (getattr(response, "tool_calls", None) or []):
            self.agent._forward_provider_event(assistant_id, {"type": "tool_call_start", "call_id": call.call_id, "name": call.name, "order": call.order})
            self.agent._forward_provider_event(assistant_id, {"type": "tool_call_end", "call_id": call.call_id, "name": call.name, "arguments": call.arguments, "order": call.order})
        if getattr(response, "text", None):
            self.agent._forward_provider_event(assistant_id, {"type": "text_delta", "text": response.text})
        return response

    @staticmethod
    def _empty_response():
        """最小空响应：text="" / tool_calls=[] / finish_reason="stop"。"""
        from core.protocols.base import ChatResponse
        return ChatResponse(text="", tool_calls=[], finish_reason="stop", usage=None)
