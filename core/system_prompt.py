# -*- coding: utf-8 -*-
"""
System Prompt 构建器 —— 从 prompt/ 目录读取引导文件 + 代码固定规则

使用方式：
    from core.system_prompt import SystemPrompt

    builder = SystemPrompt(name="JKagent")
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

logger = logging.getLogger("jk_agent")

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
【原生工具调用】
- 工具由运行时通过原生 function calling 提供。需要工具时直接选择工具并填写参数，不要把调用内容写进回复文本。
- 工具调用和最终可见回复是两种互斥状态：调用工具时无需解释性占位文本；完成后直接给出用户可读的自然语言或 Markdown。
- 不要输出 JSON 信封、XML 标签或任何文本控制协议；工具执行结果会自动进入下一轮上下文。
【安全】
- 高风险操作前说明风险并请求确认；不得泄露密钥或执行破坏性系统操作。"""

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
{agent_md_section}{memory_md_section}{memory_storage_section}{session_instructions}
</SYSTEM_DYNAMIC_CONTEXT>"""


    def __init__(self, name: str = "JKagent"):
        self.name = name
        self._session_instructions: list[str] = []
        self._project_root: Optional[str] = None
        self._framework_prompt_root: Optional[str] = None
        self._project_prompt_root: Optional[str] = None
        self._workspace: Optional[str] = None
        self._agent_profile_prompt: Optional[str] = None
        self._memory_path: Optional[str] = None
        self._memory_instruction: Optional[str] = None
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
        """设置项目根目录路径（兼容入口，等价 set_project_prompt_root）。

        保留原语义：项目 prompt/ 目录与工作目录都锚定到该根。
        """
        if path:
            self._project_root = str(path)
            self._project_prompt_root = str(path)

    def set_workspace(self, path: str) -> None:
        """设置工作目录路径（用于动态区声明 + 工具操作边界）。"""
        self._workspace = str(path)

    def set_framework_prompt_root(self, path: str) -> None:
        """设置框架级 prompt/ 目录（默认引导文件兜底来源）。"""
        self._framework_prompt_root = str(path)

    def set_project_prompt_root(self, path: str) -> None:
        """设置项目级 prompt/ 目录（优先来源）。"""
        self._project_prompt_root = str(path)

    def set_runtime_context(self, *, framework_root=None, project_root=None,
                            working_directory=None) -> None:
        """统一设置三目录运行上下文（Phase 0 WorkspaceRuntimeContext 注入）。"""
        if framework_root:
            self._framework_prompt_root = str(framework_root)
        if project_root:
            self._project_root = str(project_root)
            self._project_prompt_root = str(project_root)
        if working_directory:
            self._workspace = str(working_directory)

    def set_agent_profile_prompt(self, text: str) -> None:
        """设置 Agent Profile 的 System Prompt 文本（智能体编辑预览/运行用）。"""
        self._agent_profile_prompt = text.strip() if text else None

    def set_memory_context(self, memory_path: Optional[str] = None,
                          instruction: Optional[str] = None) -> None:
        """设置长期记忆上下文（工作区模式注入记忆路径与优先搜索指令）。"""
        if memory_path:
            self._memory_path = str(memory_path)
        if instruction:
            self._memory_instruction = instruction.strip()

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

    def build_sections(self, tool_descs: str, skill_descs: str = "",
                       mcp_descs: str = "") -> list[dict]:
        """分区构建 System Prompt（Phase 2 Prompt 预览/运行共用核心逻辑）。

        返回有序 section 列表：
          without an Agent Profile: FRAMEWORK_IDENTITY / TOOL_POLICY / BUILTIN_TOOLS /
          MCP_TOOLS / SKILLS / FRAMEWORK_RULES; with an Agent Profile: AGENT_PROFILE /
          TOOL_POLICY / BUILTIN_TOOLS / MCP_TOOLS / SKILLS / FRAMEWORK_RULES
        每个 section: {"name": str, "content": str, "chars": int,
                       "estimated_tokens": int}
        """
        sections = []
        soul = self._load_prompt_file("SOUL.md") or _DEFAULT_SOUL.format(name=self.name)
        tools_md = self._load_prompt_file("TOOLS.md")
        agent_md = self._load_prompt_file("AGENT.md")

        def _add(name, content):
            if content is None:
                content = ""
            sections.append({
                "name": name,
                "content": content,
                "chars": len(content),
                "estimated_tokens": _estimate_tokens(content),
            })

        # 1. 框架身份（SOUL.md / 默认人格）
        # A complete Agent Profile owns its role and behavioral identity. Do not
        # prepend SOUL.md in that case: the two prompts otherwise duplicate or
        # conflict in both preview and runtime output.
        if self._agent_profile_prompt:
            _add("AGENT_PROFILE", self._agent_profile_prompt)
        else:
            _add("FRAMEWORK_IDENTITY", soul)
        _add("TOOL_POLICY", tools_md or _DEFAULT_TOOLS)
        # 4. 内置工具描述
        builtin = f"【内置工具】\n{tool_descs}" if tool_descs else "【内置工具】\n（当前没有可用工具）"
        _add("BUILTIN_TOOLS", builtin)
        # 5. MCP 工具描述
        mcp = f"【MCP 工具】\n{mcp_descs}" if mcp_descs else "【MCP 工具】\n（当前没有可用 MCP 工具）"
        _add("MCP_TOOLS", mcp)
        # 6. 技能描述
        skill = f"【可用技能】\n{skill_descs}" if skill_descs else "【可用技能】\n（当前没有可用技能）"
        _add("SKILLS", skill)
        # 7. 框架固定规则
        _add("FRAMEWORK_RULES", _NATIVE_TOOL_RULES)
        # 8. 项目行为准则（AGENT.md）并入动态上下文声明
        dynamic_notice = ""
        if agent_md:
            dynamic_notice = f"【项目行为准则（AGENT.md）】\n{agent_md}"
        _add("PROJECT_CONTEXT", dynamic_notice)
        if self._memory_path:
            memory_section = (
                "【工作区长期记忆】\n"
                f"- 存储路径：{self._memory_path}\n"
                f"- {self._memory_instruction or '当用户询问与当前项目相关的问题时，优先调用 memory_search 检索本工作区长期记忆，再作答。'}"
            )
            _add("WORKSPACE_MEMORY", memory_section)
        return sections
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
        # Agent Profile owns the identity for profile-backed sessions. The
        # framework SOUL.md identity remains only for base sessions with no
        # profile, preventing duplicate identity instructions at runtime.
        if self._agent_profile_prompt:
            parts.append(f"[Agent Profile]\n{self._agent_profile_prompt}")
        else:
            soul = self._load_prompt_file("SOUL.md")
            if soul:
                parts.append(soul.replace("{name}", self.name))
            else:
                parts.append(_DEFAULT_SOUL.replace("{name}", self.name))

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
        # 动态区使用显式 working_directory；无显式值时才回退到项目根/框架根，
        # 绝不依赖进程 CWD（Phase 0：多工作区并发禁止 os.chdir 污染）。
        workspace = (self._workspace
                     or (self._project_root if self._project_root else None)
                     or (self._framework_prompt_root if self._framework_prompt_root else None)
                     or os.getcwd())

        # AGENT.md（从 prompt/ 目录读取，项目优先、框架兜底）
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

        memory_storage_section = ""
        if self._memory_path:
            memory_storage_section = (
                "\n【工作区长期记忆】\n"
                f"- 存储路径：{self._memory_path}\n"
                f"- {self._memory_instruction or '当用户询问与当前项目相关的问题时，优先调用 memory_search 检索本工作区长期记忆，再作答。'}\n"
            )

        return self.DYNAMIC_TEMPLATE.format(
            bootstrap_notice=_BOOTSTRAP_NOTICE,
            workspace=workspace,
            date=today,
            os_name=os_name,
            agent_md_section=agent_md_section,
            memory_md_section=memory_md_section,
            memory_storage_section=memory_storage_section,
            session_instructions=session_section,
        )

    # ================================================================
    # prompt/ 引导文件加载
    # ================================================================

    def _load_prompt_file(self, filename: str) -> Optional[str]:
        """从 prompt/ 目录读取引导文件，超限时截断。

        查找顺序（Phase 0 起不再依赖进程 CWD）：
          1. {project_prompt_root}/prompt/{filename}   （项目 prompt/ 优先）
          2. {framework_prompt_root}/prompt/{filename} （框架 prompt/ 兜底）
          3. 兼容：旧 set_project_root 设置的根

        文件不存在 → 返回 None（调用方使用代码内嵌默认值）。
        超过 _max_chars_per_file → 尾截断 + 追加告警。
        """
        search_paths = []
        roots = []
        if self._project_prompt_root:
            roots.append(self._project_prompt_root)
        elif self._project_root:
            roots.append(self._project_root)
        if self._framework_prompt_root:
            roots.append(self._framework_prompt_root)
        for root in roots:
            search_paths.append(Path(root) / "prompt" / filename)

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
    # prompt/ 引导文件加载
    # ================================================================

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


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（与 core.message_store._estimate_tokens 一致）。"""
    if not text:
        return 0
    chinese = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    other = len(text) - chinese
    return int(chinese / 1.5 + other / 4)
