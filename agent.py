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

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import logging

from core import HelloAgentsLLM, SystemPrompt
from core.llm_client import detect_context_length
from core.compressor import Compressor
from core.task_list import TaskList
from core.debug import (
    logger, setup_logging,
    set_debug, is_debug,
    log_messages, log_llm_response,
    log_tool_call, log_tool_result,
    log_info,
    enable_with_agent,
)
from core.message_store import MessageStore
from core.permission import PermissionChecker, ALLOW, ASK, DENY
from tools import BaseTool, ToolRegistry
from tools.builtin_tools import register_all_tools
from tools.web_tools import register_web_tools
from memory import MemoryManager

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
# 正则匹配 ACTION + INPUT（用 findall 捕获全部，不区分大小写）
# INPUT 使用前瞻边界匹配，支持嵌套 JSON
_ACTION_RE = re.compile(
    rf"(?:{TAG_ACTION}|行动)[：:]\s*\[?(\w+)\]?\s*(?:\n|$)"
    rf"(?:\s*(?:{TAG_INPUT}|输入)[：:]\s*"
    rf"(.+?)"
    rf"(?=\s*(?:\n(?:{TAG_ACTION}|行动)[：:]|\n(?:{TAG_FINAL}|最终回答)[：:]|\n(?:{TAG_THOUGHT}|思考)[：:]|$))"
    rf")?",
    re.DOTALL | re.IGNORECASE
)


def _normalize_tags(text: str) -> str:
    """

    将 XML 风格标签标准化为 ReAct 解析器支持的格式
    处理格式：
      <THOUGHT>...</THOUGHT>    →  THOUGHT：...
      <ACTION>...</ACTION>      →  ACTION：...
      <INPUT>...</INPUT>        →  INPUT：...
      <FINAL_ANSWER>...</FINAL_ANSWER>  →  FINAL_ANSWER：...
    同时清理模型生成的特殊 token 标记（如 <|im_end|> 等），
    防止混入工具参数导致 JSON 解析失败。
    """

    # 清理特殊 token 标记
    text = re.sub(r"<\|[\w_]+\|>", "", text)
    # 标准化 XML 风格标签
    text = re.sub(
        r"<\s*THOUGHT\s*>\s*(.*?)\s*<\s*/\s*THOUGHT\s*>",
        r"THOUGHT：\1",
        text, flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"<\s*ACTION\s*>\s*(.*?)\s*<\s*/\s*ACTION\s*>",
        r"ACTION：\1",
        text, flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"<\s*INPUT\s*>\s*(.*?)\s*<\s*/\s*INPUT\s*>",
        r"INPUT：\1",
        text, flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"<\s*FINAL_ANSWER\s*>\s*(.*?)\s*<\s*/\s*FINAL_ANSWER\s*>",
        r"FINAL_ANSWER：\1",
        text, flags=re.DOTALL | re.IGNORECASE,
    )
    return text.strip()
def parse_react_response(response: str) -> dict:
    """

    解析 LLM 的 ReAct 回复，返回：
      { thought, actions: [{name, input}], final_answer }
    actions 可能包含多个工具调用（分批执行+合并结果）
    """

    # 0. 标准化标签格式（支持 XML 风格的 <THOUGHT> 等标签）
    response = _normalize_tags(response)
    # 1. FINAL_ANSWER
    final_answer = None
    m = re.search(
        rf"(?:{TAG_FINAL}|最终回答)[：:]\s*(.*)",
        response, re.DOTALL | re.IGNORECASE
    )
    if m:
        final_answer = m.group(1).strip().rstrip('\n')
    # 2. 所有 THOUGHT
    thought = None
    m = re.search(
        rf"(?:{TAG_THOUGHT}|思考)[：:]\s*(.*?)"
        rf"(?=\n*(?:{TAG_ACTION}|行动)[：:]|\n*(?:{TAG_FINAL}|最终回答)[：:]|$)",
        response, re.DOTALL
    )
    if m:
        thought = m.group(1).strip()
    # 3. 所有 ACTION + INPUT（支持多个）
    actions = []
    for name, input_str in _ACTION_RE.findall(response):
        actions.append({"name": name.strip(), "input": (input_str or "{}").strip()})
    return {
        "thought": thought,
        "actions": actions,
        "final_answer": final_answer,
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
        llm: HelloAgentsLLM,
        tool_registry: ToolRegistry,
        system_prompt: str = None,
        system_prompt_builder: SystemPrompt = None,
        max_steps: int = 50,           # 最大 ReAct 循环步数
        max_history_tokens: int = 0,   # 上下文预算阈值（0=自动：取 context_length/2）
        debug: bool = False,
        permission_checker: PermissionChecker = None,
        memory: MemoryManager = None,  # 跨会话记忆系统（可选）
        mcp_servers: list = None,      # MCP 服务器配置列表（可选）
    ):
        self.name = name
        self.llm = llm
        self.tool_registry = tool_registry
        self.max_steps = max_steps
        # 上下文预算阈值：0 时取模型上下文长度的一半（留一半给输出）
        if max_history_tokens == 0:
            self.max_history_tokens = max(llm.context_length // 2, 4096)
        else:
            self.max_history_tokens = max_history_tokens
        self.debug = debug
        self.permission = permission_checker or PermissionChecker()
        self.memory = memory  # 跨会话记忆系统
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

    @staticmethod

    def _estimate_tokens(text: str) -> int:
        """

        粗略估算文本的 token 数量
        中文约 1.5 字/token，英文约 4 字符/token
        """

        if not text:
            return 0
        chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
        other_chars = len(text) - chinese_chars
        return int(chinese_chars / 1.5 + other_chars / 4)

    def _count_history_tokens(self) -> int:
        """统计当前历史消息的总 token 数"""

        total = 0
        for msg in self.messages:
            total += self._estimate_tokens(msg.get("content", ""))
        return total

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
            self.store.save_session()
        return ok

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

    def clear_history(self):
        """清空对话历史，但保留系统提示词"""

        self.store.clear()
        # 锚点失效——清空历史后 next-think 重新校准
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

        self.llm = HelloAgentsLLM(**kwargs)
        # 根据新模型的上下文长度重新计算压缩阈值
        self.max_history_tokens = max(self.llm.context_length // 2, 4096)
        # 同步 store 的阈值和模型配置（/stats 显示用的 store.max_tokens）
        self.store.max_tokens = self.max_history_tokens
        self.store.model_id = self.llm.model or ""
        self.store.model_provider = getattr(self.llm, "provider", "") or ""
        self.store.model_base_url = getattr(self.llm, "base_url", "") or ""
        self.store.model_llm_type = getattr(self.llm, "llm_type", "") or ""
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

    def _build_system_prompt(self) -> str:
        """使用 SystemPrompt 构建器生成带静态区和动态区的提示词"""

        tool_descs = self.tool_registry.get_tool_descriptions()
        # 创建构建器（如果外部没有传入）
        builder = self.system_prompt_builder or SystemPrompt(name=self.name)
        builder.set_project_root(os.getcwd())
        return builder.build(tool_descs=tool_descs)

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
        if not self._mcp_pending_init:
            return

        import asyncio
        from core.mcp_client import MCPClientManager
        from tools.mcp_tools import MCPTool

        configs = self._mcp_pending_init
        self._mcp_pending_init = None  # 避免重复初始化

        # 使用 asyncio.run() 执行异步初始化
        asyncio.run(self._async_init_mcp(configs, MCPClientManager, MCPTool))

    async def _async_init_mcp(self, configs, MCPClientManager_cls, MCPTool_cls):
        """异步初始化 MCP 连接（由 _init_mcp_if_needed 调用）"""
        self.mcp_manager = MCPClientManager_cls()

        # 1. 添加所有服务器配置
        for cfg in configs:
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
                mcp_tool = MCPTool_cls(connection=conn, tool_desc=prefixed_desc)
                try:
                    self.tool_registry.register_tool(mcp_tool)
                    self._mcp_tool_names.append(mcp_tool.name)
                    logger.info(f"注册 MCP 工具: {mcp_tool.name}")
                except ValueError as e:
                    logger.warning(
                        f"注册 MCP 工具失败 '{mcp_tool.name}': {e}"
                    )

        # 4. 重建 System Prompt 以包含 MCP 工具描述
        if self._mcp_tool_names:
            self.system_prompt = self._build_system_prompt()
            # 如果对话已开始（已有 system 消息），更新它
            if self.messages and self.messages[0].get("role") == "system":
                self.messages[0]["content"] = self.system_prompt
            logger.info(
                f"MCP 初始化完成: {len(self._mcp_tool_names)} 个工具已注册"
            )

    def add_instruction(self, instruction: str) -> None:
        """

        向 System Prompt 动态区添加额外指令。
        参数:
            instruction: 指令文本，如 "本次对话请使用英文回复"
        """

        # 获取或创建 builder
        builder = self.system_prompt_builder or SystemPrompt(name=self.name)
        builder.add_session_instruction(instruction)
        builder.set_project_root(os.getcwd())
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
            f"信息足够 → FINAL_ANSWER；需要更多 → 继续 ACTION + INPUT）"
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
                     "信息足够 → FINAL_ANSWER；需要更多 → 继续 ACTION + INPUT")
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

    # ============================================================
    # 核心运行方法

    # ============================================================
    # ============================================================
    # 对话历史管理

    # ============================================================

    def run(self, user_input: str, verbose: bool = True) -> str:
        """

        执行完整的 ReAct 循环
        对话历史跨 run() 调用保留，每次的新输入会追加到历史中。
        用 agent.clear_history() 清空历史。
        消息流:
          user → assistant(tool_use) → user(tool_result) → assistant → ...
        """

        max_steps = self.max_steps
        # ---- 首次运行时初始化 MCP 连接（如果有配置） ----
        self._init_mcp_if_needed()
        # ---- 首次调用时初始化对话历史 ----
        if not self.messages:
            self.messages.append({"role": "system", "content": self.system_prompt})
        # ---- 轻量压缩：先压缩旧的工具结果，减少上下文占用 ----
        self._light_compress()
        # ---- 上下文检查：超阈值时自动压缩或提示 ----
        self._check_context(verbose=verbose)
        # ---- 追加本次用户输入 ----
        self.messages.append({"role": "user", "content": user_input})
        # ---- 紧急截断（压缩仍超阈值时，丢弃最早的消息兜底） ----
        self._truncate_history()
        # 调试：打印当前消息列表
        log_messages(0, self.messages, f"[对话历史] 共 {len(self.messages)} 条，追加用户输入")
        if verbose:
            print(f"\n{'='*55}")
            print(f"  🤖 {self.name}（最多 {max_steps} 步 | 历史 {len(self.messages)} 条）")
            print(f"{'='*55}")
            print(f"  👤 {user_input}")

        for step in range(1, max_steps + 1):

            if verbose:
                task_tag = ""
                if self._task_list:
                    current = self._task_list.get_current()
                    if current:
                        task_tag = f" | 任务 {current.id}/{self._task_list.total}: {current.description[:40]}"
                print(f"\n  ─── 第 {step}/{max_steps} 步{task_tag} ───")
            # 调试：打印发送给 LLM 的消息
            log_messages(step, self.messages, f"第 {step} 步 → 发送给 LLM")
            # --- 调用 LLM ---
            try:
                response = self.llm.think(self.messages, temperature=0)
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}", exc_info=True)
                return f"❌ LLM 调用失败: {e}"
            # 用 API 返回的 usage 校准上下文计数器（锚点）
            self.store.set_anchor(self.llm.last_usage)
            if not response:
                logger.warning("LLM 返回空响应")
                return "❌ LLM 调用失败"
            # 调试：打印 LLM 返回
            log_llm_response(step, response)
            if verbose:
                m = _THOUGHT_RE.search(response)
                if m:
                    print(f"  💭 {m.group(1).strip()[:200]}")
            parsed = parse_react_response(response)
            actions = parsed.get("actions", [])
            # ---- PLAN 检测（仅第一轮，无任务清单时） ----
            if step == 1 and not self._task_list:
                plan_match = re.search(
                    r"PLAN[：:]\s*(\S[\s\S]*?)(?=\n*(?:THOUGHT|ACTION|FINAL_ANSWER)|\Z)",
                    response, re.IGNORECASE,
                )
                if plan_match:
                    plan_text = plan_match.group(1).strip()
                    if self._handle_plan(plan_text, verbose=verbose):
                        cleaned = re.sub(
                            r"PLAN[：:].*", "", response,
                            flags=re.DOTALL | re.IGNORECASE,
                        ).strip()
                        if cleaned:
                            self.messages.append({"role": "assistant", "content": cleaned})
                        self.store.save_session()
                        # 方案已确认，跳出主循环，进入任务执行阶段
                        if verbose:
                            print('  🔀 方案已确认，进入任务执行阶段')
                        break
                    else:
                        return "用户取消方案，请继续讨论或输入 /plan 重新规划。"
            # ---- COMPLETE_TASK 检测 ----
            complete_match = re.search(
                r"COMPLETE_TASK[：:]\s*(\d+)\s*(.*)", response, re.IGNORECASE
            )
            if complete_match and self._task_list:
                task_id = int(complete_match.group(1))
                result = complete_match.group(2).strip()
                has_next = self._task_list.mark_done(task_id, result)
                self._update_task_list_in_prompt()
                self.store.save_session()
                if verbose:
                    print(f"  ✅ 任务 {task_id}/{self._task_list.total} 完成 → {result or '已完成'}")
                if self._task_list.is_all_done():
                    if verbose:
                        print(f"  🎉 全部 {self._task_list.total} 个任务完成！")
                    break
                continue
            # 调试：打印解析结果
            log_info(
                f"解析结果: actions={len(actions)}, "
                f"final_answer={'有' if parsed['final_answer'] else '无'}"
            )
            if parsed["final_answer"] and not actions:
                log_info("→ 走 FINAL_ANSWER 分支")
                self.messages.append({"role": "assistant", "content": parsed["final_answer"]})
                if verbose:
                    print(f"\n  ✅ 结论（{step} 步）")
                    print(f"  🤖 {parsed['final_answer']}")
                self.store.save_session()
                self._save_memory(user_input)
                # 任务清单模式：标记当前任务完成，检查是否全部完成
                if self._task_list:
                    current = self._task_list.get_current()
                    if current:
                        self._task_list.mark_done(current.id, parsed["final_answer"][:200])
                        self._update_task_list_in_prompt()
                        self.store.save_session()
                        if verbose:
                            print(f"  ✅ 任务 {current.id}/{self._task_list.total} 完成")
                    if not self._task_list.is_all_done():
                        # 还有任务未完成，继续下一轮（不 return）
                        # 下一轮循环开始时，会通过下面的 _execute_next_task 发送新任务
                        continue
                    else:
                        break
                return parsed["final_answer"]
            # --- 批量 ACTION（支持多个工具并发执行） ---
            if actions:
                log_info(f"→ 走 ACTIONS 分支: {len(actions)} 个工具")
                n = len(actions)
                if verbose:
                    names = [a["name"] for a in actions]
                    print(f"  🛠️  {TAG_ACTION}({n}): {', '.join(names)}")
                # 调试：打印每个工具调用
                for a in actions:
                    log_tool_call(step, a["name"], a["input"])

                # ===== 权限检查：对每个工具做 allow/ask/deny 判断 =====
                checked_actions = []  # 通过检查的：[(name, input)]
                denied_actions = []   # 被拒绝的：[(name, input, reason)]
                for a in actions:
                    tool_name = a["name"]
                    input_str = a["input"]
                    try:
                        params = json.loads(input_str) if input_str else {}
                    except json.JSONDecodeError:
                        params = {}
                    # 非 dict 的 INPUT（如 JSON 数组）→ LLM 传错格式，走拒绝并给提示
                    if not isinstance(params, dict):
                        reason = (
                            f"INPUT 必须是 JSON 对象（如 {{\"action\": \"delete\", \"path\": \"...\"}}），"
                            f"但收到了 JSON {type(params).__name__}，长度 {len(params)}。"
                        )
                        denied_actions.append((tool_name, input_str, reason))
                        continue
                    level = self.permission.check(tool_name, params)
                    if level == ALLOW:
                        checked_actions.append(a)
                    elif level == DENY:
                        reason = f"权限不足，操作已被系统拒绝"
                        denied_actions.append((tool_name, input_str, reason))
                        print(f"  ⛔ {tool_name}: {reason}")
                    elif level == ASK:
                        # 显示要执行的操作，等待用户确认
                        print(f"\n  ❓ 需要确认: {tool_name}")
                        for k, v in params.items():
                            v_str = str(v)[:120]
                            print(f"     {k}: {v_str}")
                        print(f"  ─────────────────────────────")
                        print(f"  A = 本次会话工作区内全部放行")
                        print(f"  Y = 允许本次操作")
                        print(f"  N = 拒绝本次操作")
                        print(f"  S = 跳过本次操作")
                        prompt_text = input(f"  请选择 [A/Y/N/S] (默认Y) ").strip().lower()
                        if prompt_text == "a":
                            # 工作区全放行
                            self.permission.allow_workspace()
                            checked_actions.append(a)
                            print(f"  ✅ 工作区内操作已全部放行（本会话有效）")
                        elif prompt_text in ("", "y", "yes", "是"):
                            print(f"  ✅ 已允许")
                            checked_actions.append(a)
                        elif prompt_text == "s":
                            reason = f"用户选择跳过"
                            denied_actions.append((tool_name, input_str, reason))
                            print(f"  ⏭️  已跳过")
                        else:
                            # N = 拒绝并结束本轮对话
                            print(f"  ⛔ 用户已拒绝操作，结束本次会话")
                            return f"操作已取消：用户拒绝了 {tool_name}，对话已终止。"

                # ===== 并发执行通过权限检查的工具 =====
                results = []
                if checked_actions:
                    m = len(checked_actions)
                    with ThreadPoolExecutor(max_workers=min(m, 5)) as pool:
                        future_map = {
                            pool.submit(self._execute_tool, a["name"], a["input"]): a
                            for a in checked_actions
                        }
                        for future in as_completed(future_map):
                            action = future_map[future]
                            try:
                                obs = future.result()
                                is_error = obs.startswith("❌")
                            except Exception as e:
                                obs = f"❌ 工具执行异常: {type(e).__name__}: {e}"
                                is_error = True
                            results.append((action["name"], action["input"], obs, is_error))

                # ===== 被拒绝的也加入结果 =====
                for tool_name, input_str, reason in denied_actions:
                    results.append((tool_name, input_str, f"⏭️ 跳过: {reason}", True))
                # 统计成功/失败
                ok_count = sum(1 for _, _, _, err in results if not err)
                fail_count = sum(1 for _, _, _, err in results if err)
                # 调试：打印每个工具返回
                for name, _, obs, _ in results:
                    log_tool_result(step, name, obs)
                if verbose:
                    status = f"✅ {ok_count} 成功" if ok_count else ""
                    if fail_count:
                        status += f"，❌ {fail_count} 失败" if status else f"❌ {fail_count} 失败"
                    print(f"  📊 执行完毕: {status}")
                    for name, _, obs, is_err in results:
                        short = obs[:200].replace("\n", " ")
                        prefix = "❌" if is_err else "✅"
                        print(f"    {prefix} {name} → {short}{'...' if len(obs) > 200 else ''}")

                # ====== 只添加一条 assistant + 一条合并的 tool_result ======
                self.messages.append({"role": "assistant", "content": response})
                self.messages.append({
                    "role": "user",
                    "name": "tool_result",
                    "content": self._combine_results(results),
                })
            else:
                log_info("→ 走 ELSE 分支（无 Action 无 FinalAnswer）")
                if verbose:
                    print(f"  💬 直接回复（无标签）")
                # 无标签时仍将 LLM 回复作为最终答案返回
                answer = response.strip()
                self.messages.append({"role": "assistant", "content": answer})
                if verbose:
                    print(f"  🤖 {answer}")
                self.store.save_session()
                self._save_memory(user_input)
                # 任务清单模式：标记当前任务完成，推进下一个
                if self._task_list:
                    current = self._task_list.get_current()
                    if current:
                        self._task_list.mark_done(current.id, answer[:200])
                        self._update_task_list_in_prompt()
                        self.store.save_session()
                        if verbose:
                            print(f"  ✅ 任务 {current.id}/{self._task_list.total} 完成")
                return answer

        # ---- Phase 2: 任务清单执行 ----
        if self._task_list:
            return self._run_task_list(user_input, max_steps, verbose)

        self.store.save_session()
        self._save_memory(user_input)
        return (
            f"⚠️ 已达最大步数（{max_steps} 步），任务可能未完成。\n"
            f"建议拆分子任务，或用 create_agent(max_steps=50) 增加上限。"
        )

    # ============================================================
    # 流式运行

    # ============================================================


    def _run_task_list(self, user_input, max_steps, verbose):
        """Phase 2: 任务清单模式 —— 框架主动推进，逐任务执行"""
        # plan 模式：自动信任工作区，区内操作免确认，区外仍需审批
        self.permission.allow_workspace()
        while not self._task_list.is_all_done():
            current = self._task_list.get_current()
            if not current:
                break
            task_prompt = (
                f"## 请执行任务 {current.id}/{self._task_list.total}：{current.description}\n"
                f"执行此任务，完成后输出 FINAL_ANSWER。"
            )
            self.messages.append({"role": "user", "content": task_prompt})
            if verbose:
                print(f"\n  📋 任务 {current.id}/{self._task_list.total}: {current.description}")

            task_done = False
            for t_step in range(1, max_steps + 1):
                if verbose:
                    print(f"\n  ─── 任务 {current.id} 第 {t_step} 步 ───")
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
                        level = self.permission.check(tool_name, params)
                        if level == ALLOW:
                            checked_acts.append(a)
                        elif level == DENY:
                            reason = "权限不足，操作已被系统拒绝"
                            denied_acts.append((tool_name, input_str, reason))
                            if verbose:
                                print(f"  ⛔ {tool_name}: {reason}")
                        elif level == ASK:
                            if verbose:
                                print(f"\n  ❓ 需要确认: {tool_name}")
                                for k, v in params.items():
                                    print(f"     {k}: {str(v)[:120]}")
                            choice = input(f"  请选择 [A/Y/N/S] (默认Y) ").strip().lower()
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
                    self.messages.append({
                        "role": "user", "name": "tool_result",
                        "content": self._combine_results(t_results),
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
                break
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

    def stream_run(self, user_input: str):
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
                action = actions[0]
                tool_name = action["name"]
                input_str = action.get("input", "{}")
                try:
                    params = json.loads(input_str) if input_str else {}
                except json.JSONDecodeError:
                    params = {}
                if not isinstance(params, dict):
                    params = {}
                level = self.permission.check(tool_name, params)
                if level == DENY:
                    observation = "⏭️ 跳过: 权限不足，操作已被系统拒绝"
                elif level == ASK:
                    observation = "⏭️ 跳过: 工作区外操作需在交互模式下确认"
                else:
                    yield f"🛠️  {TAG_ACTION}: {tool_name}\n"
                    try:
                        observation = self._execute_tool(tool_name, input_str)
                    except Exception as e:
                        logger.error(f"工具执行失败: {e}", exc_info=True)
                        observation = f"工具执行失败: {e}"
                yield f"📊 {observation[:500]}\n"
                self.messages.append({"role": "assistant", "content": response})
                self.messages.append({
                    "role": "user",
                    "name": "tool_result",
                    "content": self._format_tool_result(tool_name, input_str, observation),
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


# ============================================================
# MCP 配置加载

# ============================================================


def _load_mcp_config(mcp_servers: list = None) -> list:
    """
    自动加载 MCP 配置，优先级：
    1. 显式传入的 mcp_servers 参数
    2. config/mcp_config.json 文件
    3. 项目根目录的 mcp_config.json 文件（兼容旧路径）

    参数:
        mcp_servers: 代码传入的配置（最高优先级）
    返回:
        MCP 服务器配置列表，无配置时返回 None
    """
    # 优先级 1：显式传入
    if mcp_servers is not None:
        return mcp_servers

    # 优先级 2：config/mcp_config.json
    config_paths = [
        os.path.join(os.getcwd(), "config", "mcp_config.json"),
        os.path.join(os.getcwd(), "mcp_config.json"),  # 旧路径兼容
    ]
    for config_path in config_paths:
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    parsed = json.load(f)
                if isinstance(parsed, list):
                    rel = os.path.relpath(config_path)
                    logger.info(f"从 {rel} 加载了 {len(parsed)} 个 MCP 服务器配置")
                    return parsed
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"{config_path} 读取失败: {e}")

    return None


# ============================================================
# 快速启动

# ============================================================


def create_agent(
    name: str = "helloworld agent",
    model: str = None,
    api_key: str = None,
    base_url: str = None,
    provider: str = None,
    max_steps: int = 30,
    max_history_tokens: int = 0,
    debug: bool = False,
    permission: bool = True,
    memory: bool = True,
    mcp_servers: list = None,    # MCP 服务器配置列表（可选）
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
        mcp_servers: MCP 服务器配置列表，每项含 name/transport/command 等
    """

    setup_logging(debug=debug)
    if debug:
        set_debug(True)
    llm = HelloAgentsLLM(model=model, api_key=api_key, base_url=base_url, provider=provider)
    # 记忆系统（跨会话）
    memory_manager = MemoryManager() if memory else None
    registry = ToolRegistry()
    register_all_tools(registry, memory_manager=memory_manager)
    register_web_tools(registry)
    # 权限管理
    checker = PermissionChecker() if permission else None
    # MCP 配置：代码参数 > .env > mcp_config.json
    mcp_servers = _load_mcp_config(mcp_servers)
    return Agent(
        name=name, llm=llm, tool_registry=registry,
        max_steps=max_steps,
        max_history_tokens=max_history_tokens,
        debug=debug,
        permission_checker=checker,
        memory=memory_manager,
        mcp_servers=mcp_servers,
    )

# ============================================================
# 交互式 CLI

# ============================================================


def start_interactive_shell(debug: bool = False, resume_session_id: str = None):
    """启动交互式命令行，支持 /model 切换模型"""

    print("\n╔═══════════════════════════════════════════════╗")
    print("║   🚀 HelloAgent 交互式命令行                  ║")
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
    print("║   /stats             查看上下文占用             ║")
    print("║   /history             查看当前会话内容             ║")
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
            print("   检查 .env 中 LLM_MODEL_ID / LLM_CLOUD_API_KEY / LLM_CLOUD_BASE_URL")
            sys.exit(1)
    # 显示当前状态
    print(f"\n🤖 {agent.name}")
    print(f"📡 当前模型: {agent.llm}  |  session: {agent.store.session_id}")
    perm_status = f"🛡️  权限管理: {'启用' if agent.permission else '关闭'}"
    print(f"📦 {agent.tool_registry.count()} 个工具就绪 | {perm_status}")
    while True:
        try:
            u = input("\n👤 你: ").strip()
            if not u:
                continue
            # ---- 退出 ----
            if u.lower() in ("exit", "quit", "q", "退出"):
                # 清理 MCP 连接（关闭 aiohttp session 和子进程）
                if hasattr(agent, 'mcp_manager') and agent.mcp_manager:
                    import asyncio
                    try:
                        asyncio.run(agent.mcp_manager.close_all())
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
                    content = msg.get("content", "")
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
                plan_prompt = (
                    "请分析以下任务需求，输出一个分步骤的执行方案。\n\n"
                    "格式要求：\n"
                    "PLAN：\n"
                    "[1] 步骤1描述\n"
                    "[2] 步骤2描述\n"
                    "...（分步骤列出，5-8 步为宜）\n\n"
                    "只需要输出 PLAN 部分，不要执行任何工具。"
                )
                plan_response = agent.llm.think(
                    agent.messages + [{"role": "user", "content": plan_prompt}],
                    temperature=0.3,
                    stream=False,
                    silent=True,
                )
                if plan_response:
                    plan_match = re.search(
                        r"PLAN[：:]\s*(\S[\s\S]*?)(?:\Z|(?=\n*(?:THOUGHT|ACTION|FINAL_ANSWER)))",
                        plan_response, re.IGNORECASE | re.DOTALL,
                    )
                    if plan_match:
                        plan_text = plan_match.group(1).strip()
                        if agent._handle_plan(plan_text, verbose=True):
                            # 用户确认 → 进入任务执行阶段
                            result = agent._run_task_list(
                                user_input=plan_input or "plan execution",
                                max_steps=agent.max_steps,
                                verbose=True,
                            )
                            print(f"\n  🤖 {result}")
                    else:
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
                    print("    2. 在 config/mcp_config.json 中配置")
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
        "\n"
        "示例:\n"
        "  python agent.py                    启动交互模式\n"
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