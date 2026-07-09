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
from core.debug import (
    logger, setup_logging,
    set_debug, is_debug,
    log_messages, log_llm_response,
    log_tool_call, log_tool_result,
    log_info,
    enable_with_agent,
)
from core.permission import PermissionChecker, ALLOW, ASK, DENY
from tools import BaseTool, ToolRegistry
from tools.builtin_tools import register_all_tools
from tools.web_tools import register_web_tools


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
    rf"(?=\s*(?:\n(?:{TAG_ACTION}|行动)[：:]|\n(?:{TAG_FINAL}|最终回答)[：:]|$))"
    rf")?",
    re.DOTALL | re.IGNORECASE
)


def parse_react_response(response: str) -> dict:
    """
    解析 LLM 的 ReAct 回复，返回：
      { thought, actions: [{name, input}], final_answer }
    actions 可能包含多个工具调用（分批执行+合并结果）
    """
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
        max_history_tokens: int = 0,   # 上下文截断阈值（0=不截断）
        debug: bool = False,
        permission_checker: PermissionChecker = None,
    ):
        self.name = name
        self.llm = llm
        self.tool_registry = tool_registry
        self.max_steps = max_steps
        # 上下文截断阈值：0 时取模型上下文长度的一半（留一半给回复）
        if max_history_tokens == 0:
            self.max_history_tokens = max(llm.context_length // 2, 4096)
        else:
            self.max_history_tokens = max_history_tokens
        self.debug = debug
        self.permission = permission_checker or PermissionChecker()
        if debug:
            set_debug(True)
        # System Prompt
        self.system_prompt_builder = system_prompt_builder
        if system_prompt:
            # 用户提供了自定义字符串，直接使用
            self.system_prompt = system_prompt
        else:
            self.system_prompt = self._build_system_prompt()
        # 对话历史 —— 跨 run() 调用持久化，保留上下文
        self.messages: list = []

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
        当历史消息超出 max_history_tokens 时，丢弃最早的消息
        始终保留：系统提示词 + 最近的一轮对话
        """
        if self.max_history_tokens <= 0 or len(self.messages) <= 3:
            return

        total = self._count_history_tokens()
        if total <= self.max_history_tokens:
            return

        # 从最早的消息开始丢弃，保留 system + 最近至少 2 条消息
        dropped = 0
        while len(self.messages) > 3 and total > self.max_history_tokens:
            msg = self.messages.pop(1)  # 跳过 system（index 0）
            total -= self._estimate_tokens(msg.get("content", ""))
            dropped += 1

        log_info(
            f"上下文截断: {dropped} 条消息（{total} → ≤{self.max_history_tokens} tokens）"
        )

    def clear_history(self):
        """清空对话历史，但保留系统提示词"""
        self.messages = []

    def switch_llm(self, **kwargs):
        """
        运行时切换 LLM 模型，不影响对话历史和工具

        用法:
            agent.switch_llm(provider="ollama", model="gemma4")
            agent.switch_llm(model="gpt-4", base_url="https://api.openai.com", llm_type="cloud")
        """
        self.llm = HelloAgentsLLM(**kwargs)
        # 根据新模型的上下文长度自动更新截断阈值
        self.max_history_tokens = max(self.llm.context_length // 2, 4096)
        print(f"  ✅ 已切换模型: {self.llm}")
        print(f"  📐 上下文: {self.llm.context_length} tokens | 截断: {self.max_history_tokens} tokens")

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

        # ---- 首次调用时初始化对话历史 ----
        if not self.messages:
            self.messages.append({"role": "system", "content": self.system_prompt})

        # ---- 追加本次用户输入 ----
        self.messages.append({"role": "user", "content": user_input})

        # ---- 上下文截断（超出阈值时丢弃最早的历史） ----
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
                print(f"\n  ─── 第 {step}/{max_steps} 步 ───")

            # 调试：打印发送给 LLM 的消息
            log_messages(step, self.messages, f"第 {step} 步 → 发送给 LLM")

            # --- 调用 LLM ---
            try:
                response = self.llm.think(self.messages, temperature=0)
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}", exc_info=True)
                return f"❌ LLM 调用失败: {e}"

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

            # 调试：打印解析结果
            log_info(
                f"解析结果: actions={len(actions)}, "
                f"final_answer={'有' if parsed['final_answer'] else '无'}"
            )

            # --- FINAL_ANSWER（仅在没有待执行的工具时返回） ---
            if parsed["final_answer"] and not actions:
                log_info("→ 走 FINAL_ANSWER 分支")
                self.messages.append({"role": "assistant", "content": parsed["final_answer"]})
                if verbose:
                    print(f"\n  ✅ 结论（{step} 步）")
                    print(f"  🤖 {parsed['final_answer']}")
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
                            reason = f"用户已拒绝"
                            denied_actions.append((tool_name, input_str, reason))
                            print(f"  ⏭️  已拒绝")

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
                return answer

        return (
            f"⚠️ 已达最大步数（{max_steps} 步），任务可能未完成。\n"
            f"建议拆分子任务，或用 create_agent(max_steps=50) 增加上限。"
        )

    # ============================================================
    # 流式运行
    # ============================================================

    def stream_run(self, user_input: str):
        """逐步输出 Agent 的思考过程"""
        max_steps = self.max_steps
        
        # 初始化对话历史
        if not self.messages:
            self.messages.append({"role": "system", "content": self.system_prompt})
        self.messages.append({"role": "user", "content": user_input})
        
        yield f"🤖 {self.name}（最大 {max_steps} 步）\n"
        for step in range(1, max_steps + 1):
            yield f"\n── 第 {step}/{max_steps} 步 ──\n"
            
            try:
                response = self.llm.think(self.messages, temperature=0)
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}", exc_info=True)
                yield f"❌ LLM 调用失败: {e}\n"
                return
            
            if not response:
                yield "❌ LLM 调用失败\n"
                return
            parsed = parse_react_response(response)
            if parsed["thought"]:
                yield f"💭 {parsed['thought']}\n"
            
            actions = parsed.get("actions", [])
            
            # 如果有最终答案且没有待执行的工具
            if parsed["final_answer"] and not actions:
                yield f"\n✅ {parsed['final_answer']}\n"
                return
            
            # 如果有工具调用
            if actions:
                # 流式模式只执行第一个工具
                action = actions[0]
                tool_name = action["name"]
                input_str = action.get("input", "{}")
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
            else:
                # 没有工具调用，直接返回响应
                self.messages.append({"role": "assistant", "content": response.strip()})
                yield f"💬 {response}\n"
                return
        yield f"\n⚠️ 已达最大步数 {max_steps}\n"


# ============================================================
# 快速启动
# ============================================================

def create_agent(
    name: str = "helloworld agent",
    model: str = None,
    api_key: str = None,
    base_url: str = None,
    max_steps: int = 30,
    max_history_tokens: int = 0,
    debug: bool = False,
    permission: bool = True,
) -> Agent:
    """一行创建 Agent（含 read/write/edit/grep/glob/bash + search/web_fetch）"""
    # 初始化日志系统
    setup_logging(debug=debug)

    if debug:
        set_debug(True)
    llm = HelloAgentsLLM(model=model, api_key=api_key, base_url=base_url)
    registry = ToolRegistry()
    register_all_tools(registry)
    register_web_tools(registry)

    # 权限管理
    checker = PermissionChecker() if permission else None

    return Agent(
        name=name, llm=llm, tool_registry=registry,
        max_steps=max_steps,
        max_history_tokens=max_history_tokens,
        debug=debug,
        permission_checker=checker,
    )


# ============================================================
# 交互式 CLI
# ============================================================

def start_interactive_shell(debug: bool = False):
    """启动交互式命令行，支持 /model 切换模型"""
    print("\n╔════════════════════════════════════════════╗")
    print("║   🚀 HelloAgent 交互式命令行               ║")
    if debug:
        print("║   🐛 调试模式已开启                        ║")
    print("║                                              ║")
    print("║   /model        查看当前模型                 ║")
    print("║   /model list   列出支持的本地服务商          ║")
    print("║   /model ollama gemma4   切换到本地模型      ║")
    print("║   /model cloud xxx url   切换到云端模型      ║")
    print("║   /help         显示帮助                     ║")
    print("║   exit          退出                         ║")
    print("╚════════════════════════════════════════════╝")
    try:
        agent = create_agent(debug=debug)
    except ValueError as e:
        print(f"\n❌ 创建失败: {e}")
        print("   检查 .env 中 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID")
        sys.exit(1)

    # 显示当前模型和工具
    print(f"\n🤖 {agent.name}")
    print(f"📡 当前模型: {agent.llm}")
    perm_status = f"🛡️  权限管理: {'启用' if agent.permission else '关闭'}"
    print(f"📦 {agent.tool_registry.count()} 个工具 | {perm_status}")
    for t in agent.tool_registry.list_tools():
        print(f"   ✅ {t.name}")

    while True:
        try:
            u = input("\n👤 你: ").strip()
            if not u:
                continue

            # ---- 退出 ----
            if u.lower() in ("exit", "quit", "q", "退出"):
                print("👋 再见！")
                break

            # ---- /model 命令 ----
            if u.startswith("/model"):
                parts = u.split()
                cmd = parts[1] if len(parts) > 1 else ""

                if cmd == "list" or cmd == "ls":
                    from core.llm_client import LOCAL_PROVIDERS
                    print(f"\n  本地服务商:")
                    for k, v in LOCAL_PROVIDERS.items():
                        print(f"    {k:12s} → {v['base_url']}")

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

            # ---- /clear ----
            if u.startswith("/clear"):
                agent.clear_history()
                print(f"  🗑️  对话历史已清空")
                continue

            # ---- /help ----
            if u.startswith("/help"):
                print("\n  命令:")
                print("    /model              查看当前模型")
                print("    /model list         列出可用服务商")
                print("    /model ollama xxx   切换本地模型")
                print("    /model cloud xxx    切换云端模型")
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    debug_mode = "--debug" in flags

    if args:
        query = " ".join(args)
        agent = create_agent(debug=debug_mode)
        result = agent.run(query)
        print(f"\n🤖 {agent.name}:\n{result}")
    else:
        start_interactive_shell(debug=debug_mode)
