"""Provider-neutral reasoning-effort configuration helpers.

The public config is intentionally a small common vocabulary.  Providers may
have richer, incompatible controls; those belong in provider-specific work
rather than being silently guessed here.
"""

from __future__ import annotations

from typing import Any, Mapping


REASONING_LEVELS = (
    "provider_default",
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

_VALID_LEVELS = frozenset(REASONING_LEVELS)


def normalize_reasoning_level(value: Any, *, source: str = "reasoning.level") -> str:
    """Return a validated public level, treating an omitted value as default."""
    if value is None or value == "":
        return "provider_default"
    if not isinstance(value, str):
        raise ValueError(f"{source} 必须是字符串")
    level = value.strip().lower()
    if level not in _VALID_LEVELS:
        choices = " / ".join(REASONING_LEVELS)
        raise ValueError(f"{source} 必须是以下之一: {choices}")
    return level


def reasoning_level_from_config(
    llm_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    explicit: str | None = None,
) -> str:
    """Resolve explicit > model > global configuration priority."""
    if explicit is not None:
        return normalize_reasoning_level(explicit, source="reasoning_level")

    model_reasoning = model_config.get("reasoning") or {}
    if not isinstance(model_reasoning, Mapping):
        raise ValueError("llm.models.<model>.reasoning 必须是对象")
    if "level" in model_reasoning:
        return normalize_reasoning_level(
            model_reasoning.get("level"), source="llm.models.<model>.reasoning.level")

    global_reasoning = llm_config.get("reasoning") or {}
    if not isinstance(global_reasoning, Mapping):
        raise ValueError("llm.reasoning 必须是对象")
    return normalize_reasoning_level(global_reasoning.get("level"), source="llm.reasoning.level")
