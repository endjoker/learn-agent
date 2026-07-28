# -*- coding: utf-8 -*-
"""
System Prompt 构建器 —— 将提示词分为静态区和动态区

使用方式：
    from core.system_prompt import SystemPrompt

    builder = SystemPrompt(name="helloworld agent")
    builder.add_session_instruction("本次对话请使用英文回复")
    prompt = builder.build(tool_descs="...")

输出结构：
    <SYSTEM_STATIC_CONTEXT>
      ...固定规则...
    </SYSTEM_STATIC_CONTEXT>

    <SYSTEM_DYNAMIC_CONTEXT>
      ...运行时信息...
    </SYSTEM_DYNAMIC_CONTEXT>
"""

import os
import platform
from datetime import date
from pathlib import Path
from typing import Optional


class SystemPrompt:
    """
    System Prompt 构建器

    将提示词分为两个区域：
      - 静态区（SYSTEM_STATIC_CONTEXT）：角色、风格、原则、规范等固定内容
      - 动态区（SYSTEM_DYNAMIC_CONTEXT）：工作目录、日期、OS、AGENT.md、会话指令
    """

    # ================================================================
    # 静态区模板
    # ================================================================

    STATIC_TEMPLATE = """\
<SYSTEM_STATIC_CONTEXT>
【角色定义】
你是{name}，一个 AI 智能体，帮助用户完成任务。

【回答风格】
- 保持回答简洁务实，直击要点
- 用中文回复，技术术语可保留英文
- 复杂问题时先拆解再回答

【工具使用原则】
- 优先使用专用工具而非 Shell 命令
- 读文件用 read，搜索用 grep/glob，写文件用 write，修改用 edit
- 工具能满足需求就不要拼 shell 命令

【行为规范】
- 不确定时先确认再执行
- 执行可能造成影响的操作前先告知用户
- 不要凭空捏造信息，引用来源

【修改代码的基本要求】
- 先阅读理解代码再修改
- 修改后尽量验证（观察运行结果）
- 一次改一个问题，保持改动可追溯

【内置工具】
{tool_descs}

【MCP 工具】
{mcp_descs}

【可用技能】
{skill_descs}

【技能使用规则】
- 技能是 AI 学习到的可复用能力，调用后返回操作指令，不是最终结果
- 收到技能指令后，请一步一步执行，期间可以调用其他工具
- 所有步骤执行完毕后，输出 FINAL_ANSWER
- 如果发现某个流程需要重复执行，可以使用 create_skill 工具创建新技能

【回复格式——必须使用英文标签】
每次回复严格使用英文标签（避免编码问题）：

THOUGHT：[分析当前情况，决定下一步做什么]
ACTION：[工具名称]
INPUT：[JSON 格式的参数]

当你获得工具的返回结果后（结果会以"【工具执行结果】"标记呈现），继续你的分析。
如果已经足够回答用户，输出：

FINAL_ANSWER：[给用户的最终答案]

【规则】
1. 支持多工具并行调用，允许一次输出多个 ACTION + INPUT，它们会被并发执行后合并结果
2. INPUT 必须是合法 JSON
3. name=tool_result，是工具返回的数据
4. 信息足够时输出 FINAL_ANSWER
5. 标签回复必须用英文

【交互命令（你无法直接执行，仅在合适时机建议用户发送）】
- /compact — 对话较长、工具调用多、上下文紧张时建议
- /clear — 用户想重新开始对话时建议
- /model <名称> — 用户想切换模型时建议
- /stats — 用户询问上下文占用或剩余空间时建议
- /session — 用户想保存、恢复或管理历史会话时建议
- /help — 查看全部命令

</SYSTEM_STATIC_CONTEXT>"""

    # ================================================================
    # 动态区模板
    # ================================================================

    DYNAMIC_TEMPLATE = """\
<SYSTEM_DYNAMIC_CONTEXT>
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

    # ================================================================
    # 公开方法
    # ================================================================

    def add_session_instruction(self, instruction: str) -> None:
        """
        添加本轮会话的额外指令，会出现在动态区底部。

        参数:
            instruction: 指令文本，如 "本次对话请使用英文回复"
        """
        if instruction and instruction.strip():
            self._session_instructions.append(instruction.strip())

    def set_project_root(self, path: str) -> None:
        """
        设置项目根目录路径（用于查找 AGENT.md）。

        参数:
            path: 项目根目录的绝对路径
        """
        self._project_root = path

    def set_workspace(self, path: str) -> None:
        """
        设置工作目录路径（用于动态区声明 + 工具操作边界）。

        参数:
            path: 工作目录的绝对路径
        """
        self._workspace = path

    def build(self, tool_descs: str, skill_descs: str = "", mcp_descs: str = "") -> str:
        """
        构建完整的 System Prompt。

        参数:
            tool_descs: 工具描述的文本，由 ToolRegistry.get_tool_descriptions() 生成
            skill_descs: 技能描述的文本，由 SkillManager.get_skill_descriptions() 生成
            mcp_descs: MCP 工具描述的文本，由 ToolRegistry.get_mcp_tool_descriptions() 生成

        返回:
            包含静态区和动态区的完整 system prompt
        """
        static = self._build_static(tool_descs, skill_descs, mcp_descs)
        dynamic = self._build_dynamic()
        return static + "\n\n" + dynamic

    # ================================================================
    # 内部构建
    # ================================================================

    def _build_static(self, tool_descs: str, skill_descs: str = "", mcp_descs: str = "") -> str:
        if not skill_descs:
            skill_descs = "（当前没有可用技能）"
        if not mcp_descs:
            mcp_descs = "（当前没有可用 MCP 工具）"
        return self.STATIC_TEMPLATE.format(
            name=self.name,
            tool_descs=tool_descs,
            skill_descs=skill_descs,
            mcp_descs=mcp_descs,
        )

    def _build_dynamic(self) -> str:
        # 操作系统
        os_name = self._get_os_name()

        # 当前日期（仅年月日，不含时间）
        today = date.today().strftime("%Y-%m-%d")

        # 工作目录
        workspace = self._workspace or os.getcwd()

        # AGENT.md
        agent_md = self._load_agent_md()
        agent_md_section = f"\n【项目记忆（AGENT.md）】\n{agent_md}\n" if agent_md else ""

        # memory.md（长期记忆配置引用）
        memory_md = self._load_memory_md()
        memory_md_section = f"\n【跨会话记忆】\n{memory_md}\n" if memory_md else ""

        # 会话指令
        if self._session_instructions:
            lines = "\n".join(f"- {inst}" for inst in self._session_instructions)
            session_section = f"\n【本轮会话指令】\n{lines}\n"
        else:
            session_section = ""

        return self.DYNAMIC_TEMPLATE.format(
            workspace=workspace,
            date=today,
            os_name=os_name,
            agent_md_section=agent_md_section,
            memory_md_section=memory_md_section,
            session_instructions=session_section,
        )

    # ================================================================
    # AGENT.md 加载
    # ================================================================

    def _load_agent_md(self) -> Optional[str]:
        """在项目根目录查找 AGENT.md，存在则返回其内容"""
        search_paths = []
        if self._project_root:
            search_paths.append(Path(self._project_root) / "AGENT.md")
        # 兜底：从当前目录向上查找
        search_paths.append(Path.cwd() / "AGENT.md")
        # 再向上一级
        search_paths.append(Path.cwd().parent / "AGENT.md")

        for md_path in search_paths:
            if md_path.exists() and md_path.is_file():
                try:
                    content = md_path.read_text(encoding="utf-8").strip()
                    if content:
                        return content
                except (OSError, UnicodeDecodeError):
                    pass
        return None

    # ================================================================
    # 工具方法
    # ================================================================

    # ================================================================
    # memory.md 加载
    # ================================================================

    def _load_memory_md(self) -> Optional[str]:
        """在项目根目录查找 memory/memory.md，存在则返回其内容"""
        search_paths = []
        if self._project_root:
            search_paths.append(Path(self._project_root) / "memory" / "memory.md")
        search_paths.append(Path.cwd() / "memory" / "memory.md")
        search_paths.append(Path.cwd().parent / "memory" / "memory.md")

        for md_path in search_paths:
            if md_path.exists() and md_path.is_file():
                try:
                    content = md_path.read_text(encoding="utf-8").strip()
                    if content:
                        return content
                except (OSError, UnicodeDecodeError):
                    pass
        return None

    @staticmethod
    def _get_os_name() -> str:
        s = platform.system().lower()
        return {"windows": "Windows", "darwin": "macOS", "linux": "Linux"}.get(s, s)
