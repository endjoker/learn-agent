"""Local tool schema validation and provider-safe name mapping."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from typing import Any


class ToolNameCodec:
    """Encode registry names (including MCP ``server/tool`` names) safely."""
    @staticmethod
    def encode(internal_name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", internal_name).strip("_") or "tool"
        return f"{safe[:48]}__{hashlib.sha256(internal_name.encode()).hexdigest()[:8]}"


_MESSAGE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def sanitize_message_name(name: Any) -> str:
    """Normalize a message ``name`` to the OpenAI ``^[a-zA-Z0-9_-]+$`` pattern.

    模型回显的工具名可能携带斜杠/点/空格/中文（例如 MCP 工具原始名
    ``server/tool``、``web-search/search``），直接作为 tool 结果消息的
    ``name`` 透传给 OpenAI 会触发 400 invalid_value（input[N].name）。
    这里把非法字符替换为 ``_``；合法名原样保留；超长截断；空结果回退
    ``tool``。工具解析/执行仍使用原始 provider_name，不受影响。
    """
    value = str(name or "")
    if _MESSAGE_NAME_RE.match(value):
        return value
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return safe[:64] or "tool"


@lru_cache(maxsize=256)
def _validator_for_schema(schema_json: str):
    """按 schema 的 JSON 序列化键缓存 jsonschema Validator（B4）。

    工具 schema 是注册时固定的静态 JSON；每次工具调用都重新构造 Validator
    属于纯重复开销。缓存后同一 schema 只构造一次；Validator 的 iter_errors
    无状态，可反复调用。
    """
    from jsonschema import Draft202012Validator
    schema = json.loads(schema_json)
    return Draft202012Validator(schema or {"type": "object"})


def validate_arguments(schema: dict[str, Any], arguments: Any) -> list[str]:
    """Return validation messages; a missing optional dependency fails closed.

    B4：Validator 按 schema JSON 缓存（lru_cache）；schema 不可序列化时
    退化为一次性构造并保持 fail-closed 语义。
    """
    if not isinstance(arguments, dict):
        return ["arguments must be a JSON object"]
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["jsonschema is required to validate tool arguments"]
    try:
        validator = _validator_for_schema(
            json.dumps(schema or {"type": "object"}, sort_keys=True))
    except Exception as exc:
        return [f"tool schema is invalid: {exc}"]
    return [error.message for error in sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))]
