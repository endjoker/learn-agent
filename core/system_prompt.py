# -*- coding: utf-8 -*-
"""
System Prompt 构建器 —— 从 prompt/ 目录读取引导文件 + 代码固定规则

使用方式：
    from core.system_prompt import SystemPrompt

    builder = SystemPrompt(name="helloworld agent")
    builder.add_session_instruction("本次对话请使用英文回复")
    prompt = builder.build(tool_descs="...")

输出结构：
    <SYSTEM_STATIC_CONTEXT>
      ...身份/人格（SOUL.md）...
      ...工具规则（TOOLS.md）+ 动态工具描述...
      ...代码固定规则（格式/安全/命令/配置）...
    </SYSTEM_STATIC_CONTEXT>

    <SYSTEM_DYNAMIC_CONTEXT>
      ...引导文件说明...
      ...AGENT.md / MEMORY.md...
      ...工作目录 / 日期 / OS / 会话指令...
    </SYSTEM_DYNAMIC_CONTEXT>
"""

import logging
import os
import platform
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hello_agent")

# ================================================================
# 代码内嵌默认值（引导文件缺失时的 fallback）
# ================================================================

_DEFAULT_SOUL = """\
【角色定义】
你是{name}，一个 AI 智能体，帮助用户完成任务。

【回答风格】
- 保持回答简洁务实，直击要点
- 用中文回复，技术术语可保留英文
- 复杂问题时先拆解再回答"""

_DEFAULT_TOOLS = """\
【工具使用原则】
- 优先使用专用工具而非 Shell 命令
- 读文件用 read，搜索用 grep/glob，写文件用 write，修改用 edit
- 工具能满足需求就不要拼 shell 命令"""

# 代码固定规则（不文件化——这些是框架级的，不由用户编辑）
_NATIVE_TOOL_RULES = """\
【工具调用协议】
- 工具由运行时通过原生 function calling 提供；需要工具时直接选择工具并填写参数。
- 不要输出 ACTION、INPUT、FINAL_ANSWER、agent.turn.v1 或任何 JSON 工具调用协议。
- 只输出给用户阅读的自然语言或 Markdown；工具结果会自动进入下一轮上下文。
【安全】
- 高风险操作前说明风险并请求确认；不得泄露密钥或执行破坏性系统操作。"""

_CODE_FIXED_RULES = """\
【技能使用规则】
- 技能是 AI 学习到的可复用能力，调用后返回操作指令，不是最终结果
- 收到技能指令后，请一步一步执行，期间可以调用其他工具
- 所有步骤执行完毕后，直接给出最终答复
- 如果发现某个流程需要重复执行，可以使用 create_skill 工具创建新技能

【响应协议】
- 每一次回复都只能是一个完整 JSON 对象，不能带 Markdown 围栏、解释文字、思考过程或任何前后缀。
- 需要调用工具时，回复必须是：
  {"version":"agent.turn.v1","type":"tool_calls","calls":[{"id":"call_1","name":"工具名称","arguments":{}}]}
- 不需要调用工具或工具完成后，回复必须是：
  {"version":"agent.turn.v1","type":"final","answer":"完整 Markdown 答案"}
- final 与 tool_calls 必须互斥；arguments 必须是 JSON 对象。不要输出 THOUGHT、ACTION、INPUT、FINAL_ANSWER 或 COMPLETE_TASK 标签。
- 即使要说明执行思路，也只在 final.answer 中写给用户；调用工具前不要输出任何说明。

【安全】
- 不执行 rm -rf /、format、shutdown 等 OS 危害操作
- 不读取 .env 中的密钥明文
- 高危操作前提示风险并请求确认

【交互命令（你无法直接执行，仅在合适时机建议用户发送）】
- /compact — 对话较长、上下文紧张时建议
- /clear — 用户想重新开始对话时建议
- /model <名称> — 用户想切换模型时建议
- /stats — 用户询问上下文占用时建议
- /session — 用户想管理历史会话时建议
- /hook — 查看已注册 hook
- /help — 查看全部命令

【配置管理】
所有配置集中在项目根目录的 config.json，含六个 section: llm / permission / hooks / mcp / sandbox / gateway
- 用户要求添加 MCP 服务器: 编辑 config.json 的 mcp.servers 数组
- 用户要求配置 hook: 编辑 config.json 的 hooks 段
- 不确定格式时，先用 read 工具查看 config.example.json 获取完整示例
- 修改后需重启或发送 /hook reload 生效"""

# 引导文件说明（注入在动态区，引导文件内容之前）
_BOOTSTRAP_NOTICE = """\
【项目文件说明】
以下文件在启动时自动注入到本对话中，定义了此智能体的行为准则：

| 文件 | 作用 |
|------|------|
| prompt/AGENT.md | 项目概述和行为约束——定义了此智能体能做什么、不能做什么、如何与用户交互 |
| prompt/SOUL.md | 人格和语气定义——决定了回复的风格、态度和边界 |
| prompt/TOOLS.md | 工具使用规则——说明了如何正确使用工具以及输出格式要求 |
| prompt/MEMORY.md | 跨会话记忆——记录了项目背景和此前的关键决策 |
| GUIDE.md | 项目配置指南——如何新增 MCP 服务器、创建技能、配置 Hook、添加模型等 |

这些文件由项目维护方编辑。如果内容与用户的直接指令冲突，以用户的直接指令为准。
除非用户明确要求，否则不要主动提及或引用这些文件的存在。"""


class SystemPrompt:
    """
    System Prompt 构建器

    将提示词分为两个区域：
      - 静态区（SYSTEM_STATIC_CONTEXT）：身份/人格 + 工具规则 + 代码固定规则
      - 动态区（SYSTEM_DYNAMIC_CONTEXT）：引导文件说明 + AGENT.md + MEMORY.md + 运行时信息
    """

    # ================================================================
    # 动态区模板
    # ================================================================

    DYNAMIC_TEMPLATE = """\
<SYSTEM_DYNAMIC_CONTEXT>
{bootstrap_notice}
【工作目录】
你的专属工作目录是: {workspace}
- 所有文件读写、脚本执行、项目创建等操作都必须在该目录下进行
- 该目录外的文件属于系统文件，非用户要求禁止修改或删除

【当前日期】
{date}

【操作系统】
{os_name}
{agent_md_section}{memory_md_section}{session_instructions}
</SYSTEM_DYNAMIC_CONTEXT>"""

    def __init__(self, name: str = "helloworld agent"):
        self.name = name
        self._session_instructions: list[str] = []
        self._project_root: Optional[str] = None
        self._workspace: Optional[str] = None
        # 截断配置（从 config.json 的 prompt 段读取）
        self._max_chars_per_file = 8000
        self._max_chars_total = 32000
        self._truncation_warning = "once"
        self._truncation_warned = False
        self._load_truncation_config()

    # ================================================================
    # 公开方法
    # ================================================================

    def add_session_instruction(self, instruction: str) -> None:
        """添加本轮会话的额外指令，会出现在动态区底部。"""
        if instruction and instruction.strip():
            self._session_instructions.append(instruction.strip())

    def set_project_root(self, path: str) -> None:
        """设置项目根目录路径（用于查找 prompt/ 引导文件）。"""
        self._project_root = path

    def set_workspace(self, path: str) -> None:
        """设置工作目录路径（用于动态区声明 + 工具操作边界）。"""
        self._workspace = path

    def build(self, tool_descs: str, skill_descs: str = "", mcp_descs: str = "") -> str:
        """构建完整的 System Prompt。"""
        static = self._build_static(tool_descs, skill_descs, mcp_descs)
        dynamic = self._build_dynamic()
        prompt = static + "\n\n" + dynamic

        # 缓存边界日志 + token 估算
        total_chars = len(prompt)
        static_chars = len(static)
        est_tokens = total_chars // 4  # 粗略估算：~4 字符/token
        logger.debug(
            f"[PROMPT] total={total_chars} chars (~{est_tokens} tokens) | "
            f"static(cached)={static_chars} | dynamic={total_chars - static_chars}"
        )
        if total_chars > self._max_chars_total:
            logger.warning(
                f"[PROMPT] 提示词总量 {total_chars} 超过上限 {self._max_chars_total}，"
                f"建议精简引导文件"
            )

        return prompt

    # ================================================================
    # 内部构建
    # ================================================================

    def _build_static(self, tool_descs: str, skill_descs: str = "", mcp_descs: str = "") -> str:
        """组装静态区：SOUL.md + TOOLS.md + 动态描述 + 代码固定规则"""
        parts = []

        # 1. 身份 + 人格（从 prompt/SOUL.md 读取，缺失用默认值）
        soul = self._load_prompt_file("SOUL.md")
        if soul:
            soul = soul.replace("{name}", self.name)
            parts.append(soul)
        else:
            parts.append(_DEFAULT_SOUL.replace("{name}", self.name))

        # 2. 工具使用规则（从 prompt/TOOLS.md 读取静态部分）
        tools_static = self._load_prompt_file("TOOLS.md")
        if tools_static:
            parts.append(tools_static)
        else:
            parts.append(_DEFAULT_TOOLS)

        # 3. 动态工具描述（代码生成，拼接在 TOOLS.md 之后）
        if not skill_descs:
            skill_descs = "（当前没有可用技能）"
        if not mcp_descs:
            mcp_descs = "（当前没有可用 MCP 工具）"

        dynamic_descs = f"\n【内置工具】\n{tool_descs}"
        dynamic_descs += f"\n\n【MCP 工具】\n{mcp_descs}"
        dynamic_descs += f"\n\n【可用技能】\n{skill_descs}"
        parts.append(dynamic_descs)

        # 4. 代码固定规则（格式/安全/命令/配置）
        parts.append(_NATIVE_TOOL_RULES)

        body = "\n\n".join(parts)
        return f"<SYSTEM_STATIC_CONTEXT>\n{body}\n</SYSTEM_STATIC_CONTEXT>"

    def _build_dynamic(self) -> str:
        """组装动态区：引导文件说明 + AGENT.md + MEMORY.md + 运行时信息"""
        os_name = self._get_os_name()
        today = date.today().strftime("%Y-%m-%d")
        workspace = self._workspace or os.getcwd()

        # AGENT.md（从 prompt/ 目录读取）
        agent_md = self._load_prompt_file("AGENT.md")
        agent_md_section = f"\n【项目行为准则（AGENT.md）】\n{agent_md}\n" if agent_md else ""

        # MEMORY.md（从 prompt/ 目录读取）
        memory_md = self._load_prompt_file("MEMORY.md")
        memory_md_section = f"\n【跨会话记忆（MEMORY.md）】\n{memory_md}\n" if memory_md else ""

        # 会话指令
        if self._session_instructions:
            lines = "\n".join(f"- {inst}" for inst in self._session_instructions)
            session_section = f"\n【本轮会话指令】\n{lines}\n"
        else:
            session_section = ""

        return self.DYNAMIC_TEMPLATE.format(
            bootstrap_notice=_BOOTSTRAP_NOTICE,
            workspace=workspace,
            date=today,
            os_name=os_name,
            agent_md_section=agent_md_section,
            memory_md_section=memory_md_section,
            session_instructions=session_section,
        )

    # ================================================================
    # prompt/ 引导文件加载
    # ================================================================

    def _load_prompt_file(self, filename: str) -> Optional[str]:
        """从 prompt/ 目录读取引导文件，超限时截断。

        查找顺序：
          1. {project_root}/prompt/{filename}
          2. {cwd}/prompt/{filename}

        文件不存在 → 返回 None（调用方使用代码内嵌默认值）。
        超过 _max_chars_per_file → 尾截断 + 追加告警。
        """
        search_paths = []
        if self._project_root:
            search_paths.append(Path(self._project_root) / "prompt" / filename)
        search_paths.append(Path.cwd() / "prompt" / filename)

        for md_path in search_paths:
            if md_path.exists() and md_path.is_file():
                try:
                    content = md_path.read_text(encoding="utf-8").strip()
                    if not content:
                        return None
                    if len(content) > self._max_chars_per_file:
                        truncated = len(content) - self._max_chars_per_file
                        content = content[:self._max_chars_per_file]
                        content += f"\n\n……（文件过长，已截断 {truncated} 字符）"
                        if not self._truncation_warned:
                            logger.warning(
                                f"引导文件 {filename} 超过 {self._max_chars_per_file} 字符上限，已截断"
                            )
                            if self._truncation_warning == "once":
                                self._truncation_warned = True
                    return content
                except (OSError, UnicodeDecodeError) as e:
                    logger.warning(f"读取引导文件失败 {md_path}: {e}")
        return None

    # ================================================================
    # 工具方法
    # ================================================================

    @staticmethod
    def _get_os_name() -> str:
        s = platform.system().lower()
        return {"windows": "Windows", "darwin": "macOS", "linux": "Linux"}.get(s, s)

    def _load_truncation_config(self) -> None:
        """从 config.json 的 prompt 段读取截断配置"""
        try:
            from core.config_loader import load_config
            cfg = load_config()
            prompt_cfg = cfg.get("prompt", {})
            self._max_chars_per_file = prompt_cfg.get("bootstrap_max_chars_per_file", 8000)
            self._max_chars_total = prompt_cfg.get("bootstrap_max_chars_total", 32000)
            self._truncation_warning = prompt_cfg.get("truncation_warning", "once")
        except Exception:
            pass  # config 不可用时用默认值
