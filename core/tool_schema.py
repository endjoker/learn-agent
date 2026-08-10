"""Local tool schema validation and provider-safe name mapping."""
from __future__ import annotations

import hashlib
import re
from typing import Any


class ToolNameCodec:
    """Encode registry names (including MCP ``server/tool`` names) safely."""
    @staticmethod
    def encode(internal_name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", internal_name).strip("_") or "tool"
        return f"{safe[:48]}__{hashlib.sha256(internal_name.encode()).hexdigest()[:8]}"


def validate_arguments(schema: dict[str, Any], arguments: Any) -> list[str]:
    """Return validation messages; a missing optional dependency fails closed."""
    if not isinstance(arguments, dict):
        return ["arguments must be a JSON object"]
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["jsonschema is required to validate tool arguments"]
    try:
        validator = Draft202012Validator(schema or {"type": "object"})
    except Exception as exc:
        return [f"tool schema is invalid: {exc}"]
    return [error.message for error in sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))]
