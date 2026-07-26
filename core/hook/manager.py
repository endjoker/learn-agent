# -*- coding: utf-8 -*-
"""
Hook 管理器 — 注册 / 分发 / 配置加载 / 结果合并

HookManager 是用户和 agent 的唯一交互入口：
  - register()  / unregister()     — 代码注册 hook
  - dispatch()                      — agent 在各事件点调用
  - 便捷方法（run_*）                — agent 用，少写样板
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
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

    def __init__(self, config_path: str | None = None,
                 enabled: bool = True):
        self.enabled = enabled
        self._hooks: dict[HookEvent, list[_HookEntry]] = defaultdict(list)
        self._agent_name = ""
        self._session_id = ""

        # 从 config.json 的 hooks section 加载
        self._try_load_unified()

    def _try_load_unified(self) -> bool:
        """尝试从 config.json 的 hooks section 加载。成功返回 True。"""
        try:
            from core.config_loader import load_config as _load_cfg
            cfg = _load_cfg()
            hooks_cfg = cfg.get("hooks", {})
            if not hooks_cfg:
                return False
            # 清空已有配置 hook，避免 reload 时叠加
            self._hooks.clear()
            self._load_from_dict(hooks_cfg)
            logger.info("从 config.json 加载了 hooks 配置")
            return True
        except Exception:
            return False

    def _load_from_dict(self, cfg: dict) -> int:
        """从 dict 加载 hook 配置。
        返回加载的 hook 总数。
        """
        if not cfg.get("enabled", True):
            self.enabled = False
            return 0

        count = 0

        # 加载事件 hook
        hooks = cfg.get("hooks", {})
        for event_name, hook_list in hooks.items():
            try:
                event = HookEvent(event_name)
            except ValueError:
                logger.warning(f"hooks 配置中忽略未知事件: {event_name}")
                continue
            if not isinstance(hook_list, list):
                continue
            for entry in hook_list:
                if not isinstance(entry, dict):
                    continue
                matcher = entry.get("matcher", "")
                for hdef in entry.get("hooks", []):
                    if not isinstance(hdef, dict):
                        continue
                    htype = hdef.get("type", "command")
                    if htype == "command":
                        try:
                            hook = CommandHook.from_config(hdef)
                            self.register(event, hook, matcher=matcher)
                            count += 1
                        except PermissionError as e:
                            logger.warning(
                                f"hooks 配置: 命令被拒绝注册 [{str(hdef.get('command', ''))[:80]}]: {e}"
                            )
                        except Exception as e:
                            logger.warning(f"hooks 配置: 命令注册失败: {e}")
                    elif htype == "python":
                        logger.warning("hooks 配置不支持 python 类型（安全限制）")
                    else:
                        logger.warning(f"hooks 配置: 未知 hook 类型 '{htype}'")

        # 加载内置过滤器（sensitive_words / block_patterns）
        filters = cfg.get("filters", {})
        if filters.get("enabled", True):
            sensitive_words = filters.get("sensitive_words", [])
            if sensitive_words:
                words = list(sensitive_words)

                def _check_sensitive(ctx: HookContext) -> HookResult:
                    prompt = ctx.data.get("prompt", "")
                    for w in words:
                        if w in prompt:
                            return HookResult(Decision.BLOCK,
                                              reason=f"敏感词拦截: {w}")
                    return HookResult(Decision.CONTINUE)

                self.register(HookEvent.USER_PROMPT,
                              PythonHook(_check_sensitive, name="sensitive_words_filter"))
                count += 1

            block_patterns = filters.get("block_patterns", [])
            if block_patterns:
                patterns = list(block_patterns)

                def _check_block_patterns(ctx: HookContext) -> HookResult:
                    params_str = str(ctx.data.get("params", ""))
                    for pat in patterns:
                        if pat in params_str:
                            return HookResult(Decision.BLOCK,
                                              reason=f"危险模式拦截: {pat}")
                    return HookResult(Decision.CONTINUE)

                self.register(HookEvent.PRE_TOOL,
                              PythonHook(_check_block_patterns, name="block_patterns_filter"))
                count += 1

        logger.info(f"从配置加载了 {count} 个 hook")
        return count

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
