# -*- coding: utf-8 -*-
"""
内置 Hook 示例 — 审计日志 / 通知 / 敏感词过滤

用法:
    from core.hook.builtin import audit_logger, webhook_notifier, sensitive_word_filter
    hm.register(HookEvent.POST_TOOL, PythonHook(audit_logger, name="audit"))
    hm.register(HookEvent.USER_PROMPT,
                PythonHook(sensitive_word_filter(["password"]), name="swf"))
"""

from __future__ import annotations

import logging
import threading

from .events import HookContext, HookEvent, HookResult, Decision

_hook_logger = logging.getLogger("jk_agent.hook.audit")


# ================================================================
# audit_logger — 工具调用审计
# ================================================================

def audit_logger(ctx: HookContext) -> HookResult:
    """POST_TOOL / DENIED 审计日志。

    把工具调用和拦截事件写入 log/hook-audit.log（走 jk_agent.hook.audit logger）。
    可注册到 post_tool + denied 事件。
    """
    p = ctx.payload or {}
    _hook_logger.info(
        "[HOOK-AUDIT] event=%s tool=%s is_error=%s reason=%s",
        ctx.event.value,
        p.get("tool_name", ""),
        p.get("is_error", False),
        p.get("reason", ""),
    )
    return HookResult(Decision.CONTINUE)


# ================================================================
# webhook_notifier — 企业微信 / 钉钉通知
# ================================================================

def webhook_notifier(url: str, tools: tuple = ("bash", "write", "file_mgr")):
    """工厂：pre_tool 高危工具调用时推送到 webhook。

    在独立 daemon 线程跑 HTTP，不阻塞主 agent 循环。
    URL 支持企业微信 / 钉钉 / Slack webhook。

    用法:
        notifier = webhook_notifier("https://hook.example/webhook")
        hm.register(HookEvent.PRE_TOOL, PythonHook(notifier))
    """
    def _notify(ctx: HookContext) -> HookResult:
        tool_name = (ctx.payload or {}).get("tool_name", "")
        if tool_name not in tools:
            return HookResult(Decision.CONTINUE)

        def _post() -> None:
            try:
                import requests
                requests.post(
                    url,
                    json={
                        "text": (
                            f"🤖 agent 调用 {tool_name}\n"
                            f"session: {ctx.session_id}\n"
                            f"params: {ctx.payload.get('params', {})}"
                        ),
                    },
                    timeout=5,
                )
            except Exception:
                pass  # 通知失败不影响 agent

        threading.Thread(target=_post, daemon=True).start()
        return HookResult(Decision.CONTINUE)

    return _notify


# ================================================================
# sensitive_word_filter — 用户输入敏感词拦截
# ================================================================

def sensitive_word_filter(words: list[str]) -> object:
    """工厂：user_prompt 敏感词过滤，命中则 BLOCK。

    用法:
        swf = sensitive_word_filter(["password", "token", "密钥"])
        hm.register(HookEvent.USER_PROMPT, PythonHook(swf))
    """
    _words = [str(w) for w in words]

    def _filter(ctx: HookContext) -> HookResult:
        prompt = (ctx.payload or {}).get("prompt", "")
        prompt_lower = prompt.lower()
        for w in _words:
            if w.lower() in prompt_lower:
                return HookResult(Decision.BLOCK,
                                  reason=f"输入含敏感词: {w}")
        return HookResult(Decision.CONTINUE)

    _filter.__name__ = "sensitive_word_filter"
    return _filter


# ================================================================
# block_pattern_filter — 工具调用模式拦截
# ================================================================

def block_pattern_filter(patterns: list[str]) -> object:
    """工厂：pre_tool 模式匹配拦截——匹配工具参数中是否含危险模式。

    用法:
        bpf = block_pattern_filter(["rm -rf /", "format C:"])
        hm.register(HookEvent.PRE_TOOL, PythonHook(bpf))
    """
    _patterns = [str(p) for p in patterns]

    def _filter(ctx: HookContext) -> HookResult:
        import json as _json
        p = ctx.payload or {}
        params_str = _json.dumps(p, ensure_ascii=False, default=str)
        for pat in _patterns:
            if pat.lower() in params_str.lower():
                return HookResult(Decision.BLOCK,
                                  reason=f"命中拦截模式: {pat}")
        return HookResult(Decision.CONTINUE)

    _filter.__name__ = "block_pattern_filter"
    return _filter
