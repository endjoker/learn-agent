"""Non-destructive context view for provider calls."""
from __future__ import annotations
from copy import deepcopy
from typing import Any

class AgentContext:
    def __init__(self, messages: list[dict[str, Any]]):
        self._messages = messages

    def llm_messages(self) -> list[dict[str, Any]]:
        # Copy top-level records so provider adaptation cannot mutate transcript.
        # UI-only runtime records (Plan/Goal background tool calls and final
        # replies) are persisted for the WebUI timeline but never enter model
        # context. `internal` is deliberately NOT the filter: the compressor's
        # history_summary records are internal yet must reach the model.
        kept: list[dict[str, Any]] = []
        pending_ids: set[str] = set()  # 最近一条 assistant tool_calls 声明的调用 id
        for message in self._messages:
            if message.get("runtime"):
                continue
            role = message.get("role")
            if role == "tool":
                # 防御性修复：压缩/截断历史可能在 assistant tool_calls 缺失时
                # 留下孤立的 tool 结果，provider 会直接 400。丢弃孤儿（其内容
                # 早已被消费），不影响后续成对消息。
                call_id = message.get("tool_call_id")
                if not call_id or call_id not in pending_ids:
                    continue
                pending_ids.discard(call_id)
            elif role == "assistant":
                tool_calls = message.get("tool_calls") or []
                pending_ids = {t.get("id") for t in tool_calls} if isinstance(tool_calls, list) else set()
            else:
                pending_ids = set()
            kept.append(self._copy_message(message))
        # 第二遍：按 assistant 声明的 call_id 集合完整校验配对。某个声明的
        # call_id 缺 result（轮次被中断/单工具异常逃逸后只落了部分结果，即
        # "N 声明 M 结果"）时，为缺失者合成 is_error 占位结果，保持 OpenAI
        # 兼容配对；不再像旧实现那样只看"后一条是否 tool"——那会放过缺了
        # 其余 id 的转录，provider 报 "No tool output found for function call"
        # 并让整个会话级 400 卡死。
        result: list[dict[str, Any]] = []
        total = len(kept)
        index = 0
        while index < total:
            message = kept[index]
            result.append(message)
            if message.get("role") == "assistant":
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    cursor = index + 1
                    answered: set[str] = set()
                    # 先原样交付紧随其后的连续 tool 结果，并记录已覆盖的 id。
                    while cursor < total and kept[cursor].get("role") == "tool":
                        result.append(kept[cursor])
                        call_id = kept[cursor].get("tool_call_id")
                        if call_id:
                            answered.add(call_id)
                        cursor += 1
                    # 为缺失结果声明的 id 合成占位（顺序在真实结果之后，
                    # 仍位于下一条 assistant 之前，配对完整）。
                    for call in tool_calls:
                        call_id = call.get("id") if isinstance(call, dict) else None
                        if call_id and call_id not in answered:
                            result.append({
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": "工具执行中断，无结果",
                                "is_error": True,
                            })
                    index = cursor
                    continue
            index += 1
        return result

    @staticmethod
    def _copy_message(message: dict[str, Any]) -> dict[str, Any]:
        """浅拷贝外层 + 深拷贝可变负载（content/tool_calls）。

        P2：provider 适配层可能就地改写多模态 content blocks 或 tool_calls
        （如注入 usage/排序/去重），深拷贝杜绝其污染 transcript。
        """
        copied = dict(message)
        content = copied.get("content")
        if isinstance(content, list):
            copied["content"] = deepcopy(content)
        tool_calls = copied.get("tool_calls")
        if isinstance(tool_calls, list):
            copied["tool_calls"] = deepcopy(tool_calls)
        return copied
