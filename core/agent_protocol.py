"""Provider-neutral response protocol and safe text fallbacks.

Natural-language output is never searched for executable commands.  The legacy
adapter deliberately accepts only a complete, unambiguous legacy response so
that examples, logs, and fenced code remain ordinary visible text.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProtocolMode(str, Enum):
    NATIVE = "native"
    JSON_ENVELOPE = "json_envelope"
    LEGACY = "legacy"
    PLAIN_TEXT = "plain_text"


class ParseStatus(str, Enum):
    VALID = "valid"
    FALLBACK = "fallback"
    INVALID = "invalid"
    RETRYABLE_ERROR = "retryable_error"


@dataclass(frozen=True)
class ProtocolDiagnostic:
    code: str
    message: str


@dataclass
class ToolCall:
    call_id: str
    internal_name: str
    provider_name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None
    order: int = 0


@dataclass
class AssistantTurn:
    raw_text: str
    visible_text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    protocol_mode: ProtocolMode = ProtocolMode.PLAIN_TEXT
    parse_status: ParseStatus = ParseStatus.VALID
    diagnostics: list[ProtocolDiagnostic] = field(default_factory=list)
    usage: dict | None = None
    thought: str | None = None


_FINAL_LINE = re.compile(r"^(?:FINAL_ANSWER|最终回答)[：:]\s*(.*)$", re.I)
_ACTION_LINE = re.compile(r"^(?:ACTION|行动)[：:]\s*\[?([\w./-]+)\]?\s*$", re.I)
_INPUT_LINE = re.compile(r"^(?:INPUT|输入)[：:]\s*(.*)$", re.I)
_THOUGHT_LINE = re.compile(r"^(?:THOUGHT|思考)[：:]\s*(.*)$", re.I)


def _fallback(raw: str, code: str, message: str, *, invalid: bool = False) -> AssistantTurn:
    return AssistantTurn(
        raw_text=raw, visible_text=raw, protocol_mode=ProtocolMode.PLAIN_TEXT,
        parse_status=ParseStatus.INVALID if invalid else ParseStatus.FALLBACK,
        diagnostics=[ProtocolDiagnostic(code, message)],
    )


def parse_json_envelope(raw: str) -> AssistantTurn:
    """Parse only an entire JSON object; never extract JSON from prose."""
    stripped = raw.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return _fallback(raw, "not_json_envelope", "response is not a JSON envelope")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return _fallback(raw, "invalid_json", str(exc), invalid=True)
    if not isinstance(value, dict) or set(value) - {"version", "type", "answer", "calls"}:
        return _fallback(raw, "invalid_envelope_fields", "unknown or invalid envelope fields", invalid=True)
    if value.get("version") != "agent.turn.v1":
        return _fallback(raw, "invalid_envelope_version", "unsupported envelope version", invalid=True)
    kind = value.get("type")
    if kind == "final" and set(value) == {"version", "type", "answer"} and isinstance(value["answer"], str):
        return AssistantTurn(raw, value["answer"], protocol_mode=ProtocolMode.JSON_ENVELOPE)
    if kind == "tool_calls" and set(value) == {"version", "type", "calls"} and isinstance(value["calls"], list):
        calls: list[ToolCall] = []
        seen: set[str] = set()
        for order, call in enumerate(value["calls"]):
            if not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}:
                return _fallback(raw, "invalid_tool_call", "tool call shape is invalid", invalid=True)
            call_id, name, arguments = call["id"], call["name"], call["arguments"]
            if not isinstance(call_id, str) or not call_id or call_id in seen or not isinstance(name, str) or not name or not isinstance(arguments, dict):
                return _fallback(raw, "invalid_tool_call", "tool call id, name, or arguments are invalid", invalid=True)
            seen.add(call_id)
            calls.append(ToolCall(call_id, name, name, arguments, json.dumps(arguments, ensure_ascii=False), order))
        return AssistantTurn(raw, "", calls, "tool_calls", ProtocolMode.JSON_ENVELOPE)
    return _fallback(raw, "invalid_envelope", "final and tool_calls are mutually exclusive", invalid=True)


def parse_legacy_response(raw: str, *, allow_execute: bool = False) -> AssistantTurn:
    """Strict compatibility parser for historic ReAct responses.

    A final tag is presentation-only and only valid as the first nonempty line.
    Executable actions require opt-in and a complete action-only document.
    """
    if "```" in raw or "<" in raw:
        return _fallback(raw, "legacy_ambiguous_markup", "legacy tags in markup are visible text")
    lines = raw.splitlines()
    nonempty = [(index, line) for index, line in enumerate(lines) if line.strip()]
    if not nonempty:
        return AssistantTurn(raw, raw, protocol_mode=ProtocolMode.PLAIN_TEXT)
    _, first = nonempty[0]
    final = _FINAL_LINE.match(first.strip())
    control_lines = [line for _, line in nonempty if _FINAL_LINE.match(line.strip()) or _ACTION_LINE.match(line.strip()) or _INPUT_LINE.match(line.strip())]
    if final:
        if len(control_lines) != 1:
            return _fallback(raw, "legacy_mixed_controls", "mixed legacy controls are not executable", invalid=True)
        answer = "\n".join([final.group(1), *lines[nonempty[0][0] + 1:]]).strip()
        return AssistantTurn(raw, answer, protocol_mode=ProtocolMode.LEGACY)
    if not allow_execute:
        return _fallback(raw, "legacy_execution_disabled", "legacy tool execution is disabled")
    calls: list[ToolCall] = []
    thought: str | None = None
    pos = 0
    while pos < len(lines) and not lines[pos].strip():
        pos += 1
    if pos < len(lines) and (m := _THOUGHT_LINE.match(lines[pos].strip())):
        thought = m.group(1).strip()
        pos += 1
    while pos < len(lines):
        if not lines[pos].strip():
            pos += 1
            continue
        action = _ACTION_LINE.match(lines[pos].strip())
        if not action or pos + 1 >= len(lines):
            return _fallback(raw, "legacy_invalid_action", "legacy response is not action-only", invalid=True)
        input_line = _INPUT_LINE.match(lines[pos + 1].strip())
        if not input_line:
            return _fallback(raw, "legacy_missing_input", "legacy action has no INPUT", invalid=True)
        raw_args = input_line.group(1).strip()
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            return _fallback(raw, "legacy_invalid_arguments", str(exc), invalid=True)
        if not isinstance(args, dict):
            return _fallback(raw, "legacy_arguments_not_object", "INPUT must be a JSON object", invalid=True)
        calls.append(ToolCall(f"legacy_{uuid.uuid4().hex}", action.group(1), action.group(1), args, raw_args, len(calls)))
        pos += 2
    if not calls:
        return _fallback(raw, "legacy_no_action", "no legacy action found")
    return AssistantTurn(raw, "", calls, "tool_calls", ProtocolMode.LEGACY, thought=thought)


def parse_text_response(raw: str, *, mode: str = "legacy", legacy_execute: bool = False) -> AssistantTurn:
    if mode in {"json_envelope", "auto"}:
        parsed = parse_json_envelope(raw)
        if parsed.protocol_mode == ProtocolMode.JSON_ENVELOPE:
            return parsed
        if mode == "json_envelope":
            return parsed
    return parse_legacy_response(raw, allow_execute=legacy_execute)
