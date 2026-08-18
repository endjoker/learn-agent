# -*- coding: utf-8 -*-
"""

Agent 主程序 —— 把 LLM + 工具串起来的"智能体指挥官"
工作模式：ReAct（Reasoning + Acting）循环
消息流结构：
  user               "帮我看看 main.py"
  ───────────────────────────────────────────────────
  assistant          ← 这是 tool_use（LLM 决定调用工具）
    "THOUGHT：需要查看文件
     ACTION：read
     INPUT：{"file_path": "test.txt"}"
  ───────────────────────────────────────────────────
  user (name=tool_result)  ← 这是 tool_result（工具返回数据）
    "【工具执行结果】
     工具: read
     返回: 文件内容..."
  ───────────────────────────────────────────────────
  assistant          ← 最终回答
    "THOUGHT：我看到了...
     FINAL_ANSWER：文件内容是..."
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

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import logging

from core import JKAgentLLM, SystemPrompt
from core.llm_client import detect_context_length
from core.compressor import Compressor
from core.task_list import TaskList
from core.debug import (
    logger, setup_logging,
    set_debug,
    log_llm_response,
    log_info,
)
from core.message_store import MessageStore, _content_to_text
from core.config_loader import is_enabled
from core.config_loader import load_config
from core.agent_protocol import parse_text_response
from core.protocols.vision import is_image_file
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
from core.runtime.tool_runtime import ToolRuntime
from core.runtime.text_normalization import normalize_model_text


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


def _configured_runtime_store_path(config: Optional[dict] = None) -> Path:
    """Resolve the durable runtime database relative to the project root."""
    if config is None:
        config = load_config()
    configured = config.get("runtime_store", {}).get("path")
    if not configured:
        return _configured_workspace_path(config) / ".agent" / "state" / "runtime.db"
    path = Path(str(configured))
    if path.is_absolute():
        return path.resolve()
    from core.config_loader import _find_project_root
    return (_find_project_root() / path).resolve()


# ============================================================
# ReAct 关键字（英文，避免中文编码兼容问题）

# ============================================================
TAG_THOUGHT = "THOUGHT"
TAG_ACTION = "ACTION"
TAG_INPUT = "INPUT"
TAG_FINAL = "FINAL_ANSWER"
# 正则匹配 THOUGHT（不区分大小写）
_THOUGHT_RE = re.compile(
    rf"(?:{TAG_THOUGHT}|思考)[：:]\s*(.*?)"
    rf"(?=\n*(?:{TAG_ACTION}|行动)[：:]|\n*(?:{TAG_FINAL}|最终回答)[：:]|$)",
    re.DOTALL | re.IGNORECASE
)


# ============================================================
# Plan 规划（CLI /plan 与 WebUI plan 预览共用）
# ============================================================
PLAN_PROMPT_TEXT = (
    "请分析以下任务需求，输出一个分步骤的执行方案。\n\n"
    "格式要求：\n"
    "PLAN：\n"
    "[1] 步骤1描述\n"
    "[2] 步骤2描述\n"
    "...（分步骤列出，5-8 步为宜）\n\n"
    "只需要输出 PLAN 部分，不要执行任何工具。"
)
_PLAN_RE = re.compile(
    r"PLAN[：:]\s*(\S[\s\S]*?)(?:\Z|(?=\n*(?:THOUGHT|ACTION|FINAL_ANSWER)))",
    re.IGNORECASE | re.DOTALL,
)


def extract_plan_text(response: str) -> Optional[str]:
    """从 LLM 响应中提取 PLAN 正文；未识别返回 None"""
    m = _PLAN_RE.search(response or "")
    return m.group(1).strip() if m else None





def _build_tool_result_content(combined: str):
    """构造 tool_result 的 content：检测 [IMAGE:path=...] 标记并转为多模态块。

    标记必须指向真实存在的图片文件才会被采纳——防止工具输出中恰好包含
    标记字面量（如 agent 读取本文件源码）时被误匹配而污染会话历史，
    也防止模型伪造标记读取任意文件。重复标记去重，保持出现顺序。
    无有效图片时原样返回字符串。
    """
    markers = re.findall(r'\[IMAGE:path=(.+?)\]', combined)
    if not markers:
        return combined
    valid_paths = []
    for p in dict.fromkeys(markers):
        if Path(p).is_file() and is_image_file(p):
            valid_paths.append(p)
        else:
            logger.debug(f"忽略无效图片标记: {p}")
    if not valid_paths:
        return combined
    content = [{"type": "text", "text": combined}]
    for p in valid_paths:
        content.append({"type": "image", "source": "file", "path": p})
    return content


def parse_react_response(response: str) -> dict:
    """

    解析 LLM 的 ReAct 回复，返回：
      { thought, actions: [{name, input}], final_answer }
    actions 可能包含多个工具调用（分批执行+合并结果）
    """

    runtime = load_config().get("agent_runtime", {})
    turn = parse_text_response(
        response or "",
        mode=runtime.get("response_protocol", "legacy"),
        legacy_execute=is_enabled(runtime.get("legacy_execute"), False),
    )
    final_answer = (turn.visible_text if not turn.tool_calls
                    and turn.protocol_mode.value in {"legacy", "json_envelope"} else None)
    thought = turn.thought
    actions = [
        {"name": call.internal_name, "input": call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False)}
        for call in turn.tool_calls
    ]
    return {
        "thought": thought,
        "actions": actions,
        "final_answer": final_answer,
        "raw_text": turn.raw_text,
        "visible_text": turn.visible_text,
        "protocol_mode": turn.protocol_mode.value,
        "parse_status": turn.parse_status.value,
        "diagnostics": [{"code": d.code, "message": d.message} for d in turn.diagnostics],
    }

# ============================================================
# Agent 核心

# ============================================================


class Agent:
    """

    AI 智能体 —— ReAct 模式
    消息流约定：
      user (提问)
      → assistant (tool_use: THOUGHT + ACTION + INPUT)
      → user name=tool_result (tool_result: 工具返回的数据)
      → assistant (继续思考或 FINAL_ANSWER)
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
        max_steps: int = 100,          # 最大 ReAct 循环步数
        max_history_tokens: int = 0,   # 上下文预算阈值（0=自动：取 context_length/2）
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
        self.auto_approve_plan = True  # 非交互模式下自动批准 PLAN
        # 权限审批回调（WebUI ask 档审批桥注入；None = 现状自动放行）
        self.ask_callback = None
        # 协作式停止标志（WebUI 暂停按钮；run/_run_task_list 每步检查）
        self._stop_requested = False
        # 上下文预算阈值：0 时取模型上下文长度的一半（留一半给输出）
        if max_history_tokens == 0:
            self.max_history_tokens = max(llm.context_length // 2, 4096)
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
        self._stop_block_count = 0  # 单次 run() 内 stop hook 连续 BLOCK 计数（上限 3）
        # 长驻进程工具权限：L1 注册为 ALLOW（危险判定全交 L2：exec:shell 查 command、proc:manage 查 input）
        if self.process_manager is not None:
            for _t in ("proc_start", "proc_send", "proc_read", "proc_list", "proc_stop"):
                self.permission.set_rule(_t, ALLOW)
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
        self.messages = self.store.messages  # 指向同一列表，现有代码兼容
        # 保存当前模型配置到 store（用于会话持久化）
        self.store.model_id = self.llm.model or ""
        self.store.model_provider = getattr(self.llm, "provider", "") or ""
        self.store.model_base_url = getattr(self.llm, "base_url", "") or ""
        self.store.model_llm_type = getattr(self.llm, "llm_type", "") or ""
        # ---- 上下文压缩器 ----
        self._compressor = Compressor(llm=self.llm)
        # ---- 任务清单（复杂任务拆分执行）----
        self._task_list: Optional[TaskList] = None

    # ============================================================
    # 对话历史管理

    # ============================================================

    def _truncate_history(self):
        """

        紧急截断：当压缩后仍超出上下文预算时，丢弃最早的消息。
        作为压缩策略的最后一道防线（正常情况下应极少触发）。
        始终保留：系统提示词 + 最近的一轮对话
        """

        if self.max_history_tokens <= 0 or len(self.messages) <= 3:
            return
        total = self.store.live_tokens()
        if total <= self.max_history_tokens:
            return
        # 从最早的消息开始丢弃，保留 system + 最近至少 2 条消息
        dropped = 0
        while len(self.messages) > 3 and total > self.max_history_tokens:
            msg = self.messages.pop(1)  # 跳过 system（index 0）
            dropped += 1
            total = self.store.live_tokens()  # 重新计算，避免估算口径不一致
        log_info(
            f"⚠️ 紧急截断: 压缩后仍超预算，丢弃 {dropped} 条消息（剩余 {total} tokens）"
        )

    # ============================================================
    # 上下文压缩

    # ============================================================

    def _light_compress(self):
        """

        轻量压缩：规则替换已消费的工具结果为短摘要。
        在每轮对话后自动执行，零 LLM 开销。
        """

        old_total = self.store.live_tokens()
        self._compressor.light_compress(self.messages)
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
            self.store.save_session()
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

        检查上下文占用率，按阈值自动压缩或提示。
        优先级：自动全量压缩 > 提示 > 无操作
        返回:
            True 表示执行了全量压缩
        """

        return self._compressor.check_and_compact(
            store=self.store,
            messages=self.messages,
            verbose=verbose,
        )

    # ============================================================
    # 任务清单管理

    # ============================================================

    def _update_task_list_in_prompt(self):
        """

        在 system prompt 末尾追加/更新/移除任务清单段。
        每次任务推进后调用，保证 LLM 感知最新进度。
        """

        if not self.messages or self.messages[0].get("role") != "system":
            return
        content = self.messages[0]["content"]
        # 移除旧的任务清单段
        content = re.sub(
            r"\n*<SYSTEM_TASK_LIST>.*?</SYSTEM_TASK_LIST>\n*",
            "", content, flags=re.DOTALL
        )
        # 追加新的
        if self._task_list:
            content += f"\n\n{self._task_list.to_prompt_section()}"
        self.messages[0]["content"] = content
        self.system_prompt = content

    def _handle_plan(
        self,
        plan_text: str,
        verbose: bool = True,
    ) -> bool:
        """

        处理 LLM 返回的 PLAN 内容：展示给用户，等待确认。
        参数:
            plan_text: LLM 输出的 PLAN 文本
            verbose:   是否输出提示信息
        返回:
            True = 用户确认，已创建 TaskList
            False = 用户拒绝
        """

        print(f"\n  📋 方案如下：")
        for line in plan_text.strip().split("\n"):
            line = line.strip()
            if line:
                print(f"    {line}")
        print(f"  ─────────────────────────────")
        print(f"  Y = 确认执行")
        print(f"  N = 拒绝方案，继续讨论")
        if self.non_interactive:
            choice = "y" if self.auto_approve_plan else "n"
        else:
            choice = input(f"  确认执行？[Y/n] ").strip().lower()
        if choice in ("", "y", "yes", "是"):
            self._task_list = TaskList.from_plan_text(plan_text)
            if not self._task_list.tasks:
                print(f"  ❌ 方案解析失败，未识别出有效任务")
                return False
            print(f"  ✅ 任务清单已创建（共 {len(self._task_list.tasks)} 步），开始执行…")
            self._update_task_list_in_prompt()
            self.store.save_session()
            return True
        else:
            print(f"  ⏭️  已取消，可继续讨论或输入 /plan 重新规划")
            return False

    def _ask_user(self, tool_name: str, params: dict) -> str:
        """
        工具权限确认统一入口。
        交互模式：显示操作详情，等待用户输入 A/Y/N/S。
        非交互模式：优先走 ask_callback（WebUI 审批桥），否则自动返回 "y"。
        """
        if self.non_interactive:
            if self.ask_callback is not None:
                return self.ask_callback(tool_name, params)
            return "y"
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
        return input(f"  请选择 [A/Y/N/S] (默认Y) ").strip().lower()

    def request_stop(self):
        """请求协作式停止：run/_run_task_list 在下一步检查点退出。"""
        self._stop_requested = True
        logger.info("收到停止请求（将在下一步检查点生效）")

    def _consume_stop(self) -> bool:
        """检查并消费停止标志。返回 True 表示应停止。"""
        if getattr(self, "_stop_requested", False):
            self._stop_requested = False
            return True
        return False

    def clear_history(self):
        """清空对话历史，但保留系统提示词"""

        self.store.clear()        # 锚点失效——清空历史后 next-think 重新校准
        # store.clear() 已重置 store._anchor_total 和 _anchor_msg_count
        # 清空任务清单
        self._task_list = None
        # 标记记忆系统：下次保存时强制新建条目，防止覆盖清空前的内容
        self._memory_clear_count = getattr(self, '_memory_clear_count', 0) + 1

    def switch_llm(self, **kwargs):
        """

        运行时切换 LLM 模型，不影响对话历史和工具
        用法:
            agent.switch_llm(provider="ollama", model="gemma4")
            agent.switch_llm(model="gpt-4", base_url="https://api.openai.com", llm_type="cloud")
        """

        self.llm = JKAgentLLM(**kwargs)
        # 根据新模型的上下文长度重新计算压缩阈值
        self.max_history_tokens = max(self.llm.context_length // 2, 4096)
        # 同步 store 的阈值和模型配置（/stats 显示用的 store.max_tokens）
        self.store.max_tokens = self.max_history_tokens
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
        # 锚点失效——不同模型的 tokenizer 不同
        self.store._anchor_total = 0
        self.store._anchor_msg_count = 0
        # 压缩器也要跟新 LLM 走（全量压缩用）
        self._compressor._llm = self.llm
        # 任务清单失效——不同模型 context 不同
        self._task_list = None
        self._update_task_list_in_prompt()
        print(f"  ✅ 已切换模型: {self.llm}")
        print(f"  📐 上下文: {self.llm.context_length:,} tokens | 压缩阈值: {self.max_history_tokens:,} tokens")

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

    def _build_system_prompt(self) -> str:
        """使用 SystemPrompt 构建器生成带静态区和动态区的提示词"""

        tool_descs = self.tool_registry.get_tool_descriptions()
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
                # 技能只返回指令文本（无副作用），免确认；
                # 其指挥的后续工具调用仍各自被 SecurityGate 拦截
                self.permission.set_rule(tool.name, ALLOW)
            except (ValueError, TypeError) as e:
                logger.warning(f"注册技能工具失败 '{tool.name}': {e}")

    def _refresh_skills(self, force: bool = False) -> bool:
        """Reload skill files and synchronize the live tool catalog/prompt."""
        manager = getattr(self, "skill_manager", None)
        if manager is None:
            return False
        try:
            skills = manager.load_all()
        except Exception as exc:
            logger.warning("skill refresh failed: %s", exc)
            return False
        signature = tuple(
            (skill.name, getattr(skill, "version", ""), skill.description,
             skill.instruction, repr(skill.parameters))
            for skill in sorted(skills, key=lambda item: item.name)
        )
        if not force and signature == getattr(self, "_skill_catalog_signature", None):
            return False
        # sync_skill_tools removes tools whose files disappeared and updates
        # descriptions for files changed while a session stays alive.
        tools = [SkillTool(skill) for skill in skills]
        if hasattr(self.tool_registry, "sync_skill_tools"):
            registered = self.tool_registry.sync_skill_tools(tools)
            for name in registered:
                self.permission.set_rule(name, ALLOW)
        else:
            self._register_skill_tools()
        self._skill_catalog_signature = signature
        self._rebuild_system_prompt()
        return True

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
        """重新构建 system prompt（技能/MCP 注册后调用）"""
        self.system_prompt = self._build_system_prompt()
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self.system_prompt

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

        # 在 MCP 专用事件循环中执行异步初始化（长生命周期，跨多次调用复用）
        run_in_mcp_loop(self._async_init_mcp(configs, MCPClientManager, MCPTool))

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
        if self._mcp_tool_names or self.tool_registry._mcp_tool_names:
            self.system_prompt = self._build_system_prompt()
            # 如果对话已开始（已有 system 消息），更新它
            if self.messages and self.messages[0].get("role") == "system":
                self.messages[0]["content"] = self.system_prompt
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
        run_in_mcp_loop(self._async_reload_mcp(configs or []))

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

        run_in_mcp_loop(_do_reconnect())
        logger.info("MCP 重连完成: %s", name)

    def add_instruction(self, instruction: str) -> None:
        """

        向 System Prompt 动态区添加额外指令。
        参数:
            instruction: 指令文本，如 "本次对话请使用英文回复"
        """

        # 获取或创建 builder
        builder = self.system_prompt_builder or SystemPrompt(name=self.name)
        builder.add_session_instruction(instruction)
        self._apply_prompt_roots(builder)
        tool_descs = self.tool_registry.get_tool_descriptions()
        new_prompt = builder.build(tool_descs=tool_descs)
        # 更新消息列表中的 system prompt（如果已在对话中）
        if self.messages:
            for msg in self.messages:
                if msg.get("role") == "system":
                    msg["content"] = new_prompt
                    break
        self.system_prompt = new_prompt
        self.system_prompt_builder = builder

    # ============================================================
    # 格式化工具结果

    # ============================================================

    @staticmethod

    def _format_tool_result(tool_name: str, tool_input: str, observation: str) -> str:
        """用固定模板包装工具返回数据"""

        MAX_LEN = 10000
        if len(observation) > MAX_LEN:
            observation = observation[:MAX_LEN] + f"\n……（截断，共 {len(observation)} 字符）"
        # 输入参数只保留摘要（太长会占上下文，LLM 已知道自己传了什么）
        INPUT_MAX = 300
        if len(tool_input) > INPUT_MAX:
            tool_input = tool_input[:INPUT_MAX] + f"……（共 {len(tool_input)} 字符）"
        return (
            f"【工具执行结果】\n"
            f"工具: {tool_name}\n"
            f"输入摘要: {tool_input}\n"
            f"返回结果:\n{observation}\n"
            f"【工具执行完毕】\n\n"
            f"（这是工具 '{tool_name}' 返回的数据，请基于此继续。\n"
            f"信息足够时输出 agent.turn.v1/final JSON 信封；需要更多时输出 "
            f"agent.turn.v1/tool_calls JSON 信封。）"
        )

    @staticmethod

    def _combine_results(results: list) -> str:
        """

        合并多个工具的执行结果为一个消息
        参数:
            results: [(tool_name, tool_input, observation, is_error), ...]
        返回:
            合并后的格式化文本
        """

        if len(results) == 1:
            # 单个工具直接走原有格式
            name, inp, obs, _ = results[0]
            return Agent._format_tool_result(name, inp, obs)
        # 统计
        ok_count = sum(1 for _, _, _, err in results if not err)
        fail_count = sum(1 for _, _, _, err in results if err)
        parts = [f"【批量工具执行结果】共 {len(results)} 个工具"]
        if ok_count:
            parts[0] += f"，✅ {ok_count} 个成功"
        if fail_count:
            parts[0] += f"，❌ {fail_count} 个失败"
        parts.append("")
        for i, (name, inp, obs, is_error) in enumerate(results, 1):
            MAX_OBS = 5000
            if len(obs) > MAX_OBS:
                obs = obs[:MAX_OBS + 100] + f"\n……（截断，共 {len(obs)} 字符）"
            mark = "❌" if is_error else "✅"
            parts.append(f"  ─── 工具 {i}/{len(results)}: {mark} {name} ───")
            parts.append(f"  输入: {inp[:200]}")
            parts.append(f"  返回:\n{obs}")
            parts.append("")
        parts.append("【批量执行完毕】\n\n"
                     "以上是所有工具的执行结果（✅ 成功 / ❌ 失败），请综合分析后继续。\n"
                     "信息足够时输出 agent.turn.v1/final JSON 信封；需要更多时输出 "
                     "agent.turn.v1/tool_calls JSON 信封。")
        return "\n".join(parts)

    # ============================================================
    # 跨会话记忆自动保存

    # ============================================================

    def _save_memory(self, user_input: str):
        """

        将本轮对话归档到跨会话记忆（memory/daily/）
        在每轮对话结束时自动调用，与 self.store.save_session() 并行。
        """

        if not self.memory:
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

    def _execute_tool(self, tool_name: str, input_str: str = None) -> str:
        """查找 → 解析参数 → 执行 → 返回"""

        tool = self.tool_registry.get_tool(tool_name)
        if not tool:
            avail = ", ".join(t.name for t in self.tool_registry.list_tools())
            return f"❌ 未知工具 '{tool_name}'。可用: {avail}"
        kwargs = {}
        if input_str:
            try:
                kwargs = json.loads(input_str)
            except json.JSONDecodeError as e:
                return f"❌ 参数不是合法 JSON: {e}\n收到: {input_str}"
        if not isinstance(kwargs, dict):
            return "❌ 参数必须是 JSON 对象"
        validation_errors = self.tool_registry.validate_arguments(tool_name, kwargs)
        if validation_errors:
            return "❌ 参数校验失败: " + "; ".join(validation_errors)
        try:
            return tool.execute(**kwargs)
        except TypeError as e:
            return (
                f"❌ 参数不匹配: {e}\n"
                f"工具 '{tool_name}' 需要的参数:\n"
                f"{json.dumps(tool.parameters, ensure_ascii=False, indent=2)}"
            )
        except Exception as e:
            logger.error(f"工具 '{tool_name}' 执行失败: {e}", exc_info=True)
            return f"❌ 工具出错: {type(e).__name__}: {e}"

    def _emit_event(self, event_type: str, **payload) -> None:
        """Emit one ordered, typed runtime event to every presentation layer."""
        sink = getattr(self, "_event_sink", None)
        if sink:
            try:
                self._event_seq = getattr(self, "_event_seq", 0) + 1
                payload.setdefault("run_id", getattr(self, "_run_id", ""))
                payload.setdefault("turn_id", getattr(self, "_turn_id", ""))
                payload.setdefault("sequence", self._event_seq)
                sink({"type": event_type, "data": payload})
            except Exception:
                logger.debug("运行事件投递失败", exc_info=True)

    # ============================================================
    # 核心运行方法

    # ============================================================
    # ============================================================
    # 对话历史管理

    # ============================================================

    def run(self, user_input: str, verbose: bool = True,
            images: list | None = None, event_sink=None) -> str:
        """Run the native, typed tool-call loop.

        Tools are never parsed from model text.  The provider yields native
        function calls; this loop validates, authorizes, executes and records
        them as separate assistant/tool messages, mirroring Pi's agent loop.
        """
        self._event_sink = event_sink
        self._run_id = uuid.uuid4().hex
        self._event_seq = 0
        if not hasattr(self, "_tool_runtime"):
            self._tool_runtime = ToolRuntime(max_result_chars=10000)
        if not hasattr(self, "hooks"):
            self.hooks = HookManager(enabled=False)
        if not hasattr(self, "_task_list"):
            self._task_list = None
        if not hasattr(self, "_stop_block_count"):
            self._stop_block_count = 0
        self._init_mcp_if_needed()
        if not self.messages:
            self.messages.append({"role": "system", "content": self.system_prompt})
        self._light_compress()
        self._check_context(verbose=verbose)
        hr = self.hooks.run_user_prompt(user_input)
        if hr.decision == Decision.BLOCK:
            return f"⛔ 输入被 hook 拦截: {hr.reason}"
        if hr.decision == Decision.MODIFY and hr.data:
            user_input = hr.data.get("prompt", user_input)
        user_message = {"role": "user", "content": user_input}
        if images:
            blocks = ([{"type": "text", "text": user_input}] if user_input else []) + list(images)
            user_message["content"] = blocks
        self.messages.append(user_message)
        self._truncate_history()
        self._emit_event("agent_start", message_id="user")
        self._emit_event("message_start", role="user", content=user_input)
        self._emit_event("message_end", role="user")
        provider_tools, name_map = self.tool_registry.get_provider_tools()

        for step in range(1, self.max_steps + 1):
            if self._consume_stop():
                self.store.save_session()
                self._emit_event("agent_end", reason="stopped")
                return "⏹️ 已停止"
            self._turn_id = f"turn_{step}"
            self._emit_event("turn_start", step=step)
            assistant_id = uuid.uuid4().hex
            self._emit_event("message_start", message_id=assistant_id, role="assistant")
            try:
                cfg = getattr(self, "_config", None) or load_config()
                native_stream = is_enabled(
                    cfg.get("agent_runtime", {}).get("native_tool_streaming"), True)
                if native_stream:
                    response = self.llm.stream_with_tools(
                        self.messages, provider_tools, temperature=0,
                        on_event=lambda event: self._forward_provider_event(assistant_id, event),
                    )
                else:
                    response = self.llm.complete(self.messages, tools=provider_tools, temperature=0)
                    for call in response.tool_calls:
                        self._forward_provider_event(assistant_id, {
                            "type": "tool_call_start", "call_id": call.call_id,
                            "name": call.name, "order": call.order,
                        })
                        self._forward_provider_event(assistant_id, {
                            "type": "tool_call_end", "call_id": call.call_id,
                            "name": call.name, "arguments": call.arguments, "order": call.order,
                        })
                    if response.text:
                        self._forward_provider_event(assistant_id,
                                                     {"type": "text_delta", "text": response.text})
            except Exception as exc:
                logger.error("原生工具调用失败: %s", exc, exc_info=True)
                self._emit_event("message_end", message_id=assistant_id, status="error")
                self._emit_event("agent_end", reason="error")
                return f"❌ LLM 调用失败: {exc}"
            self.store.set_anchor(self.llm.last_usage)

            if not response.tool_calls:
                answer = normalize_model_text(response.text)
                self.messages.append({"role": "assistant", "content": answer, "kind": "final"})
                self._emit_event("message_end", message_id=assistant_id, role="assistant",
                                 content=answer, finish_reason=response.finish_reason)
                self._emit_event("turn_end", step=step, tool_calls=0)
                self._emit_event("agent_end", reason="completed")
                self.store.save_session()
                self._save_memory(user_input)
                return answer or "（模型未返回可见文本）"

            native_calls = []
            for call in sorted(response.tool_calls, key=lambda item: item.order):
                call_id = call.call_id or uuid.uuid4().hex
                internal_name = name_map.get(call.name)
                if internal_name is None and self.tool_registry.get_tool(call.name):
                    internal_name = call.name
                native_calls.append((call_id, call.name, internal_name, call.arguments, call.raw_arguments))
            self.messages.append({
                "role": "assistant", "content": response.text or None, "kind": "tool_calls",
                "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": provider_name, "arguments": raw_arguments or "{}"}}
                    for call_id, provider_name, _, _, raw_arguments in native_calls],
            })
            self._emit_event("message_end", message_id=assistant_id, role="assistant",
                             finish_reason="tool_calls")

            result_count = 0
            for call_id, provider_name, tool_name, arguments, raw_arguments in native_calls:
                observation, is_error = self._execute_native_tool_call(
                    call_id, provider_name, tool_name, arguments, raw_arguments)
                self.messages.append({"role": "tool", "tool_call_id": call_id,
                                      "name": provider_name, "content": observation,
                                      "kind": "tool_result", "is_error": is_error})
                self._emit_event("message_start", message_id=f"result_{call_id}", role="tool",
                                 tool_call_id=call_id, tool=tool_name or provider_name)
                self._emit_event("message_end", message_id=f"result_{call_id}", role="tool",
                                 tool_call_id=call_id, tool=tool_name or provider_name,
                                 content=observation, is_error=is_error)
                result_count += 1
            self._emit_event("turn_end", step=step, tool_calls=result_count)
            self._light_compress()
            self._check_context(verbose=False)
            self._truncate_history()

        self.store.save_session()
        self._emit_event("agent_end", reason="max_steps")
        return f"⚠️ 已达最大步骤数 {self.max_steps}"

    def _forward_provider_event(self, assistant_id: str, event: dict) -> None:
        """Project provider-native events into the stable Agent event schema."""
        event_type = event.get("type")
        if event_type == "text_delta":
            self._emit_event("text_delta", message_id=assistant_id, text=normalize_model_text(event.get("text", "")))
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

    def _run_task_list(self, user_input, max_steps, verbose):
        """Phase 2: 任务清单模式 —— 框架主动推进，逐任务执行"""
        # plan 模式：自动信任工作区，区内操作免确认，区外仍需审批
        self.permission.allow_workspace()
        while not self._task_list.is_all_done():
            if self._consume_stop():
                self.store.save_session()
                return "⏹ 已停止（任务清单中断）"
            current = self._task_list.get_current()
            if not current:
                break
            task_prompt = (
                f"## 请执行任务 {current.id}/{self._task_list.total}：{current.description}\n"
                f"执行此任务，完成后只输出 agent.turn.v1/final JSON 信封。"
            )
            self.messages.append({"role": "user", "content": task_prompt})
            if verbose:
                print(f"\n  📋 任务 {current.id}/{self._task_list.total}: {current.description}")

            task_done = False
            # plan 模式子任务步数不应受默认 max_steps 限制，至少 200 步
            task_max_steps = max(max_steps, 200)
            for t_step in range(1, task_max_steps + 1):
                if verbose:
                    print(f"\n  ─── 任务 {current.id} 第 {t_step}/{task_max_steps} 步 ───")
                try:
                    response = self.llm.think(self.messages, temperature=0)
                except Exception as e:
                    logger.error(f"LLM 调用失败: {e}", exc_info=True)
                    return f"❌ LLM 调用失败: {e}"
                self.store.set_anchor(self.llm.last_usage)
                if not response:
                    return "❌ LLM 调用失败"
                log_llm_response(t_step, response)
                if verbose:
                    m = _THOUGHT_RE.search(response)
                    if m:
                        print(f"  💭 {m.group(1).strip()[:200]}")
                parsed = parse_react_response(response)
                acts = parsed.get("actions", [])
                # COMPLETE_TASK
                cm = re.search(r"COMPLETE_TASK[：:]\s*(\d+)\s*(.*)", response, re.IGNORECASE)
                if cm and self._task_list:
                    tid, res = int(cm.group(1)), cm.group(2).strip()
                    self._task_list.mark_done(tid, res)
                    task_done = True
                    if verbose:
                        print(f"  ✅ 任务 {tid}/{self._task_list.total} 完成{(' → ' + res) if res else ''}")
                    break
                # FINAL_ANSWER
                if parsed["final_answer"] and not acts:
                    self.messages.append({"role": "assistant", "content": parsed["final_answer"]})
                    if verbose:
                        print(f"  ✅ {parsed['final_answer']}")
                    self._task_list.mark_done(current.id, parsed["final_answer"][:200])
                    task_done = True
                    if verbose:
                        print(f"  ✅ 任务 {current.id}/{self._task_list.total} 完成")
                    break
                # ACTIONS
                if acts:
                    if verbose:
                        names = [a["name"] for a in acts]
                        print(f"  🛠️  {TAG_ACTION}({len(acts)}): {', '.join(names)}")
                    self.messages.append({"role": "assistant", "content": response})
                    # 权限检查（plan 模式：区内自动放行，区外需确认）
                    checked_acts = []
                    denied_acts = []
                    for a in acts:
                        tool_name = a["name"]
                        input_str = a["input"]
                        try:
                            params = json.loads(input_str) if input_str else {}
                        except json.JSONDecodeError:
                            params = {}
                        if not isinstance(params, dict):
                            reason = f"INPUT 必须是 JSON 对象，但收到了 JSON {type(params).__name__}。"
                            denied_acts.append((tool_name, input_str, reason))
                            continue
                        level, gate_reason = self._gate_check(tool_name, params)
                        if level == ALLOW:
                            checked_acts.append(a)
                        elif level == DENY:
                            reason = gate_reason or "权限不足，操作已被系统拒绝"
                            denied_acts.append((tool_name, input_str, reason))
                            if verbose:
                                print(f"  ⛔ {tool_name}: {reason}")
                        elif level == ASK:
                            choice = self._ask_user(tool_name, params)
                            if choice == "a":
                                self.permission.allow_workspace()
                                checked_acts.append(a)
                            elif choice in ("", "y", "yes"):
                                checked_acts.append(a)
                            elif choice == "s":
                                denied_acts.append((tool_name, input_str, "用户选择跳过"))
                            else:
                                return f"操作已取消：用户拒绝了 {tool_name}，对话已终止。"
                    t_results = []
                    if checked_acts:
                        with ThreadPoolExecutor(max_workers=min(len(checked_acts), 5)) as pool:
                            tfm = {pool.submit(self._execute_tool, a["name"], a["input"]): a for a in checked_acts}
                            for f in as_completed(tfm):
                                a = tfm[f]
                                try:
                                    obs = f.result()
                                except Exception as e:
                                    obs = f"❌ 异常: {e}"
                                t_results.append((a["name"], a["input"], obs, obs.startswith("❌")))
                    for tool_name, input_str, reason in denied_acts:
                        t_results.append((tool_name, input_str, f"⏭️ 跳过: {reason}", True))
                    combined = self._combine_results(t_results)
                    self.messages.append({
                        "role": "user", "name": "tool_result",
                        "content": _build_tool_result_content(combined),
                    })
                    # 上下文管理（与 Phase 1 保持一致）
                    self._light_compress()
                    self._check_context(verbose=verbose)
                    self._truncate_history()
                    continue
                # ELSE
                answer = response.strip()
                self.messages.append({"role": "assistant", "content": answer})
                if verbose:
                    print(f"  💬 {answer}")
                self._task_list.mark_done(current.id, answer[:200])
                task_done = True
                break
            if not task_done:
                # 子任务超步数：标记失败，继续执行下一个任务
                fail_reason = f"已达最大步数（{task_max_steps} 步），任务未完成"
                self._task_list.mark_done(current.id, fail_reason)
                if verbose:
                    print(f"  ⚠️  任务 {current.id}/{self._task_list.total} {fail_reason}")
            self._update_task_list_in_prompt()
            self.store.save_session()
        # 全部完成
        self._update_task_list_in_prompt()
        self.store.save_session()
        if self._task_list.is_all_done():
            total = self._task_list.total
            summary_lines = [f"🎉 全部 {total} 个任务完成！"]
            summary_lines.append("")
            summary_lines.append(self._task_list.to_summary())
            self._task_list = None
            self._update_task_list_in_prompt()
            self.store.save_session()
            self._save_memory(user_input)
            summary = "\n".join(summary_lines)
            if verbose:
                print(f"\n  {summary}")
            return summary
        self._save_memory(user_input)
        return f"⚠️ 任务执行中断"

    # ============================================================
    # 流式运行
    # ============================================================

    def run_events(self, user_input: str, images: list | None = None):
        """Run the canonical Agent loop in a worker and yield runtime events."""
        events: queue.Queue = queue.Queue()

        def sink(event):
            events.put(event)

        def worker():
            try:
                answer = self.run(user_input, verbose=False, images=images,
                                  event_sink=sink)
                events.put({"type": "final", "data": {"answer": answer}})
            except Exception as exc:
                logger.exception("流式运行失败")
                events.put({"type": "error", "data": {"message": str(exc)}})
            finally:
                events.put(None)

        threading.Thread(target=worker, name="agent-stream", daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                return
            yield event

    def stream_run(self, user_input: str, images: list | None = None):
        """Backward-compatible text view over the canonical Agent loop."""
        visible_text_emitted = False
        for event in self.run_events(user_input, images=images):
            event_type = event["type"]
            data = event["data"]
            if event_type == "text_delta":
                visible_text_emitted = True
                yield data.get("text", "")
            elif event_type == "text_reset":
                yield "\n"
            elif event_type in {"tool_result", "tool_execution_end"}:
                yield f"\n{data.get('result', '')}\n"
            elif event_type == "final":
                if not visible_text_emitted:
                    yield data.get("answer", "")
            elif event_type == "error":
                yield f"❌ {data.get('message', '流式运行失败')}"
        return
        """逐步输出 Agent 的思考过程"""

        max_steps = self.max_steps
        if not self.messages:
            self.messages.append({"role": "system", "content": self.system_prompt})
        # plan 模式：自动信任工作区，区内操作免确认，区外仍需审批
        self.permission.allow_workspace()
        self._light_compress()
        self._check_context(verbose=False)
        self.messages.append({"role": "user", "content": user_input})
        self._truncate_history()
        yield f"🤖 {self.name}（最大 {max_steps} 步）\n"
        for step in range(1, max_steps + 1):
            yield f"\n── 第 {step}/{max_steps} 步 ──\n"
            try:
                response = self.llm.think(self.messages, temperature=0)
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}", exc_info=True)
                yield f"❌ LLM 调用失败: {e}\n"
                return
            self.store.set_anchor(self.llm.last_usage)
            if not response:
                yield "❌ LLM 调用失败\n"
                return
            parsed = parse_react_response(response)
            if parsed["thought"]:
                yield f"💭 {parsed['thought']}\n"
            actions = parsed.get("actions", [])
            if parsed["final_answer"] and not actions:
                self.messages.append({"role": "assistant", "content": parsed["final_answer"]})
                self.store.save_session()
                self._save_memory(user_input)
                yield f"\n✅ {parsed['final_answer']}\n"
                return
            if actions:
                self._emit_event("text_reset", reason="tool_calls")
                # Keep streaming compatible while executing every structured
                # call in this turn.  The former implementation silently
                # ignored actions[1:], causing tool loops to stall.
                results = []
                for order, action in enumerate(actions):
                    tool_name = action["name"]
                    input_str = action.get("input", "{}")
                    try:
                        params = json.loads(input_str)
                    except (TypeError, json.JSONDecodeError) as exc:
                        observation = f"❌ 协议错误：工具参数不是合法 JSON 对象: {exc}"
                        results.append((tool_name, input_str, observation, True))
                        yield f"📊 {observation}\n"
                        continue
                    if not isinstance(params, dict):
                        observation = "❌ 协议错误：工具参数必须是 JSON 对象"
                        results.append((tool_name, input_str, observation, True))
                        yield f"📊 {observation}\n"
                        continue
                    errors = self.tool_registry.validate_arguments(tool_name, params)
                    if errors:
                        observation = "❌ 参数校验失败: " + "; ".join(errors)
                        results.append((tool_name, input_str, observation, True))
                        yield f"📊 {observation}\n"
                        continue
                    level, gate_reason = self._gate_check(tool_name, params)
                    if level == DENY:
                        observation = f"⏭️ 跳过: {gate_reason or '权限不足，操作已被系统拒绝'}"
                    elif level == ASK:
                        observation = "⏭️ 跳过: 工作区外操作需在交互模式下确认"
                    else:
                        yield f"🛠️  {TAG_ACTION}[{order + 1}/{len(actions)}]: {tool_name}\n"
                        try:
                            observation = self._execute_tool(tool_name, input_str)
                        except Exception as e:
                            logger.error(f"工具执行失败: {e}", exc_info=True)
                            observation = f"❌ 工具执行失败: {type(e).__name__}: {e}"
                    is_error = observation.startswith(("❌", "⏭️", "⛔"))
                    results.append((tool_name, input_str, observation, is_error))
                    yield f"📊 {observation[:500]}\n"
                for _tname, _input_str, _obs, _is_err in results:
                    self._emit_event("tool_result", tool=_tname,
                                     result=_obs, is_error=_is_err)
                self.messages.append({"role": "assistant", "content": response,
                                      "kind": "tool_calls"})
                self.messages.append({
                    "role": "user",
                    "name": "tool_result",
                    "content": _build_tool_result_content(self._combine_results(results)),
                })
                self._light_compress()
                self._check_context(verbose=False)
                self._truncate_history()
            else:
                answer = response.strip()
                self.messages.append({"role": "assistant", "content": answer})
                self.store.save_session()
                self._save_memory(user_input)
                yield f"💬 {answer}\n"
                return
        self.store.save_session()
        self._save_memory(user_input)
        yield f"\n⚠️ 已达最大步数 {max_steps}\n"


    def generate_plan(self, user_input: str) -> dict:
        """Ask the provider for a typed plan using one native tool call."""
        from core.plan import PlanStatus
        plan_tool = {
            "type": "function",
            "function": {
                "name": "submit_plan",
                "description": "Submit an ordered execution plan without performing work.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "steps": {"type": "array", "minItems": 1,
                                  "items": {"type": "object", "additionalProperties": False,
                                            "properties": {"description": {"type": "string"}},
                                            "required": ["description"]}},
                    },
                    "required": ["steps"],
                },
            },
        }
        messages = [{"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_input}]
        response = self.llm.complete(messages, tools=[plan_tool], temperature=0)
        calls = getattr(response, "tool_calls", None) or []
        call = next((item for item in calls if item.name == "submit_plan"), None)
        if call is None or not isinstance(call.arguments, dict):
            raise ValueError("plan generation failed: model did not return submit_plan")
        steps = call.arguments.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("plan generation failed: steps is empty")
        return {"steps": steps}

    def execute_plan(self, plan: dict, source_prompt: str = "", *, verbose: bool = True) -> str:
        """Execute a typed plan through the durable local runtime."""
        import asyncio
        from core.plan import PlanManager, PlanStatus
        from core.runtime import RuntimeStore, TaskEnvelope, TaskResult, TaskStatus, TaskRuntime
        runtime_store = RuntimeStore(_configured_runtime_store_path())
        manager = PlanManager(runtime_store)
        session_id = f"cli:{self.store.session_id}"
        title = source_prompt[:120] or "CLI Plan"
        persisted = manager.create_preview(session_id, plan, source_prompt=source_prompt, title=title)
        manager.approve(persisted.plan_id, actor="cli")
        manager.activate(persisted.plan_id)
        self.permission.allow_workspace()

        async def execute_task(envelope, token):
            token.checkpoint()
            task = manager.get(envelope.metadata["plan_task_id"])
            prompt = envelope.prompt
            try:
                output = self.run(prompt, verbose=False)
                token.checkpoint()
                return TaskResult(task_id=envelope.task_id, status=TaskStatus.COMPLETED,
                                  visible_text=output, summary=output[:1000])
            except Exception as exc:
                return TaskResult(task_id=envelope.task_id, status=TaskStatus.FAILED,
                                  summary=str(exc), error_message=str(exc))

        async def run_all():
            runtime = TaskRuntime(runtime_store, execute_task, max_global_concurrency=1,
                                  worker_id="cli", max_attempts=1)
            await runtime.start()
            try:
                while True:
                    current = manager.get(persisted.plan_id)
                    ready = manager.ready_tasks(persisted.plan_id)
                    if not ready:
                        if current.status in PlanStatus.terminal():
                            break
                        await asyncio.sleep(0)
                        continue
                    for item in ready:
                        envelope = TaskEnvelope.create(
                            session_id=session_id, session_key=session_id, source="plan",
                            prompt=item.description, priority=50,
                            timeout_seconds=60, max_steps=self.max_steps,
                            metadata={"plan_task_id": item.plan_task_id},
                        )
                        manager.assign_task(persisted.plan_id, item.plan_task_id, envelope.task_id)
                        manager.start_task(persisted.plan_id, item.plan_task_id)
                        await runtime.submit(envelope)
                        result = await runtime.wait(envelope.task_id)
                        manager.finish_task(persisted.plan_id, item.plan_task_id,
                                            success=result.status is TaskStatus.COMPLETED,
                                            summary=(result.summary or result.visible_text or result.error_message))
            finally:
                await runtime.stop()
            return manager.get(persisted.plan_id)

        final = asyncio.run(run_all())
        completed = sum(1 for item in final.tasks if item.status.value == "completed")
        if final.status is PlanStatus.COMPLETED:
            return f"Plan \u5df2\u5b8c\u6210: {completed}/{len(final.tasks)} (ID: {final.plan_id})"
        return f"Plan status={final.status.value}, completed={completed}/{len(final.tasks)} (ID: {final.plan_id})"

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
        max_steps: 最大 ReAct 步数
        max_history_tokens: 上下文预算阈值
        debug: 调试模式
        permission: 权限管理
        memory: 跨会话记忆
        skills: 学习型技能系统
        sandbox: 沙箱执行器（L2 内容拦截+资源隔离）
        hooks: Hook 模块（事件驱动自定义扩展，配置于 config.json 的 hooks section）
        mcp_servers: MCP 服务器配置列表，每项含 name/transport/command 等
    """

    setup_logging(debug=debug)
    if debug:
        set_debug(True)
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
    # 沙箱执行器（L2 内容拦截 + 资源隔离）——显式工作目录
    sandbox_executor = SandboxExecutor(workspace=_working_directory) if sandbox else None
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
        process_manager = ProcessManager(sandbox_executor, _working_directory)
    registry = ToolRegistry()
    register_all_tools(
        registry, memory_manager=memory_manager,
        sandbox=sandbox_executor, process_manager=process_manager,
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

# ============================================================
# 交互式 CLI

# ============================================================


def start_interactive_shell(debug: bool = False, resume_session_id: str = None):
    """启动交互式命令行，支持 /model 切换模型"""

    # Linux: 激活 input() 的行编辑（方向键/Home/End/历史）和 tab 补全
    try:
        import readline
    except ImportError:
        pass

    print("\n╔═══════════════════════════════════════════════╗")
    print("║   🚀 JKagent 交互式命令行                  ║")
    if debug:
        print("║   🐛 调试模式已开启                           ║")
    print("║                                                 ║")
    print("║   /model             查看当前模型               ║")
    print("║   /model list        列出本地服务商             ║")
    print("║   /model <name>      切换到云端模型             ║")
    print("║   /model local <name> 切换到本地模型            ║")
    print("║   /session           查看/管理会话              ║")
    print("║   /session list      列出所有历史会话           ║")
    print("║   /session save      保存当前会话               ║")
    print("║   /session delete <id> 删除指定会话             ║")
    print("║   /skill list        列出所有技能               ║")
    print("║   /skill <name>      直接调用指定技能           ║")
    print("║   /skill delete <name> 删除指定技能             ║")
    print("║   /sandbox           查看沙箱状态               ║")
    print("║   /sandbox on/off    开启/关闭沙箱              ║")
    print("║   /sandbox strict    切换到严格模式             ║")
    print("║   /sandbox bypass    临时绕过沙箱               ║")
    print("║   /sandbox profile   切换配置档                 ║")
    print("║   /proc              查看长驻进程会话           ║")
    print("║   /proc stop <id>    停止指定进程               ║")
    print("║   /proc tail <id>    持续打印进程输出          ║")
    print("║   /hook              查看已注册 hook            ║")
    print("║   /hook reload       重新加载 config.json 的 hooks 配置 ║")
    print("║   /stats             查看上下文占用             ║")
    print("║   /history             查看当前会话内容         ║")
    print("║   /compact           全量压缩历史（释放上下文）  ║")
    print("║   /clear             清空对话历史               ║")
    print("║   /help              显示帮助                   ║")
    print("║   exit               退出                       ║")
    print("╚═══════════════════════════════════════════════╝")
    # ---- 恢复会话 or 新建 ----
    if resume_session_id:
        from pathlib import Path
        from core.message_store import DEFAULT_SESSION_DIR
        session_file = Path(DEFAULT_SESSION_DIR) / f"{resume_session_id}.json"
        if not session_file.exists():
            print(f"\n❌ 未找到会话: {resume_session_id}")
            print(f"   尝试: python agent.py --resume <session_id>")
            print(f"   或:  /session list 查看已有会话")
            sys.exit(1)
        with open(session_file, "r", encoding="utf-8") as f:
            session_data = json.load(f)
        # 用会话中的模型配置创建 Agent
        agent = create_agent(
            debug=debug,
            model=session_data.get("model_id"),
            provider=session_data.get("model_provider") or None,
        )
        # 恢复消息到 store（不含 system，由后续插入）
        agent.store.load_session_data(session_data)
        # 在 index 0 插入新的 system prompt
        agent.messages.insert(0, {"role": "system", "content": agent.system_prompt})
        # 恢复任务清单（如果有）
        resume_task_info = ""
        if agent.store.task_list:
            agent._task_list = TaskList.from_dict(agent.store.task_list)
            agent._update_task_list_in_prompt()
            current = agent._task_list.get_current()
            if current:
                resume_task_info = f"，任务 {current.id}/{agent._task_list.total}: {current.description}"
        print(f"\n📂 已恢复会话: {resume_session_id}（{session_data.get('message_count', 0)} 条消息{resume_task_info}）")
    else:
        try:
            agent = create_agent(debug=debug)
        except ValueError as e:
            print(f"\n❌ 创建失败: {e}")
            print("   配置文件: config.json（项目根目录）")
            sys.exit(1)
    # 显示当前状态
    print(f"\n🤖 {agent.name}")
    print(f"📡 当前模型: {agent.llm}  |  session: {agent.store.session_id}")
    perm_status = f"[PERM] 权限管理: {'启用' if agent.permission else '关闭'}"
    sb_status = f" | Sandbox: {'ON' if getattr(agent, 'sandbox', None) and agent.sandbox.enabled else 'OFF'}" if getattr(agent, 'sandbox', None) else ""
    print(f"[TOOLS] {agent.tool_registry.count()} 个工具就绪 | {perm_status}{sb_status}")
    while True:
        try:
            u = input("\n👤 你: ").strip()
            if not u:
                continue
            # ---- 退出 ----
            if u.lower() in ("exit", "quit", "q", "退出"):
                # 清理 MCP 连接（关闭 aiohttp session 和子进程）
                if hasattr(agent, 'mcp_manager') and agent.mcp_manager:
                    try:
                        from core.mcp_client import run_in_mcp_loop
                        # 在 MCP 专用事件循环中关闭，复用同一连接状态
                        run_in_mcp_loop(agent.mcp_manager.close_all(), timeout=10)
                    except Exception:
                        pass
                # 清理长驻子进程（杀整树）
                if getattr(agent, 'process_manager', None):
                    try:
                        agent.process_manager.cleanup_all()
                    except Exception:
                        pass
                print("👋 再见！")
                break
            # ---- /model 命令 ----
            if u.startswith("/model"):
                parts = u.split()
                cmd = parts[1] if len(parts) > 1 else ""
                if cmd == "list" or cmd == "ls":
                    llm = agent.llm
                    mode = "☁️ 云端" if llm.llm_type == "cloud" else "🏠 本地"
                    prov = getattr(llm, "provider", "") or ""
                    print(f"\n  当前配置:")
                    print(f"    模型:   {llm.model}")
                    print(f"    类型:   {mode}{f' [{prov}]' if prov else ''}")
                    print(f"    地址:   {llm.base_url}")
                    print(f"    上下文:     {llm.context_length:,} tokens")
                    print(f"    压缩阈值:   {agent.max_history_tokens:,} tokens（60% 提示 / 80% 自动全量压缩）")
                    print()
                elif cmd == "local":
                    # /model local <model_name>
                    local_model = parts[2] if len(parts) > 2 else None
                    provider = os.getenv("LLM_PROVIDER") or "ollama"
                    kwargs = {"provider": provider}
                    if local_model:
                        kwargs["model"] = local_model
                    agent.switch_llm(**kwargs)
                elif cmd == "":
                    # /model → 查看当前
                    print(f"  📡 {agent.llm}")
                else:
                    # /model <model_name> → 切换云端模型（其他参数从 .env 读）
                    agent.switch_llm(model=cmd, llm_type="cloud")
                continue
            # ---- /session ----
            if u.startswith("/session"):
                parts = u.split()
                cmd = parts[1] if len(parts) > 1 else ""
                if cmd == "" or cmd == "info":
                    s = agent.store
                    print(f"\n  session: {s.session_id}")
                    print(f"  模型: {s.model_id or '未知'}")
                    print(f"  消息: {len(s)} 条")
                    print(f"  创建: {s.session_id}")
                elif cmd == "list":
                    from core.message_store import MessageStore
                    sessions = MessageStore.list_session_files()
                    if not sessions:
                        print("  📭 暂无已保存的会话")
                    else:
                        print(f"\n  已保存的会话（共 {len(sessions)} 个）:")
                        for s in sessions:
                            print(f"    {s['session_id']}  {s['model_id']:20s}  {s['message_count']:3d} 条  {s['created_at'][:16]}")
                elif cmd == "save":
                    path = agent.store.save_session()
                    print(f"  ✅ 已保存: {path}")
                elif cmd == "delete":
                    targets = parts[2:] if len(parts) > 2 else []
                    if targets:
                        from core.message_store import MessageStore
                        for target in targets:
                            if MessageStore.delete_session_file(target):
                                print(f"  🗑️  已删除会话: {target}")
                            else:
                                print(f"  ❌ 未找到会话: {target}")
                    else:
                        print("  ❓ 用法: /session delete <session_id1> [<session_id2> ...]")
                else:
                    print("  ❓ 用法: /session [info|list|save|delete]")
                continue
            # ---- /skill ----
            if u.startswith("/skill"):
                parts = u.split()
                cmd = parts[1] if len(parts) > 1 else ""
                sm = getattr(agent, 'skill_manager', None)

                if cmd == "" or cmd == "list":
                    if not sm or sm.skill_count() == 0:
                        print("  📭 暂无技能（使用 create_skill 工具创建）")
                    else:
                        print(f"\n  已保存的技能（共 {sm.skill_count()} 个）:")
                        for s in sm.get_all_skills():
                            print(f"    ▶ {s.name}")
                            print(f"      描述: {s.description[:60]}")
                            print()
                elif cmd == "delete":
                    target = parts[2] if len(parts) > 2 else None
                    if not target:
                        print("  ❓ 用法: /skill delete <skill_name>")
                    elif not sm:
                        print("  ❌ 技能系统未就绪")
                    elif sm.delete_skill(target):
                        print(f"  🗑️  已删除技能: {target}")
                    else:
                        print(f"  ❌ 未找到技能: {target}")
                else:
                    # /skill <name> [args...] → 直接调用技能（传参）
                    if not sm:
                        print("  ❌ 技能系统未就绪")
                    else:
                        skill_name = cmd
                        skill = sm.get_skill(skill_name)
                        if not skill:
                            print(f"  ❌ 未找到技能: {skill_name}")
                            print("  可用: /skill list 查看所有技能")
                        else:
                            # 解析额外参数传入技能
                            extra_args = parts[2:]
                            kwargs_desc = f"参数：{extra_args}" if extra_args else "无参数"
                            msg = (
                                f'用户通过 /skill 命令调用了技能 "{skill_name}"，{kwargs_desc}。\n\n'
                                f"请按以下技能指令逐步执行，可调用其他工具：\n"
                                f"{skill.instruction}\n\n"
                                f"所有步骤完成后，只输出 agent.turn.v1/final JSON 信封。"
                            )
                            agent.run(msg)
                continue
            # ---- /sandbox ----
            if u.startswith("/sandbox"):
                parts = u.split()
                cmd = parts[1] if len(parts) > 1 else ""
                sb = getattr(agent, 'sandbox', None)
                if not sb:
                    print("  ❌ 沙箱未启用（create_agent(sandbox=False)")
                elif cmd == "on":
                    sb.enabled = True
                    print("  [OK] 沙箱已开启")
                elif cmd == "off":
                    sb.enabled = False
                    print("  [WARN] 沙箱已关闭（仅保留 L1 权限检查）")
                elif cmd == "bypass":
                    sb.bypass_next()
                    print("  [BYPASS] 下一条命令绕过沙箱")
                elif cmd == "strict":
                    msg = sb.set_profile("restricted")
                    print(f"  [LOCK] {msg}")
                elif cmd == "profile":
                    name = parts[2] if len(parts) > 2 else ""
                    if name:
                        msg = sb.set_profile(name)
                        print(f"  [CFG] {msg}")
                    else:
                        print(f"  Usage: /sandbox profile <name>")
                elif cmd == "list":
                    profiles = sb.list_profiles()
                    print(f"  可用配置档: {', '.join(profiles)}")
                else:
                    print(f"\n  {sb.get_status_text()}")
                continue
            # ---- /proc ----
            if u.startswith("/proc"):
                pm = getattr(agent, 'process_manager', None)
                if not pm:
                    print("  ❌ 长驻进程模块未启用（create_agent 需 sandbox=True）")
                    continue
                parts = u.split()
                sub = parts[1] if len(parts) > 1 else "list"
                if sub == "list":
                    sessions = pm.list_sessions()
                    if not sessions:
                        print("  📭 暂无进程会话")
                    else:
                        print(f"\n  🖥️  进程会话（共 {len(sessions)} 个）:")
                        for s in sessions:
                            print(f"    [{s['id']}] {s['name']} | {s['status']}"
                                  f" | exit={s['exit_code']} | idle={s['idle_for']}s")
                elif sub == "stop" and len(parts) > 2:
                    try:
                        print(f"  {pm.stop(int(parts[2]))}")
                    except ValueError:
                        print("  ❌ 用法: /proc stop <id>")
                elif sub == "tail" and len(parts) > 2:
                    try:
                        sid = int(parts[2])
                        print(f"  📺 tail session={sid}（Ctrl-C 中断）")
                        import time as _t
                        try:
                            while True:
                                out, err, trunc, status = pm.read(sid)
                                if out:
                                    print(out, end="", flush=True)
                                if err:
                                    print(f"\n📕 {err}", end="", flush=True)
                                if trunc:
                                    print("\n⚠️ 部分输出因缓冲区满被丢弃", end="", flush=True)
                                if status and ("exited" in status or "killed" in status):
                                    print(f"\n  {status.strip()}")
                                    break
                                _t.sleep(0.5)
                        except KeyboardInterrupt:
                            print("\n  ⏹  tail 已中断")
                    except ValueError:
                        print("  ❌ 用法: /proc tail <id>")
                else:
                    print("  用法: /proc [list | stop <id> | tail <id>]")
                continue
            # ---- /hook ----
            if u.startswith("/hook"):
                parts = u.split()
                cmd = parts[1] if len(parts) > 1 else ""
                if cmd == "reload":
                    # 强制重新加载 config.json，然后重新加载 hooks
                    from core.config_loader import load_config as _reload_cfg
                    _reload_cfg(force_reload=True)
                    if agent.hooks._try_load_unified():
                        print(f"  🔄 已重新加载 hooks 配置")
                    else:
                        print(f"  ⚠️  hooks 重新加载失败")
                else:
                    hlist = agent.hooks.list_hooks()
                    if not hlist:
                        print("  📭 暂无已注册 hook")
                    else:
                        print(f"\n  已注册 hook（共 {len(hlist)} 个）:")
                        print("  ─" * 25)
                        for h in hlist:
                            print(f"  [{h['event']:18s}] {h['hook']}  matcher={h['matcher']}")
                continue
            # ---- /stats ----
            if u.startswith("/stats"):
                print(f"\n{agent.store.format_stats()}")
                continue
            # ---- /history ----
            if u.startswith("/history"):
                print(f"\n  📋 当前会话内容（共 {len(agent.messages)} 条消息）:")
                print(f"  {'='*55}")
                for i, msg in enumerate(agent.messages):
                    role = msg.get("role", "?")
                    name = msg.get("name", "")
                    # 多模态 list content → 纯文本预览
                    content = _content_to_text(msg.get("content", ""))
                    # 角色图标
                    icon = {"system": "⚙️", "user": "👤", "assistant": "🤖", "tool": "🔧"}.get(role, "❓")
                    label = f"{role}"
                    if name:
                        label += f"({name})"
                    # 截取预览
                    preview = content[:300].replace("\n", " ")
                    if len(content) > 300:
                        preview += "..."
                    print(f"  [{i:3d}] {icon} {label:20s} {preview}")
                print(f"  {'='*55}")
                continue
            # ---- /plan ----
            if u.startswith("/plan"):
                if not agent.messages:
                    agent.messages.append({"role": "system", "content": agent.system_prompt})
                # 支持 /plan <任务描述> 直接指定任务
                plan_input = u[len("/plan "):].strip() if len(u) > len("/plan ") else ""
                if plan_input:
                    agent.messages.append({"role": "user", "content": plan_input})
                print(f"\n  📐 正在分析任务并生成方案…")
                plan_response = agent.llm.think(
                    agent.messages + [{"role": "user", "content": PLAN_PROMPT_TEXT}],
                    temperature=0.3,
                    stream=False,
                    silent=True,
                )
                plan_text = extract_plan_text(plan_response) if plan_response else None
                if plan_text:
                    if agent._handle_plan(plan_text, verbose=True):
                        # 用户确认 → 进入任务执行阶段
                        result = agent._run_task_list(
                            user_input=plan_input or "plan execution",
                            max_steps=agent.max_steps,
                            verbose=True,
                        )
                        print(f"\n  🤖 {result}")
                elif plan_response:
                    print(f"  ❌ 未能识别出方案内容，请重试")
                else:
                    print(f"  ❌ LLM 返回空")
                continue
            # ---- /compact ----
            if u.startswith("/compact"):
                print(f"\n  📐 执行全量压缩…")
                before = agent.store.stats()
                ok = agent._full_compress(verbose=True)
                if ok:
                    after = agent.store.stats()
                    saved = before["total_tokens"] - after["total_tokens"]
                    print(f"  ✅ 压缩完成: 释放了 {saved:,} tokens，剩余 {after['remaining_tokens']:,} tokens")
                else:
                    print(f"  ℹ️  无需压缩")
                continue
            # ---- /clear ----
            if u.startswith("/clear"):
                agent.store.save_session()  # 先保存当前历史到文件
                agent.clear_history()
                print(f"  🗑️  当前上下文已清空（历史仍保存在会话文件中）")
                continue
            # ---- /mcp ----
            if u.startswith("/mcp"):
                from tools.mcp_tools import MCPTool

                # 有未初始化的配置则立即初始化
                if not agent.mcp_manager and agent._mcp_pending_init:
                    agent._init_mcp_if_needed()

                if not agent.mcp_manager:
                    print("\n  ℹ️  未配置 MCP 服务器")
                    print("  配置方式：")
                    print("    1. create_agent(mcp_servers=[...])")
                    print("    2. 在 config.json 的 mcp.servers 中配置")
                else:
                    parts = u.split()
                    sub = parts[1] if len(parts) > 1 else "status"
                    conn_names = agent.mcp_manager.list_connections()

                    if sub == "list":
                        print(f"\n  🔌 MCP 服务器详情")
                        print(f"  ─────────────────────────────────")
                        for i, name in enumerate(conn_names, 1):
                            conn = agent.mcp_manager.get_connection(name)
                            status = "✅ 已连接" if (conn and conn.is_initialized) else "❌ 未连接"
                            transport = agent.mcp_manager._configs.get(name, {}).get("transport", "?")
                            print(f"  [{i}] {name} ({transport})")
                            print(f"     状态: {status}")
                            # 列出该服务器的工具
                            mcp_tools = [
                                t for t in agent.tool_registry.list_tools()
                                if isinstance(t, MCPTool) and t.name.startswith(f"{name}/")
                            ]
                            if mcp_tools:
                                print(f"     工具:")
                                for t in mcp_tools:
                                    desc_short = t.description[:50]
                                    print(f"       - {t.name}")
                                    if desc_short:
                                        print(f"         {desc_short}")
                            else:
                                print(f"     工具: （无）")
                            print()
                    elif sub == "tools":
                        # /mcp tools [server]
                        target = parts[2] if len(parts) > 2 else None
                        all_mcp = [t for t in agent.tool_registry.list_tools() if isinstance(t, MCPTool)]
                        if target:
                            all_mcp = [t for t in all_mcp if t.name.startswith(f"{target}/")]
                        if not all_mcp:
                            print(f"\n  ℹ️  未找到 MCP 工具" + (f"（服务器: {target}）" if target else ""))
                        else:
                            print(f"\n  🔧 MCP 工具列表" + (f"（{target}）" if target else ""))
                            print(f"  ─────────────────────────────────")
                            for t in all_mcp:
                                desc_short = t.description[:60]
                                print(f"  ▶ {t.name}")
                                if desc_short:
                                    print(f"     {desc_short}")
                                # 显示参数摘要
                                props = t.parameters.get("properties", {})
                                if props:
                                    required = t.parameters.get("required", [])
                                    param_hints = []
                                    for pname, pinfo in props.items():
                                        req = "必填" if pname in required else "可选"
                                        param_hints.append(f"{pname} ({pinfo.get('type', '?')}, {req})")
                                    print(f"     参数: {', '.join(param_hints[:5])}")
                                    if len(param_hints) > 5:
                                        print(f"           ... 共 {len(param_hints)} 个参数")
                                print()
                    else:
                        # /mcp 或 /mcp status — 概览
                        ok = sum(1 for n in conn_names
                                 if (c := agent.mcp_manager.get_connection(n)) and c.is_initialized)
                        fail = len(conn_names) - ok
                        # 统计 MCP 工具总数
                        all_mcp = [t for t in agent.tool_registry.list_tools() if isinstance(t, MCPTool)]
                        print(f"\n  🔌 MCP 服务器状态")
                        print(f"  ─────────────────────────────────")
                        for name in conn_names:
                            conn = agent.mcp_manager.get_connection(name)
                            if conn and conn.is_initialized:
                                server_tools = [t for t in all_mcp if t.name.startswith(f"{name}/")]
                                print(f"  🔗 {name:<16} ✅ 已连接  ({len(server_tools)} 个工具)")
                            else:
                                print(f"  🔗 {name:<16} ❌ 未连接")
                        print(f"  ─────────────────────────────────")
                        print(f"  共 {len(conn_names)} 个服务器, {len(all_mcp)} 个 MCP 工具注册")
                        if ok:
                            print(f"  运行 /mcp list 查看详情")
                        if fail > 0:
                            print(f"  ⚠️  {fail} 个服务器初始化失败，检查日志了解详情")
                continue
            # ---- /help ----
            if u.startswith("/help"):
                print("\n  命令:")
                print("    /model              查看当前模型")
                print("    /model list         列出可用服务商")
                print("    /model <name>       切换到云端模型")
                print("    /model local <name> 切换到本地模型")
                print("    /session            查看/管理会话")
                print("    /session list       列出所有会话")
                print("    /session save       保存当前会话")
                print("    /session delete <id> 删除指定会话")
                print("    /skill list         列出所有技能")
                print("    /skill <name>       直接调用技能")
                print("    /skill delete <name> 删除指定技能")
                print("    /sandbox            查看沙箱状态")
                print("    /sandbox on/off     开启/关闭沙箱")
                print("    /sandbox strict     切换到严格模式")
                print("    /sandbox bypass     临时绕过沙箱")
                print("    /sandbox profile    切换配置档")
                print("    /proc               查看长驻进程会话")
                print("    /proc stop <id>     停止指定进程")
                print("    /proc tail <id>     持续打印进程输出")
                print("    /hook               查看已注册 hook")
                print("    /hook reload        重新加载 config.json 的 hooks 配置")
                print("    /stats              查看上下文占用统计")
                print("    /history            查看当前会话内容")
                print("    /mcp                查看 MCP 服务器状态")
                print("    /mcp list           查看 MCP 服务器详情与工具列表")
                print("    /compact            手动执行全量压缩（上下文 >60% 时推荐使用）")
                print("    /clear              清空对话历史")
                print("    /help               显示此帮助")
                print("    exit                退出")
                continue
            # ---- 普通对话 ----
            agent.run(u)
        except KeyboardInterrupt:
            print("\n👋 中断")
            break
        except Exception as e:
            print(f"\n❌ {e}")

# ============================================================
# 入口

# ============================================================
if __name__ == "__main__":
    # 用法帮助（可在多处复用）
    USAGE = (
        "用法: python agent.py [参数] [问题]\n"
        "\n"
        "参数:\n"
        "  --help, -h         显示此帮助\n"
        "  --debug            开启调试日志\n"
        "  --resume <id>      恢复指定会话\n"
        "  --resume last      恢复最新会话\n"
        "  init               运行交互式初始化向导（配置 LLM/MCP/hooks）\n"
        "  gateway            启动 gateway 服务（飞书/微信消息网关）\n"
        "\n"
        "示例:\n"
        "  python agent.py                    启动交互模式\n"
        "  python agent.py init               交互式初始化配置\n"
        "  python agent.py gateway             启动消息网关服务\n"
        "  python agent.py --debug            启动交互模式（带调试）\n"
        "  python agent.py --resume a7f3e2c9  恢复指定会话\n"
        "  python agent.py --resume last      恢复最新会话\n"
        '  python agent.py "帮我看看目录"      直接提问，不进入交互模式\n'
    )
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        sys.exit(0)

    # ============================================================
    # 参数解析
    # 已知 flag 列表（以 - 开头的参数不在这个列表里就是非法的）

    # ============================================================
    KNOWN_FLAGS = {"--help", "-h", "--debug", "-debug", "--resume", "-resume"}
    debug_mode = "--debug" in sys.argv or "-debug" in sys.argv
    resume_id = None
    # 收集所有需要跳过的参数（flag 本身 + 它的值）
    skip_args = set()
    for flag in ("--resume", "-resume"):
        if flag in sys.argv:
            idx = sys.argv.index(flag)
            skip_args.add(flag)
            if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("-"):
                raw = sys.argv[idx + 1]
                skip_args.add(raw)
                if raw == "last":
                    from core.message_store import MessageStore
                    sessions = MessageStore.list_session_files()
                    if sessions:
                        resume_id = sessions[0]["session_id"]
                        print(f"📂 恢复最新会话: {resume_id}")
                    else:
                        print("❌ 没有已保存的会话")
                        sys.exit(1)
                else:
                    resume_id = raw
    # ---- gateway 子命令（需在 flag 检查前拦截，gateway 自带 --port/--dry-run 等参数）----
    non_flag_args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if non_flag_args and non_flag_args[0] == "gateway":
        gw_idx = sys.argv.index("gateway")
        gw_args = sys.argv[gw_idx + 1:]  # gateway 后的所有参数
        from gateway.cli import main as gateway_main
        sys.exit(gateway_main(gw_args, debug_mode))
    # ---- 检查非法参数 ----
    unknown_flags = [a for a in sys.argv[1:]
                     if a.startswith("-")
                     and a not in KNOWN_FLAGS
                     and a not in skip_args]
    if unknown_flags:
        print(f"❌ 未知参数: {' '.join(unknown_flags)}\n")
        print(USAGE)
        sys.exit(1)
    # ---- 剩余非 flag 参数视为问题（必须不用引号包裹，实际就是不以 - 开头） ----
    query_args = [a for a in sys.argv[1:]
                  if not a.startswith("-")
                  and a not in skip_args]
    # ---- init 子命令：交互式初始化向导（不创建 Agent，直接读写 config.json）----
    if query_args and query_args[0] == "init":
        if len(query_args) > 1:
            print(f"❌ init 子命令不接受额外参数: {' '.join(query_args[1:])}\n")
            print(USAGE)
            sys.exit(1)
        from core.init_wizard import run_init_wizard
        sys.exit(run_init_wizard())
    if query_args:
        query = " ".join(query_args)
        agent = create_agent(debug=debug_mode)
        result = agent.run(query)
        print(f"\n🤖 {agent.name}:\n{result}")
        # 单轮测试不保留会话文件
        import os
        from core.message_store import DEFAULT_SESSION_DIR
        session_file = os.path.join(DEFAULT_SESSION_DIR, f"{agent.store.session_id}.json")
        if os.path.exists(session_file):
            os.remove(session_file)
        sys.exit(0)
    else:
        start_interactive_shell(debug=debug_mode, resume_session_id=resume_id)
