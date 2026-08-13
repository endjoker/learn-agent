"""Normalize legacy model control text before it reaches users or memory."""

from __future__ import annotations

import re


_SPECIAL_TOKENS = re.compile(
    r"<\|(?:im_start|im_end|assistant|user|system|endoftext)\|>", re.IGNORECASE,
)
_FINAL_ANSWER = re.compile(r"(?im)^\s*FINAL_ANSWER\s*[:：]\s*")
_LEADING_LABELS = re.compile(r"(?im)^\s*(?:THOUGHT|ACTION|INPUT)\s*[:：]\s*")


def normalize_model_text(value: str | None) -> str:
    """Remove retired textual-protocol markers from an LLM-visible reply."""
    text = _SPECIAL_TOKENS.sub("", value or "")
    final = _FINAL_ANSWER.search(text)
    if final:
        return text[final.end():].strip()
    return _LEADING_LABELS.sub("", text).strip()
