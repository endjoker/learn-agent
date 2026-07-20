# -*- coding: utf-8 -*-
"""
Hook 管理器 — 注册 / 分发 / 配置加载 / 结果合并

HookManager 是用户和 agent 的唯一交互入口：
  - register()  / unregister()     — 代码注册 hook
  - load_config()                   — 从 config/hooks.json 批量加载
  - dispatch()                      — agent 在各事件点调用
  - 便捷方法（run_*）                — agent 用，少写样板
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

from .events import HookEvent, HookContext, HookResult, Decision
from .hooks import BaseHook, PythonHook, CommandHook

logger = logging.getLogger("hello_agent.hook")


# ================================================================
# 内部数据类
# ================================================================

class _HookEntry:
    """HookManager 内部条目"""
    __slots__ = ("hook", "matcher", "priority")

    def __init__(self, hook: BaseHook, matcher: re.Pattern | None,
                 priority: int = 0):
        self.hook = hook
        self.matcher = matcher
        self.priority = priority


# ================================================================
# 管理器
# ================================================================

class HookManager:
    """Hook 生命周期管理器。

    用法：
        hm = HookManager()
        hm.register(HookEvent.PRE_TOOL, PythonHook(my_check))
        hm.dispatch(HookEvent.PRE_TOOL, {"tool_name": "bash"})
    """

    # ---- 配置路径约定 ----
    DEFAULT_CONFIG_NAME = "hooks.json"

    @staticmethod
    def _resolve_config_path(path: str | None = None) -> Path | None:
        """按约定找 hooks.json：传入路径 > config/hooks.json > 项目根 hooks.json"""
        if path:
            p = Path(path)
            if p.exists():
                return p
            return None
        candidates = [
            Path.cwd() / "config" / HookManager.DEFAULT_CONFIG_NAME,
            Path.cwd() / HookManager.DEFAULT_CONFIG_NAME,
        ]
        for p in candidates:
            if p.exists():
                return p
        return None

    def __init__(self, config_path: str | None = None,
                 enabled: bool = True):
        self.enabled = enabled
        self._hooks: dict[HookEvent, list[_HookEntry]] = defaultdict(list)
        self._agent_name = ""
        self._session_id = ""

        # 加载配置（自动按约定路径查找）
        resolved = self._resolve_config_path(config_path)
        if resolved:
            self.load_config(str(resolved))

    # ---- 注册 / 注销 ----

    def register(self, event: HookEvent, hook, matcher: str = "",
                 priority: int = 0) -> None:
        """注册 hook 到事件。

        matcher: 正则字符串，仅对 PRE_TOOL/POST_TOOL/DENIED 匹配工具名；
                 对其他事件忽略。空字符串 = 全匹配。
        """
        if not isinstance(hook, BaseHook):
            raise TypeError(f"hook 必须是 BaseHook 实例，收到 {type(hook)}")
        pattern = re.compile(matcher) if matcher else None
        self._hooks[event].append(_HookEntry(hook, pattern, priority))
        logger.debug(f"注册 hook: {event.value}={hook} matcher={matcher or '*'}"
                     f" priority={priority}")

    def unregister(self, event: HookEvent, hook: BaseHook) -> bool:
        """移除指定 hook 实例（按对象 identity）。"""
        entries = self._hooks.get(event)
        if not entries:
            return False
        for i, e in enumerate(entries):
            if e.hook is hook:
                entries.pop(i)
                logger.debug(f"注销 hook: {event.value}={hook}")
                return True
        return False

    def bind_agent(self, agent_name: str, session_id: str) -> None:
        self._agent_name = agent_name
        self._session_id = session_id

    # ---- 分发 ----

    def dispatch(self, event: HookEvent, payload: dict | None = None
                 ) -> HookResult:
        """触发事件，返回聚合 HookResult。

        - 未启用 / 无 hook → CONTINUE
        - 多个 hook 按 priority 降序执行
        - 任一 BLOCK → 立即返回 BLOCK（最严裁决）
        - MODIFY → data 链式合并（浅合并）
        - hook 异常 → ERROR 日志 + CONTINUE（不拖垮主流程）
        - 每个 hook 执行前后记 DEBUG 日志（耗时）
        """
        if not self.enabled:
            return HookResult(Decision.CONTINUE)
        entries = self._hooks.get(event)
        if not entries:
            return HookResult(Decision.CONTINUE)

        entries = sorted(entries, key=lambda e: e.priority, reverse=True)
        ctx = HookContext(
            event=event,
            agent_name=self._agent_name,
            session_id=self._session_id,
            payload=payload or {},
            timestamp=time.time(),
        )

        # matcher 过滤（仅工具类事件）
        if event in (HookEvent.PRE_TOOL, HookEvent.POST_TOOL, HookEvent.DENIED):
            tool_name = (payload or {}).get("tool_name", "")
            entries = [e for e in entries
                       if not e.matcher or e.matcher.search(tool_name)]

        results: list[HookResult] = []
        for entry in entries:
            t0 = time.time()
            try:
                r = entry.hook.run(ctx)
            except Exception:
                logger.error(
                    f"Hook 执行异常 [{event.value}]: {entry.hook}", exc_info=True,
                )
                r = HookResult(Decision.CONTINUE, reason="hook 内部异常")
            elapsed = (time.time() - t0) * 1000
            logger.debug(
                f"[HOOK] {event.value}: {entry.hook} → "
                f"{r.decision.value} ({elapsed:.0f}ms)"
                + (f" reason={r.reason}" if r.reason else "")
            )
            results.append(r)
            if r.decision == Decision.BLOCK:
                break   # 最严裁决：不再执行后续 hook

        return self._merge(results)

    @staticmethod
    def _merge(results: list[HookResult]) -> HookResult:
        """合并多个 HookResult — 最严裁决（BLOCK 优先，MODIFY 链式）。"""
        if not results:
            return HookResult(Decision.CONTINUE)
        final = HookResult(Decision.CONTINUE)
        merged_data: dict = {}
        has_explicit = False
        for r in results:
            if r.decision == Decision.BLOCK:
                return r
            if r.decision in (Decision.ALLOW, Decision.MODIFY):
                has_explicit = True
                if r.decision == Decision.MODIFY and r.data:
                    merged_data.update(r.data)
        if merged_data:
            final.decision = Decision.MODIFY
            final.data = merged_data
        elif has_explicit:
            final.decision = Decision.ALLOW
        return final

    # ---- 配置加载 ----

    def load_config(self, path: str) -> int:
        """从 config/hooks.json 加载命令式 hook + 内置过滤器。

        返回加载的 hook 总数。自动注册：
          - filters.sensitive_words → user_prompt hook（敏感词 BLOCK）
          - filters.block_patterns  → pre_tool hook（模式匹配 BLOCK）
          - hooks.{event}[].hooks  → CommandHook
        """
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        if not cfg.get("enabled", True):
            self.enabled = False
            return 0

        count = 0

        # ---- 内置过滤器 ----
        filters = cfg.get("filters", {})

        # 敏感词过滤 → user_prompt
        sensitive_words = filters.get("sensitive_words")
        if sensitive_words:
            from .builtin import sensitive_word_filter
            swf = sensitive_word_filter([str(w) for w in sensitive_words])
            self.register(HookEvent.USER_PROMPT, PythonHook(swf, name="sensitive_word_filter"))
            logger.info(f"已注册 sensitive_word_filter: {len(sensitive_words)} 个词")
            count += 1

        # 模式匹配拦截 → pre_tool（匹配工具参数中的危险模式）
        block_patterns = filters.get("block_patterns")
        if block_patterns:
            def _block_pattern_fn(ctx: HookContext) -> HookResult:
                p = ctx.payload or {}
                params_str = json.dumps(p, ensure_ascii=False)
                for pat in block_patterns:
                    if pat in params_str:
                        return HookResult(Decision.BLOCK,
                                          reason=f"命中拦截模式: {pat}")
                return HookResult(Decision.CONTINUE)

            self.register(HookEvent.PRE_TOOL,
                          PythonHook(_block_pattern_fn, name="block_patterns"))
            logger.info(f"已注册 block_patterns: {block_patterns}")
            count += 1

        # ---- 命令式 hook ----
        hooks_cfg = cfg.get("hooks", {})
        for event_name, matcher_groups in hooks_cfg.items():
            try:
                evt = HookEvent(event_name)
            except ValueError:
                logger.warning(f"hooks.json 中忽略未知事件: {event_name}")
                continue
            if not isinstance(matcher_groups, list):
                continue
            for group in matcher_groups:
                matcher = group.get("matcher", "")
                hook_list = group.get("hooks", [])
                for hc in hook_list:
                    htype = hc.get("type", "command")
                    if htype == "command":
                        cmd = hc.get("command", "")
                        if not cmd:
                            continue
                        # 安全预检：拒绝 DANGEROUS 命令
                        from core.sandbox.guard import check_command_safety
                        ok, reason = check_command_safety(cmd, "CommandHook")
                        if not ok:
                            logger.warning(
                                f"hooks.json: 命令被拒绝注册 [{cmd[:80]}]: {reason}"
                            )
                            continue
                        timeout = hc.get("timeout", 30)
                        cwd = hc.get("cwd")
                        ch = CommandHook(cmd, timeout=timeout, cwd=cwd)
                        self.register(evt, ch, matcher=matcher)
                        count += 1
                    else:
                        logger.warning(f"hooks.json: 未知 hook 类型 '{htype}'")

        logger.info(f"从 {path} 加载了 {count} 个 hook")
        return count

    # ---- 便捷方法（agent 调用点用，少写样板）----

    def run_user_prompt(self, prompt: str) -> HookResult:
        return self.dispatch(HookEvent.USER_PROMPT, {"prompt": prompt})

    def run_pre_tool(self, tool_name: str, params: dict,
                     gate_level: str = "allow") -> HookResult:
        return self.dispatch(HookEvent.PRE_TOOL, {
            "tool_name": tool_name,
            "params": params,
            "gate_level": gate_level,
        })

    def run_post_tool(self, tool_name: str, params: dict,
                      result: str, is_error: bool = False) -> HookResult:
        return self.dispatch(HookEvent.POST_TOOL, {
            "tool_name": tool_name,
            "params": params,
            "result": result,
            "is_error": is_error,
        })

    def run_notification(self, tool_name: str, params: dict,
                         message: str = "") -> HookResult:
        return self.dispatch(HookEvent.NOTIFICATION, {
            "tool_name": tool_name,
            "params": params,
            "message": message,
        })

    def run_denied(self, tool_name: str, reason: str,
                   level: str = "") -> HookResult:
        return self.dispatch(HookEvent.DENIED, {
            "tool_name": tool_name,
            "reason": reason,
            "level": level,
        })

    def run_stop(self, answer: str, step_count: int = 0) -> HookResult:
        return self.dispatch(HookEvent.STOP, {
            "answer": answer,
            "step_count": step_count,
        })

    # ---- 状态 ----

    def list_hooks(self) -> list[dict]:
        """列出所有已注册 hook（供 /hook 命令使用）"""
        rows = []
        for evt in HookEvent:
            for e in self._hooks.get(evt, []):
                rows.append({
                    "event": evt.value,
                    "hook": repr(e.hook),
                    "matcher": e.matcher.pattern if e.matcher else "*",
                    "priority": e.priority,
                })
        return rows

    def __repr__(self):
        total = sum(len(v) for v in self._hooks.values())
        status = "on" if self.enabled else "off"
        return f"<HookManager: {total} hooks, {status}>"
