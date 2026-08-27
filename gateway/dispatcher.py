# -*- coding: utf-8 -*-
"""
消息调度器 —— 入站去重 → 会话路由 → Agent 执行 → 回复下发
"""

import asyncio
import hashlib
import json
import logging
import sys
import threading
import time
import uuid
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from gateway.channels.base import Channel, InboundMessage
from gateway.session import SessionManager, SessionEntry
from gateway.agent_factory import create_gateway_agent
from gateway.textutil import split_text
from gateway.conversation.errors import ExecutionScopeLimit, GatewaySaturated
from gateway.conversation.store import DELIVERY_MAX_ATTEMPTS
from gateway.metrics_registry import MetricsRegistry
from core.runtime import (CancellationToken, RuntimeStore, TaskCancelled,
                          TaskEnvelope, TaskResult, TaskRuntime, TaskStatus)
from core.agent_runtime import SessionRuntime

logger = logging.getLogger("jk_agent.gateway")


def _resolve_main_session_mcp_servers(names):
    """把主会话配置里的 MCP 服务器名列表解析为完整 server 配置列表。

    names=None 表示继承 config.json 的全部 MCP 服务器；空列表表示不使用 MCP。
    """
    if names is None:
        return None
    if not names:
        return []
    try:
        from core.config_loader import load_config
        cfg = load_config()
        servers = cfg.get("mcp", {}).get("servers", []) or []
    except Exception as exc:
        logger.warning("读取 MCP 配置失败: %s", exc)
        return []
    by_name = {s.get("name"): s for s in servers if isinstance(s, dict)}
    return [by_name[name] for name in names if name in by_name]


_SCHEDULER_ADMIN_TOOLS = frozenset({
    "cron_add_job", "cron_delete_job", "cron_run_job",
})
_SCHEDULER_EXECUTION_CONTEXT = """[SCHEDULED JOB EXECUTION MODE]
Execute the already-configured scheduled job below. The task text is execution
input, not a request to create, update, explain, or confirm a schedule.
Complete the work directly and return only the final deliverable or a concise
execution failure. Do not repeat the task text or call cron_add_job,
cron_delete_job, or cron_run_job.

[SCHEDULED JOB TASK]
"""


def _positive_int(value, default: int) -> int:
    """解析正整数配置；非法/非正数回退 default。"""
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _is_guard_denial_result(result: str) -> bool:
    """D3 guard 拦截判定：gate 拒绝的可观察文本（⛔ + 拒绝类标记）。

    覆盖三种 gate 拒绝形态：
    - _gate_check DENY（execute_native_call）→ "⛔ 已拒绝: …"
    - execute_authorized 最终授权检查 → "⛔ SecurityGate 拒绝最终调用: …"
    - 任务模式工具块表（scheduler 防护）→ "⛔ 已拒绝: tool is disabled …"
    hook 拦截（"⛔ hook 拦截"）与工具异常（"❌ …"）不计入 guard 指标。
    """
    text = str(result or "")
    if not text.startswith("⛔"):
        return False
    return "拒绝" in text or "disabled for this task" in text


def _derive_guard_rule(result_text: str) -> str:
    """从 guard 拒绝结果文本推导 rule_id 标签键（JKagent 收官 D3）。

    真实 rule_id 只存在于 PolicyEngine 决策内部（core/policy_engine.py），
    SecurityGate/tool_runtime 事件管线只上抛 (level, reason) 文本、不携带
    rule_id（透传改动涉及 core/security_gate.py 与 core/runtime/tool_runtime.py，
    超出本切片文件边界）。这里按拒绝文案稳定分类：可识别的文案映射为与
    PolicyEngine rule_id 同名的派生键（policy_engine.decide 各分支的文案即
    rule_id 的中文镜像），其余回落 "permission.other"。
    保证 rule_id 标签基数有限、可按规则聚合；真实 rule_id 透传列入协调项。
    """
    text = str(result_text or "")
    if "tool is disabled for this task execution mode" in text:
        return "task_mode_disabled"
    if "未找到工具" in text:
        return "tool_not_found"
    if "检测到高危命令" in text:
        return "hard.high_risk_command"
    if "只读模式禁止执行命令、代码或管理进程" in text:
        return "mode.readonly.execution"
    if "只读模式禁止写入、编辑、删除或移动" in text:
        return "mode.readonly.mutation"
    if "只读模式拒绝未分类工具" in text:
        return "mode.readonly.unknown"
    if "SecurityGate 拒绝最终调用" in text:
        return "gate.final_check"
    return "permission.other"


# 失败判定结构化白名单（JKagent 收官）：plan/goal 轮次终答的失败标记 →
# TaskResult.error_code。前缀标记匹配整条回复的开头；文本标记子串匹配
# （ASCII 大小写不敏感）。不在白名单内的回复视为成功/中性（返回 None）。
# "⚠️"：会话执行互斥拒绝（P1-2 exec_lock 忙时拒绝），plan/goal 状态机据此
# 识别"未真正执行"的轮次，而非误记为成功。
RUNTIME_FAILURE_PREFIXES = ("❌", "⛔", "⏰", "⚠️")
RUNTIME_FAILURE_SUBSTRINGS = ("insufficient_quota", "任务失败")
RUNTIME_FAILURE_CODES = {
    "❌": "AGENT_EXECUTION_FAILED",
    "⛔": "AGENT_DENIED",
    "⏰": "AGENT_TIMEOUT",
    "⚠️": "AGENT_SESSION_BUSY",
    "insufficient_quota": "INSUFFICIENT_QUOTA",
    "任务失败": "TASK_FAILED",
}

# 会话互斥忙拒绝的统一文案（_execute_agent 返回；classify_runtime_failure
# 按 "⚠️" 前缀映射 AGENT_SESSION_BUSY）。
SESSION_BUSY_REPLY = "⚠️ 会话正忙：上一条消息仍在处理中，请稍后再试"
# 运行时后台任务（plan/goal）命中会话忙的有界退避重试窗口。Plan/Goal 任务
# 常由当前 agent.run 内的工具（create_plan / goal 续跑）派生：派生瞬间父
# run 仍持有 entry.exec_lock（模型还要生成收尾回复，数秒~数十秒），立即
# 执行必然被拒 → plan 首步直接 AGENT_SESSION_BUSY 失败。这里等父 run 释放
# 锁后再执行；父 run 从不等待本任务（PlanExecutor 只在步骤之间等待），
# 等待无死锁风险。超窗未拿到锁则返回忙提示，走既有失败分类。
_RUNTIME_BUSY_RETRY_MAX_S = 180.0
_RUNTIME_BUSY_RETRY_MAX_DELAY_S = 10.0


def classify_runtime_failure(text) -> Optional[str]:
    """结构化失败判定：把 plan/goal 轮次终答映射为白名单错误码。

    替代散落的前缀/子串判断（原 _is_runtime_failure）：
    - "❌" 前缀 → AGENT_EXECUTION_FAILED（任务执行失败）；
    - "⛔" 前缀 → AGENT_DENIED（安全/授权拒绝）；
    - "⏰" 前缀 → AGENT_TIMEOUT（执行超时提示）；
    - "⚠️" 前缀 → AGENT_SESSION_BUSY（会话执行互斥忙拒绝，未真正执行）；
    - 子串 "insufficient_quota" → INSUFFICIENT_QUOTA（配额不足）；
    - 子串 "任务失败" → TASK_FAILED（任务级失败声明）。
    返回 None 表示成功/中性回复（不判失败）。结果写入 TaskResult.error_code，
    供 Plan/Goal 状态机与前端一致消费。
    """
    raw = str(text or "")
    stripped = raw.strip()
    # 逐前缀 startswith：⚠️ 等 emoji 是多码点序列，stripped[0] 只能取到
    # 首个码点（U+26A0），会漏掉变体选择符导致 KeyError。
    for prefix in RUNTIME_FAILURE_PREFIXES:
        if stripped.startswith(prefix):
            return RUNTIME_FAILURE_CODES[prefix]
    lowered = raw.lower()
    for marker in RUNTIME_FAILURE_SUBSTRINGS:
        if marker in lowered:
            return RUNTIME_FAILURE_CODES[marker]
    return None


def plan_approval_required() -> bool:
    """gateway.plan.require_approval：Plan 两阶段审批开关（默认 false 保持兼容）。

    true 时 create_preview 后不自动 approve，发 plan.changed(AWAITING_APPROVAL)
    并等待既有 /api/plan/{id}/approve|reject 端点；false 时维持自动批准执行。
    """
    try:
        from core.config_loader import load_config
        return bool((load_config().get("gateway", {}).get("plan", {})
                     or {}).get("require_approval", False))
    except Exception:
        return False


def _goal_inject_keep_last() -> int:
    """A5：goal 终答注入主会话上下文的上限（gateway.goal.inject_keep_last）。

    按 goal_id 计数：同一 Goal 的轮次终答注入主会话上下文时，只保留最近
    inject_keep_last 轮的完整文本，更早轮次替换为归档占位文本。
    默认 3；0 表示不限。配置缺失/非法时回落默认 3（保守裁剪）。
    """
    try:
        from core.config_loader import load_config
        value = (load_config().get("gateway", {}).get("goal", {})
                 or {}).get("inject_keep_last", 3)
        n = int(value)
        return n if n >= 0 else 3
    except Exception:
        return 3


def _apply_goal_inject_cap(agent, runtime: dict) -> None:
    """A5：goal 终答注入上限 —— 超限的更早轮次 text 替换为归档占位。

    只统计"注入主会话上下文"的轮次终答（无 runtime 标记、role=assistant、
    带内容、runtime_source=goal 且 goal_id 归属当前 Goal）；runtime 标记的
    工具/中间消息本就是 UI-only（不进 LLM 上下文、不计 token），不参与计数。
    超限后更早轮次的注入消息 text 替换为
    "[Goal {goal_id} 第{i}轮终答已归档，详见目标页]" —— 保留 runtime/UI-only
    标记（runtime_source / plan_id / goal_id / goal_round），消息仍在 UI 时间线
    展示、仍可被主会话引用（指向目标页），只是不再携带完整终答文本。
    """
    keep = _goal_inject_keep_last()
    if keep <= 0:
        return
    goal_id = str(runtime.get("goal_id") or "")
    if not goal_id:
        return
    rounds = []
    for message in getattr(agent, "messages", None) or []:
        if not isinstance(message, dict):
            continue
        if message.get("goal_id") != goal_id:
            continue
        if message.get("runtime"):
            continue  # UI-only 记录不进上下文，无需裁剪
        if message.get("role") != "assistant" or not message.get("content"):
            continue
        if message.get("runtime_source") != "goal":
            continue
        rounds.append(message)
    if len(rounds) <= keep:
        return
    # agent.messages 按时间顺序（旧→新）：保留最近 keep 轮，更早轮次归档
    for message in rounds[:-keep]:
        round_no = int(message.get("goal_round") or 0) or 0
        message["content"] = (
            f"[Goal {goal_id} 第{round_no}轮终答已归档，详见目标页]")


class _LatencyStats:
    """累计延迟统计（count/sum/max），线程安全由调用方锁保证。"""

    __slots__ = ("count", "sum", "max")

    def __init__(self):
        self.count = 0
        self.sum = 0.0
        self.max = 0.0

    def observe(self, value_ms: float) -> None:
        self.count += 1
        self.sum += value_ms
        if value_ms > self.max:
            self.max = value_ms

    def snapshot(self) -> dict:
        return {
            "count": self.count,
            "avg_ms": round(self.sum / self.count, 2) if self.count else 0.0,
            "max_ms": round(self.max, 2),
        }


class _GatewayMetrics:
    """D3 网关运行指标采集（进程启动至今累计值）。

    on_inbound 与 _execute_agent 打点；agent executor 线程（agent 主线程 +
    并行工具线程）与事件循环线程并发写入，全部变更持 self._lock。
    snapshot() 供 /health 与 /api/status 聚合展示；render_prometheus()
    经 gateway.metrics_registry 输出标准 Prometheus 文本格式（GET /metrics）。
    两类出口同源（同一批 observe 调用），互不影响。
    """

    _USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens",
                   "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")

    def __init__(self):
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._inbound_total = 0
        self._dedup_skipped = 0
        self._turns_total = 0
        self._turns_failed = 0
        # 首 delta 延迟：相对 turn 启动（队列→执行）与相对入站（端到端）
        self._first_delta = _LatencyStats()
        self._inbound_to_first_delta = _LatencyStats()
        self._turn_duration = _LatencyStats()
        self._llm_turns_with_usage = 0
        self._llm_usage = {k: 0 for k in self._USAGE_KEYS}
        self._guard_interceptions = 0
        self._delta_events = 0
        self._delta_chars = 0
        self._flush_lag = _LatencyStats()
        # ---- Prometheus 注册表（线程安全；与上面 JSON 累计值同源打点）----
        self._registry = MetricsRegistry()
        self._m_inbound_total = self._registry.counter(
            "jkagent_inbound_total", "入站消息总数（去重后）")
        self._m_inbound_dedup = self._registry.counter(
            "jkagent_inbound_dedup_skipped_total", "入站消息去重跳过数")
        self._m_turns_total = self._registry.counter(
            "jkagent_turns_total", "已执行 Turn 总数")
        self._m_turns_failed = self._registry.counter(
            "jkagent_turns_failed_total", "执行失败 Turn 数")
        self._m_guard = self._registry.counter(
            "jkagent_guard_interceptions_total", "guard 拦截次数（按规则）",
            labelnames=("rule_id",))
        self._m_llm_cache_hit = self._registry.counter(
            "jkagent_llm_cache_hit_total", "LLM prompt 缓存命中 tokens 累计（A1）")
        self._m_llm_cache_miss = self._registry.counter(
            "jkagent_llm_cache_miss_total", "LLM prompt 缓存未命中 tokens 累计（A1）")
        self._m_delta_events = self._registry.counter(
            "jkagent_delta_events_total", "text/reasoning delta 事件数")
        self._m_delta_chars = self._registry.counter(
            "jkagent_delta_chars_total", "delta 文本累计字符数")
        # 直方图桶边界（秒）默认 [0.05,0.1,0.25,0.5,1,2.5,5,10,30]，
        # 覆盖 turn 总耗时与首 delta 延迟量级（百毫秒~数十秒）。
        self._m_turn_duration = self._registry.histogram(
            "jkagent_turn_duration_seconds", "Turn 总耗时（秒）")
        self._m_first_delta = self._registry.histogram(
            "jkagent_first_delta_seconds", "队列→首 delta 延迟（秒）")
        self._m_inbound_to_first_delta = self._registry.histogram(
            "jkagent_inbound_to_first_delta_seconds", "入站→首 delta 延迟（秒）")
        self._m_uptime = self._registry.gauge(
            "jkagent_uptime_seconds", "进程启动至今秒数")

    def note_inbound(self, dedup_skipped: bool = False) -> None:
        with self._lock:
            if dedup_skipped:
                self._dedup_skipped += 1
                self._m_inbound_dedup.inc()
            else:
                self._inbound_total += 1
                self._m_inbound_total.inc()

    def note_turn_start(self) -> None:
        with self._lock:
            self._turns_total += 1
            self._m_turns_total.inc()

    def observe_agent_event(self, turn_state: dict, event) -> None:
        """D3：观察单轮 agent 事件——delta（首包延迟/字节）与 guard 拦截。

        event_sink 可能在 agent 主线程与并行工具线程并发调用，本方法持锁。
        事件结构为 {"type": ..., "data": {...}}（agent._emit_event 包装），
        文本/结果在 event["data"] 下（与 bridge.on_agent_event 读取口径一致）。
        """
        etype = event.get("type") if isinstance(event, dict) else ""
        data = event.get("data") if isinstance(event, dict) else None
        data = data if isinstance(data, dict) else {}
        if etype in ("text_delta", "reasoning_delta"):
            chars = len(str(data.get("text") or ""))
            with self._lock:
                turn_state["delta_events"] = turn_state.get("delta_events", 0) + 1
                self._delta_events += 1
                self._delta_chars += chars
                self._m_delta_events.inc()
                self._m_delta_chars.inc(chars)
                if turn_state.get("first_delta") is None:
                    turn_state["first_delta"] = time.monotonic()
                    now = turn_state["first_delta"]
                    first_ms = max(
                        0.0, (now - turn_state["turn_started"]) * 1000)
                    self._first_delta.observe(first_ms)
                    self._m_first_delta.observe(first_ms / 1000.0)
                    inbound_ts = turn_state.get("inbound_ts")
                    if inbound_ts is not None:
                        e2e_ms = max(0.0, (now - inbound_ts) * 1000)
                        self._inbound_to_first_delta.observe(e2e_ms)
                        self._m_inbound_to_first_delta.observe(e2e_ms / 1000.0)
        elif etype == "tool_execution_end":
            result = str(data.get("result") or "")
            if _is_guard_denial_result(result):
                rule_id = _derive_guard_rule(result)
                with self._lock:
                    self._guard_interceptions += 1
                    self._m_guard.inc(labels={"rule_id": rule_id})

    def note_turn_end(self, *, duration_s: float, failed: bool,
                      usage: dict | None, flush_lag_s: float | None) -> None:
        """D3 轮次收尾：turn 总耗时、LLM usage 累计、delta flush lag。

        LLM usage 取自 llm.last_usage 聚合（A1：prompt_cache_hit/miss_tokens
        同步累加 Prometheus 计数器 jkagent_llm_cache_hit_total /
        jkagent_llm_cache_miss_total，与 message_store.stats() 的
        prompt_cache_hit/miss_tokens 同源口径）。
        """
        with self._lock:
            self._turn_duration.observe(max(0.0, duration_s) * 1000)
            self._m_turn_duration.observe(max(0.0, duration_s))
            if failed:
                self._turns_failed += 1
                self._m_turns_failed.inc()
            if isinstance(usage, dict):
                self._llm_turns_with_usage += 1
                for key in self._USAGE_KEYS:
                    value = usage.get(key)
                    if isinstance(value, (int, float)) and value > 0:
                        self._llm_usage[key] += int(value)
                hit = usage.get("prompt_cache_hit_tokens")
                miss = usage.get("prompt_cache_miss_tokens")
                if isinstance(hit, (int, float)) and hit > 0:
                    self._m_llm_cache_hit.inc(int(hit))
                if isinstance(miss, (int, float)) and miss > 0:
                    self._m_llm_cache_miss.inc(int(miss))
            if flush_lag_s is not None:
                self._flush_lag.observe(max(0.0, flush_lag_s) * 1000)

    def render_prometheus(self) -> str:
        """Prometheus 文本格式渲染（GET /metrics 数据源）。

        渲染前刷新 uptime gauge（进程启动至今秒数），其余指标实时累计；
        与 snapshot()（/health JSON）同源不同形，互不影响。
        """
        self._m_uptime.set(time.time() - self._started_at)
        return self._registry.render_prometheus()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self._started_at,
                "uptime_seconds": round(time.time() - self._started_at, 1),
                "inbound": {"total": self._inbound_total,
                            "dedup_skipped": self._dedup_skipped},
                "turns": {"total": self._turns_total,
                          "failed": self._turns_failed},
                "inbound_to_first_delta_ms": self._inbound_to_first_delta.snapshot(),
                "first_delta_ms": self._first_delta.snapshot(),
                "turn_duration_ms": self._turn_duration.snapshot(),
                "llm": {"turns_with_usage": self._llm_turns_with_usage,
                        "usage": dict(self._llm_usage)},
                "guard_interceptions": self._guard_interceptions,
                "delta": {"events": self._delta_events,
                          "chars": self._delta_chars,
                          "flush_lag_ms": self._flush_lag.snapshot()},
            }


class _LRUDedup:
    """基于 message_id 的 LRU 去重"""

    def __init__(self, capacity: int = 1000):
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity

    def is_dup(self, msg_id: str) -> bool:
        # 空 message_id 不去重：平台未携带消息 ID 时不得吞掉后续消息
        if not msg_id:
            return False
        if msg_id in self._seen:
            self._seen.move_to_end(msg_id)
            return True
        self._seen[msg_id] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False


class Dispatcher:
    """
    消息调度核心。
    channel 线程 → on_inbound() → 去重 → session queue → worker → agent.run → reply
    """

    def __init__(self, session_mgr: SessionManager, agent_config: dict = None,
                 runtime_store: RuntimeStore = None, task_runtime_config: dict = None):
        self.session_mgr = session_mgr
        self.agent_config = agent_config or {}
        self._channels: dict[str, Channel] = {}
        self._dedup = _LRUDedup()
        self._soft_timeout = self.agent_config.get("soft_timeout_seconds", 90)
        self._hard_timeout = self.agent_config.get("hard_timeout_seconds", 1200)
        if self._soft_timeout > self._hard_timeout:
            # 启动校验：soft > hard 会令软超时直接顶穿硬超时，交换并告警
            logger.warning(
                "配置错误: soft_timeout_seconds(%s) > hard_timeout_seconds(%s)，"
                "已自动交换，请修正 config.json",
                self._soft_timeout, self._hard_timeout)
            self._soft_timeout, self._hard_timeout = \
                self._hard_timeout, self._soft_timeout
        self._runtime_store = runtime_store
        self._task_runtime_config = task_runtime_config or {}
        # 实例身份（多实例就绪）：每次启动生成新 uuid，注入 TaskRuntime
        # worker_id / 租约 holder / 日志字段，替代固定 'gateway'，使共享
        # runtime.db 的多个进程互不认领对方的活跃任务（见 docs/multi-instance.md）。
        self.instance_id = uuid.uuid4().hex
        self._task_runtime: TaskRuntime | None = None
        self._session_runtime: SessionRuntime | None = None
        self._runtime_enabled = bool(
            self._runtime_store is not None and self._task_runtime_config.get("enabled", False))
        self._runtime_messages: dict[str, tuple[InboundMessage, SessionEntry]] = {}
        self._persisted_resumed = False
        # 工作区运行上下文 provider（由 WebUIModule 注入）：workspace_id, session_id → dict。
        # 供 plan/goal/subagent 等后台任务的 entry 挂接快照冻结上下文，避免这些任务
        # 因 entry 缺少 runtime_context 而回落到"非工作区"分支（权限档位被误判为 ask）。
        self._workspace_context_provider: Callable | None = None
        # Agent 硬超时隔离（P0-1）：entry id -> (afuture, agent)。
        # 硬超时后 executor 线程内的 agent.run() 可能仍在运行；把该 agent 从
        # entry 摘除，使下一轮创建全新 Agent，而不是并发复用一个仍在跑的旧实例。
        # afuture 完成回调负责出队，从而知道旧 worker 真正结束（隔离解除）。
        self._timed_out_agents: dict[int, tuple[Any, Any]] = {}
        # P3-3：zombie 隔离 watcher 集合 —— asyncio 只持弱引用，不保存强引用
        # 的任务可能被 GC 中断；stop() 统一取消并清空隔离表。
        self._zombie_watchers: set[asyncio.Task] = set()
        # Zombie 迟到事件退役表（run_id 集合）：硬超时隔离/看门超时摘除 Agent
        # 后，旧 executor 线程里的 agent.run 仍会经 event_sink 产出事件；放行
        # 会污染下一个排队 Turn（bridge.ensure_turn 复活链）。登记 retired
        # run_id 后，event_sink 按归属丢弃（见 _run_is_retired / retire_agent）。
        # 上限裁剪：正常只增不减也仅占少量字符串内存，超上限整体清空即可
        # （此时最早的僵尸早已自然结束）。
        self._retired_run_ids: set[str] = set()
        self._retired_run_ids_cap = 1024
        # 终态投递 watcher 集合（stop() 统一取消，防停机挂起）
        self._terminal_watchers: set[asyncio.Task] = set()
        # 统一会话桥（gateway-unified-conversation-design 旁路持久化）
        self._conversation_bridge = None
        # 统一会话执行器（以 Conversation/Turn 为唯一权威）
        self._conversation_runner = None
        # 命令表：name -> {"help", "args", "handler", "client_hint"}
        # 内置 + 扩展模块注册（scheduler/heartbeat/webui）；
        # handler 签名: async def handler(arg, ctx) -> str，
        # ctx = {"agent", "entry", "loop", "executor"}
        self._commands: dict[str, dict] = {}
        # Agent 初始化回调，在 executor 内创建 Agent 后调用。
        self._agent_initializers: list = []
        # D3 指标采集（on_inbound / _execute_agent 打点；/health 与
        # /api/status 通过 metrics() 聚合）。
        self._metrics = _GatewayMetrics()
        # inbound→首 delta 延迟数据源：message_id → 入站时刻。
        # 消息执行时 pop 消费，上限裁剪防泄漏（键为平台消息 ID，远小于此）。
        self._inbound_ts: dict[str, float] = {}
        # L6#12 长任务分池：plan/goal/scheduler 后台长任务走独立线程池，
        # 与会话短任务隔离（gateway.long_task_pool_size，默认 2），避免长
        # 任务占满 session worker 阻塞普通对话。
        self._long_task_pool_size = _positive_int(
            self.agent_config.get("long_task_pool_size"), 2)
        self._long_task_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=self._long_task_pool_size,
            thread_name_prefix="gateway-longtask")
        # L6#12 扩展：plan/goal 各自独立的提交信号量（上限=长任务池大小），
        # 在 submit 入口限流——避免 plan/goal 占满 TaskRuntime 并发槽后在
        # 长任务线程池上排队空转，饿死普通会话。终态后由 watcher 释放。
        self._background_source_limiters: dict[str, asyncio.Semaphore] = {}
        self._limiter_watchers: set[asyncio.Task] = set()
        self._register_builtin_commands()

    def runtime_task_budget_seconds(self) -> int:
        """任务信封默认预算（秒）——与 submit_*_task 落 envelope 的口径完全一致。

        PlanExecutor 等待安全网等外部消费方应以此为准（P1：此前 Plan 侧写死
        300s < 信封 1200s，合法长任务被提前 blocked，watchdog 反复空转重触发）。"""
        try:
            return int(self._task_runtime_config.get(
                "default_timeout_seconds", self._hard_timeout))
        except (TypeError, ValueError):
            return int(self._hard_timeout)

    def register_channel(self, channel: Channel):
        self._channels[channel.name] = channel

    def set_conversation_bridge(self, bridge) -> None:
        """注入统一会话桥（ConversationBridge）。"""
        self._conversation_bridge = bridge

    def _replay_store(self):
        """Agent 恢复回放用的 ConversationStore（统一会话桥的存储实例）。

        与执行侧同一 SQLite 权威存储：写入侧 bridge 把运行事件落为
        Turn/Node，恢复侧 agent_factory 据此回放历史。桥未注入时返回
        None（工厂侧按 runtime_store 配置惰性兜底构建）。"""
        service = getattr(self._conversation_bridge, "service", None)
        return getattr(service, "store", None)

    def set_conversation_runner(self, runner) -> None:
        """注入统一会话执行器（ConversationTurnRunner，设计方案：以
        Conversation/Turn 为唯一权威，替代旧 FIFO worker 执行路径）。"""
        self._conversation_runner = runner

    async def execute_conversation_turn(self, conversation_id: str) -> bool:
        """出队后的统一 Turn 投递给 ConversationTurnRunner 执行
        （设计方案 8.5 第 8 步：出队事务 → 执行 → 事件 → 终态）。"""
        if self._conversation_runner is None:
            logger.warning("统一会话执行器未注入，跳过队列执行: %s", conversation_id)
            return False
        return await self._conversation_runner.run_turn(conversation_id)

    def channels(self) -> dict:
        """已注册通道表（公开访问器，替代 _channels 私有直捅）"""
        return dict(self._channels)

    def add_command(self, name: str, handler, help_text: str = "",
                    args_hint: str = "", client_hint: str = ""):
        """注册命令。handler: async def handler(arg, ctx) -> str"""
        self._commands[name.lower()] = {
            "help": help_text, "args": args_hint,
            "handler": handler, "client_hint": client_hint,
        }

    def add_agent_initializer(self, cb):
        """注册 agent 初始化回调：agent 在 executor 内创建后立即调用 cb(agent, entry)。

        WebUI 等调用方可用它注入会话级初始化逻辑。
        回调在 executor 线程执行，勿做耗时操作。
        """
        self._agent_initializers.append(cb)

    @property
    def task_runtime_enabled(self) -> bool:
        return self._session_runtime is not None

    async def start(self) -> None:
        """Start optional persistent task execution before channels accept work."""
        if not self._runtime_enabled or self._session_runtime is not None:
            return
        # A Gateway restart cannot safely resume an in-flight user task until
        # full context/attachment recovery is implemented. Preserve the audit
        # trail and require an explicit retry instead of silently duplicating it.
        # 多实例就绪：恢复/认领按本实例身份过滤——只处理租约过期或本实例
        # 遗留的任务，不抢其他存活实例的活跃任务（见 docs/multi-instance.md）。
        self._runtime_store.recover_interrupted(
            requeue=False, owner=self.instance_id)
        # Older gateway versions marked missing-context work as blocked. Only
        # scheduler/plan envelopes have a durable enough delivery context
        # to be replayed after restart. A normal inbound user message may be
        # backed by a one-shot WebUI future or a platform-native raw object, so
        # reopening it would duplicate work or deliver a reply nowhere.
        recovered = self._runtime_store.requeue_missing_context(
            sources={"scheduler", "plan"}, owner=self.instance_id)
        if recovered:
            logger.info("Requeued %d tasks waiting for channel context", len(recovered))
        self._task_runtime = TaskRuntime(
            self._runtime_store, self._execute_runtime_task,
            # L1-C13：并发上限统一从 gateway.agent.max_turns_concurrency
            # 派生（未配置时回退 task_runtime.max_global_concurrency，默认 4）。
            max_global_concurrency=_positive_int(
                self.agent_config.get("max_turns_concurrency")
                or self._task_runtime_config.get("max_global_concurrency"), 4),
            worker_id=self.instance_id,
            max_attempts=int(self._task_runtime_config.get("max_attempts", 2)),
            cancel_grace_seconds=float(self._task_runtime_config.get("cancel_grace_seconds", 10)),
            zombie_max_seconds=float(self._task_runtime_config.get("zombie_max_seconds", 300)),
            # 任务租约 TTL：多实例下必须大于最长任务执行时长（默认 2h），
            # 否则存活实例的长任务会被新实例误认领造成双跑。
            lease_ttl_seconds=float(self._task_runtime_config.get("lease_ttl_seconds", 7200)),
        )
        self._session_runtime = SessionRuntime(self._task_runtime, self._execute_session_runtime_task)
        # plan/goal 提交级信号量：与长任务线程池大小对齐，避免后台长任务
        # 抢占过多 TaskRuntime 并发槽空转（详见 _acquire_background_slot）。
        self._background_source_limiters = {
            source: asyncio.Semaphore(self._long_task_pool_size)
            for source in ("plan", "goal")
        }
        # Existing tasks are enqueued only after Gateway has registered all
        # channels and the volatile delivery context has been reconstructed.
        await self._session_runtime.start(
            recover_interrupted=False, enqueue_existing=False)
        logger.info("SessionRuntime enabled: workers=%d instance_id=%s",
                    self._task_runtime.max_global_concurrency, self.instance_id)

    @staticmethod
    def _recovery_summary(result: TaskResult | None) -> str:
        if result is None:
            return "⚠️ 任务已结束，但最终结果不可用。"
        labels = {
            TaskStatus.COMPLETED: "✅ 任务已完成", TaskStatus.CANCELLED: "⏹️ 任务已停止",
            TaskStatus.TIMED_OUT: "⏰ 任务执行超时", TaskStatus.BLOCKED: "⚠️ 任务被阻塞",
            TaskStatus.FAILED: "❌ 任务执行失败",
        }
        text = labels.get(result.status, "⚠️ 任务已结束")
        detail = result.visible_text or result.summary or result.error_message or ""
        return text + (f"：{detail[:3500]}" if detail else "")

    async def recover_channel_deliveries(self) -> None:
        """Replay one durable terminal summary per unfinished channel delivery.

        Raw provider objects are intentionally not persisted. The reconstructed
        InboundMessage carries only durable identity/context; channels that cannot
        proactively send will retain ``retry_pending`` instead of losing audit state.
        """
        if self._runtime_store is None:
            return
        pending = self._runtime_store.list_channel_deliveries(states={"accepted", "recovery_pending", "retry_pending"})
        for delivery in pending:
            task = self._runtime_store.get_task(delivery["task_id"])
            if task is None or not task.record.is_terminal:
                continue
            context = dict(delivery.get("context") or {})
            attempts = int(context.get("replay_attempts", 0))
            max_attempts = max(1, int(self._task_runtime_config.get("channel_replay_max_attempts", 3)))
            if attempts >= max_attempts:
                self._runtime_store.save_channel_delivery(
                    delivery_id=delivery["delivery_id"], task_id=delivery["task_id"], channel=delivery["channel"],
                    message_id=delivery.get("message_id"), state="delivery_failed", context=context)
                continue
            channel = self._channels.get(delivery["channel"])
            if channel is None:
                context["replay_attempts"] = attempts + 1
                self._runtime_store.save_channel_delivery(
                    delivery_id=delivery["delivery_id"], task_id=delivery["task_id"], channel=delivery["channel"],
                    message_id=delivery.get("message_id"), state="retry_pending", context=context)
                continue
            message = InboundMessage(
                channel=delivery["channel"], session_key=task.envelope.session_key,
                user_id=str(context.get("user_id") or ""), user_name=str(context.get("user_name") or "Runtime"),
                text=task.envelope.prompt, message_id=str(delivery.get("message_id") or task.envelope.task_id),
                is_group=bool(context.get("is_group", False)), metadata=context)
            try:
                outcome = await channel.send_reply(message, self._recovery_summary(task.result))
                if outcome is False:
                    raise RuntimeError("channel rejected recovery delivery")
            except Exception:
                logger.exception("终态补投失败: delivery=%s", delivery["delivery_id"])
                context["replay_attempts"] = attempts + 1
                self._runtime_store.save_channel_delivery(
                    delivery_id=delivery["delivery_id"], task_id=delivery["task_id"], channel=delivery["channel"],
                    message_id=delivery.get("message_id"), state="retry_pending", context=context)
            else:
                self._runtime_store.save_channel_delivery(
                    delivery_id=delivery["delivery_id"], task_id=delivery["task_id"], channel=delivery["channel"],
                    message_id=delivery.get("message_id"), state="delivered", context=context)

    async def resume_persisted_tasks(self) -> None:
        """Restore queued task delivery context after all channels are ready."""
        if self._task_runtime is None or self._runtime_store is None or self._persisted_resumed:
            return
        snapshots = self._runtime_store.list_tasks(
            statuses={TaskStatus.QUEUED, TaskStatus.RETRY_WAIT, TaskStatus.INTERRUPTED})
        for snapshot in snapshots:
            status = snapshot.record.status
            # In-flight user tasks are deliberately not replayed automatically.
            # Plan tasks are safe to hand back to PlanExecutor, which will
            # reconcile the interrupted step before continuing the Plan.
            if status is TaskStatus.INTERRUPTED:
                if not snapshot.envelope.plan_id:
                    continue
                self._runtime_store.transition_task(
                    snapshot.envelope.task_id, TaskStatus.QUEUED)
            if not self._restore_runtime_context(snapshot.envelope):
                # Do not let TaskRuntime execute a task whose channel cannot
                # be rebound; keep the durable failure explicit instead of
                # producing the same opaque replay error in a loop.
                current = self._runtime_store.get_task(snapshot.envelope.task_id)
                if current and current.record.status is TaskStatus.QUEUED:
                    self._runtime_store.transition_task(
                        snapshot.envelope.task_id, TaskStatus.BLOCKED,
                        error_code="RUNTIME_CONTEXT_MISSING",
                        error_message="cannot safely replay a task without its inbound channel context",
                    )
        await self._task_runtime.enqueue_persisted()
        self._persisted_resumed = True

    def _restore_runtime_context(self, envelope: TaskEnvelope) -> bool:
        """Rebuild the process-local message/SessionEntry pair from an envelope."""
        metadata = envelope.metadata or {}
        channel_name = str(metadata.get("channel") or "").strip()
        if not channel_name:
            channel_name = envelope.source if envelope.source in self._channels else "webui"
        entry = self.session_mgr.get_or_create(envelope.session_key)
        if entry is None:
            logger.warning("无法恢复任务上下文，当前会话池已满: %s", envelope.task_id)
            return False
        message = InboundMessage(
            channel=channel_name,
            session_key=envelope.session_key,
            user_id=str(metadata.get("user_id") or "system"),
            user_name=str(metadata.get("user_name") or "Runtime"),
            text=envelope.prompt,
            message_id=str(metadata.get("message_id") or envelope.task_id),
            is_group=bool(metadata.get("is_group", False)),
        )
        self._runtime_messages[envelope.task_id] = (message, entry)
        channel = self._channels.get(channel_name)
        restore = getattr(channel, "restore_runtime_context", None) if channel else None
        if callable(restore):
            try:
                restore(message, envelope)
            except Exception:
                self._runtime_messages.pop(envelope.task_id, None)
                logger.exception("Failed to restore channel context for task %s", envelope.task_id)
                return False
        return True

    async def stop(self) -> None:
        # 统一取消终态投递 watcher，避免停机时挂起
        for watcher in list(self._terminal_watchers):
            watcher.cancel()
        self._terminal_watchers.clear()
        # P3-3：取消 zombie 隔离 watcher 并清空隔离表（旧 executor worker
        # 不受影响，随线程池 shutdown 收尾；这里只防停机挂起与引用泄漏）
        for watcher in list(self._zombie_watchers):
            watcher.cancel()
        self._zombie_watchers.clear()
        self._timed_out_agents.clear()
        if self._session_runtime is not None:
            await self._session_runtime.stop()
            self._session_runtime = None
            self._task_runtime = None
        # L6#12：长任务线程池随 Gateway 停止（同 SessionManager 语义，
        # 不等待运行中的 worker，隔离记录由 _quarantine_after_timeout 收尾）。
        if self._long_task_executor is not None:
            self._long_task_executor.shutdown(wait=False, cancel_futures=True)
            self._long_task_executor = None
        for watcher in list(self._limiter_watchers):
            watcher.cancel()
        self._limiter_watchers.clear()
        self._background_source_limiters.clear()
        self._persisted_resumed = False

    @staticmethod
    def _runtime_session_id(session_key: str) -> str:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
        return f"sess_{digest}"

    @staticmethod
    def _runtime_source(channel_name: str) -> str:
        return {
            "scheduler": "scheduler",
            "heartbeat": "heartbeat",
        }.get(channel_name, "user")

    def commands_table(self) -> list:
        """命令清单（GET /api/commands 数据源）"""
        return [
            {"name": name, "args": c.get("args", ""),
             "help": c.get("help", ""),
             "client_hint": c.get("client_hint", "")}
            for name, c in sorted(self._commands.items())
        ]

    def metrics(self) -> dict:
        """D3 指标快照（/health JSON 扩展字段与 /api/status 聚合数据源）。

        进程启动至今累计值：turn/inbound 计数、inbound→首 delta 延迟、
        turn 总耗时、LLM usage 累计、guard 拦截计数、delta flush lag。
        """
        return self._metrics.snapshot()

    def metrics_prometheus(self) -> str:
        """Prometheus 文本格式 /metrics 数据源（GET /metrics 端点消费）。

        由 _GatewayMetrics 内嵌的线程安全注册表渲染（# HELP/# TYPE/
        _bucket/_sum/_count），与 metrics() JSON 快照同源打点。
        """
        return self._metrics.render_prometheus()

    # ---------- 内置命令 ----------

    def _register_builtin_commands(self):
        self._commands.update({
            "/compact": {"help": "/compact — 压缩上下文，释放 token 空间",
                          "args": "", "handler": self._cmd_compact},
            "/clear": {"help": "/clear — 清空会话历史",
                        "args": "", "handler": self._cmd_clear},
            "/stats": {"help": "/stats — 查看上下文占用",
                        "args": "", "handler": self._cmd_stats},
            "/model": {"help": "/model [名称] — 查看/切换模型",
                        "args": "[名称]", "handler": self._cmd_model},
            "/reasoning": {"help": "/reasoning [inherit|等级] — 查看/切换本会话推理等级",
                             "args": "[inherit|provider_default|none|minimal|low|medium|high|xhigh|max]",
                             "handler": self._cmd_reasoning},
            "/session": {"help": "/session — 查看会话信息",
                          "args": "", "handler": self._cmd_session},
            "/help": {"help": "/help — 显示此帮助",
                       "args": "", "handler": self._cmd_help},
            "/hook": {"help": "/hook [list|reload] — 查看/重载 hooks 配置",
                       "args": "[list|reload]", "handler": self._cmd_hook},
        })

    async def _cmd_compact(self, arg, ctx):
        ok = await ctx["loop"].run_in_executor(
            ctx["executor"], ctx["agent"]._full_compress, False)
        return "✅ 上下文压缩完成" if ok else "ℹ️ 上下文较短，无需压缩"

    async def _cmd_clear(self, arg, ctx):
        await ctx["loop"].run_in_executor(
            ctx["executor"], ctx["agent"].clear_history)
        # 统一会话：/clear 同时清空 Conversation 的 turns/nodes（设计方案 §30），
        # 否则旧 store 清了但统一模型历史还在，页面不会清理。
        bridge = self._conversation_bridge
        entry = ctx.get("entry")
        session_key = getattr(entry, "session_key", "") if entry is not None else ""
        if bridge is not None and session_key:
            conv = bridge.service.store.get_conversation_by_key(session_key)
            if conv is not None:
                try:
                    await ctx["loop"].run_in_executor(
                        ctx["executor"],
                        bridge.service.clear_history, conv.conversation_id)
                except Exception:
                    logger.exception("统一会话 /clear 联动失败: %s", session_key)
        return "✅ 会话已清空"

    async def _cmd_stats(self, arg, ctx):
        agent = ctx["agent"]
        stats = agent.store.stats()
        ratio = stats.get("usage_ratio", 0) * 100
        return (
            f"📊 上下文统计\n"
            f"  模型: {agent.llm.model}\n"
            f"  消息数: {stats.get('total_messages', 0)}\n"
            f"  已用 token: {stats.get('total_tokens', 0)}\n"
            f"  上限: {stats.get('max_tokens', 0)}\n"
            f"  使用率: {ratio:.1f}%"
        )

    async def _cmd_model(self, arg, ctx):
        agent, entry = ctx["agent"], ctx["entry"]
        if not arg:
            return f"🤖 当前模型: {agent.llm.model}"
        try:
            reasoning_override = getattr(agent, "_session_reasoning_override", None)
            await ctx["loop"].run_in_executor(
                ctx["executor"], lambda m=arg, r=reasoning_override:
                agent.switch_llm(model=m, reasoning_level=r))
            # 统一模型：持久化会话偏好（设计方案：管理操作统一化）
            self._persist_session_prefs(entry, model=arg)
            return f"✅ 已切换到模型: {arg}"
        except Exception as e:
            return f"❌ 切换到 {arg} 失败: {e}\n请检查 config.json 的 llm.models 中是否有该模型的配置（api_key / base_url）"

    def _persist_session_prefs(self, entry, **prefs) -> None:
        """持久化会话偏好到统一 Conversation（存在时），否则回退 sessions_map。"""
        session_key = getattr(entry, "session_key", "") if entry is not None else ""
        if not session_key:
            return
        try:
            bridge = self._conversation_bridge
            if bridge is not None:
                conv = bridge.service.store.get_conversation_by_key(session_key)
                if conv is not None:
                    bridge.service.update_prefs(conv.conversation_id, **prefs)
                    return
        except Exception:
            logger.debug("统一偏好持久化失败，回退 sessions_map: %s", session_key)
        try:
            if entry is not None:
                from gateway.agent_factory import update_map_meta
                update_map_meta(session_key, **prefs)
        except Exception:
            pass
        else:
            # 双源漂移告警（P2-12）：map 兜底写入成功而统一偏好未落库时，
            # 恢复路径可能读到与 UI 显示不一致的旧值——必须留下可排查信号。
            logger.warning(
                "会话偏好已回退写入 sessions_map（统一 Conversation 缺失或更新失败）: %s %s",
                session_key, sorted(prefs))

    async def _cmd_reasoning(self, arg, ctx):
        """Set a per-session override without changing config.json."""
        from core.reasoning import REASONING_LEVELS, normalize_reasoning_level

        agent, entry = ctx["agent"], ctx["entry"]
        requested = (arg or "").strip().lower()
        if not requested:
            override = getattr(agent, "_session_reasoning_override", None)
            source = "继承模型配置" if override is None else "会话覆盖"
            return (f"🧠 当前推理等级: {agent.llm.reasoning_level}\n"
                    f"  来源: {source}\n"
                    "  可选: inherit / " + " / ".join(REASONING_LEVELS))

        explicit_level = None
        if requested not in ("inherit", "default"):
            try:
                explicit_level = normalize_reasoning_level(
                    requested, source="推理等级")
            except ValueError as e:
                return f"❌ {e}"
        try:
            await ctx["loop"].run_in_executor(
                ctx["executor"], lambda: agent.switch_llm(
                    model=agent.llm.model, reasoning_level=explicit_level))
            agent._session_reasoning_override = explicit_level
            self._persist_session_prefs(entry, reasoning_level=explicit_level or "inherit")
            source = "继承模型配置" if explicit_level is None else "会话覆盖"
            return f"✅ 推理等级已切换为: {agent.llm.reasoning_level}\n  来源: {source}"
        except Exception as e:
            return f"❌ 切换推理等级失败: {e}"

    async def _cmd_session(self, arg, ctx):
        agent = ctx["agent"]
        return f"💾 会话 ID: {agent.store.session_id}\n  消息数: {len(agent.messages)}"

    async def _cmd_help(self, arg, ctx):
        lines = ["📋 可用命令:"]
        for c in sorted(self._commands.values(),
                        key=lambda c: c.get("help", "")):
            if c.get("help"):
                lines.append(c["help"])
        return "\n".join(lines)

    async def _cmd_hook(self, arg, ctx):
        """D5 /hook [list|reload]。

        reload = load_config(force_reload=True) 强制重载 config.json 缓存 +
        重建所有可达 Agent 的 HookManager（与 GUIDE 文档
        "/hook reload 即时生效" 对齐）；list 列出已注册 hooks。
        """
        action = (arg or "").strip().lower()
        if action in ("", "list"):
            return self._hook_list()
        if action == "reload":
            from core.config_loader import load_config
            from core.hook import HookManager
            try:
                cfg = load_config(force_reload=True)
            except Exception as exc:
                logger.exception("hook reload: config.json 重载失败")
                return f"❌ config.json 重载失败: {exc}"
            agents = self._reachable_agents()
            reloaded = 0
            for agent in agents:
                try:
                    # 重建 HookManager：构造即从（已 force_reload 的）配置
                    # 缓存加载 hooks 段，使修改即时生效。
                    agent.hooks = HookManager()
                    reloaded += 1
                except Exception as exc:
                    logger.warning("hook reload 失败 session=%s: %s",
                                   getattr(getattr(agent, "store", None),
                                           "session_id", "?"), exc)
            enabled = bool((cfg.get("hooks") or {}).get("enabled", True))
            return (f"✅ hooks 已重载（config.json force_reload；"
                    f"{reloaded}/{len(agents)} 个 Agent），enabled={enabled}")
        return "❌ 用法: /hook [list|reload]"

    def _hook_list(self) -> str:
        """/hook list：聚合全部可达 Agent 的已注册 hook 统计。"""
        agents = self._reachable_agents()
        if not agents:
            return ("ℹ️ 当前没有已加载的 Agent（hooks 将在 Agent 创建时"
                    "从 config.json 加载）")
        by_event: Counter = Counter()
        samples: list[str] = []
        for agent in agents:
            hooks = getattr(agent, "hooks", None)
            if hooks is None:
                continue
            try:
                rows = hooks.list_hooks()
            except Exception:
                logger.debug("hook list 读取失败", exc_info=True)
                continue
            for row in rows:
                by_event[row.get("event", "?")] += 1
                if len(samples) < 8:
                    samples.append(
                        f"  {row.get('event')}: {row.get('hook')} "
                        f"(matcher={row.get('matcher')})")
        lines = [f"📋 已注册 hooks（{len(agents)} 个 Agent）:"]
        if by_event:
            lines.append("  按事件计数: " + ", ".join(
                f"{k}={v}" for k, v in sorted(by_event.items())))
            lines.extend(samples)
        else:
            lines.append("  （无 hooks）")
        return "\n".join(lines)

    def _reachable_agents(self) -> list:
        """遍历会话池与统一会话 runner 缓存的全部 Agent（/hook 命令用）。

        Agent 跨路径可能被引用（会话池 entry / runner entry），按 id 去重。
        """
        agents: list = []
        seen: set[int] = set()
        sessions = (getattr(self.session_mgr, "_sessions", {})
                    if self.session_mgr is not None else {})
        for entry in list(sessions.values()):
            agent = getattr(entry, "agent", None)
            if agent is not None and id(agent) not in seen:
                seen.add(id(agent))
                agents.append(agent)
        runner = getattr(self, "_conversation_runner", None)
        runner_entries = getattr(runner, "_entries", None)
        if isinstance(runner_entries, dict):
            for entry in list(runner_entries.values()):
                agent = getattr(entry, "agent", None)
                if agent is not None and id(agent) not in seen:
                    seen.add(id(agent))
                    agents.append(agent)
        return agents

    async def on_inbound(self, msg: InboundMessage):
        """入站消息处理（从 channel 线程通过 run_coroutine_threadsafe 调用）"""
        # 去重
        if self._dedup.is_dup(msg.message_id):
            logger.debug("重复消息，跳过: %s", msg.message_id)
            self._metrics.note_inbound(dedup_skipped=True)
            return
        self._metrics.note_inbound()
        # D3 inbound→首 delta 延迟数据源：记录入站时刻（执行时 pop 消费）。
        self._inbound_ts[msg.message_id] = time.monotonic()
        if len(self._inbound_ts) > 4096:
            self._inbound_ts = dict(list(self._inbound_ts.items())[-2048:])
        # 渠道 /stop（设计方案 11.7）：飞书/微信等外部渠道发送 /stop
        # 停止当前会话活动 Turn（不进入队列执行）。
        if msg.channel not in ("webui", "debug") \
                and (msg.text or "").strip().lower() in ("/stop", "停止", "停止任务"):
            if self._conversation_bridge is not None:
                conversation_id = self._conversation_bridge.on_inbound_stop(msg)
                if conversation_id is not None:
                    try:
                        channel = self._channels.get(msg.channel)
                        if channel is not None:
                            await channel.send_reply(msg, "⏹️ 已停止当前任务")
                    except Exception:
                        pass
            return
        # 统一会话：入队 + 渠道持久化去重（重复返回 None 则跳过）
        if self._conversation_bridge is not None:
            conversation_id = self._conversation_bridge.on_inbound(msg)
            if conversation_id is None:
                return
        # 渠道消息（飞书/微信/debug 等）改走统一 runner（设计方案 11.2：渠道执行统一化）。
        # 入队后出队建 queued Turn 并触发执行（忙碌时 run_turn 跳过，终态后由
        # 队首倒计时/下一条消息再触发）。
        if self._conversation_runner is not None \
                and msg.channel not in ("webui",):
            try:
                bridge = self._conversation_bridge
                if bridge is not None:
                    # ensure_turn 出队队首并原位升级渠道 queued node（设计方案 11.3）
                    bridge.ensure_turn(
                        conversation_id,
                        source_message_id=getattr(msg, "message_id", ""))
            except (ExecutionScopeLimit, GatewaySaturated) as exc:
                # D1 饱和反馈：执行域/全局并发打满。消息已由 on_inbound 入队
                # （队列项保留，终态后队首倒计时/下一条消息自动再触发），
                # 这里仅提示"系统繁忙，消息已排队"；渠道不可达只记日志。
                await self._notify_busy(msg, exc)
            except Exception:
                logger.debug("渠道出队建 Turn 失败: %s", conversation_id)
            await self.execute_conversation_turn(conversation_id)
            return
        if self.task_runtime_enabled:
            await self._on_inbound_task_runtime(msg)
            return
        # 旧 _session_worker 漏斗已移除：所有消息已由统一 runner / TaskRuntime 接管。
        logger.warning("on_inbound 无执行路径（统一 runner 未接管）: %s %s",
                       msg.channel, msg.session_key)

    async def _notify_busy(self, msg: InboundMessage, exc: Exception) -> None:
        """D1 饱和反馈：发送"系统繁忙，消息已排队"提示（队列项保留）。

        ExecutionScopeLimit / GatewaySaturated 表示执行域/全局并发打满；
        消息已入队等待执行，不丢消息。send_progress 优先（不抢占 future 型
        通道的最终回复），失败回退 send_reply；渠道不可达时只记日志。
        """
        logger.info("并发饱和反馈: %s (%s) channel=%s session=%s",
                    type(exc).__name__, exc,
                    getattr(msg, "channel", ""), getattr(msg, "session_key", ""))
        channel = self._channels.get(getattr(msg, "channel", ""))
        if channel is None:
            logger.warning("饱和反馈渠道不可达（未注册）: channel=%s session=%s",
                           getattr(msg, "channel", ""), getattr(msg, "session_key", ""))
            return
        notice = "🈵 系统繁忙，消息已排队，请稍候…"
        try:
            await channel.send_progress(msg, notice)
        except Exception:
            try:
                await channel.send_reply(msg, notice)
            except Exception:
                logger.warning("饱和反馈投递失败: channel=%s session=%s",
                               getattr(msg, "channel", ""),
                               getattr(msg, "session_key", ""))

    async def submit_plan_task(self, *, session_key: str, plan_id: str, plan_task_id: str,
                               prompt: str,
                               priority: int = 60, timeout_seconds: int | None = None,
                               metadata: dict | None = None, task_id: str | None = None) -> str:
        """Submit one approved PlanTask through the only persistent execution path."""
        if self._task_runtime is None or self._runtime_store is None:
            raise RuntimeError("TaskRuntime must be enabled before executing a Plan")
        limiter = self._background_source_limiters.get("plan")
        if limiter is not None:
            # plan 源信号量限流（上限=长任务池大小）：提交前占位，终态后由
            # watcher 释放——避免超过长任务线程池容量的 plan 任务占满
            # TaskRuntime 并发槽后在池上排队空转，饿死普通会话。
            await limiter.acquire()
        try:
            entry = self.session_mgr.get_or_create(session_key)
            if entry is None:
                raise RuntimeError("session capacity has been reached")
            self._ensure_workspace_context(entry)
            channel = self._channels.get("webui")
            if channel is None:
                raise RuntimeError("the WebUI delivery channel is unavailable")
            session_id = self._runtime_session_id(session_key)
            self._runtime_store.upsert_session(session_id, session_key, channel="webui", status="active")
            envelope = TaskEnvelope.create(
                session_id=session_id, session_key=session_key, source="plan", prompt=prompt, task_id=task_id,
                plan_id=plan_id, plan_task_id=plan_task_id, priority=priority,
                timeout_seconds=timeout_seconds or int(self._task_runtime_config.get(
                    "default_timeout_seconds", self._hard_timeout)),
                max_steps=int(self.agent_config.get("max_steps", 100)),
                metadata=self._with_workspace_ownership(session_key, {
                    "channel": "webui", "deliver_reply": False,
                    "task_source": "plan", "plan_id": plan_id,
                    "plan_task_id": plan_task_id, **(metadata or {})}),
            )
            message = InboundMessage(
                channel="webui", session_key=session_key, user_id="system", user_name="PlanRuntime",
                text=prompt, message_id=envelope.task_id,
                metadata=dict(envelope.metadata),
            )
            self._runtime_messages[envelope.task_id] = (message, entry)
            submitted_id = await self._session_runtime.submit_envelope(envelope)
        except BaseException:
            if limiter is not None:
                limiter.release()
            raise
        if submitted_id != envelope.task_id:
            self._runtime_messages.pop(envelope.task_id, None)
        self._schedule_background_slot_release(submitted_id, "plan")
        return submitted_id

    async def submit_goal_task(self, *, session_id: str, session_key: str, prompt: str,
                               goal_id: str, goal_revision: int, goal_round: int,
                               task_id: str | None = None) -> str:
        """Submit one low-priority autonomous Goal round through SessionRuntime."""
        if self._session_runtime is None or self._runtime_store is None:
            raise RuntimeError("SessionRuntime must be enabled before running a Goal")
        limiter = self._background_source_limiters.get("goal")
        if limiter is not None:
            # goal 源信号量限流（上限=长任务池大小）：与 plan 同语义，避免
            # 多 Goal 轮次占满 TaskRuntime 并发槽空转饿死普通会话。
            await limiter.acquire()
        try:
            entry = self.session_mgr.get_or_create(session_key)
            if entry is None:
                raise RuntimeError("session capacity has been reached")
            self._ensure_workspace_context(entry)
            self._runtime_store.upsert_session(session_id, session_key, channel="webui", status="active",
                                               active_goal_id=goal_id)
            envelope = TaskEnvelope.create(
                session_id=session_id, session_key=session_key, source="goal", prompt=prompt,
                task_id=task_id, priority=30,
                timeout_seconds=int(self._task_runtime_config.get("default_timeout_seconds", self._hard_timeout)),
                max_steps=int(self.agent_config.get("max_steps", 100)),
                idempotency_key=f"goal:{goal_id}:revision:{goal_revision}:round:{goal_round}",
                metadata=self._with_workspace_ownership(session_key, {
                    "channel": "webui", "deliver_reply": False,
                    "task_source": "goal", "goal_id": goal_id,
                    "goal_revision": goal_revision, "goal_round": goal_round,
                    # 每轮都是"当前最后一步"：终答经 chat.runtime_final 展示，
                    # 前端按 goal_id 只保留最新一轮的最终回复。
                    "final_response": True}),
            )
            message = InboundMessage(channel="webui", session_key=session_key, user_id="system",
                                     user_name="GoalRoundDriver", text=prompt, message_id=envelope.task_id,
                                     metadata=dict(envelope.metadata))
            self._runtime_messages[envelope.task_id] = (message, entry)
            submitted_id = await self._session_runtime.submit_envelope(envelope)
        except BaseException:
            if limiter is not None:
                limiter.release()
            raise
        if submitted_id != envelope.task_id:
            self._runtime_messages.pop(envelope.task_id, None)
        self._schedule_background_slot_release(submitted_id, "goal")
        return submitted_id

    async def submit_subagent_task(self, *, session_id: str, session_key: str, prompt: str,
                                   parent_task_id: str | None = None, metadata: dict | None = None) -> str:
        """Submit a child run through the same SessionRuntime path as every task."""
        if self._session_runtime is None:
            raise RuntimeError("SessionRuntime must be enabled before creating a Subagent")
        entry = self.session_mgr.get_or_create(session_key)
        if entry is None:
            raise RuntimeError("session capacity has been reached")
        self._ensure_workspace_context(entry)
        self._runtime_store.upsert_session(
            session_id, session_key, channel="webui", status="active",
            parent_session_id=(metadata or {}).get("parent_session_id"), origin="subagent",
            subagent_mode=(metadata or {}).get("subagent_mode"), metadata=metadata)
        envelope = TaskEnvelope.create(
            session_id=session_id, session_key=session_key, source="subagent", prompt=prompt,
            parent_task_id=parent_task_id, priority=50,
            timeout_seconds=int(self._task_runtime_config.get("default_timeout_seconds", self._hard_timeout)),
            max_steps=int(self.agent_config.get("max_steps", 100)),
            metadata=self._subagent_envelope_metadata(session_key, metadata),
        )
        message = InboundMessage(channel="webui", session_key=session_key, user_id="root",
                                 user_name="SubagentRuntime", text=prompt, message_id=envelope.task_id)
        self._runtime_messages[envelope.task_id] = (message, entry)
        return await self._session_runtime.submit_envelope(envelope)

    @staticmethod
    def _subagent_envelope_metadata(session_key: str, metadata: dict | None) -> dict:
        """子任务 envelope 元数据：注入父工作区归属（workspace_id/session_id）。

        子 Agent 的审批/提问桥记录从 agent._webui_metadata 取归属字段；缺失时
        前端弹窗按"全局"作用域显示（任何页面可见），且跨页面答复无法被作用域
        过滤拦截。父键是 workspace: 时把父会话归属带给孩子。"""
        merged = {"channel": "webui", "deliver_reply": False, **(metadata or {})}
        return Dispatcher._with_workspace_ownership(session_key, merged)

    @staticmethod
    def _with_workspace_ownership(session_key: str, metadata: dict) -> dict:
        """给后台任务 envelope 元数据注入父工作区归属（workspace_id/session_id）。

        plan/goal/subagent 的审批/提问桥记录从 agent._webui_metadata 取归属字段；
        缺失时前端弹窗按"全局"显示、且无法被 SSE 作用域精确转发/过滤。父键是
        workspace: 时把父会话归属带给任务，保证 record 与前端载荷一致（不产生
        "归属不匹配"403，弹窗也正确限在工作区页面）。"""
        parent_key = Dispatcher._workspace_key_of(session_key)
        if parent_key:
            parts = parent_key.split(":")
            metadata.setdefault("workspace_id", parts[1])
            metadata.setdefault("workspace_session_id", parts[2])
        return metadata

    async def wait_runtime_task(self, task_id: str, *, timeout: float | None = None) -> TaskResult:
        if self._task_runtime is None:
            raise RuntimeError("TaskRuntime is not enabled")
        result = await self._session_runtime.wait(task_id, timeout=timeout)
        # PlanRuntime is the only caller without the inbound terminal-delivery
        # watcher. Release its reconstructed/volatile context once its durable
        # result has been observed.
        if result.status in TaskStatus.terminal():
            self._runtime_messages.pop(task_id, None)
        return result

    async def cancel_runtime_task(self, task_id: str, *, reason: str = "user_requested") -> None:
        """Cancel one durable runtime task without exposing TaskRuntime internals."""
        if self._task_runtime is None:
            raise RuntimeError("TaskRuntime is not enabled")
        await self._session_runtime.cancel(task_id, reason)

    def _schedule_background_slot_release(self, task_id: str, source: str) -> None:
        """plan/goal 提交信号量在任务终态后由 watcher 释放。

        必须在真正终结时释放（不能提交即释放，否则限流失效）；watcher 等待
        TaskRuntime 终态（超时/取消/完成都算），滞留任务保持占位。
        """
        limiter = self._background_source_limiters.get(source)
        if limiter is None:
            return
        watcher = asyncio.create_task(
            self._release_background_slot(task_id, source),
            name=f"limiter-{source}-{task_id[:12]}")
        self._limiter_watchers.add(watcher)
        watcher.add_done_callback(self._limiter_watchers.discard)

    async def _release_background_slot(self, task_id: str, source: str) -> None:
        limiter = self._background_source_limiters.get(source)
        if limiter is None:
            return
        try:
            await self._session_runtime.wait(task_id)
        except (KeyError, asyncio.CancelledError):
            pass
        except Exception:
            pass
        finally:
            limiter.release()

    async def stop_session_runtime(self, session_key: str) -> None:
        """停止指定会话的 Plan/Goal 后台任务（停止会话时联动取消）。

        goal/plan 任务经 TaskRuntime/SessionRuntime 独立运行（submit_plan_task/
        submit_goal_task），与 entry.agent 不是同一执行路径。停止会话必须：
        1) 取消该 runtime session 的所有活动任务（含 goal round / plan step）；
        2) 把该会话的非终态 Plan/Goal 业务记录置为终态，使其驱动停止。
        """
        runtime_session_id = self._runtime_session_id(session_key)
        # 1) 取消所有活动任务（goal / plan / user），兜底释放执行域名额。
        if self._runtime_store is not None:
            try:
                active = {TaskStatus.CREATED, TaskStatus.QUEUED, TaskStatus.LEASED,
                          TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL,
                          TaskStatus.RETRY_WAIT}
                for task in self._runtime_store.list_tasks(
                        session_id=runtime_session_id, statuses=active):
                    task_id = getattr(task, "task_id", "") or ""
                    if task_id:
                        try:
                            await self.cancel_runtime_task(task_id, reason="session_stopped")
                        except (KeyError, RuntimeError):
                            pass
            except Exception:
                logger.exception("停止会话：取消运行时任务失败 %s", session_key)
        # 2) 置终态：取消该会话所有非终态 Plan。
        if self._plan_runtime is not None:
            try:
                for plan in self._plan_runtime.manager.list(runtime_session_id, limit=1000):
                    if not plan.is_terminal:
                        try:
                            await self._plan_runtime.cancel(plan.plan_id)
                        except (KeyError, ValueError, RuntimeError):
                            pass
            except Exception:
                logger.exception("停止会话：取消 Plan 失败 %s", session_key)
        # 3) 置终态：取消该会话所有非终态 Goal（含当前 round task 兜底）。
        if self._goal_runtime is not None:
            try:
                for goal in self._goal_runtime.list(runtime_session_id, limit=1000):
                    if not goal.is_terminal:
                        task_id = getattr(goal, "current_task_id", "") or ""
                        if task_id:
                            try:
                                await self.cancel_runtime_task(task_id, reason="session_stopped")
                            except (KeyError, RuntimeError):
                                pass
                        try:
                            self._goal_runtime.cancel(goal.goal_id)
                        except (KeyError, ValueError, RuntimeError):
                            pass
            except Exception:
                logger.exception("停止会话：取消 Goal 失败 %s", session_key)

    async def clear_session_runtime(self, session_key: str) -> dict:
        """清空会话时联动清理该会话的 Plan/Goal 业务记录。

        先停止运行中的 plan/goal（把它们置终态），再删除其业务记录。
        返回已删除的 plan_id / goal_id 列表，供调用方删除 system 会话与投影。"""
        await self.stop_session_runtime(session_key)
        runtime_session_id = self._runtime_session_id(session_key)
        removed_plans: list[str] = []
        removed_goals: list[str] = []
        if self._plan_runtime is not None:
            try:
                for plan in self._plan_runtime.manager.list(runtime_session_id, limit=1000):
                    try:
                        self._plan_runtime.manager.archive_terminal(plan.plan_id)
                        removed_plans.append(plan.plan_id)
                    except (KeyError, ValueError):
                        pass
            except Exception:
                logger.exception("清空会话：清理 Plan 记录失败 %s", session_key)
        if self._goal_runtime is not None:
            try:
                for goal in self._goal_runtime.list(runtime_session_id, limit=1000):
                    try:
                        self._goal_runtime.archive(goal.goal_id)
                        removed_goals.append(goal.goal_id)
                    except (KeyError, ValueError, RuntimeError):
                        pass
            except Exception:
                logger.exception("清空会话：清理 Goal 记录失败 %s", session_key)
        return {"plans": removed_plans, "goals": removed_goals}

    async def _on_inbound_task_runtime(self, msg: InboundMessage) -> None:
        """Persist an inbound message before scheduling its Agent execution."""
        entry = self.session_mgr.get_or_create(msg.session_key)
        channel = self._channels.get(msg.channel)
        if entry is None:
            if channel:
                await channel.send_reply(msg, "🈵 当前会话数已达上限，请稍后再试")
            return
        if channel is None:
            logger.warning("TaskRuntime 收到未注册 channel: %s", msg.channel)
            return

        source = self._runtime_source(msg.channel)
        priority = {"user": 80, "scheduler": 20, "heartbeat": 10}.get(source, 40)
        runtime_session_id = self._runtime_session_id(msg.session_key)
        self._runtime_store.upsert_session(
            runtime_session_id, msg.session_key, channel=msg.channel,
            status="active", metadata={"user_id": msg.user_id, "is_group": msg.is_group},
        )
        envelope = TaskEnvelope.create(
            session_id=runtime_session_id,
            session_key=msg.session_key,
            source=source,
            prompt=msg.text or "[image message]",
            priority=priority,
            timeout_seconds=int(self._task_runtime_config.get("default_timeout_seconds", self._hard_timeout)),
            max_steps=int(self.agent_config.get("max_steps", 100)),
            idempotency_key=msg.message_id or None,
            metadata={"channel": msg.channel, "message_id": msg.message_id,
                      "has_images": bool(getattr(msg, "images", None)),
                      "user_id": msg.user_id, "user_name": msg.user_name,
                      "is_group": bool(msg.is_group),
                      # Only scheduler currently needs a durable raw context.
                      # Platform-native raw objects are intentionally not
                      # serialized into SQLite.
                      "channel_context": (msg.raw if msg.channel == "scheduler"
                                          and isinstance(msg.raw, dict) else {}),
                      **dict(getattr(msg, "metadata", None) or {})},
        )
        # Register volatile context before queueing: a worker may start as soon
        # as submit() yields to PriorityQueue.put().  The durable delivery row,
        # however, has a foreign key to tasks and must be written *after*
        # submit_envelope has persisted the TaskEnvelope.
        self._runtime_messages[envelope.task_id] = (msg, entry)
        task_id = await self._session_runtime.submit_envelope(envelope)
        self._runtime_store.save_channel_delivery(
            delivery_id=f"delivery:{task_id}", task_id=task_id, channel=msg.channel,
            message_id=msg.message_id, state="accepted", context=envelope.metadata)
        if task_id == envelope.task_id:
            # Channels receive an immediate acknowledgement, while token-level
            # patches remain WebUI-only. Terminal delivery is handled separately.
            # WebUI already has the request Future and should render only the
            # command/answer result. An additional delivery acknowledgement
            # becomes a visible duplicate status bubble (especially for /model
            # and /permission commands). External channels still receive the
            # immediate acknowledgement required by their webhook contracts.
            if msg.channel != "webui":
                try:
                    await channel.send_progress(msg, "✅ 已收到，正在处理中…")
                except Exception:
                    logger.debug("即时确认投递失败: %s", envelope.task_id, exc_info=True)
            watcher = asyncio.create_task(self._deliver_runtime_terminal(task_id, msg))
            self._terminal_watchers.add(watcher)
            watcher.add_done_callback(self._terminal_watchers.discard)
        else:
            self._runtime_messages.pop(envelope.task_id, None)
            logger.info("任务幂等命中: message=%s -> task=%s", msg.message_id, task_id)

    async def _deliver_runtime_terminal(self, task_id: str, msg: InboundMessage) -> None:
        """Deliver terminal failures/cancellation that have no Agent reply body."""
        # 加超时（默认 zombie_max_seconds）：终态迟迟不来时不再无限等待，
        # 投递行保留 accepted 状态供重启后 recover_channel_deliveries 补投。
        zombie_timeout = float(self._task_runtime_config.get(
            "zombie_max_seconds", 300))
        try:
            result = await self._session_runtime.wait(
                task_id, timeout=zombie_timeout)
        except asyncio.TimeoutError:
            logger.warning("等待 TaskRuntime 终态超时(%ss): %s",
                           zombie_timeout, task_id)
            return
        except Exception:
            logger.exception("等待 TaskRuntime 终态失败: %s", task_id)
            return
        if result.status == TaskStatus.COMPLETED:
            self._runtime_store.save_channel_delivery(
                delivery_id=f"delivery:{task_id}", task_id=task_id, channel=msg.channel,
                message_id=msg.message_id, state="completed", context={})
            self._runtime_messages.pop(task_id, None)
            return
        channel = self._channels.get(msg.channel)
        if channel is None:
            self._runtime_messages.pop(task_id, None)
            return
        labels = {
            TaskStatus.CANCELLED: "⏹️ 任务已停止",
            TaskStatus.TIMED_OUT: "⏰ 任务执行超时",
            TaskStatus.BLOCKED: "⚠️ 任务被阻塞",
            TaskStatus.FAILED: "❌ 任务执行失败",
        }
        text = labels.get(result.status, "⚠️ 任务未完成")
        detail = result.error_message or result.summary
        if detail:
            text += f"：{detail}"
        try:
            await channel.send_reply(msg, text)
        except Exception:
            logger.debug("任务终态投递失败: %s", task_id, exc_info=True)
        finally:
            self._runtime_store.save_channel_delivery(
                delivery_id=f"delivery:{task_id}", task_id=task_id, channel=msg.channel,
                message_id=msg.message_id, state=result.status.value, context={})
            # Keep the message/session pair through TaskRuntime retry attempts.
            # This watcher observes only a terminal result, so cleanup here
            # cannot make a retry lose its inbound delivery context.
            self._runtime_messages.pop(task_id, None)

    async def _execute_session_runtime_task(self, envelope: TaskEnvelope,
                                            token: CancellationToken, emit) -> TaskResult:
        """SessionRuntime adapter preserving existing Gateway delivery behavior."""
        emit("message/start", {"source": envelope.source})
        result = await self._execute_runtime_task(envelope, token)
        if result.visible_text:
            emit("message/end", {"text": result.visible_text})
        return result

    async def _execute_runtime_task(self, envelope: TaskEnvelope,
                                    token: CancellationToken) -> TaskResult:
        """TaskRuntime executor bridge: Agent execution plus channel delivery."""
        runtime_message = self._runtime_messages.get(envelope.task_id)
        if runtime_message is None:
            return TaskResult(
                task_id=envelope.task_id, status=TaskStatus.BLOCKED,
                summary="inbound delivery context is unavailable after restart",
                error_code="RUNTIME_CONTEXT_MISSING",
                error_message="cannot safely replay a task without its inbound channel context",
            )
        msg, entry = runtime_message
        channel = self._channels.get(msg.channel)
        if channel is None:
            return TaskResult(task_id=envelope.task_id, status=TaskStatus.FAILED,
                              summary="channel is unavailable", error_code="CHANNEL_UNAVAILABLE",
                              error_message=msg.channel)

        entry.is_busy = True
        started_at = time.time()
        entry.last_active = started_at
        try:
            def stop_agent(_reason: str) -> None:
                agent = entry.agent
                if agent is not None:
                    agent.request_stop()

            token.add_callback(stop_agent)
            runtime_metadata = {**envelope.metadata, "task_source": envelope.source}
            # 会话忙有界重试（见 _RUNTIME_BUSY_RETRY_MAX_S 注释）：plan/goal
            # 首步由父 agent.run 内的工具派生时，父 run 尚未释放 exec_lock，
            # 立即执行会被互斥拒绝。退避重试直到拿到锁或超窗。
            busy_deadline = time.monotonic() + _RUNTIME_BUSY_RETRY_MAX_S
            retry_delay = 1.0
            while True:
                reply = await self._execute_agent(
                    entry, msg, channel, runtime_managed=True, cancellation_token=token,
                    runtime_metadata=runtime_metadata)
                if not (isinstance(reply, str) and reply.startswith(SESSION_BUSY_REPLY)):
                    break
                if token.is_cancelled or time.monotonic() >= busy_deadline:
                    logger.warning(
                        "运行时任务会话忙重试超窗，按忙拒绝落终态: task=%s session=%s",
                        envelope.task_id, getattr(entry, "session_key", "?"))
                    break
                logger.info(
                    "运行时任务命中会话忙（父 run 持锁中），%.1fs 后重试: task=%s session=%s",
                    retry_delay, envelope.task_id, getattr(entry, "session_key", "?"))
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2.0, _RUNTIME_BUSY_RETRY_MAX_DELAY_S)
            token.checkpoint()
            elapsed = max(0.0, time.time() - started_at)
            if reply and envelope.metadata.get("deliver_reply", True):
                agent = entry.agent
                model = agent.llm.model if agent and getattr(agent, "llm", None) else ""
                msg._reply_meta = {"model": model, "elapsed": elapsed, "task_id": envelope.task_id}
                # C1-②：投递统一管线（3 次退避重试 + delivery 行落账，见
                # _deliver_channel_reply）；仍失败则抛错交 TaskRuntime 重试/
                # 终态投递（_deliver_runtime_terminal 单次兜底 + DB 行补投）。
                if not await self._deliver_channel_reply(channel, msg, reply):
                    raise RuntimeError(
                        "channel delivery failed after %d attempts"
                        % DELIVERY_MAX_ATTEMPTS)
            # 统一会话终态：chat.done 权威 + Turn 终态（含 Plan/Goal 系统 Turn）
            if self._conversation_bridge is not None:
                self._conversation_bridge.on_reply(msg, reply or "")
            # （历史 chat.runtime_final 专属广播已拆除：channel.publish_agent_event
            # 无任何生产渠道实现；Plan/Goal 终答经 on_reply → chat.done 与
            # Plan/Goal 投影到达前端。）
            # 失败判定结构化：白名单分类（❌/⛔/⏰/insufficient_quota/任务失败）
            # → 错误码，写入 TaskResult.error_code；plan/goal 状态机据此落终态。
            failure_code = (classify_runtime_failure(reply)
                            if envelope.source in {"plan", "goal"} else None)
            execution_failed = failure_code is not None
            result = TaskResult(
                task_id=envelope.task_id,
                status=TaskStatus.FAILED if execution_failed else TaskStatus.COMPLETED,
                visible_text=reply or "", summary=(reply or "")[:1000],
                error_code=failure_code if execution_failed else None,
                error_message=(reply or "")[:1000] if execution_failed else None,
            )
            self._runtime_store.save_channel_delivery(
                delivery_id=f"delivery:{envelope.task_id}", task_id=envelope.task_id, channel=msg.channel,
                message_id=msg.message_id, state=("failed" if execution_failed else "completed"), context=envelope.metadata)
            return result
        except TaskCancelled:
            if self._conversation_bridge is not None:
                self._conversation_bridge.on_stopped(msg.session_key)
            raise
        except Exception:
            # TaskRuntime records the error and _deliver_runtime_terminal sends
            # one terminal reply. Do not duplicate channel output here.
            raise
        finally:
            entry.is_busy = False
            entry.last_active = time.time()
            # Do not discard this mapping here. TaskRuntime may turn an
            # exception/FAILED result into RETRY_WAIT and invoke this executor
            # again with the same envelope. Terminal cleanup is performed by
            # _deliver_runtime_terminal (or wait_runtime_task for plan tasks).

    @staticmethod
    def _scheduler_execution_input(text: str) -> str:
        """[Scheduled job execution mode] 提示前缀（TaskRuntime 与统一
        conversation-runner 路径共用，见 _task_source_for 的推导）。"""
        return _SCHEDULER_EXECUTION_CONTEXT + (text or "")

    @staticmethod
    def _scheduler_admin_tools() -> frozenset:
        """调度器管理工具块表（共享方法：runner 路径同样适用）。"""
        return _SCHEDULER_ADMIN_TOOLS

    @staticmethod
    def _task_source_for(msg: InboundMessage,
                         runtime_metadata: dict | None) -> str:
        """推导消息的执行来源（task_source）。

        优先级：显式 runtime_metadata.task_source（TaskRuntime 路径）→
        消息来源（channel / session_key 前缀，统一 conversation-runner 路径
        依据会话 subtype/route 元数据重建的 msg 携带这些字段）。
        无法推导时返回 ""——scheduler 防护（执行上下文注入 + 管理工具块表）
        保守不应用（调用方打日志）。
        """
        source = str((runtime_metadata or {}).get("task_source") or "").strip()
        if source:
            return source
        channel = str(getattr(msg, "channel", "") or "").strip()
        session_key = str(getattr(msg, "session_key", "") or "")
        if channel == "scheduler" or session_key.startswith("sched:"):
            return "scheduler"
        if channel == "heartbeat" or session_key.startswith("heartbeat:"):
            return "heartbeat"
        return ""

    @classmethod
    def _task_input_for_agent(cls, msg: InboundMessage,
                              runtime_metadata: dict | None) -> str:
        """Attach producer-specific execution constraints to synthetic work."""
        if cls._task_source_for(msg, runtime_metadata) == "scheduler":
            return cls._scheduler_execution_input(msg.text)
        return msg.text

    # ---- P2：父会话 turn 启动时的后台任务状态注入 ----

    def _active_plan_snapshot(self, session_id: str):
        """取当前会话最近的非终态 Plan（无则 None）；失败容错返回 None。"""
        try:
            if self._plan_runtime is None:
                return None
            plans = self._plan_runtime.manager.list(session_id, limit=20)
            active = [p for p in plans if p.status.value in {"approved", "active", "paused"}]
            return active[0] if active else None
        except Exception:
            logger.debug("active plan snapshot failed", exc_info=True)
            return None

    def _active_goal_snapshot(self, session_id: str):
        """取当前会话的活动 Goal（无则 None）；失败容错返回 None。"""
        try:
            if self._goal_runtime is None:
                return None
            goals = self._goal_runtime.list(session_id, limit=20)
            active = [g for g in goals if g.status.value in {"active", "paused", "blocked"}]
            return active[0] if active else None
        except Exception:
            logger.debug("active goal snapshot failed", exc_info=True)
            return None

    @staticmethod
    def _runtime_status_note(plan, goal) -> str:
        """把进行中的 Plan/Goal 压成一句话状态（无则空串）。纯函数便于测试。"""
        parts: list[str] = []
        if plan is not None:
            done = sum(1 for t in plan.tasks if t.status.value == "completed")
            total = len(plan.tasks)
            parts.append(f"Plan「{plan.title or plan.plan_id}」进行中：{done}/{total} 步")
        if goal is not None:
            parts.append(f"Goal「{goal.objective or goal.goal_id}」"
                         f"{goal.status.value}，第 {goal.rounds_started}/{goal.max_rounds} 轮")
        if not parts:
            return ""
        return "【后台任务状态】" + "；".join(parts) + "（可用 get_runtime_status 查询实时详情）"

    def set_workspace_context_provider(self, provider) -> None:
        """注入工作区运行上下文构建器（供 plan/goal/subagent 后台任务的 entry 使用）。

        provider 签名与 conversation runner 一致：``provider(workspace_id, session_id) -> dict|None``。
        """
        self._workspace_context_provider = provider

    def _ensure_workspace_context(self, entry) -> None:
        """为工作区会话的后台任务 entry 挂接快照冻结上下文。

        plan/goal/subagent 任务经 ``session_mgr.get_or_create`` 得到 SessionEntry；
        若该 entry 未挂接工作区上下文（不同于 conversation runner 的 entry，两者不共享
        runtime_context），Agent 会走"非工作区"分支，权限档位被硬编码为 ask——即使会话是
        unreviewed/allow 也会误弹审批。这里在工作区 session_key 缺失上下文时补齐快照上下文，
        使后台任务与父会话使用同一套模型/权限/推理/目录。

        subagent 子任务键为 ``subagent:{parent_session_key}:{child_id}``（父键自身
        可含冒号，child_id 是最后一段）：父键是 workspace: 时同样挂接父会话上下文，
        使子 Agent 权限随父会话（免审会话的子进程不再误弹审批）。"""
        key = getattr(entry, "session_key", "")
        if not isinstance(key, str):
            return
        key = self._workspace_key_of(key)
        if not key:
            return
        if getattr(entry, "runtime_context", None) is not None:
            return
        provider = self._workspace_context_provider
        if provider is None:
            return
        parts = key.split(":")
        wid, sid = parts[1], parts[2]
        try:
            ctx = provider(wid, sid)
        except Exception:
            logger.exception("工作区后台任务 context 构建失败: %s", key)
            return
        if not ctx:
            return
        entry.runtime_context = ctx.get("runtime_context")
        entry.runtime_snapshot_id = ctx.get("snapshot_id") or ""
        entry.runtime_model = ctx.get("model") or ""
        entry.runtime_permission_mode = ctx.get("permission_mode") or ""
        entry.runtime_reasoning_level = ctx.get("reasoning_level") or "inherit"
        entry.runtime_max_steps = ctx.get("max_steps")
        entry.runtime_mcp_servers = ctx.get("mcp_servers")
        entry.runtime_profile_prompt = ctx.get("profile_prompt")
        entry.runtime_allowed_tools = ctx.get("allowed_tools")
        entry.runtime_allowed_skills = ctx.get("allowed_skills")
        entry.config_stale = False

    @staticmethod
    def _workspace_key_of(key: str) -> str:
        """从会话键提取 ``workspace:wid:sid`` 形式的父工作区键；非工作区族返回 ""。

        直接支持 workspace 会话键；subagent 子任务键
        ``subagent:{parent_session_key}:{child_id}``（父键自身可含冒号，
        child_id 是最后一段）还原出父键。``subagent:continued:{child_id}``
        不携带父会话信息，返回 ""（续跑子任务保持全局默认档位）。
        """
        if not isinstance(key, str) or not key:
            return ""
        if key.startswith("workspace:"):
            parts = key.split(":")
            return key if len(parts) >= 3 else ""
        if key.startswith("subagent:"):
            parent = key.split(":", 1)[1].rsplit(":", 1)[0]
            if parent.startswith("workspace:") and len(parent.split(":")) >= 3:
                return parent
        return ""

    def _session_prefs(self, session_key: str) -> dict:
        """读取会话持久化偏好（模型/推理等级/权限档位）。

        plan/goal 等后台任务与父会话共用同一 agent；Agent 延迟创建时必须继承
        父会话已设置的模型/权限/推理，而不是回落到全局 agent_config 默认值，
        否则 plan/goal 会"用自己的那套"而与父会话脱节。
        """
        bridge = getattr(self, "_conversation_bridge", None)
        svc = getattr(bridge, "service", None) if bridge is not None else None
        if svc is None:
            return {}
        try:
            conv = svc.store.get_conversation_by_key(session_key)
            if conv is None:
                return {}
            return svc.conversation_prefs(conv.conversation_id)
        except Exception:
            logger.debug("读取会话偏好失败: %s", session_key, exc_info=True)
            return {}

    @staticmethod
    def _refresh_session_template(entry, agent) -> None:
        """把缓存 Agent 的模板（Agent Profile 的 system_prompt）对齐到会话最新值。

        单个 Agent 会被缓存在 entry 上跨 Turn 复用；Profile 的 system_prompt 被编辑
        （而不是切换 profile）时不会触发 _rebuild_on_next_message 驱逐 Agent，因此新的
        Plan / Goal 以及普通轮次仍使用"会话初始模板"而非当前模板。这里在每轮执行前
        比较 entry.runtime_profile_prompt 与 Agent 当前模板，不同则重建 system prompt。

        对非 profile 会话（runtime_profile_prompt 为 None），目标与当前都为 None，不做任何事。
        """
        target = getattr(entry, "runtime_profile_prompt", None)
        builder = getattr(agent, "system_prompt_builder", None)
        if builder is None or not hasattr(builder, "set_agent_profile_prompt"):
            return
        current = getattr(builder, "_agent_profile_prompt", None)
        if target == current:
            return
        try:
            builder.set_agent_profile_prompt(target)
            agent._rebuild_system_prompt()
            logger.info("刷新会话 Agent 模板 %s: profile_prompt %s",
                        getattr(entry, "session_key", "?"), "已更新" if target else "已清除")
        except Exception:
            logger.warning("刷新会话 Agent 模板失败 %s",
                           getattr(entry, "session_key", "?"), exc_info=True)

    async def _execute_agent(
        self, entry: SessionEntry, msg: InboundMessage, channel: Channel,
        *, runtime_managed: bool = False,
        cancellation_token: CancellationToken | None = None,
        runtime_metadata: dict | None = None,
    ) -> Optional[str]:
        """在线程池中执行 agent.run()，带 soft/hard 超时。

        P1-2：runner（统一 conversation）路径与 TaskRuntime/dispatcher 路径
        共享同一 SessionEntry 的 agent 实例，双路径并发重入会并发驱动同一
        Agent（历史/事件串扰，懒创建 check-then-act 竞态）。入口用
        entry.exec_lock 非阻塞 try-acquire 做跨路径互斥：拿不到锁说明该会话
        正在另一路径执行，直接以友好的"会话忙"提示返回（不排队，避免
        FIFO 死锁）；懒创建与 agent.run 全程持锁（见 _execute_agent_locked）。"""
        exec_lock = getattr(entry, "exec_lock", None)
        if exec_lock is not None:
            if not exec_lock.acquire(blocking=False):
                logger.warning(
                    "会话忙（另一路径执行中），拒绝并发重入: session=%s msg=%s",
                    getattr(entry, "session_key", "?"),
                    getattr(msg, "message_id", "?"))
                return SESSION_BUSY_REPLY
            # 入口处标记 busy（janitor / 心跳 defer_when_busy 据此跳过该会话）
            entry.is_busy = True
            try:
                return await self._execute_agent_locked(
                    entry, msg, channel, runtime_managed=runtime_managed,
                    cancellation_token=cancellation_token,
                    runtime_metadata=runtime_metadata)
            finally:
                exec_lock.release()
        # 兼容无 exec_lock 的 entry（如测试桩 SimpleNamespace）：退化为原行为
        return await self._execute_agent_locked(
            entry, msg, channel, runtime_managed=runtime_managed,
            cancellation_token=cancellation_token,
            runtime_metadata=runtime_metadata)

    async def _execute_agent_locked(
        self, entry: SessionEntry, msg: InboundMessage, channel: Channel,
        *, runtime_managed: bool = False,
        cancellation_token: CancellationToken | None = None,
        runtime_metadata: dict | None = None,
    ) -> Optional[str]:
        """_execute_agent 的持锁实现（entry.exec_lock 由调用方持有）。"""
        # D3 打点：turn 启动时刻与入站时间戳（inbound→首 delta 延迟数据源）。
        # 入站时间戳由 on_inbound 按 message_id 记录，执行时一次性消费。
        turn_state = {
            "turn_started": time.monotonic(),
            "first_delta": None,
            "delta_events": 0,
            "inbound_ts": self._inbound_ts.pop(
                str(getattr(msg, "message_id", "") or ""), None),
        }
        self._metrics.note_turn_start()
        # task_source 前置推导（供 L6#12 长任务分池与 scheduler 防护共用）
        task_source = self._task_source_for(msg, runtime_metadata)
        loop = asyncio.get_event_loop()
        # L6#12 长任务分池：plan/goal/scheduler 后台长任务走独立线程池
        # （gateway.long_task_pool_size，默认 2），与会话短任务隔离，避免
        # 长任务占满 session worker 阻塞普通对话。
        session_executor = self.session_mgr.get_executor()
        executor = (self._long_task_executor
                    if task_source in {"plan", "goal", "scheduler"}
                    else session_executor)

        # 延迟创建 Agent（创建后在 executor 内运行初始化回调）
        if entry.agent is None:
            cfg = self.agent_config
            initializers = list(self._agent_initializers)

            def _create():
                # 继承父会话持久化偏好（模型/权限/推理），而非用全局默认值——
                # 这样 plan/goal 等后台任务与父会话使用同一套模型/权限/推理。
                prefs = self._session_prefs(entry.session_key)
                _model = prefs.get("model")
                _perm = prefs.get("permission_mode")
                _reasoning = prefs.get("reasoning_level")
                # 推理：inherit/空 → None（继承）；否则用会话设定的等级；
                # 工作区快照冻结的等级作为 prefs 未设定时的兜底。
                _reasoning_resolved = None if _reasoning in (None, "", "inherit") else _reasoning
                if _reasoning_resolved is None and \
                        getattr(entry, "runtime_reasoning_level", "inherit") not in (None, "", "inherit"):
                    _reasoning_resolved = getattr(entry, "runtime_reasoning_level", None)
                if getattr(entry, "runtime_context", None) is not None:
                    agent = create_gateway_agent(
                        session_key=entry.session_key,
                        model=_model or getattr(entry, "runtime_model", "") or cfg.get("model", ""),
                        max_steps=getattr(entry, "runtime_max_steps", None)
                                 or cfg.get("max_steps", 100),
                        permission_mode=_perm or getattr(entry, "runtime_permission_mode", "")
                                        or cfg.get("permission_mode", "allow"),
                        quiet=cfg.get("quiet", True),
                        auto_approve_plan=cfg.get("auto_approve_plan", True),
                        runtime_context=entry.runtime_context,
                        mcp_servers=getattr(entry, "runtime_mcp_servers", None),
                        profile_prompt=getattr(entry, "runtime_profile_prompt", None),
                        allowed_tools=getattr(entry, "runtime_allowed_tools", None),
                        allowed_skills=getattr(entry, "runtime_allowed_skills", None),
                        reasoning_level=_reasoning_resolved,
                        conversation_store=self._replay_store(),
                    )
                else:
                    # 所有非工作区会话（WebUI 主会话/新建 WebUI 会话/飞书等）
                    # 继承会话持久化偏好；未设置时回落全局默认能力子集。
                    main_caps = self.agent_config.get("main_session_caps") or {}
                    mcp_configs = _resolve_main_session_mcp_servers(
                        main_caps.get("mcp_servers")) if main_caps else None
                    agent = create_gateway_agent(
                        session_key=entry.session_key,
                        model=_model or cfg.get("model", ""),
                        max_steps=cfg.get("max_steps", 100),
                        permission_mode=_perm or cfg.get("permission_mode", "allow"),
                        quiet=cfg.get("quiet", True),
                        auto_approve_plan=cfg.get("auto_approve_plan", True),
                        reasoning_level=_reasoning_resolved,
                        mcp_servers=mcp_configs,
                        allowed_tools=main_caps.get("tools") if main_caps else None,
                        allowed_skills=main_caps.get("skills") if main_caps else None,
                        conversation_store=self._replay_store(),
                    )
                for cb in initializers:
                    try:
                        cb(agent, entry)
                    except Exception as e:
                        logger.warning("agent 初始化回调异常: %s", e)
                return agent

            entry.agent = await loop.run_in_executor(executor, _create)

        agent = entry.agent
        # C2 契约②（2026-08-26 已退役为默认）：sessions/*.json 文件转录
        # 全面停写，SQLite 统一会话是唯一权威。set_file_persistence 保留
        # 仅作兼容开关（默认 False，无需再按路径区分）。
        _store = getattr(agent, "store", None)
        _persist_setter = getattr(_store, "set_file_persistence", None) \
            if _store is not None else None
        if callable(_persist_setter):
            _persist_setter(False)
        # 会话模板刷新：Plan/Goal 生成与逐轮执行必须使用"当前会话最新的模板"
        # （Agent Profile 的 system_prompt），而不是 Agent 创建时捕获的"初始模板"。
        # 编辑 Agent Profile 的 system_prompt 不会驱逐缓存 Agent，故在每轮执行前对齐。
        self._refresh_session_template(entry, agent)
        # P1-5：把外部 CancellationToken 贯通到 AgentLoop（RunControl.checkpoint），
        # 使停止能穿透流式/工具/压缩边界，而不只是下一轮开头的 _stop_requested 检查。
        # 每次执行都覆盖 _run_token（可能为 None），避免上一轮的已取消 token 泄漏到本轮。
        agent._run_token = cancellation_token
        if cancellation_token is not None and cancellation_token.is_cancelled:
            agent.request_stop()

        # ---- /skill：将已注册（即当前会话可用）的 Skill 指令与任务组合后执行 ----
        skill_handled, skill_task, skill_reply = self._prepare_skill_command(agent, msg.text)
        if skill_handled and skill_reply is not None:
            self._metrics.note_turn_end(
                duration_s=time.monotonic() - turn_state["turn_started"],
                failed=False, usage=None, flush_lag_s=None)
            return skill_reply

        # ---- / 命令拦截（不经过 LLM） ----
        command_text = skill_task if skill_handled else msg.text
        cmd_reply = await self._handle_gateway_command(
            agent, command_text, loop, executor, entry)
        if cmd_reply is not None:
            self._metrics.note_turn_end(
                duration_s=time.monotonic() - turn_state["turn_started"],
                failed=False, usage=None, flush_lag_s=None)
            return cmd_reply

        # ---- agent.run() 含 soft/hard 超时 ----
        # 注意：必须保存 afuture 引用，软超时后继续等同一个 future
        # 而不能起第二次 agent.run()——线程池里的旧调用不会因 asyncio 取消而停止
        _images = getattr(msg, 'images', None) or None
        event_sink = None
        bridge = self._conversation_bridge
        # D4 协调项：run/turn 上下文在 dispatcher 入口统一推导，event_sink 与
        # _run_agent_task 共用（agent._run_id 由 agent.run 在循环启动时生成，
        # 事件/日志在循环开始后才产生，两处读取到的是同一 run_id；首次为 None
        # 时各自 fallback 到同一 uuid4 推导路径——见 _current_run_context）。
        _ctx_turn = str((runtime_metadata or {}).get("turn_id") or "")

        def _current_run_context():
            return (getattr(agent, "_run_id", None) or uuid.uuid4().hex,
                    _ctx_turn)
        # 事件只写入统一模型（Turn/Node），不再向旧 chat.* 事件总线广播。
        # （历史 publisher/channel.publish_agent_event 分支已拆除：全仓无
        # 生产渠道实现该接口，plan/goal 终态经 bridge.on_reply → chat.done
        # 与 Plan/Goal 投影到达前端。）
        if bridge is not None:
            # The sink runs in the Agent executor thread.  WebuiChannel's
            # event bus is explicitly cross-thread safe; ConversationBridge
            # 旁路把运行时事件持久化为 Turn/Node。
            def event_sink(event):
                # Zombie 迟到事件隔离（stop_timeout 诊断后续）：硬超时隔离/
                # 看门超时摘除 Agent 后，旧 executor 线程里的 agent.run 可能
                # 仍在产出事件。若放行，bridge.ensure_turn 会因"无活动 Turn"
                # 出队下一条排队消息建新 Turn（或 start_turn 凭空建），僵尸
                # 的残余 delta/工具节点就写进无辜的新 Turn。此处按 run 归属
                # 丢弃已退役 run 的全部事件——run_id 在整个 run 生命周期稳定。
                if self._run_is_retired(agent):
                    return
                # D4 trace 全覆盖：bridge.on_agent_event / service.upsert_
                # node_deltas / events.publish 的日志由 core.debug 的
                # _RunContextFormatter 依据 contextvars 注入 [run_id/turn_id]。
                # 事件可能来自并行工具线程（ThreadPoolExecutor 原生态线程不
                # 继承 contextvars），在 sink 边界重新应用 run/turn 上下文，
                # 保证这三类调用点的日志全量携带 run_id（agent.run 主路径的
                # 上下文由 _run_agent_task 设置，两者同源同值）。
                try:
                    from core.debug import set_run_context
                    set_run_context(*_current_run_context())
                except Exception:
                    pass
                if bridge is not None:
                    bridge.on_agent_event(msg, event)
                # D3 打点：delta 事件（首 delta 延迟/字节）与 guard 拦截计数。
                # 事件可能来自并行工具线程，observe_agent_event 内部加锁。
                try:
                    self._metrics.observe_agent_event(turn_state, event)
                except Exception:
                    logger.debug("agent 事件指标打点失败", exc_info=True)
        # runner（统一 conversation-runner）构造 _execute_agent 入参时不带
        # task_source；依据会话来源（channel/session_key 路由元数据）推导，
        # 无法推导时保守不应用 scheduler 防护并打日志（task_source 已在入口
        # 推导，见 _execute_agent 头部）。
        if not (runtime_metadata or {}).get("task_source") and task_source:
            logger.debug("从消息来源推导 task_source=%s (channel=%s, session=%s)",
                         task_source, getattr(msg, "channel", ""),
                         getattr(msg, "session_key", ""))
        elif not (runtime_metadata or {}).get("task_source") and not task_source \
                and (getattr(msg, "metadata", None) or {}).get("from_conversation_queue") \
                and getattr(msg, "channel", "") not in ("webui",):
            logger.debug(
                "无法从会话来源推导 task_source，保守不应用 scheduler 防护 "
                "(channel=%s, session=%s)",
                getattr(msg, "channel", ""), getattr(msg, "session_key", ""))
        prior_blocklist = getattr(agent, "_runtime_tool_blocklist", frozenset())
        agent._runtime_task_source = task_source or ""
        agent._runtime_plan_id = (runtime_metadata or {}).get("plan_id", "")
        agent._runtime_plan_task_id = (runtime_metadata or {}).get("plan_task_id", "")
        agent._runtime_goal_id = (runtime_metadata or {}).get("goal_id", "")
        if task_source == "scheduler":
            # Scheduled prompts can contain wording such as "every day at
            # 11:00". Scope a guard to stop the model from interpreting the
            # execution input as a request to reconfigure the scheduler.
            agent._runtime_tool_blocklist = (
                frozenset(prior_blocklist) | self._scheduler_admin_tools())
        task_input = (skill_task if skill_handled
                      else self._task_input_for_agent(msg, runtime_metadata))
        # P2：父会话普通 turn 启动时，若有进行中的 Plan/Goal 子任务，注入一句话
        # 状态，使模型无需轮询即可感知后台任务进展（调度器已有同类输入前置先例）。
        if not task_source:
            try:
                runtime_session_for_note = self._runtime_session_id(
                    getattr(msg, "session_key", ""))
                status_note = self._runtime_status_note(
                    self._active_plan_snapshot(runtime_session_for_note),
                    self._active_goal_snapshot(runtime_session_for_note))
                if status_note:
                    task_input = f"{status_note}\n\n{task_input}"
            except Exception:
                logger.debug("后台任务状态注入失败（忽略）", exc_info=True)
        # Plan/Goal runs are execution workers, not conversational turns. Keep
        # their tool/final messages out of the user's durable transcript; the
        # structured Plan/Goal projection is the only visible history for them.
        background_transcript = task_source in {"plan", "goal"}
        # B12：transcript 快照改用 list(...) 浅拷贝，避免 deepcopy 大历史
        # 的开销（消息 dict 含工具结果，深拷贝代价高）。
        # 已核实无原地 mutation 证据：
        # - core/agent_runtime/context.py：AgentContext.llm_messages 只读遍历
        #   self._messages；_copy_message 对外层 dict 浅拷贝 + content/tool_calls
        #   深拷贝后交给 provider，杜绝污染 transcript（provider 适配层改写
        #   只作用于副本）。
        # - core/agent_runtime/persistence.py：仅 list.append（列表级操作）。
        # - core/hook/events.py：HookContext 明确不含完整 messages。
        # 因此浅拷贝引用的 message dict 不会被上述模块改写；_retain_runtime_
        # activity 只做切片替换与 extend（列表级操作），added/恢复正确性不受
        # 影响。注：agent.py 的 _rebuild_system_prompt/_init_mcp_if_needed 会
        # 原地更新 messages[0] 的 system prompt content（系统提示同步，属预期
        # 行为，且快照在模板刷新/初始化之后采集），不破坏本恢复逻辑。
        transcript_before = list(agent.messages) if background_transcript else None

        # ---- 当前轮次归属元数据（Question/Approval 上下文校验数据源）----
        # msg.metadata 携带工作区运行上下文（workspace_id/workspace_session_id/
        # snapshot_id/message_id），msg.message_id 为轮次消息 ID。agent.run 对
        # 同一会话 FIFO 串行执行：运行前写入、结束后还原，防止上一轮元数据
        # 泄漏到本轮/下一轮；ApprovalBridge 的 ask_callback 与 ask_question 工具
        # 在执行线程内读取该值做答复归属校验（防跨会话/跨消息答复）。
        _prev_webui_metadata = getattr(agent, "_webui_metadata", None)

        def _run_agent_task():
            # D4 协调项：日志/审计的 run/turn 上下文——贯穿本轮全部日志行与审计行。
            # run_id/turn_id 与 event_sink 共用 _current_run_context() 同一推导，
            # 保证 agent.run 主线程与事件 sink（含并行工具线程）日志 run_id 一致。
            try:
                from core.debug import set_run_context, clear_run_context
                from core.sandbox.audit import set_audit_context, clear_audit_context
                _ctx_run, _ctx_turn_run = _current_run_context()
                set_run_context(_ctx_run, _ctx_turn_run)
                set_audit_context(run_id=_ctx_run, turn_id=_ctx_turn_run)
            except Exception:
                pass
            try:
                return agent.run(task_input, False, images=_images,
                                 event_sink=event_sink)
            finally:
                try:
                    from core.debug import clear_run_context as _crc
                    from core.sandbox.audit import clear_audit_context as _cac
                    _crc()
                    _cac()
                except Exception:
                    pass
                if _prev_webui_metadata is None:
                    try:
                        del agent._webui_metadata
                    except AttributeError:
                        pass
                else:
                    agent._webui_metadata = _prev_webui_metadata
                if background_transcript and transcript_before is not None:
                    self._retain_runtime_activity(
                        agent, transcript_before, task_source, runtime_metadata)
                # D3 打点：turn 总耗时 / LLM usage 累计 / delta flush lag。
                # finally 在 executor 线程执行（agent.run 结束或异常后），
                # 软/硬超时与取消路径同样覆盖（executor 任务真正结束时记录）。
                try:
                    self._record_turn_end(turn_state, agent)
                except Exception:
                    logger.debug("turn 指标记录失败", exc_info=True)

        agent._webui_metadata = {
            **dict(getattr(msg, "metadata", None) or {}),
            "session_key": str(getattr(msg, "session_key", "") or ""),
            "message_id": str(getattr(msg, "message_id", "") or ""),
        }
        afuture = loop.run_in_executor(executor, _run_agent_task)

        def _restore_task_tool_scope(_future=None):
            agent._runtime_tool_blocklist = prior_blocklist
            agent._runtime_task_source = ""
            agent._runtime_plan_id = ""
            agent._runtime_plan_task_id = ""
            agent._runtime_goal_id = ""

        # A timed-out TaskRuntime coroutine can leave the executor worker alive
        # for quarantine accounting. Keep the scoped guard until that worker
        # actually finishes.
        afuture.add_done_callback(_restore_task_tool_scope)
        # C1-①：超时语义统一 —— runner 路径与 TaskRuntime 路径共用同一个
        # _await_agent_future（soft/hard 超时 + zombie 隔离只有一处实现），
        # 消除 TaskRuntime wait_for 与 shield 两套并存的执行等待代码。
        return await self._await_agent_future(
            entry, afuture, agent, channel, msg,
            runtime_managed=runtime_managed)

    async def _await_agent_future(
        self, entry, afuture, agent, channel, msg, *,
        runtime_managed: bool = False,
    ) -> Optional[str]:
        """统一的 Agent 执行 future 等待语义（C1-①：soft/hard 超时 + zombie 隔离）。

        统一 runner（会话/命令路径）与 TaskRuntime 路径的执行等待：wait_for
        与 shield 不再两套并存，soft/hard 超时与 zombie 隔离只在本方法实现。

        - runtime_managed=False（统一 runner / 管理命令路径）：dispatcher
          拥有超时语义 —— soft 超时发进度提示（send_progress，不抢占 future
          型通道最终回复），hard 超时经 _quarantine_after_timeout 做 zombie
          隔离（把仍在线程池运行的 Agent 从 entry 摘除，下一轮重建全新实例，
          避免并发复用），返回超时提示文本。
        - runtime_managed=True（TaskRuntime 路径）：TaskRuntime 拥有超时
          语义（envelope.timeout_seconds + zombie 登记回收），这里仅 shield
          保护 executor worker 不被外部取消误杀；取消仍由 TaskRuntime 驱动，
          取消路径同样经 _quarantine_after_timeout 隔离 Agent（zombie 隔离）。
        """
        if runtime_managed:
            # TaskRuntime owns timeout/cancel. If this coroutine is cancelled
            # (hard timeout / user stop / session teardown), the executor
            # worker may still be running agent.run(); detach the Agent so the
            # next message in this session creates a fresh instance (stale stop
            # flags die with the old one), and quarantine the old agent until
            # its worker truly finishes.
            try:
                return await asyncio.shield(afuture)
            except asyncio.CancelledError:
                self._quarantine_after_timeout(entry, afuture, agent)
                raise

        try:
            return await asyncio.wait_for(
                asyncio.shield(afuture), timeout=self._soft_timeout)
        except asyncio.TimeoutError:
            try:
                # 进度提示走 send_progress，避免抢占 future 型通道的最终回复
                await channel.send_progress(msg, "⏳ 还在处理中，请稍候…")
            except Exception:
                pass
            try:
                return await asyncio.wait_for(
                    afuture, timeout=self._hard_timeout - self._soft_timeout)
            except asyncio.TimeoutError:
                # 硬超时（P0-1）：线程池里的 agent.run() 不会因 asyncio 取消而
                # 停止，必须把它从 entry 摘除，避免下一轮并发复用同一个仍在执行
                # 的 Agent（zombie 隔离，与 TaskRuntime 取消路径同语义）。
                self._quarantine_after_timeout(entry, afuture, agent)
                return f"⏰ 处理超时（>{self._hard_timeout}s），请简化问题后重试"

    def _record_turn_end(self, turn_state: dict, agent) -> None:
        """D3 轮次收尾打点：turn 总耗时、LLM usage 累计、delta flush lag。

        executor 线程内调用（_run_agent_task finally）；sys.exc_info() 反映
        agent.run 是否抛异常（异常传播中 finally 仍可见）。
        """
        failed = sys.exc_info()[1] is not None
        duration = max(0.0, time.monotonic() - turn_state["turn_started"])
        usage = None
        llm = getattr(agent, "llm", None)
        if llm is not None:
            usage = getattr(llm, "last_usage", None)
        flush_lag = None
        if turn_state.get("delta_events", 0) > 0:
            flush_lag = self._delta_flush_lag_sample()
        self._metrics.note_turn_end(
            duration_s=duration, failed=failed,
            usage=usage if isinstance(usage, dict) else None,
            flush_lag_s=flush_lag)

    def _delta_flush_lag_sample(self) -> float | None:
        """D3 delta flush lag 采样：统一会话 delta 合并器距上次刷盘秒数。

        _DeltaMerger 每约 0.1s 刷盘一次（设计方案 16.4）；turn 结束时距上次
        刷盘的时间即当前待刷 delta 的最大可能年龄（≤ 刷盘周期 + 提交耗时）。
        """
        bridge = getattr(self, "_conversation_bridge", None)
        merger = getattr(bridge, "_merger", None) if bridge is not None else None
        if merger is None:
            return None
        try:
            with merger._lock:
                last_flush = merger._last
            return max(0.0, time.monotonic() - last_flush)
        except Exception:
            return None

    # ---- Zombie 迟到事件退役 --------------------------------------------

    def retire_agent(self, agent) -> str:
        """把 agent 当前 run 登记为已退役：其后续事件一律在 sink 处丢弃。

        调用时机 = 该 run 的产出不再被信任的时刻：硬超时隔离
        （_quarantine_after_timeout）、统一 runner 停止/Steering 看门超时、
        以及 runner 执行异常路径。返回被登记的 run_id（便于测试/日志）。"""
        run_id = str(getattr(agent, "_run_id", "") or "")
        if not run_id:
            return ""
        if len(self._retired_run_ids) >= self._retired_run_ids_cap:
            # 极端积压兜底：整体清空（最早的僵尸 worker 早已自然结束）
            self._retired_run_ids.clear()
        self._retired_run_ids.add(run_id)
        return run_id

    def _run_is_retired(self, agent) -> bool:
        """event_sink 归属判定：agent 的当前 run 是否已被退役。"""
        run_id = getattr(agent, "_run_id", None)
        return bool(run_id) and run_id in self._retired_run_ids

    def _quarantine_after_timeout(self, entry, afuture, agent) -> None:
        """硬超时/取消后把仍在运行 agent.run() 的 Agent 与 entry 解耦。

        线程池 worker 里的旧调用无法被 asyncio 取消，继续持有它会与下一轮
        复用同一 Agent（并发执行、历史/事件串扰）。这里把 entry.agent 置空，
        让下一轮懒创建全新 Agent（陈旧停止标志随旧实例丢弃）；同时安排后台
        任务等 afuture 真正结束（旧 worker 收尾）后再做 quarantine 记录
        （出队隔离表并记日志）。
        """
        try:
            key = id(entry)
            self._timed_out_agents[key] = (afuture, agent)
            # 下一个并发使用者不能拿这个还在跑的 agent。
            entry.agent = None
            entry.is_busy = False
            # Zombie 迟到事件隔离：旧 worker 里 agent.run 继续产出的 delta/
            # 工具节点不得再写入统一会话（否则 ensure_turn 会复活链式污染
            # 下一条排队消息的 Turn）。先登记 retired，再交给 watcher 收尸。
            self.retire_agent(agent)

            async def _watch_zombie():
                try:
                    # shield：watcher 被取消时不影响 afuture 本身继续运行
                    await asyncio.shield(afuture)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                finally:
                    # 旧 worker 真正结束：出队隔离记录
                    self._timed_out_agents.pop(key, None)
                    logger.info("隔离 Agent 已真正结束: session=%s",
                                getattr(entry, "session_key", "?"))

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # 无运行中的事件循环（如测试环境）：退回 done 回调兜底
                afuture.add_done_callback(
                    lambda _f: self._timed_out_agents.pop(key, None))
            else:
                watcher = loop.create_task(_watch_zombie())
                # P3-3：保存强引用防 GC，任务结束后自动出集；stop() 统一取消
                self._zombie_watchers.add(watcher)
                watcher.add_done_callback(self._zombie_watchers.discard)
        except Exception:
            logger.exception("硬超时隔离 Agent 失败: %s",
                             getattr(entry, "session_key", ""))

    @staticmethod
    def _retain_runtime_activity(agent, transcript_before, task_source,
                                 runtime_metadata) -> None:
        """Keep a Plan/Goal round's activity as UI-only records.

        The model transcript is restored to its clean pre-task state, while the
        round's tool calls and final reply are appended with a `runtime` marker
        so the WebUI timeline can render tool cards and the last step's final
        reply even after a reload. `runtime` records are excluded from
        AgentContext.llm_messages() and from MessageStore token accounting.
        """
        added = agent.messages[len(transcript_before):]
        agent.messages[:] = transcript_before
        if added:
            meta = runtime_metadata or {}
            runtime = {
                "runtime": task_source,
                "plan_id": meta.get("plan_id", "") or "",
                "plan_task_id": meta.get("plan_task_id", "") or "",
                "goal_id": meta.get("goal_id", "") or "",
                "goal_round": meta.get("goal_round", 0) or 0,
            }
            # plan 最终 step（final_response=True）与 goal 每轮的最终回复
            # 注入主会话上下文（不带 runtime 标记），让主会话能"记住"
            # plan/goal 的最终结论（用户后续对话可引用）。工具调用与中间
            # step 回复仍以 runtime 标记保留（仅 UI 展示，不进 LLM 上下文）。
            inject_final = bool(meta.get("final_response")) or task_source == "goal"
            retained = []
            for message in added:
                role = message.get("role")
                if role == "user":
                    # Task/goal-round prompt: scaffolding, never rendered.
                    continue
                if role == "tool" or (role == "assistant"
                                      and message.get("kind") == "tool_calls"):
                    retained.append({**message, **runtime})
                elif role == "assistant" and message.get("content"):
                    # 最终回复：plan 仅 final step 注入上下文；goal 每轮注入。
                    # goal_round 一并写入注入标记，供 A5 上限裁剪生成归档占位
                    # （"[Goal {goal_id} 第{i}轮终答已归档，详见目标页]"）。
                    if inject_final:
                        retained.append({**message, "runtime_source": task_source,
                                         "plan_id": runtime["plan_id"],
                                         "goal_id": runtime["goal_id"],
                                         "goal_round": runtime["goal_round"]})
                    else:
                        retained.append({**message, **runtime})
            agent.messages.extend(retained)
            # A5：goal 终答注入上限（gateway.goal.inject_keep_last，默认 3，
            # 0=不限）—— 超限的更早轮次注入消息 text 替换为归档占位文本，
            # 保留 runtime/UI-only 标记（详见 _apply_goal_inject_cap）。
            try:
                _apply_goal_inject_cap(agent, runtime)
            except Exception:
                logger.debug("goal 终答注入上限裁剪失败", exc_info=True)
        # 持久化退役（C2 契约②收尾）：Plan/Goal 轮后不再调用 no-op 的
        # save_session。历史注释"持久化失败绝不能从 finally 逃逸"的防御随
        # 调用一并移除；统一会话权威在 SQLite（bridge/service 旁路已实时落库）。

    def _prepare_skill_command(self, agent, text: str) -> tuple[bool, str, Optional[str]]:
        """Resolve a slash token against the Skill tools registered in this session.

        A skill is invoked directly by its own name: ``/code-review <task>`` loads
        the skill's instruction and executes the task in the same agent run.
        Builtin gateway commands take priority over same-named skills.

        支持可选 JSON 参数段：``/code-review {"files":["a.py"]} 检查这些文件``，
        参数会透传给 SkillTool.execute，使 skill 声明的 parameters 在调用记录中可见。
        Returns ``(handled, task_input, immediate_reply)``.
        """
        stripped = (text or "").strip()
        parts = stripped.split(None, 1)
        if not parts or not parts[0].startswith("/"):
            return False, text, None
        cmd_token = parts[0]
        # 内置命令优先（/compact、/clear 等），避免与同名 skill 冲突
        if cmd_token.lower() in self._commands:
            return False, text, None
        name = cmd_token[1:]
        skill_names = set(getattr(agent.tool_registry, "_skill_tool_names", set()))
        if name not in skill_names:
            # 非内置命令、非当前会话 skill —— 交给后续普通命令 / LLM 处理
            return False, text, None
        task = parts[1].strip() if len(parts) > 1 else ""
        tool = agent.tool_registry.get_tool(name)
        if tool is None:
            return True, "", f"❌ Skill '{name}' 不可用于当前会话"

        # 可选 JSON 参数段：解析任务文本开头的 JSON 对象（健壮处理嵌套），
        # 透传给 SkillTool.execute，使 skill 声明的 parameters 在调用记录中可见。
        params: dict = {}
        if task.startswith("{"):
            try:
                parsed, end = json.JSONDecoder().raw_decode(task)
                if isinstance(parsed, dict):
                    params = parsed
                    task = task[end:].strip()
            except json.JSONDecodeError:
                pass  # 不是合法 JSON，整体当作任务文本

        if not task and not params:
            return True, "", f"已选择 skill '{name}'，请在后面补充要执行的任务。"
        # 有必填参数但未在 JSON 中提供时，把自然语言 task 映射到第一个必填参数
        # （仅用于【技能执行】头部展示，task 仍保留作为用户任务传给 LLM）。
        # 例如 /find-skill 找代码审查的skill → query="找代码审查的skill"
        schema = getattr(tool, "parameters", None) or {}
        required = schema.get("required") or []
        if required and task and not params:
            params[required[0]] = task
        try:
            instruction = tool.execute(**params)
        except Exception as exc:
            logger.warning("准备 skill '%s' 失败: %s", name, exc)
            return True, "", f"❌ 载入 skill '{name}' 失败"
        # 审计标记：skill 名与参数已包含在 instruction 中（SkillTool.execute 输出），
        # 与用户任务合并后作为单条输入，保留完整调用痕迹供历史追踪。
        return True, f"{instruction}\n\n【用户任务】\n{task}", None

    async def _handle_gateway_command(
        self, agent, text: str, loop, executor, entry=None
    ) -> Optional[str]:
        """拦截 / 前缀命令，直接处理不经过 LLM。返回回复文本或 None（非命令）。"""
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        command = self._commands.get(cmd)
        if command is None:
            # 非 gateway 命令，交给 agent 处理（可能是 /plan 等需要 LLM 参与的命令）
            return None
        ctx = {"agent": agent, "entry": entry, "loop": loop, "executor": executor}
        return await command["handler"](arg, ctx)

    async def _send_chunked(self, channel: Channel, msg: InboundMessage, text: str):
        """分片发送长文本。平台内部自行分片的 channel 跳过预切。"""
        if channel.handles_chunking:
            await channel.send_reply(msg, text)
        else:
            max_len = 1500
            chunks = split_text(text, max_len)
            for chunk in chunks:
                await channel.send_reply(msg, chunk)

    async def _deliver_channel_reply(
        self, channel: Channel, msg: InboundMessage, reply: str, *,
        bridge=None,
    ) -> bool:
        """统一渠道回复投递管线（C1-②）：3 次退避重试 + delivery 状态落账。

        合并 runner 渠道投递（原内联 3 次退避循环，设计方案 11.5）与
        TaskRuntime 投递（原单次 _send_chunked）为同一管线：
        - 发送失败按 DELIVERY_MAX_ATTEMPTS（3）线性退避重试
          （0.5s * attempt），两条路径重试语义一致；
        - bridge 给定（统一 runner 路径）：发送前落 pending_delivery，
          成功落 delivered / 失败落 delivery_failed（conversation delivery
          状态，前端 delivery.status）；
        - bridge 为 None（TaskRuntime 路径）：DB delivery 行由调用方在
          提交时落 accepted、终态落 completed/failed —— DB 行仍是重启恢复源
          （recover_channel_deliveries 对 accepted/recovery_pending/
          retry_pending 补投终态摘要），本方法只负责发送重试语义。

        返回是否投递成功；最终失败由调用方决定后续（runner 记
        delivery_failed；TaskRuntime 抛错交重试/终态投递）。
        """
        if bridge is not None:
            bridge.record_channel_delivery(msg, "pending_delivery")
        for attempt in range(DELIVERY_MAX_ATTEMPTS):
            try:
                await self._send_chunked(channel, msg, reply)
                if bridge is not None:
                    bridge.record_channel_delivery(msg, "delivered")
                return True
            except Exception:
                if attempt < DELIVERY_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
        if bridge is not None:
            bridge.record_channel_delivery(msg, "delivery_failed")
        logger.warning(
            "渠道回复投递失败（%d 次尝试后放弃）: %s",
            DELIVERY_MAX_ATTEMPTS, getattr(msg, "session_key", "?"))
        return False
