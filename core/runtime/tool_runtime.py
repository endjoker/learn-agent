"""The single, policy-aware entry point for executing Agent tools."""

from __future__ import annotations

import json
from typing import Any

from core.debug import logger
from core.hook import Decision
from core.permission import ALLOW, ASK, DENY


class ToolRuntime:
    """Validate, authorize, hook and execute every native tool call.

    The runtime deliberately depends on a narrow Agent-shaped object rather
    than importing ``Agent``. This keeps ordinary chats, durable tasks and
    Child sessions on the same tool path without creating an import cycle.
    """

    def __init__(self, *, max_result_chars: int = 10000):
        try:
            self.max_result_chars = max(0, int(max_result_chars))
        except (TypeError, ValueError):
            self.max_result_chars = 10000

    def execute_text(self, agent, tool_name: str, input_str: str | None = None) -> str:
        if not input_str:
            arguments: dict[str, Any] = {}
        else:
            try:
                arguments = json.loads(input_str)
            except json.JSONDecodeError as exc:
                return f"❌ 参数不是合法 JSON: {exc}\n收到: {input_str}"
        if not isinstance(arguments, dict):
            return "❌ 参数必须是 JSON 对象"
        return self.execute_arguments(agent, tool_name, arguments)

    def execute_arguments(self, agent, tool_name: str, arguments: dict[str, Any]) -> str:
        tool = agent.tool_registry.get_tool(tool_name)
        if tool is None:
            available = ", ".join(item.name for item in agent.tool_registry.list_tools())
            return f"❌ 未知工具 '{tool_name}'。可用: {available}"
        errors = agent.tool_registry.validate_arguments(tool_name, arguments)
        if errors:
            return "❌ 参数校验失败: " + "; ".join(errors)
        try:
            return tool.execute(**arguments)
        except TypeError as exc:
            return (
                f"❌ 参数不匹配: {exc}\n"
                f"工具 '{tool_name}' 需要的参数:\n"
                f"{json.dumps(tool.parameters, ensure_ascii=False, indent=2)}"
            )
        except Exception as exc:
            logger.error("工具 '%s' 执行失败: %s", tool_name, exc, exc_info=True)
            return f"❌ 工具出错: {type(exc).__name__}: {exc}"

    def execute_native_call(self, agent, call_id: str, provider_name: str,
                            tool_name: str | None, arguments: Any,
                            raw_arguments: str | None = None) -> tuple[str, bool]:
        """Run one provider-native call and always return a typed observation."""
        display_name = tool_name or provider_name
        agent._emit_event("tool_execution_start", tool_call_id=call_id, tool=display_name,
                          arguments=arguments)
        if not tool_name:
            observation, is_error = f"❌ 未知工具: {provider_name}", True
        elif not isinstance(arguments, dict) or "__invalid_raw_arguments__" in arguments:
            observation, is_error = "❌ 工具参数不是完整 JSON 对象", True
        else:
            errors = agent.tool_registry.validate_arguments(tool_name, arguments)
            if errors:
                observation, is_error = "❌ 参数校验失败: " + "; ".join(errors), True
            else:
                level, reason = agent._gate_check(tool_name, arguments)
                if level == DENY:
                    agent.hooks.run_denied(tool_name, reason or "权限不足", level="gate")
                    observation, is_error = f"⛔ 已拒绝: {reason or '权限不足'}", True
                elif level == ASK and not self._approved(agent, tool_name, arguments):
                    observation, is_error = "⏭️ 用户未批准工具调用", True
                else:
                    observation, is_error = self.execute_authorized(agent, tool_name, arguments)
        observation = self._truncate_observation(observation)
        agent._emit_event("tool_execution_end", tool_call_id=call_id, tool=display_name,
                          result=observation, is_error=is_error)
        return observation, is_error

    @staticmethod
    def _approved(agent, tool_name: str, arguments: dict[str, Any]) -> bool:
        run_notification = getattr(agent.hooks, "run_notification", None)
        if callable(run_notification):
            notification = run_notification(
                tool_name, arguments, message="tool approval requested")
            if notification.decision == Decision.BLOCK:
                return False
        answer = agent._ask_user(tool_name, arguments)
        return answer in ("", "y", "yes", "a")

    def _truncate_observation(self, observation: Any) -> str:
        """Bound one tool observation before it is persisted into LLM context."""
        text = str(observation)
        limit = self.max_result_chars
        if limit <= 0 or len(text) <= limit:
            return text
        if limit < 80:
            return text[:limit]
        omitted = len(text) - limit
        marker = f"\n\n… [工具结果已截断，省略 {omitted} 个字符] …\n\n"
        payload_limit = max(1, limit - len(marker))
        omitted = len(text) - payload_limit
        marker = f"\n\n… [工具结果已截断，省略 {omitted} 个字符] …\n\n"
        payload_limit = max(1, limit - len(marker))
        head = max(1, int(payload_limit * 0.8))
        tail = max(0, payload_limit - head)
        return text[:head] + marker + (text[-tail:] if tail else "")

    def execute_authorized(self, agent, tool_name: str,
                           arguments: dict[str, Any]) -> tuple[str, bool]:
        before = agent.hooks.run_pre_tool(tool_name, arguments, gate_level="allow")
        if before.decision == Decision.BLOCK:
            return f"⛔ hook 拦截: {before.reason}", True
        if before.decision == Decision.MODIFY and before.data:
            arguments = before.data
        observation = self.execute_arguments(agent, tool_name, arguments)
        is_error = observation.startswith(("❌", "⛔", "⏭️"))
        after = agent.hooks.run_post_tool(tool_name, arguments, observation, is_error)
        if after.decision == Decision.MODIFY and after.data:
            observation = after.data.get("result", observation)
            is_error = observation.startswith(("❌", "⛔", "⏭️"))
        elif after.decision == Decision.BLOCK:
            observation, is_error = f"⛔ hook 拦截: {after.reason}", True
        return observation, is_error
