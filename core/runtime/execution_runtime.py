"""Provider-native Agent execution loop shared by every task source."""

from __future__ import annotations

import uuid

from core.config_loader import is_enabled
from core.debug import logger
from core.hook import Decision, HookManager
from core.runtime.text_normalization import normalize_model_text


class AgentExecutionRuntime:
    """Run the canonical native tool-call loop for an Agent-shaped host."""

    def run(self, agent, user_input: str, verbose: bool = True,
            images: list | None = None, event_sink=None) -> str:
        """Run the native, typed tool-call loop.

        Tools are never parsed from model text.  The provider yields native
        function calls; this loop validates, authorizes, executes and records
        them as separate assistant/tool messages, mirroring Pi's agent loop.
        """
        agent._event_sink = event_sink
        agent._run_id = uuid.uuid4().hex
        agent._event_seq = 0
        if not hasattr(agent, "hooks"):
            agent.hooks = HookManager(enabled=False)
        if not hasattr(agent, "_stop_block_count"):
            agent._stop_block_count = 0
        # The WebUI may have changed skill files since this long-lived session
        # was created.  Synchronize before sending the system message to LLM.
        agent._refresh_skills()
        agent._init_mcp_if_needed()
        if not agent.messages:
            agent.messages.append({"role": "system", "content": agent.system_prompt})
        agent._light_compress()
        agent._check_context(verbose=verbose)
        hr = agent.hooks.run_user_prompt(user_input)
        if hr.decision == Decision.BLOCK:
            return f"⛔ 输入被 hook 拦截: {hr.reason}"
        if hr.decision == Decision.MODIFY and hr.data:
            user_input = hr.data.get("prompt", user_input)
        user_message = {"role": "user", "content": user_input}
        if images:
            blocks = ([{"type": "text", "text": user_input}] if user_input else []) + list(images)
            user_message["content"] = blocks
        agent.messages.append(user_message)
        agent._truncate_history()
        agent._emit_event("agent_start", message_id="user")
        agent._emit_event("message_start", role="user", content=user_input)
        agent._emit_event("message_end", role="user")
        provider_tools, name_map = agent.tool_registry.get_provider_tools()

        for step in range(1, agent.max_steps + 1):
            if agent._consume_stop():
                agent.store.save_session()
                agent._emit_event("agent_end", reason="stopped")
                return "⏹️ 已停止"
            agent._turn_id = f"turn_{step}"
            agent._emit_event("turn_start", step=step)
            assistant_id = uuid.uuid4().hex
            agent._emit_event("message_start", message_id=assistant_id, role="assistant")
            try:
                native_stream = is_enabled(
                    getattr(agent, "native_tool_streaming", True), True)
                if native_stream:
                    response = agent.llm.stream_with_tools(
                        agent.messages, provider_tools, temperature=0,
                        on_event=lambda event: agent._forward_provider_event(assistant_id, event),
                    )
                else:
                    response = agent.llm.complete(agent.messages, tools=provider_tools, temperature=0)
                    for call in response.tool_calls:
                        agent._forward_provider_event(assistant_id, {
                            "type": "tool_call_start", "call_id": call.call_id,
                            "name": call.name, "order": call.order,
                        })
                        agent._forward_provider_event(assistant_id, {
                            "type": "tool_call_end", "call_id": call.call_id,
                            "name": call.name, "arguments": call.arguments, "order": call.order,
                        })
                    if response.text:
                        agent._forward_provider_event(assistant_id,
                                                     {"type": "text_delta", "text": response.text})
            except Exception as exc:
                logger.error("原生工具调用失败: %s", exc, exc_info=True)
                agent._emit_event("message_end", message_id=assistant_id, status="error")
                agent._emit_event("agent_end", reason="error")
                return f"❌ LLM 调用失败: {exc}"
            agent.store.set_anchor(agent.llm.last_usage)

            if not response.tool_calls:
                answer = normalize_model_text(response.text)
                agent.messages.append({"role": "assistant", "content": answer, "kind": "final"})
                agent._emit_event("message_end", message_id=assistant_id, role="assistant",
                                 content=answer, finish_reason=response.finish_reason)
                agent._emit_event("turn_end", step=step, tool_calls=0)
                agent._emit_event("agent_end", reason="completed")
                agent.store.save_session()
                agent._save_memory(user_input)
                return answer or "（模型未返回可见文本）"

            native_calls = []
            for call in sorted(response.tool_calls, key=lambda item: item.order):
                call_id = call.call_id or uuid.uuid4().hex
                internal_name = name_map.get(call.name)
                if internal_name is None and agent.tool_registry.get_tool(call.name):
                    internal_name = call.name
                native_calls.append((call_id, call.name, internal_name, call.arguments, call.raw_arguments))
            agent.messages.append({
                "role": "assistant", "content": response.text or None, "kind": "tool_calls",
                "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": provider_name, "arguments": raw_arguments or "{}"}}
                    for call_id, provider_name, _, _, raw_arguments in native_calls],
            })
            agent._emit_event("message_end", message_id=assistant_id, role="assistant",
                             finish_reason="tool_calls")

            result_count = 0
            for call_id, provider_name, tool_name, arguments, raw_arguments in native_calls:
                observation, is_error = agent._tool_runtime.execute_native_call(agent, 
                    call_id, provider_name, tool_name, arguments, raw_arguments)
                agent.messages.append({"role": "tool", "tool_call_id": call_id,
                                      "name": provider_name, "content": observation,
                                      "kind": "tool_result", "is_error": is_error})
                agent._emit_event("message_start", message_id=f"result_{call_id}", role="tool",
                                 tool_call_id=call_id, tool=tool_name or provider_name)
                agent._emit_event("message_end", message_id=f"result_{call_id}", role="tool",
                                 tool_call_id=call_id, tool=tool_name or provider_name,
                                 content=observation, is_error=is_error)
                result_count += 1
            agent._emit_event("turn_end", step=step, tool_calls=result_count)
            agent._light_compress()
            agent._check_context(verbose=False)
            agent._truncate_history()

        agent.store.save_session()
        agent._emit_event("agent_end", reason="max_steps")
        return f"⚠️ 已达最大步骤数 {agent.max_steps}"
