# -*- coding: utf-8 -*-
"""

Agent 主程序 —— 把 LLM + 工具串起来的"智能体指挥官"
工作模式：原生 provider tool-call AgentLoop
消息流结构：原生 provider tool calls → tool results → assistant final response
使用示例：
    from agent import create_agent
    agent = create_agent()
    result = agent.run("帮我看看当前目录")
"""

import json
import re
import os
import sys
import queue
import threading
import uuid
import itertools
import time

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import logging

from core import JKAgentLLM, SystemPrompt
from core.llm_client import detect_context_length
from core.compressor import LIGHT_KEEP_RECENT_RESULTS, Compressor, tool_compress_needed
from core.debug import (
    logger, setup_logging,
    set_debug,
    log_llm_response,
    log_info,
)
from core.message_store import MessageStore, _content_to_text, _estimate_tokens
from core.config_loader import is_enabled
from core.config_loader import load_config
from core.permission import PermissionChecker, ALLOW, ASK, DENY
from core.security_gate import SecurityGate
from core.process_manager import ProcessManager
from tools import BaseTool, ToolRegistry
from tools.builtin_tools import register_all_tools
from tools.web_tools import register_web_tools
from memory import MemoryManager, workspace_memory_dir
from skills import SkillManager, SkillTool, CreateSkillTool
from core.sandbox import SandboxExecutor
from core.hook import HookManager, HookEvent, Decision
from core.runtime import TaskCancelled
from core.runtime.tool_runtime import ToolRuntime
from core.agent_runtime import AgentContext, ToolBatchExecutor
from core.agent_runtime.control import BudgetExceeded
from core.runtime.text_normalization import normalize_model_text
from core.tool_schema import sanitize_message_name


# 输出预留（tokens）：从模型上下文窗口中扣减，作为回复/工具调用输出的余量。
# 历史预算 = context_length - 输出预留（替代旧的 context_length // 2）。
# 8192 为默认单次输出上限，×3 覆盖多轮工具调用内的连续输出，另加 4096 固定余量；
# 小上下文模型按 1/4 缩放，避免预留挤占历史空间。
OUTPUT_RESERVE = 8192 * 3 + 4096


def _history_budget(context_length: int) -> int:
    """历史消息预算：模型上下文减去输出预留（上限 28k、小模型按 1/4 缩放）。

    L2#12 小模型输出预留：预算下限 = max(context_length - reserve,
    context_length // 2)。旧实现以 4096 为绝对下限——小上下文模型（如 4k）
    预算会被顶到整个窗口，输出无预留空间；改为随上下文缩放的下限后，
    小模型至少留出半窗口给回复/工具调用输出。
    """
    reserve = min(OUTPUT_RESERVE, max(context_length // 4, 0))
    return max(context_length - reserve, context_length // 2)


def _configured_workspace_path(config: Optional[dict] = None) -> Path:
    """Return the configured workspace anchored to the project root.

    ``create_agent`` changes the process CWD to the workspace.  Resolving a
    relative setting against that mutable CWD would turn ``./workspace`` into
    ``workspace/workspace`` on later agent creation or prompt rebuilding.
    """
    if config is None:
        config = load_config()
    configured = config.get("permission", {}).get("workspace", "./workspace")
    path = Path(configured)
    if path.is_absolute():
        return path.resolve()
    from core.config_loader import _find_project_root
    return (_find_project_root() / path).resolve()


# ============================================================
# Agent 核心

# ============================================================


class Agent:
    """

    AI 智能体 —— 原生工具调用模式
    消息流约定：
      user (提问)
      → assistant (native tool_calls)
      → tool (tool results)
      → assistant (final response)
    工具选择优先级：
      read/write/edit/grep/glob → 文件操作首选
      bash → 仅用于运行脚本/安装包/git
    """

    def __init__(
        self,
        name: str,
        llm: JKAgentLLM,
        tool_registry: ToolRegistry,
        system_prompt: str = None,
        system_prompt_builder: SystemPrompt = None,
        max_steps: int = 100,          # 最大 AgentLoop 步数
        max_history_tokens: int = 0,   # 上下文预算阈值（0=自动：模型上下文-输出预留）
        debug: bool = False,
        permission_checker: PermissionChecker = None,
        memory: MemoryManager = None,  # 跨会话记忆系统（可选）
        mcp_servers: list = None,      # MCP 服务器配置列表（可选）
        sandbox: SandboxExecutor = None,  # 沙箱执行器（可选）
        process_manager = None,           # 长驻子进程管理器（可选）
        hooks_enabled: bool = True,       # Hook 模块（事件驱动自定义扩展）
        non_interactive: bool = False,    # 非交互模式（gateway 等无 TTY 场景）
        quiet: bool = False,              # 静默模式（LLM 不流式打印到 stdout）
    ):
        self.name = name
        # ---- 运行上下文（Phase 0：显式三目录分离，禁止 os.chdir）----
        self._framework_root: Optional[str] = None
        self._agent_data_root: Optional[str] = None
        self._project_root: Optional[str] = None
        self._working_directory: Optional[str] = None
        self._extra_workspace_roots: tuple = ()
        self._runtime_context: Optional[object] = None
        self.llm = llm
        self.tool_registry = tool_registry
        self.max_steps = max_steps
        self.non_interactive = non_interactive
        self.quiet = quiet
        self.auto_approve_plan = True  # WebUI Plan approval bridge default
        # 权限审批回调（WebUI ask 档审批桥注入；None 时 ASK fail-closed）
        self.ask_callback = None
        # 协作式停止标志（WebUI 停止按钮；统一 AgentLoop 每步检查）
        self._stop_requested = False
        # 上下文预算阈值：0 时取模型上下文减去输出预留（留足输出空间）
        if max_history_tokens == 0:
            self.max_history_tokens = _history_budget(llm.context_length)
        else:
            self.max_history_tokens = max_history_tokens
        self.debug = debug
        self.permission = permission_checker or PermissionChecker()
        self.memory = memory  # 跨会话记忆系统
        self.sandbox = sandbox  # 沙箱执行器
        self.process_manager = process_manager  # 长驻子进程管理器
        self.skill_manager = None  # 技能系统（延迟注册）
        # 中央安全闸门：统一 L1 权限 + L2 沙箱，覆盖所有工具（含 skill/MCP）
        self._gate = SecurityGate(
            self.permission, self.sandbox, str(self.permission.workspace)
        )
        # 运行事件锁：并行工具段多线程调用 _emit_event 时保护 seq 自增与 sink 调度
        self._event_lock = threading.Lock()
        # proc_* 工具权限：由 PolicyEngine 四档裁决（proc:manage/exec:shell 能力
        # 分类），不再注册 set_rule——规则表无执行方读取，历史注册已清理。
        # Hook 模块：事件驱动自定义扩展（用户审计/通知/改写/拦截）
        self.hooks = HookManager(enabled=hooks_enabled)
        if debug:
            set_debug(True)
        # MCP 客户端管理器（延迟初始化）
        self.mcp_manager = None
        self._mcp_pending_init = mcp_servers
        self._mcp_tool_names: list = []  # 记录 MCP 工具名称（方便清理）
        # System Prompt
        self.system_prompt_builder = system_prompt_builder
        if system_prompt:
            self.system_prompt = system_prompt
        else:
            self.system_prompt = self._build_system_prompt()
        # 消息存储 —— 管理历史 + 上下文统计
        self.store = MessageStore(
            max_tokens=self.max_history_tokens,
        )
        self.store.model_context_length = self.llm.context_length
        self.messages = self.store.messages  # 指向同一列表，现有代码兼容
        # 保存当前模型配置到 store（用于会话持久化）
        self.store.model_id = self.llm.model or ""
        self.store.model_provider = getattr(self.llm, "provider", "") or ""
        self.store.model_base_url = getattr(self.llm, "base_url", "") or ""
        self.store.model_llm_type = getattr(self.llm, "llm_type", "") or ""
        # ---- 上下文压缩器 ----
        self._compressor = Compressor(llm=self.llm)
        # ---- A1：挂起的 system 模板（技能刷新/MCP reload/profile 更新后延迟应用）----
        self._pending_system_template = None
        # ---- A4：后台增量压缩状态 ----
        self._compression_pending = False
        self._compression_cooldown_until = 0.0

    # ============================================================
    # Only perform destructive history eviction when the context is genuinely
    # near overflow. Normal high usage is handled by full compression first.
    EMERGENCY_TRUNCATE_RATIO = 0.95
    # A4：自动压缩失败后的冷却秒数（冷却期间自动压缩跳过；手动 /compact 不受限）
    COMPRESSION_FAILURE_COOLDOWN = 60.0

    def _truncate_history(self):
        """Safely evict old history only at the emergency overflow threshold.

        Tool-call assistant messages and their following tool results are removed
        as one unit so provider message pairing is never left malformed. Any
        destructive eviction invalidates the provider usage anchor because its
        original message prefix no longer exists.
        """
        if self.max_history_tokens <= 0 or len(self.messages) <= 3:
            return
        total = self.store.live_tokens()
        ratio = total / self.max_history_tokens
        if ratio <= self.EMERGENCY_TRUNCATE_RATIO:
            return

        # P2：O(n²) → O(n)——预计算每条消息的 token 估算（UI-only runtime 记录
        # 与 _estimate_all_tokens 语义一致地计 0），逐轮增量扣减，避免每轮
        # 全量重算 _estimate_all_tokens()。
        per_msg = [
            0 if m.get("runtime") else _estimate_tokens(m.get("content", ""))
            for m in self.messages
        ]
        total = sum(per_msg)

        dropped = 0
        while len(self.messages) > 3 and total > self.max_history_tokens:
            index = 1  # preserve system prompt
            first = self.messages[index]
            remove_count = 1
            # Never leave tool_result messages without their assistant tool call.
            if first.get("role") == "assistant" and first.get("kind") == "tool_calls":
                while index + remove_count < len(self.messages):
                    candidate = self.messages[index + remove_count]
                    if candidate.get("role") == "tool":
                        remove_count += 1
                    else:
                        break
            elif first.get("role") == "tool":
                # A defensive repair for malformed/legacy history: remove the
                # contiguous tool-result group rather than one orphan result.
                while index + remove_count < len(self.messages):
                    candidate = self.messages[index + remove_count]
                    if candidate.get("role") == "tool":
                        remove_count += 1
                    else:
                        break
            removed_tokens = sum(per_msg[index:index + remove_count])
            del self.messages[index:index + remove_count]
            del per_msg[index:index + remove_count]
            dropped += remove_count
            total -= removed_tokens

        if dropped:
            # The exact provider usage no longer describes this message list.
            self.store.reset_anchor()
            log_info(f"⚠️ 上下文接近溢出（{ratio:.0%}），安全截断 {dropped} 条历史消息")
    # ============================================================

    def _light_compress(self):
        """
        轻量压缩：规则替换已消费的旧工具结果为短摘要 + 降级已消费图片。
        在每轮对话后自动执行，零 LLM 开销。

        修复"工具输出被过早压缩 → 模型反复重读同一文件"：
        - 压力门控：仅当历史预算占用 ≥ LIGHT_RESULT_RATIO（默认 60%）才
          压缩工具结果；预算未配置时保持旧的始终压缩行为（保守兜底）；
        - 近端保护：最近 LIGHT_KEEP_RECENT_RESULTS 条仍有内容的工具结果
          永不压缩——模型当前工作集（刚读的文件等）必须保持可见，
          否则同一个 Turn 内会陷入 read → 被压缩 → 再 read 的死循环；
        - 已消费图片降级始终执行（收益大、几乎无回读需求）。
        """

        old_total = self.store.live_tokens()
        compress_results = tool_compress_needed(
            old_total, self.store.max_tokens)
        self._cancel_checkpoint()
        self._compressor.light_compress(
            self.messages,
            compress_results=compress_results,
            keep_recent_results=LIGHT_KEEP_RECENT_RESULTS,
        )
        new_total = self.store.live_tokens()
        saved = old_total - new_total
        if saved > 0:
            log_info(f"轻量压缩: 释放了约 {saved} tokens")

    def _full_compress(self, verbose: bool = True) -> bool:
        """

        全量压缩：LLM 结构化摘要替代早期对话。
        在 /compact 命令或自动阈值触发时执行。
        压缩后锚点失效，下次 API 调用重新校准。
        返回:
            True 表示压缩成功
        """

        # 全量压缩本身
        before_tokens = self.store.live_tokens()
        self._cancel_checkpoint()
        ok = self._compressor.full_compress(
            store=self.store,
            messages=self.messages,
            verbose=verbose,
        )
        if ok:
            stats = self.store.stats()
            log_info(
                f"全量压缩完成: {stats['total_messages']} 条消息, "
                f"剩余 {stats['remaining_tokens']:,} tokens"
            )
            self.store.record_event(
                "compaction",
                tokens_before=before_tokens,
                tokens_after=stats.get("remaining_tokens", 0),
                summary_preview=self._history_summary_preview(),
            )
            # 持久化退役（C2 契约②）：文件转录停写后 save_session 为 no-op，
            # 不再调用；会话权威在 SQLite 统一会话（gateway 路径）。
        return ok

    def _history_summary_preview(self) -> str:
        """提取当前上下文中的 history_summary 内容预览（用于结构化事件）。"""
        for msg in self.messages:
            if msg.get("kind") == "history_summary":
                text = _content_to_text(msg.get("content", ""))
                return text[:300]
        return ""

    def _check_context(self, verbose: bool = True) -> bool:
        """
        检查上下文占用率，按阈值自动压缩或提示（A4 压缩策略）。

        阈值（相对 store.max_tokens 历史预算）：
          ≥95%  同步紧急截断（安全网优先，任何后台延迟都不允许溢出）；
          ≥80%  且非失败冷却 → 置 self._compression_pending，由 AgentLoop 在
                turn 边界后台执行增量压缩（不阻塞本轮 provider 调用）；
          ≥60%  提示用户可手动 /compact。
        返回:
            恒为 False（不再同步执行全量压缩；旧语义的“已执行全量压缩”
            由后台增量压缩路径取代）
        """
        max_tokens = self.store.max_tokens
        if max_tokens <= 0:
            return False
        ratio = self.store.live_tokens() / max_tokens

        if ratio >= self._compressor.EMERGENCY_RATIO:
            # 安全网优先：同步紧急截断，避免任何后台延迟导致溢出。
            self._truncate_history()
            ratio = self.store.live_tokens() / max_tokens

        if ratio >= self._compressor.AUTO_RATIO:
            if time.monotonic() < self._compression_cooldown_until:
                if verbose:
                    print(f"  ⏳ 上下文占用 {ratio:.0%}，自动压缩处于失败冷却中，跳过")
                return False
            self._compression_pending = True
            if verbose:
                print(f"  📐 上下文占用 {ratio:.0%}，本轮结束后将后台增量压缩…")
            return False

        if ratio >= self._compressor.WARN_RATIO:
            if verbose:
                print(f"  ⚠️  上下文占用 {ratio:.0%}，输入 /compact 可压缩历史释放空间")
        return False

    def _compression_commit_guard(self) -> bool:
        """增量压缩提交护栏：新一轮 run 已开始时放弃本次提交。

        增量压缩在后台线程完成 LLM 合并（不碰消息列表），提交
        （clear+extend）前检查该护栏：若新一轮 run 已启动，放弃提交并重新
        置位 _compression_pending，留待下一轮 turn 边界重试，避免与运行中的
        消息列表变更竞争。
        """
        if getattr(self, "_is_running", False):
            self._compression_pending = True
            return False
        return True

    def _incremental_compress(self) -> bool:
        """A4：turn 边界后台增量压缩（由 AgentLoop 后台线程调用）。

        以现有 history_summary 为基底，仅对摘要之后的新片段做 LLM 合并摘要，
        不重读全量历史（省 token/省时）；失败进入 60s 冷却——冷却期间自动
        压缩跳过，手动 /compact（直接走 _full_compress）不受限。
        """
        before_tokens = self.store.live_tokens()
        try:
            self._cancel_checkpoint()
        except Exception:
            pass
        try:
            ok = self._compressor.incremental_compress(
                store=self.store,
                messages=self.messages,
                verbose=False,
                commit_guard=self._compression_commit_guard,
            )
        except Exception as exc:
            logger.warning("增量压缩异常: %s", exc, exc_info=True)
            ok = False
        if not ok:
            # 失败冷却：自动压缩跳过；手动 /compact 不受限。
            self._compression_cooldown_until = (
                time.monotonic() + self.COMPRESSION_FAILURE_COOLDOWN)
            return False
        stats = self.store.stats()
        logger.info(
            "增量压缩完成: %s 条消息, 剩余 %s tokens",
            stats["total_messages"], stats.get("remaining_tokens", 0))
        try:
            self.store.record_event(
                "compaction_incremental",
                tokens_before=before_tokens,
                tokens_after=stats.get("remaining_tokens", 0),
                summary_preview=self._history_summary_preview(),
            )
        except Exception:
            pass
        # 持久化退役：增量压缩后不再调用 no-op 的 save_session
        return True

    def _ask_user(self, tool_name: str, params: dict) -> str:
        """
        工具权限确认统一入口。
        交互模式：显示操作详情，等待用户输入 A/Y/N/S。
        非交互模式：优先走 ask_callback（WebUI 审批桥）；没有审批器时拒绝。
        """
        if self.non_interactive:
            if self.ask_callback is not None:
                return self.ask_callback(tool_name, params)
            logger.warning("非交互 ASK 缺少审批桥，已按 fail-closed 拒绝: %s", tool_name)
            return "n"
        # 交互模式：原有逻辑
        print(f"\n  ❓ 需要确认: {tool_name}")
        for k, v in params.items():
            v_str = str(v)[:120]
            print(f"     {k}: {v_str}")
        print(f"  ─────────────────────────────")
        print(f"  A = 本次会话工作区内全部放行")
        print(f"  Y = 允许本次操作")
        print(f"  N = 拒绝本次操作")
        print(f"  S = 跳过本次操作")
        try:
            return input(f"  请选择 [A/Y/N/S] (默认Y) ").strip().lower()
        except EOFError:
            # P2-5：stdin 关闭（管道结束/守护环境）时 input() 抛 EOFError。
            # 异常若逃逸会跳过本批其余工具结果的落账，留下悬空 tool_calls，
            # provider 直接会话级 400。EOF 视为用户拒绝（fail-closed），
            # 由调用方生成 is_error 的拒绝结果继续本批。
            print("\n  ⚠️ 输入流已关闭（EOF），本次操作按拒绝处理")
            return "n"
        except KeyboardInterrupt:
            # Ctrl+C 同样视为用户拒绝，而不是让异常炸出工具批次。
            print("\n  ⚠️ 收到中断信号（Ctrl+C），本次操作按拒绝处理")
            return "n"

    def request_stop(self):
        """请求协作式停止：统一 AgentLoop 在下一步检查点退出。

        stop_timeout Fix-2：同时对本 agent 名下的在途工具进程组直接 SIGKILL
        （run_killable 经线程本地登记的进程组）——不再依赖工具侧 0.2s 轮询
        先观察到标志，停止即时生效，10s 停止看门不再有竞态窗口。
        """
        self._stop_requested = True
        try:
            from core.shell import kill_owner_process_groups
            killed = kill_owner_process_groups(id(self))
            if killed:
                logger.info("停止请求：已强杀 %d 个在途工具进程组", killed)
        except Exception:
            logger.debug("停止请求：在途进程组强杀失败（忽略，协作式停止仍生效）", exc_info=True)
        logger.info("收到停止请求（将在下一步检查点生效）")

    def _consume_stop(self) -> bool:
        """检查并消费停止标志。返回 True 表示应停止。"""
        if getattr(self, "_stop_requested", False):
            self._stop_requested = False
            return True
        return False

    def _cancel_checkpoint(self) -> None:
        """P1-5：协作取消检查点。外部 token 被取消则抛 TaskCancelled，交由
        AgentLoop.run 收口为 CANCELLED。在压缩/截断等昂贵步骤边界调用。"""
        control = getattr(self, "_run_control", None)
        if control is not None:
            control.checkpoint()

    def clear_history(self):
        """Clear the live conversation and persist the empty state immediately.

        ``/clear`` is handled as a gateway command, so it does not pass through
        the normal ``agent.run`` finalization path. Saving here is therefore
        required; otherwise the next WebUI history refresh or agent recreation
        reloads the old session file and makes the clear appear ineffective.
        """
        self.store.clear()
        self._memory_clear_count = getattr(self, "_memory_clear_count", 0) + 1
        self.store.record_event("history_cleared", clear_count=self._memory_clear_count)
        # 文件转录已退役（SQLite 统一会话为唯一权威）：/clear 由 dispatcher
        # 联动 bridge.service.clear_history 清空统一会话 turns/nodes，重建时
        # SQLite 回放自然为空，无需再写空会话文件防"清了又回来"。

    def switch_llm(self, **kwargs):
        """
        运行时切换 LLM 模型，不影响对话历史和工具
        用法:
            agent.switch_llm(provider="ollama", model="gemma4")
            agent.switch_llm(model="gpt-4", base_url="https://api.openai.com", llm_type="cloud")

        线程安全（P2-9）：若 AgentLoop 正在运行，切换不即刻改共享的 llm/
        压缩器/token 预算，而是挂起为 pending，由本轮结束后的下一轮原子应用，
        避免同一轮前后半段用不同模型或上下文预算。
        """
        if getattr(self, "_is_running", False):
            self._pending_llm_config = dict(kwargs)
            logger.info("Agent 运行中，模型切换挂起，下一轮生效: %s", kwargs.get("model"))
            return
        self._apply_llm_config(**kwargs)

    def _apply_llm_config(self, **kwargs):
        """实际应用 LLM 配置（_apply / 下轮消费 pending 共用同一实现）。"""
        old_llm = self.llm
        self.llm = JKAgentLLM(**kwargs)
        # P3：旧实例尽力关闭（有 close 则调），释放连接/会话资源；无 close 跳过。
        try:
            close = getattr(old_llm, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            logger.debug("关闭旧 LLM 实例失败（可忽略）: %s", exc)
        # 根据新模型的上下文长度重新计算压缩阈值（模型上下文 - 输出预留）
        self.max_history_tokens = _history_budget(self.llm.context_length)
        # 同步 store 的阈值和模型配置（/stats 显示用的 store.max_tokens）
        self.store.max_tokens = self.max_history_tokens
        self.store.model_context_length = self.llm.context_length
        self.store.model_id = self.llm.model or ""
        self.store.model_provider = getattr(self.llm, "provider", "") or ""
        self.store.model_base_url = getattr(self.llm, "base_url", "") or ""
        self.store.model_llm_type = getattr(self.llm, "llm_type", "") or ""
        # 会话进行中切换模型/推理等级才记为结构化事件（首次创建不记）
        if len(self.messages) > 1:
            self.store.record_event(
                "model_change",
                model=self.store.model_id,
                provider=self.store.model_provider,
                base_url=self.store.model_base_url,
                llm_type=self.store.model_llm_type,
                reasoning_level=getattr(self.llm, "reasoning_level", None),
            )
        # 锚点失效——不同模型的 tokenizer 不同。
        # 修复：仅推理等级变化（模型与上下文长度不变）时不失效锚点。
        # 原实现无条件清零锚点 → live_tokens 回退到字符估算（远小于 provider
        # 精确值），UI 上下文占用骤降，长会话下看起来像"上下文被清空"
        # （实际消息完好，模型记忆不受影响）。纯推理切换不改变历史 token 数，
        # 保留锚点反而更精确；下次 API 调用仍会自动校准。
        _model_changed = (
            getattr(old_llm, "model", "") != getattr(self.llm, "model", "")
            or int(getattr(old_llm, "context_length", 0) or 0)
                != int(getattr(self.llm, "context_length", 0) or 0))
        if _model_changed:
            self.store.reset_anchor()
        # 压缩器也要跟新 LLM 走（全量压缩用）
        self._compressor._llm = self.llm
        print(f"  ✅ 已切换模型: {self.llm}")
        print(f"  📐 上下文: {self.llm.context_length:,} tokens | 压缩阈值: {self.max_history_tokens:,} tokens")

    def _consume_pending_llm(self) -> None:
        """下一轮开始时原子应用运行期挂起的模型切换（P2-9）。"""
        pending = getattr(self, "_pending_llm_config", None)
        if pending:
            self._pending_llm_config = None
            try:
                self._apply_llm_config(**pending)
            except Exception as exc:
                logger.warning("应用挂起的模型切换失败: %s", exc)

    # ============================================================
    # 系统提示词

    # ============================================================

    def _apply_prompt_roots(self, builder: "SystemPrompt") -> None:
        """向 SystemPrompt 构建器注入显式运行根（Phase 0 无 CWD 依赖）。

        优先使用运行时上下文/显式根；缺失时回退到 config 推导根。
        """
        if getattr(self, "_runtime_context", None) is not None:
            ctx = self._runtime_context
            builder.set_runtime_context(
                framework_root=getattr(ctx, "framework_root", None) or None,
                project_root=getattr(ctx, "project_root", None) or None,
                working_directory=getattr(ctx, "working_directory", None) or None,
            )
            return
        if self._framework_root:
            builder.set_framework_prompt_root(self._framework_root)
        if self._project_root:
            builder.set_project_prompt_root(self._project_root)
        if self._working_directory:
            builder.set_workspace(self._working_directory)
        elif getattr(builder, "_workspace", None) is None:
            try:
                cfg = getattr(self, "_config", None) or load_config()
                builder.set_workspace(str(_configured_workspace_path(cfg)))
            except Exception:
                pass

    def _full_tool_tables_enabled(self) -> bool:
        """A2：读取 system_prompt.full_tool_tables（默认 False=紧凑索引）。"""
        try:
            cfg = getattr(self, "_config", None) or load_config()
            return bool((cfg.get("system_prompt") or {}).get("full_tool_tables", False))
        except Exception:
            return False

    def _build_system_prompt(self) -> str:
        """使用 SystemPrompt 构建器生成带静态区和动态区的提示词"""


        tool_descs = self.tool_registry.get_tool_descriptions(
            compact=not self._full_tool_tables_enabled())
        skill_descs = self.skill_manager.get_skill_descriptions() if self.skill_manager else ""
        mcp_descs = self.tool_registry.get_mcp_tool_descriptions()
        builder = self.system_prompt_builder or SystemPrompt(name=self.name)
        self._apply_prompt_roots(builder)
        return builder.build(tool_descs=tool_descs, skill_descs=skill_descs, mcp_descs=mcp_descs)

    # ============================================================
    # 技能系统
    # ============================================================

    def _apply_allowed_skills(self, allowed_names: list) -> None:
        """Phase 4：从注册表中移除未选中的 Skill 工具（保留 create_skill 内部能力）。"""
        if not allowed_names:
            allowed_names = []
        keep = set(allowed_names)
        for name in list(self.tool_registry._skill_tool_names):
            if name not in keep:
                self.tool_registry.remove_tool(name)
        self._rebuild_system_prompt()

    def _register_skill_tools(self):
        """将 SkillManager 中的技能注册为 SkillTool"""
        if not self.skill_manager:
            return
        for skill in self.skill_manager.get_all_skills():
            tool = SkillTool(skill)
            try:
                self.tool_registry.register_skill_tool(tool)
                # 技能执行只返回指令文本（无直接副作用）；技能名为动态值，
                # 不在 PolicyEngine._PURE_TOOLS 名单内，按未分类工具走四档
                # 兜底（readonly 拒 / ask 确认 / allow·unreviewed 放行）。
                # 此前这里的 set_rule(ALLOW) 是无执行方读取的死代码，移除后
                # 行为不变；技能指挥的后续工具调用仍各自被 SecurityGate 拦截。
            except (ValueError, TypeError) as e:
                logger.warning(f"注册技能工具失败 '{tool.name}': {e}")

    def _register_create_skill_tool(self):
        """注册 create_skill 工具（LLM 运行时创建技能）"""
        if not self.skill_manager:
            return
        tool = CreateSkillTool(self.skill_manager)
        tool.set_tool_registry(self.tool_registry)
        tool.set_agent_ref(self)
        try:
            self.tool_registry.register_tool(tool)
        except (ValueError, TypeError) as e:
            logger.warning(f"注册 create_skill 失败: {e}")

    def _rebuild_system_prompt(self):
        """重新构建 system prompt（技能刷新/MCP reload/profile 更新后调用）。

        A1 前缀稳定化：最新模板只写入 self._pending_system_template，不再立即
        改写 messages[0]（避免打断 provider prompt 缓存）；AgentLoop 在下一轮
        begin_run 后原子替换 messages[0] 并清除。
        """
        self.system_prompt = self._build_system_prompt()
        self._pending_system_template = self.system_prompt

    # ============================================================
    # MCP 初始化

    # ============================================================

    def _init_mcp_if_needed(self):
        """
        延迟初始化 MCP 连接

        在首次 run() 时执行，依次完成：
        1. 创建 MCPClientManager
        2. 添加所有服务器配置
        3. 初始化连接（带重试）
        4. 发现工具并注册到 ToolRegistry
        5. 重建 System Prompt 以包含 MCP 工具描述
        """
        if not getattr(self, "_mcp_pending_init", None):
            return

        from core.mcp_client import MCPClientManager, run_in_mcp_loop
        from tools.mcp_tools import MCPTool

        configs = self._mcp_pending_init
        self._mcp_pending_init = None  # 避免重复初始化

        # 在 MCP 专用事件循环中执行异步初始化（长生命周期，跨多次调用复用）；
        # P3：传入有限超时（默认 60s），避免初始化挂起拖死首轮 run。
        run_in_mcp_loop(
            self._async_init_mcp(configs, MCPClientManager, MCPTool), timeout=60)

    async def _async_init_mcp(self, configs, MCPClientManager_cls, MCPTool_cls):
        """异步初始化 MCP 连接（由 _init_mcp_if_needed 调用）"""
        self.mcp_manager = MCPClientManager_cls()

        # 服务器 trust 标志映射：{name: trust}（来自 config.json 的 mcp.servers）
        cfg_trust = {
            cfg.get("name"): bool(cfg.get("trust", False))
            for cfg in configs
            if cfg.get("name")
        }

        # 1. 添加所有服务器配置（跳过 enabled=false 的，兼容 "false" 等字符串写法）
        for cfg in configs:
            if not is_enabled(cfg.get("enabled")):
                logger.info(f"跳过已禁用的 MCP 服务器: {cfg.get('name')}")
                continue
            try:
                self.mcp_manager.add_server(cfg)
            except Exception as e:
                logger.error(
                    f"添加 MCP 服务器 '{cfg.get('name')}' 失败: {e}"
                )

        # 2. 初始化所有连接
        await self.mcp_manager.initialize_all()

        # 3. 发现工具并注册到注册表
        all_tools = await self.mcp_manager.discover_all_tools()
        for server_name, tools in all_tools.items():
            conn = self.mcp_manager.get_connection(server_name)
            if not conn or not tools:
                continue
            for tool_desc in tools:
                # 添加服务器前缀以避免不同服务器的工具重名
                prefixed_desc = dict(tool_desc)
                prefixed_desc["name"] = f"{server_name}/{tool_desc['name']}"
                # trust 标志：受信任服务器的工具直接放行，否则每次确认
                trust = bool(cfg_trust.get(server_name, False))
                mcp_tool = MCPTool_cls(connection=conn, tool_desc=prefixed_desc, trust=trust)
                try:
                    self.tool_registry.register_mcp_tool(mcp_tool)
                    logger.info(f"注册 MCP 工具: {mcp_tool.name}")
                except ValueError as e:
                    logger.warning(
                        f"注册 MCP 工具失败 '{mcp_tool.name}': {e}"
                    )

        # 4. 重建 System Prompt 以包含 MCP 工具描述
        # A1：写入挂起模板（_rebuild_system_prompt），由 AgentLoop 在 turn
        # 边界原子应用，不立即改写 messages[0]。
        if self._mcp_tool_names or self.tool_registry._mcp_tool_names:
            self._rebuild_system_prompt()
            logger.info(
                f"MCP 初始化完成: {len(self._mcp_tool_names)} 个工具已注册"
            )

    # ============================================================
    # MCP 运行期增删（WebUI /mcp reload·reconnect，executor 线程内调用）
    # ============================================================

    def reload_mcp(self, configs: list):
        """按 config 全量 diff 增删 MCP 服务器并重建工具与提示词。

        config.json 是唯一事实源（避免双账本分叉）。须由 executor 线程调用。
        """
        if not self.mcp_manager:
            # 尚未初始化（首跑未完成）→ 只更新待初始化配置，首跑自然生效
            self._mcp_pending_init = [
                c for c in (configs or []) if is_enabled(c.get("enabled"))]
            logger.info("MCP 未初始化，reload 已写入待初始化配置")
            return
        from core.mcp_client import run_in_mcp_loop
        # P3：reload 同样带有限超时，避免挂在 MCP 事件循环上。
        run_in_mcp_loop(self._async_reload_mcp(configs or []), timeout=60)

    async def _async_reload_mcp(self, configs: list):
        """全量 diff 增删 MCP（MCP 事件循环内执行）"""
        from tools.mcp_tools import MCPTool

        wanted = {
            c.get("name"): c for c in configs
            if c.get("name") and is_enabled(c.get("enabled"))
        }
        cfg_trust = {n: bool(c.get("trust", False)) for n, c in wanted.items()}
        current = set(self.mcp_manager.list_connections())

        # ---- 删除项 ----
        for name in current - set(wanted):
            try:
                self.mcp_manager.remove_server(name)
            except Exception as e:
                logger.warning(f"移除 MCP 服务器 '{name}' 失败: {e}")
            # 注销该服务器的全部工具（按 "{server}/" 前缀匹配）
            prefix = f"{name}/"
            for tool_name in list(self.tool_registry._mcp_tool_names):
                if tool_name.startswith(prefix):
                    self.tool_registry.remove_tool(tool_name)
                    self.tool_registry._mcp_tool_names.discard(tool_name)
            logger.info(f"MCP 服务器已移除: {name}")

        # ---- 新增项 ----
        for name in set(wanted) - current:
            try:
                self.mcp_manager.add_server(wanted[name])
            except Exception as e:
                logger.error(f"添加 MCP 服务器 '{name}' 失败: {e}")

        if set(wanted) - current:
            # initialize_all 只碰未初始化的连接
            await self.mcp_manager.initialize_all()

        # ---- 工具发现与注册（仅新增的服务器）----
        all_tools = await self.mcp_manager.discover_all_tools()
        for server_name, tools in all_tools.items():
            if server_name not in (set(wanted) - current):
                continue
            conn = self.mcp_manager.get_connection(server_name)
            if not conn or not tools:
                continue
            for tool_desc in tools:
                prefixed_desc = dict(tool_desc)
                prefixed_desc["name"] = f"{server_name}/{tool_desc['name']}"
                trust = bool(cfg_trust.get(server_name, False))
                mcp_tool = MCPTool(connection=conn, tool_desc=prefixed_desc, trust=trust)
                try:
                    self.tool_registry.register_mcp_tool(mcp_tool)
                    logger.info(f"注册 MCP 工具: {mcp_tool.name}")
                except ValueError as e:
                    logger.warning(f"注册 MCP 工具失败 '{mcp_tool.name}': {e}")

        # ---- 收尾：重建提示词 ----
        self._rebuild_system_prompt()
        logger.info(
            f"MCP reload 完成: 目标 {len(wanted)} 个服务器, "
            f"已注册 MCP 工具 {len(self.tool_registry._mcp_tool_names)} 个")

    def reconnect_mcp(self, name: str):
        """重连单个 MCP 服务器 = remove + add + init + 重注册（无 ping 能力下的重连定义）"""
        if not self.mcp_manager:
            logger.warning("MCP 未初始化，reconnect 忽略: %s", name)
            return
        from core.config_loader import load_config
        configs = load_config().get("mcp", {}).get("servers", [])
        target = next((c for c in configs if c.get("name") == name), None)
        if target is None:
            logger.warning("reconnect: config 中无此 MCP 服务器: %s", name)
            return

        from core.mcp_client import run_in_mcp_loop

        async def _do_reconnect():
            from tools.mcp_tools import MCPTool
            try:
                self.mcp_manager.remove_server(name)
            except Exception as e:
                logger.debug(f"reconnect 移除旧连接失败（可忽略）: {e}")
            # 注销旧工具
            prefix = f"{name}/"
            for tool_name in list(self.tool_registry._mcp_tool_names):
                if tool_name.startswith(prefix):
                    self.tool_registry.remove_tool(tool_name)
                    self.tool_registry._mcp_tool_names.discard(tool_name)
            self.mcp_manager.add_server(target)
            await self.mcp_manager.initialize_all()
            tools = (await self.mcp_manager.discover_all_tools()).get(name, [])
            conn = self.mcp_manager.get_connection(name)
            trust = bool(target.get("trust", False))
            if conn:
                for tool_desc in tools:
                    prefixed_desc = dict(tool_desc)
                    prefixed_desc["name"] = f"{name}/{tool_desc['name']}"
                    mcp_tool = MCPTool(connection=conn, tool_desc=prefixed_desc, trust=trust)
                    try:
                        self.tool_registry.register_mcp_tool(mcp_tool)
                    except ValueError as e:
                        logger.warning(f"reconnect 注册工具失败 '{mcp_tool.name}': {e}")
            self._rebuild_system_prompt()

        # P3：重连带有限超时，避免挂在 MCP 事件循环上。
        run_in_mcp_loop(_do_reconnect(), timeout=60)
        logger.info("MCP 重连完成: %s", name)

    # ============================================================
    # 跨会话记忆自动保存

    # ============================================================

    def _save_memory(self, user_input: str):
        """

        将本轮对话归档到跨会话记忆（memory/daily/）
        在每轮对话结束时自动调用，与统一会话持久化（gateway 侧 SQLite）并行。
        """

        if not self.memory:
            return
        # B2 记忆降噪：Plan/Goal/scheduler/subagent 是执行 worker 轮，不是
        # 对话（与 runner 的 background_transcript 同一取向）——整轮不入记忆。
        if (getattr(self, "_runtime_task_source", "") or ""
                in {"plan", "goal", "scheduler", "subagent"}):
            return
        try:
            # 只保存不含 system prompt 的消息
            non_system = [m for m in self.messages if m.get("role") != "system"]
            # 排除刚刚追加的 system（resume 场景下 system 在 index 0）
            # 如果 /clear 过，追加计数到 session_id 使记忆走新条目
            clear_count = getattr(self, '_memory_clear_count', 0)
            mem_session_id = f"{self.store.session_id}#{clear_count}" if clear_count else self.store.session_id
            self.memory.save_conversation(
                user_call=user_input,
                messages=non_system,
                session_id=mem_session_id,
            )
        except Exception as e:
            logger.error(f"记忆保存失败: {e}")

    # ============================================================
    # 执行工具

    # ============================================================

    def _gate_check(self, tool_name: str, params: dict) -> tuple:
        """中央安全闸门检查（L1 权限 + L2 沙箱）。

        替代原先分散的 self.permission.check()——对每次工具调用（内置/
        skill/MCP）统一跑 L1+L2，覆盖不再依赖工具自觉接入沙箱。
        返回 (level, reason)，level ∈ {ALLOW, ASK, DENY}。
        """
        blocked_tools = getattr(self, "_runtime_tool_blocklist", ())
        if tool_name in blocked_tools:
            return DENY, "tool is disabled for this task execution mode"
        tool = self.tool_registry.get_tool(tool_name)
        if tool is None:
            # 模糊匹配：LLM 偶尔漏写 MCP 前缀的 /工具名，如只写 web-search 而非 web-search/search
            prefix = tool_name + "/"
            candidates = [
                n for n in self.tool_registry.list_tool_names()
                if n.startswith(prefix)
            ]
            if candidates:
                hint = "，".join(sorted(candidates)[:5])
                msg = (
                    f"未找到工具 '{tool_name}'。"
                    f"你是否想用: {hint}？"
                )
            else:
                msg = f"未知工具 '{tool_name}'"
            return DENY, msg
        return self._gate.check(tool, params, tool_name)

    def _emit_event(self, event_type: str, **payload) -> None:
        """Emit one ordered, typed runtime event to every presentation layer.

        P2：并行工具段（ThreadPoolExecutor 内 _execute_native_tool_call 会经
        ToolRuntime 触发 tool_execution_start/end）与主循环线程同时调用本方法，
        seq 自增与 sink 调度必须互斥，否则事件序号乱序/重复、sink 并发写入。
        """
        sink = getattr(self, "_event_sink", None)
        if sink:
            lock = getattr(self, "_event_lock", None)
            if lock is not None:
                with lock:
                    self._emit_event_locked(event_type, payload, sink)
            else:
                self._emit_event_locked(event_type, payload, sink)

    def _emit_event_locked(self, event_type: str, payload: dict, sink) -> None:
        """在持有 _event_lock 时执行 seq 自增与 sink 投递（单次 try/except）。"""
        try:
            # `or 0` 兜底：run 收尾把 _event_seq 置 None 后，跨轮次的零星事件
            # 仍能安全自增，而不是 None + 1 报错被静默丢弃。
            self._event_seq = (getattr(self, "_event_seq", 0) or 0) + 1
            payload.setdefault("runtime_source", getattr(self, "_runtime_task_source", ""))
            payload.setdefault("plan_id", getattr(self, "_runtime_plan_id", ""))
            payload.setdefault("plan_task_id", getattr(self, "_runtime_plan_task_id", ""))
            payload.setdefault("goal_id", getattr(self, "_runtime_goal_id", ""))
            payload.setdefault("sequence", self._event_seq)
            sink({"type": event_type, "data": payload})
        except Exception:
            logger.debug("运行事件投递失败", exc_info=True)


    @staticmethod
    def _runtime_failure_text(exc: Exception) -> str:
        """Return a concise, stable error without pretending the step succeeded."""
        text = str(exc)
        lowered = text.lower()
        if "insufficient_quota" in lowered or "allocated quota exceeded" in lowered:
            return "❌ LLM 调用失败：模型配额不足（insufficient_quota），本步骤未完成。"
        if "429" in lowered or "rate limit" in lowered:
            return "❌ LLM 调用失败：请求频率或配额受限，本步骤未完成。"
        return f"❌ LLM 调用失败：{type(exc).__name__}: {text}"
    # ============================================================
    # 核心运行方法

    # ============================================================
    # ============================================================
    # 对话历史管理

    # ============================================================

    def run(self, user_input: str, verbose: bool = True,
            images: list | None = None, event_sink=None) -> str:
        """Compatibility entry point delegated to the single AgentLoop."""
        from core.agent_runtime.loop import AgentLoop
        return AgentLoop(self).run(user_input, images=images, event_sink=event_sink).visible_text

    def _run_native_loop(self, user_input: str, verbose: bool = True,
            images: list | None = None, event_sink=None) -> str:
        """Run the native, typed tool-call loop.

        Tools are never parsed from model text.  The provider yields native
        function calls; this loop validates, authorizes, executes and records
        them as separate assistant/tool messages, mirroring Pi's agent loop.
        """
        # AgentLoop owns control/persistence/providers. This method only hosts
        # transitional transcript mutation while remaining loop logic migrates.
        self._event_sink = event_sink
        self._run_id = uuid.uuid4().hex
        self._event_seq = 0
        if not hasattr(self, "_tool_runtime"):
            # 兼容兜底分支：create_agent 已预建 _tool_runtime（见文件尾部），
            # 正式路径不会进入；直接构造 Agent 的测试/脚本仍依赖此惰性初始化，
            # 因此保留分支而非删除。P3-2：原实现引用裸名 _config（应为
            # self._config），NameError 被下方 except 吞掉，导致
            # max_parallel_tools 配置被静默忽略——改为显式 getattr 取值。
            try:
                _cfg = getattr(self, "_config", None) or {}
                _mpt = max(1, int(_cfg.get("agent_runtime", {}).get("max_parallel_tools", 4)))
            except Exception:
                _mpt = 4
            self._tool_runtime = ToolRuntime(max_result_chars=10000,
                                             executor_max_workers=_mpt)
        if not hasattr(self, "hooks"):
            self.hooks = HookManager(enabled=False)
        self._init_mcp_if_needed()
        # P2-9：若上一轮运行期挂起了模型切换，本轮开始时原子应用，避免与本轮共享状态并发。
        self._consume_pending_llm()
        if not self.messages:
            self.messages.append({"role": "system", "content": self.system_prompt})
        self._light_compress()
        self._check_context(verbose=verbose)
        hr = self.hooks.run_user_prompt(user_input)
        if hr.decision == Decision.BLOCK:
            # P2：BLOCK 路径完整收口——先发唯一 agent_end 并复位 _last_run_reason
            # 为 blocked，避免外层 AgentLoop 因 reason 缺失把拦截轮误报为
            # completed/error；随后才返回拦截文案。
            self._active_loop.finish("blocked")
            return f"⛔ 输入被 hook 拦截: {hr.reason}"
        if hr.decision == Decision.MODIFY and hr.data:
            user_input = hr.data.get("prompt", user_input)
        user_message = {"role": "user", "content": user_input}
        if images:
            blocks = ([{"type": "text", "text": user_input}] if user_input else []) + list(images)
            user_message["content"] = blocks
        # B1 记忆降噪：runtime 注入的任务协议文本（Plan step / Goal 轮 /
        # scheduler 触发 / subagent 委派）打 internal 标记——记忆序列化会
        # 跳过 internal 消息，不让执行协议污染 BM25 语料（dispatcher 注入
        # 的运行状态提示同理，见 _runtime_status_note 前置拼接路径）。
        if (getattr(self, "_runtime_task_source", "") or ""
                in {"plan", "goal", "scheduler", "subagent"}):
            user_message["internal"] = True
        self.messages.append(user_message)
        self._truncate_history()
        self._emit_event("agent_start", message_id="user")
        self._emit_event("message_start", role="user", content=user_input)
        self._emit_event("message_end", role="user")
        provider_tools, name_map = self.tool_registry.get_provider_tools()

        # 会话不限制最大步骤数：循环按需推进，直到模型返回终答、被停止
        # （外部 CancellationToken）或被取消；真正无限循环由 RunControl 的
        # max_tool_calls 与运行超时兜底。不再用 max_steps 硬性截断。
        for step in itertools.count(1):
            self._active_loop.next_turn(step)
            if self._consume_stop():
                self._active_loop.finish("stopped")
                # 持久化退役：文件转录停写后无落盘动作（gateway 以 SQLite
                # 统一会话为权威；独立 Agent 明确不持久化，见 create_agent 文档）
                return "⏹️ 已停止"
            self._emit_event("turn_start", step=step, turn_id=getattr(self, "_turn_id", ""))
            # Full compression is a pre-provider check. Never invoke the
            # summary LLM immediately after a tool batch has been appended.
            self._check_context(verbose=verbose)
            self._truncate_history()
            assistant_id = uuid.uuid4().hex
            self._emit_event("message_start", message_id=assistant_id, role="assistant")
            try:
                llm_messages = AgentContext(self.messages).llm_messages()
                response = self._provider_turn.complete(llm_messages, provider_tools, assistant_id)
            except Exception as exc:
                logger.error("原生工具调用失败: %s", exc, exc_info=True)
                self._emit_event("message_end", message_id=assistant_id, status="error")
                # 错误分支收口（P1）：先发唯一 agent_end 再返回错误文本；
                # 不会因收尾异常跳过 finish 或丢失返回值。（持久化已退役：
                # 历史上的"错误分支落盘防重启丢输入"随文件转录停写一并移除。）
                self._active_loop.finish("error")
                return self._runtime_failure_text(exc)
            self.store.set_anchor(self.llm.last_usage)
            # P2：累计 token 预算（若有 max_total_tokens 配置）——每轮 provider
            # 调用后累加 last_usage，超限由 RunControl.checkpoint 抛 BudgetExceeded。
            try:
                self._run_control.add_tokens(self.llm.last_usage)
            except Exception as exc:
                logger.debug("累计 token 预算失败（可忽略）: %s", exc)

            if not response.tool_calls:
                answer = normalize_model_text(response.text) or "（模型未返回可见文本）"
                self.messages.append({"role": "assistant", "content": answer, "kind": "final"})
                self._emit_event("message_end", message_id=assistant_id, role="assistant",
                                 content=answer, finish_reason=response.finish_reason)
                self._emit_event("turn_end", step=step, tool_calls=0)
                # 成功收尾保护（P1）：finish("completed") 提前到压缩/落盘之前，
                # 确保成功轮次只发一次 agent_end；压缩与持久化包 try/except，
                # 后置步骤异常不得吞掉终答——本轮必返回 answer。
                self._active_loop.finish("completed")
                try:
                    # 最终回复轮次也要轻压缩：这轮工具结果带完整内容若直接持久化，
                    # 会浪费体积并抬高下次 run 首步 token 峰值。
                    self._light_compress()
                except (TaskCancelled, BudgetExceeded):
                    raise  # 取消/预算超限不能吞，交由 AgentLoop 收口
                except Exception as exc:
                    logger.error("最终轮轻量压缩失败: %s", exc, exc_info=True)
                self._save_memory(user_input)
                return answer

            batch = ToolBatchExecutor(self)
            native_calls = batch.prepare(response.tool_calls, name_map)
            self.messages.append({
                "role": "assistant", "content": response.text or None, "kind": "tool_calls",
                "tool_calls": [{"id": call.call_id, "type": "function", "function": {
                    "name": sanitize_message_name(call.provider_name),
                    "arguments": call.raw_arguments or "{}"}}
                    for call in native_calls],
            })
            self._emit_event("message_end", message_id=assistant_id, role="assistant",
                             finish_reason="tool_calls")

            result_count = 0
            for call, observation, is_error in batch.execute(native_calls):
                self.messages.append({"role": "tool", "tool_call_id": call.call_id,
                                      "name": sanitize_message_name(call.provider_name),
                                      "content": observation,
                                      "kind": "tool_result", "is_error": is_error})
                self._emit_event("message_start", message_id=f"result_{call.call_id}", role="tool",
                                 tool_call_id=call.call_id, tool=call.tool_name or call.provider_name)
                self._emit_event("message_end", message_id=f"result_{call.call_id}", role="tool",
                                 tool_call_id=call.call_id, tool=call.tool_name or call.provider_name,
                                 content=observation, is_error=is_error)
                result_count += 1
            self._run_control.add_tool_calls(result_count)
            # P1-5：工具边界取消检查点。若外部 CancellationToken 已取消则抛
            # TaskCancelled，交由 AgentLoop.run 收口为 CANCELLED，而非继续下一轮。
            self._run_control.checkpoint()
            self._emit_event("turn_end", step=step, tool_calls=result_count)
            self._light_compress()

    def _forward_provider_event(self, assistant_id: str, event: dict) -> None:
        """Project provider-native events into the stable Agent event schema."""
        event_type = event.get("type")
        if event_type == "text_delta":
            self._emit_event("text_delta", message_id=assistant_id, text=normalize_model_text(event.get("text", "")))
        elif event_type in {"reasoning_delta", "reasoning", "thinking", "thought"}:
            text = event.get("text") or event.get("delta") or event.get("reasoning") or event.get("thinking") or event.get("thought") or ""
            if text:
                self._emit_event("reasoning_delta", message_id=assistant_id, turn_id=getattr(self, "_turn_id", ""), text=str(text))
        elif event_type == "tool_call_start":
            self._emit_event("tool_call_start", message_id=assistant_id,
                             tool_call_id=event.get("call_id", ""), tool=event.get("name", ""),
                             order=event.get("order", 0))
        elif event_type == "tool_call_delta":
            self._emit_event("tool_call_delta", message_id=assistant_id,
                             tool_call_id=event.get("call_id", ""), tool=event.get("name", ""),
                             arguments_delta=event.get("arguments_delta", ""), order=event.get("order", 0))
        elif event_type == "tool_call_end":
            self._emit_event("tool_call_end", message_id=assistant_id,
                             tool_call_id=event.get("call_id", ""), tool=event.get("name", ""),
                             arguments=event.get("arguments", {}), order=event.get("order", 0))

    def _execute_native_tool_call(self, call_id, provider_name, tool_name, arguments, raw_arguments):
        """Validate, authorize, hook and execute one native tool call."""
        return self._tool_runtime.execute_native_call(
            self, call_id, provider_name, tool_name, arguments, raw_arguments)

    def generate_plan(self, user_input: str) -> dict:
        """Generate a typed plan without provider-forced tool_choice.

        Thinking endpoints commonly reject named function tool_choice. Planning is
        a normal, read-only model request; the runtime validates returned JSON.
        """
        instruction = (
            "你现在只负责为下面任务生成执行方案。不要执行任何操作。"
            "仅返回 JSON 对象，格式为 {\"steps\":[{\"description\":\"步骤\"}]}。"
            "步骤必须有序、具体、可验证，不要返回 Markdown 或额外文字。"
        )
        response = self.llm.complete(
            [{"role": "system", "content": self.system_prompt + "\n\n" + instruction},
             {"role": "user", "content": user_input}], temperature=0)
        raw_text = (getattr(response, "text", "") or "").strip()
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.I | re.S).strip()
        parsed = None
        try:
            parsed = json.loads(raw_text)
        except (TypeError, json.JSONDecodeError):
            # P3：回退改为整体匹配第一个 JSON 对象（greedy "{" 到最后一个 "}"，
            # DOTALL），容忍模型在代码块外夹带文字/嵌套花括号；解析仍容错。
            match = re.search(r"\{.*\}", raw_text, flags=re.S)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                except (TypeError, json.JSONDecodeError):
                    parsed = None
        steps = parsed.get("steps") if isinstance(parsed, dict) else None
        if isinstance(steps, list):
            valid = [{"description": str(item.get("description", "")).strip()}
                     for item in steps if isinstance(item, dict) and str(item.get("description", "")).strip()]
            if valid:
                return {"steps": valid}
        raise ValueError("方案生成失败：模型未返回有效 JSON steps")

# ============================================================
# MCP 配置加载

# ============================================================


def _load_mcp_config(mcp_servers: list = None) -> list:
    """
    自动加载 MCP 配置，优先级：
    1. 显式传入的 mcp_servers 参数
    2. config.json → mcp.servers

    参数:
        mcp_servers: 代码传入的配置（最高优先级）
    返回:
        MCP 服务器配置列表，无配置时返回 None
    """
    # 优先级 1：显式传入
    if mcp_servers is not None:
        return mcp_servers

    # 优先级 2：统一配置 config.json
    try:
        from core.config_loader import load_config as _load_cfg
        cfg = _load_cfg()
        servers = cfg.get("mcp", {}).get("servers", [])
        if servers:
            logger.info(f"从 config.json 加载了 {len(servers)} 个 MCP 服务器配置")
            return servers
    except Exception:
        pass

    return None


# ============================================================
# 快速启动

# ============================================================

# L6#9-part：进程内一次性日志初始化守卫。debug.setup_logging 现已自带
# once-guard（幂等）；此处双检锁作为纵深防御，确保 WebUI 每会话懒创建
# Agent 反复走 create_agent 时也绝无 handler 丢失/重复输出竞态。
_LOGGING_SETUP_LOCK = threading.Lock()
_LOGGING_SETUP_DONE = False


def _init_logging_once(debug: bool = False) -> None:
    """进程内一次性初始化 jk_agent 日志（L6#9-part）。

    create_agent 不再清空重建 jk_agent handlers：首次调用执行
    setup_logging（含 debug 档 set_debug），后续重复调用直接返回。
    debug.py 内部如需微调属他组范围，这里仅在 agent.py 侧加 once-guard。
    """
    global _LOGGING_SETUP_DONE
    if _LOGGING_SETUP_DONE:
        return
    with _LOGGING_SETUP_LOCK:
        if _LOGGING_SETUP_DONE:
            return
        setup_logging(debug=debug)
        if debug:
            set_debug(True)
        _LOGGING_SETUP_DONE = True


def create_agent(
    name: str = "JKagent",
    model: str = None,
    api_key: str = None,
    base_url: str = None,
    provider: str = None,
    max_steps: int = 100,
    max_history_tokens: int = 0,
    debug: bool = False,
    permission: bool = True,
    memory: bool = True,
    skills: bool = True,
    sandbox: bool = True,       # 沙箱执行器
    hooks: bool = True,         # Hook 模块（事件驱动自定义扩展）
    mcp_servers: list = None,    # MCP 服务器配置列表（可选）
    non_interactive: bool = False,  # 非交互模式（gateway 等无 TTY 场景）
    quiet: bool = False,            # 静默模式（LLM 不流式打印到 stdout）
    framework_root: str = None,     # 框架根（默认 prompt/ 兜底）
    agent_data_root: str = None,    # Agent 数据根（runtime.db / Artifact / 自身状态）
    project_root: str = None,       # 用户项目访问边界
    working_directory: str = None,  # 默认执行目录（须在 project_root / extra roots 内）
    extra_workspace_roots: list = None,  # 显式额外白名单根（None=继承 config）
    runtime_context: object = None,      # WorkspaceRuntimeContext（最高优先级）
    profile_prompt: str = None,          # Agent Profile System Prompt（Phase 2/4）
    allowed_tools: list = None,          # 工具能力子集（None=全部；系统保留工具恒在）
    allowed_skills: list = None,         # Skill 能力子集（None=全部）
    reasoning_level: str = None,         # Per-session reasoning selection; None inherits model/provider config.
) -> Agent:
    """
    一行创建 Agent（含 read/write/edit/grep/glob/bash + search/web_fetch）

    参数:
        name: Agent 名称
        model: 模型名称
        api_key: API 密钥
        base_url: API 地址
        provider: 服务商（ollama/openai/lmstudio）
        max_steps: 最大 AgentLoop 步数
        max_history_tokens: 上下文预算阈值
        debug: 调试模式
        permission: 权限管理
        memory: 跨会话记忆
        skills: 学习型技能系统
        sandbox: 沙箱执行器（L2 内容拦截+资源隔离）
        hooks: Hook 模块（事件驱动自定义扩展，配置于 config.json 的 hooks section）
        mcp_servers: MCP 服务器配置列表，每项含 name/transport/command 等
    """

    # L6#9-part：进程内一次性初始化（重复 create_agent 不再清空重建 handlers）。
    _init_logging_once(debug)
    from core.config_loader import load_config as _lc
    from core.config_loader import _find_project_root
    # Take one config snapshot before changing anything.  The snapshot is
    # retained on the Agent below for runtime feature flags and tool limits.
    _config = _lc()

    # ---- Phase 0：显式三目录分离（不再 os.chdir）----
    # 优先级：runtime_context > 显式 root 参数 > config.json 默认 > 框架兼容默认
    if runtime_context is not None:
        _framework_root = str(getattr(runtime_context, "framework_root", "") or _find_project_root())
        _agent_data_root = str(getattr(runtime_context, "agent_data_root", "") or _configured_workspace_path(_config))
        _project_root = str(getattr(runtime_context, "project_root", "") or _find_project_root())
        _working_directory = str(getattr(runtime_context, "working_directory", "") or _agent_data_root)
        _extra_workspace_roots = [
            str(p) for p in (getattr(runtime_context, "extra_workspace_roots", ()) or ())
        ]
        _permission_mode = str(getattr(runtime_context, "permission_mode", "") or "ask")
    else:
        _framework_root = framework_root or str(_find_project_root())
        _agent_data_root = agent_data_root or str(_configured_workspace_path(_config))
        _project_root = project_root or _framework_root
        _working_directory = working_directory or _agent_data_root
        _extra_workspace_roots = list(extra_workspace_roots) if extra_workspace_roots is not None else None
        _permission_mode = ""

    _framework_root = str(Path(_framework_root).expanduser().resolve())
    _agent_data_root = str(Path(_agent_data_root).expanduser().resolve())
    _project_root = str(Path(_project_root).expanduser().resolve())
    _working_directory = str(Path(_working_directory).expanduser().resolve())
    os.makedirs(_agent_data_root, exist_ok=True)

    llm = JKAgentLLM(model=model, api_key=api_key, base_url=base_url, provider=provider,
                          reasoning_level=reasoning_level)
    # 记忆系统（跨会话）——工作区模式数据目录随 agent_data_root，避免多工作区串味；
    # 普通模式保持框架 memory/ 目录，行为不变。
    if memory:
        workspace_id = ""
        if runtime_context is not None:
            workspace_id = getattr(runtime_context, "workspace_id", "") or ""
        if workspace_id:
            memory_manager = MemoryManager(memory_dir=str(workspace_memory_dir(workspace_id)))
        elif agent_data_root is not None:
            memory_manager = MemoryManager(memory_dir=os.path.join(_agent_data_root, "memory"))
        else:
            memory_manager = MemoryManager()
    else:
        memory_manager = None
    # 技能系统（学习型，保存在框架 SKILLS/ 目录）
    skill_manager = SkillManager() if skills else None
    if skill_manager:
        skill_manager.load_all()
    # Ensure config-backed hard L2 rules are loaded for every Agent instance,
    # including sessions that change permission mode at runtime.
    try:
        from core.sandbox.guard import apply_guard_config
        apply_guard_config(_config.get("sandbox", {}))
    except Exception:
        logger.debug("无法加载 sandbox 配置", exc_info=True)
    sandbox_executor = SandboxExecutor(
        workspace=_working_directory,
        extra_workspace_roots=_extra_workspace_roots,
    ) if sandbox else None
    # 权限管理——显式 project_root + 显式 extra roots（工作区模式空列表不回退全局）
    checker = PermissionChecker(
        workspace=_project_root,
        extra_workspaces=_extra_workspace_roots,
    ) if permission else None
    if checker:
        # Apply the selected session mode before the first tool call. In
        # unreviewed mode ordinary tools and paths are allowed, while sensitive
        # paths and high-risk commands still return ASK for explicit approval.
        checker.set_permission_mode(_permission_mode or "ask")
        if _permission_mode == "allow":
            checker.allow_workspace()
        if sandbox_executor:
            sandbox_executor.set_unreviewed_mode(_permission_mode == "unreviewed")
    # 长驻子进程管理器（依赖沙箱的 max_output/idle 配置）
    process_manager = None
    if sandbox_executor and checker:
        # 四档权限感知 cwd 边界：注入同一 checker，ask/allow/unreviewed 下
        # proc_start 界外 cwd 交还授权层裁决（确认后可达），readonly/无权限
        # 直调路径保持硬拒绝
        process_manager = ProcessManager(sandbox_executor, _working_directory,
                                         permission=checker)
    registry = ToolRegistry()
    # P0-3：读路径工作区边界——显式注入允许根（工作目录 + 项目根 + 额外工作区），
    # 未注入时工具层不拦截，这里必须传入以保证主链路边界生效。
    _boundary_roots = [_working_directory, _project_root]
    if _extra_workspace_roots:
        _boundary_roots.extend(
            str(Path(p).expanduser().resolve()) for p in _extra_workspace_roots
        )
    # P1 安全：图片 file 源白名单与工具读路径边界同源（同一 _boundary_roots）
    from core.protocols.vision import set_allowed_image_roots, set_vision_permission
    set_allowed_image_roots(_boundary_roots)
    # 四档权限感知：ask/allow/unreviewed 下图片白名单交还裁决（与工具读写
    # 边界的对齐方向一致），readonly/无权限直调路径保持白名单
    set_vision_permission(checker)
    register_all_tools(
        registry, memory_manager=memory_manager,
        sandbox=sandbox_executor, process_manager=process_manager,
        workspace_roots=_boundary_roots,
        # 四档权限感知写边界：ask/allow/unreviewed 模式下写边界交还
        # PolicyEngine 裁决（界外写按档位 ASK/放行，确认后的执行必须可达）；
        # readonly 与未注入权限的直调路径保持硬边界。工具持有同一 checker
        # 引用，/perm 切换模式即时生效。
        permission=checker,
    )
    register_web_tools(registry)
    # Phase 4：能力子集过滤（schemas + Prompt 描述 + catalog 三方一致）
    if allowed_tools is not None and hasattr(registry, "set_active_tools"):
        registry.set_active_tools(list(allowed_tools))
    # MCP 配置：代码参数 > config.json
    mcp_servers = _load_mcp_config(mcp_servers)
    # SystemPrompt 构建器：注入显式运行根（Phase 0 无 CWD 依赖）
    prompt_builder = SystemPrompt(name=name)
    prompt_builder.set_runtime_context(
        framework_root=_framework_root,
        project_root=_project_root,
        working_directory=_working_directory,
    )
    if runtime_context is not None:
        _ws_id = getattr(runtime_context, "workspace_id", "") or ""
        if _ws_id:
            prompt_builder.set_memory_context(
                memory_path=str(workspace_memory_dir(_ws_id)),
                instruction="当用户询问与当前项目相关的问题时，优先调用 memory_search 检索本工作区长期记忆，再作答。",
            )
    if profile_prompt:
        prompt_builder.set_agent_profile_prompt(profile_prompt)
    agent = Agent(
        name=name, llm=llm, tool_registry=registry,
        max_steps=max_steps,
        max_history_tokens=max_history_tokens,
        debug=debug,
        permission_checker=checker,
        memory=memory_manager,
        sandbox=sandbox_executor,
        process_manager=process_manager,
        mcp_servers=mcp_servers,
        hooks_enabled=hooks,
        non_interactive=non_interactive,
        quiet=quiet,
        system_prompt_builder=prompt_builder,
    )
    # ---- Phase 0：记录运行根到 Agent（重建/恢复时保持上下文）----
    agent._framework_root = _framework_root
    agent._agent_data_root = _agent_data_root
    agent._project_root = _project_root
    agent._working_directory = _working_directory
    agent._extra_workspace_roots = tuple(_extra_workspace_roots or ())
    agent._runtime_context = runtime_context
    # Hook: 绑定 agent 身份，触发 session_start
    agent._config = _config
    agent._tool_runtime = ToolRuntime(
        max_result_chars=_config.get("agent_runtime", {}).get("max_tool_result_chars", 10000))
    agent.hooks.bind_agent(agent.name, agent.store.session_id)
    agent.hooks.dispatch(HookEvent.SESSION_START, {})
    if skill_manager:
        agent.skill_manager = skill_manager
        agent._register_skill_tools()
        agent._register_create_skill_tool()
        # Phase 4：按快照过滤 Skill 工具
        if allowed_skills is not None:
            agent._apply_allowed_skills(list(allowed_skills))
        agent._rebuild_system_prompt()
    return agent

# Gateway/WebUI is the sole interactive surface.
#
if __name__ == "__main__":
    USAGE = (
        "Usage: python agent.py init\n\n"
        "JKagent runs exclusively through Gateway/WebUI.\n"
        "Start it with: jkagent-gateway run\n"
    )
    args = sys.argv[1:]
    if args in (["--help"], ["-h"]):
        print(USAGE)
        sys.exit(0)
    if args == ["init"]:
        from core.init_wizard import run_init_wizard
        sys.exit(run_init_wizard())
    print(USAGE, file=sys.stderr)
    sys.exit(2)
