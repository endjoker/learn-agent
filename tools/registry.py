# -*- coding: utf-8 -*-
"""
工具注册表模块 —— Agent 的"工具箱"

ToolRegistry 负责：
1. 注册工具  —— register_tool()
2. 查找工具  —— get_tool()
3. 列出工具  —— list_tools()
4. 生成工具描述 —— get_tool_descriptions() 给 LLM 看

设计理念：
    注册表模式 —— Agent 不直接依赖具体工具，而是通过注册表
    发现和使用工具。新增工具时只需注册，无需修改 Agent 代码。
"""

import logging
import threading
from typing import Dict, List, Optional
from .base_tool import BaseTool
from core.tool_schema import ToolNameCodec, validate_arguments

logger = logging.getLogger("jk_agent")

# 系统保留工具：不出现在用户可选 Catalog（Phase 2，设计 R3）
# 这些是 Agent 内部必需能力，用户不应在 Profile 中显式选择。
SYSTEM_RESERVED_TOOLS = frozenset({
    "create_skill",
    "notes",
    "memory_search",
    "memory_update",
    "cron_add_job",
    "cron_delete_job",
    "cron_list_jobs",
    "cron_run_job",
    "proc_start",
    "proc_send",
    "proc_read",
    "proc_list",
    "proc_stop",
    # Gateway structured capabilities are runtime-owned rather than profile selections.
    # Goal lifecycle tools must stay active even under a snapshot tool allowlist
    # (workspace sessions freeze a built-in subset; the LLM still needs to call
    # complete_goal / pause_goal / resume_goal to converge a Goal).
    "create_plan",
    "create_goal",
    "pause_goal",
    "resume_goal",
    "complete_goal",
    "cancel_goal",
    "create_subagent",
    "ask_question",
})

# 工具风险标记（供 Catalog 展示；实际执行仍由 SecurityGate 拦截）
_TOOL_RISK = {
    "read": "low", "grep": "low", "glob": "low", "search": "low",
    "web_fetch": "low", "datetime": "low", "calculate": "low",
    "write": "medium", "edit": "medium", "file_mgr": "medium",
    "bash": "high", "python": "high", "http": "medium",
}


# ================================================================
# 紧凑索引（A2：工具描述双重计费优化）
# 完整 JSON Schema 由 get_provider_tools() 经 provider tools 参数下发；
# 系统提示词内只保留紧凑索引（名称 + 一句话用途 + 必填参数名），
# 避免同一份 schema 在 system prompt 与 tools 参数里被双重计费（L1 链路）。
# ================================================================

def _first_sentence(text: str, max_chars: int = 60) -> str:
    """提取一句话用途：取第一句/第一行，超长截断，避免单条描述撑爆紧凑索引。"""
    if not text:
        return ""
    for sep in ("。", "！", "？", "\n", "；", ";"):
        idx = text.find(sep)
        if idx > 0:
            text = text[:idx]
            break
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def format_compact_tool(tool) -> str:
    """紧凑索引单行：名称 + 一句话用途 + 必填参数名。"""
    name = getattr(tool, "name", "") or "未知"
    purpose = _first_sentence(getattr(tool, "description", "") or "") or "无描述"
    required = (tool.parameters or {}).get("required") or []
    req_str = "、".join(str(p) for p in required)
    if req_str:
        return f"  ▶ {name}: {purpose}（必填参数: {req_str}）"
    return f"  ▶ {name}: {purpose}（无必填参数）"


class ToolRegistry:
    """
    工具注册表 —— 一个智能的"工具箱"

    所有工具都注册到这里，Agent 通过它来查找和调用工具。

    使用示例：
        registry = ToolRegistry()
        registry.register_tool(ReadTool())
        registry.register_tool(BashTool())

        # Agent 获取工具描述
        descriptions = registry.get_tool_descriptions()

        # 执行工具
        tool = registry.get_tool("read")
        result = tool.execute(file_path="test.txt")
    """

    def __init__(self):
        """初始化一个空的工具箱"""
        self._tools: Dict[str, BaseTool] = {}
        self._skill_tool_names: set = set()  # 技能工具名称（分开展示）
        self._mcp_tool_names: set = set()    # MCP 工具名称（分开展示）
        self._active_tools: Optional[set] = None  # Phase 4 能力子集；None=全部
        # 并发读写锁（RLock：sync_skill_tools 与 list/descriptions 并发安全）
        self._lock = threading.RLock()

    def register_skill_tool(self, tool: BaseTool) -> "ToolRegistry":
        """注册技能工具（单独追踪，与普通工具分开展示）"""
        with self._lock:
            self.register_tool(tool)
            self._skill_tool_names.add(tool.name)
        return self

    def sync_skill_tools(self, tools: List[BaseTool]) -> set[str]:
        """Replace the dynamic skill-tool catalog without touching built-ins.

        Skill files can be edited outside the running Agent (for example from
        the WebUI).  Re-registration must therefore replace old skill tools,
        including removed skills, instead of treating their names as a
        permanent duplicate-registration error.
        """
        with self._lock:
            for name in self._skill_tool_names:
                self._tools.pop(name, None)
            self._skill_tool_names.clear()

            registered: set[str] = set()
            for tool in tools:
                if tool.name in self._tools:
                    logger.warning("技能工具名与现有工具冲突，已跳过: %s", tool.name)
                    continue
                self.register_skill_tool(tool)
                registered.add(tool.name)
            return registered

    def register_mcp_tool(self, tool: BaseTool) -> "ToolRegistry":
        """注册 MCP 工具（单独追踪，与普通工具分开展示）"""
        with self._lock:
            self.register_tool(tool)
            self._mcp_tool_names.add(tool.name)
        return self

    # ==================== 注册与管理 ====================

    def register_tool(self, tool: BaseTool) -> "ToolRegistry":
        """
        注册一个工具到工具箱

        参数:
            tool: BaseTool 的子类实例（如 ReadTool()、BashTool()）

        返回:
            self，支持链式调用：registry.register_tool(A).register_tool(B)

        异常:
            TypeError: 如果 tool 不是 BaseTool 的子类
            ValueError: 如果 tool.name 为空或已存在
        """
        # 类型检查
        if not isinstance(tool, BaseTool):
            raise TypeError(
                f"注册失败：工具必须是 BaseTool 的子类实例，"
                f"而不是 {type(tool).__name__}"
            )

        # 名称检查
        if not tool.name:
            raise ValueError(f"注册失败：工具必须有 name 属性（当前为空）")

        # 重名检查 + 写入必须在同一把锁内完成（P3-4 TOCTOU：
        # 锁外检查、锁内写入会让并发注册同名工具时静默覆盖）
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(
                    f"注册失败：工具名 '{tool.name}' 已存在"
                )
            # 存入字典
            self._tools[tool.name] = tool
        return self  # 支持链式调用

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """
        根据名称查找工具

        参数:
            name: 工具名称（即注册时的 tool.name）

        返回:
            工具实例，如果没找到返回 None
        """
        return self._tools.get(name)

    def validate_arguments(self, name: str, arguments: dict) -> list[str]:
        """Validate before security checks or ``execute``; unknown tools fail closed."""
        tool = self.get_tool(name)
        if tool is None:
            return [f"unknown tool: {name}"]
        return validate_arguments(tool.parameters, arguments)

    def get_provider_tools(self) -> tuple[list[dict], dict[str, str]]:
        """OpenAI-compatible schemas plus provider-name -> internal-name mapping.

        A1：输出按工具名排序稳定化——注册顺序不参与排序，避免同一会话内
        工具列表顺序抖动破坏 provider 前缀缓存。
        """
        mapping: dict[str, str] = {}
        tools: list[dict] = []
        for tool in sorted(self.list_tools(), key=lambda t: t.name):
            if not self._is_active(tool.name):
                continue
            provider_name = ToolNameCodec.encode(tool.name)
            mapping[provider_name] = tool.name
            tools.append({"type": "function", "function": {
                "name": provider_name, "description": tool.description,
                "parameters": tool.parameters,
            }})
        return tools, mapping

    def list_tools(self) -> List[BaseTool]:
        """
        列出所有已注册的工具

        返回:
            工具实例列表
        """
        with self._lock:
            return list(self._tools.values())

    def list_tool_names(self) -> List[str]:
        """列出所有已注册的工具名（供模糊匹配等场景使用）"""
        return list(self._tools.keys())

    def get_catalog(self) -> List[dict]:
        """用户可选工具 Catalog（Phase 2）。

        排除系统保留工具（create_skill/memory_*/cron_*/proc_*）与
        Skill/MCP 工具（它们有单独区域）。
        """
        with self._lock:
            snapshot = list(self._tools.items())
        out = []
        for name, tool in snapshot:
            if name in SYSTEM_RESERVED_TOOLS:
                continue
            if name in self._skill_tool_names or name in self._mcp_tool_names:
                continue
            if not self._is_active(name):
                continue
            out.append({
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "source": "builtin",
                "risk": _TOOL_RISK.get(tool.name, "medium"),
                "available": True,
            })
        return out

    def get_descriptions_for(self, names: List[str]) -> str:
        """为指定工具子集生成文本描述（Prompt 预览/运行共用）。

        未选/系统保留工具不出现；名称未知时忽略。
        """
        with self._lock:
            selected = [n for n in (names or []) if n in self._tools
                        and n not in SYSTEM_RESERVED_TOOLS
                        and self._is_active(n)]
            if not selected:
                return "（当前没有可用工具）"
            parts = []
            for name in selected:
                tool = self._tools[name]
                params_desc = []
                props = tool.parameters.get("properties", {})
                required = tool.parameters.get("required", [])
                for pname, pinfo in props.items():
                    req_mark = "（必填）" if pname in required else "（可选）"
                    ptype = pinfo.get("type", "string")
                    pdesc = pinfo.get("description", "")
                    params_desc.append(f"      - {pname} ({ptype}){req_mark}: {pdesc}")
                param_str = "\n".join(params_desc) if params_desc else "      无参数"
                parts.append(
                    f"  \u25b6 {tool.name}\n"
                    f"    描述: {tool.description}\n"
                    f"    参数:\n{param_str}\n"
                )
            return "\n".join(parts)

    def get_available_names(self, names: List[str]) -> List[str]:
        """过滤出实际注册且非系统保留的工具名（运行装配用）。"""
        return [n for n in (names or [])
                if n in self._tools and n not in SYSTEM_RESERVED_TOOLS]

    def remove_tool(self, name: str) -> bool:
        """
        从注册表中移除一个工具

        参数:
            name: 要移除的工具名称
        返回:
            True 表示移除成功，False 表示工具不存在
        """
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                return True
            return False

    # ==================== 为 LLM 生成描述 ====================

    def get_tool_descriptions(self, compact: bool = False) -> str:
        """
        生成内置工具的文本描述（排除技能工具和 MCP 工具，它们有单独区域）

        compact=True 时输出紧凑索引（省 token，A2 工具描述双重计费优化）：
        每行 = 名称 + 一句话用途 + 必填参数名；完整 JSON Schema 仍走
        get_provider_tools() 的 provider tools 参数，不在提示词内重复全量参数描述。

        返回:
            格式化的工具描述文本
        """
        with self._lock:
            tools_to_show = [
                t for n, t in self._tools.items()
                if n not in self._skill_tool_names and n not in self._mcp_tool_names
                and self._is_active(n)
            ]

            if not tools_to_show:
                return "（当前没有可用工具）"

            if compact:
                return "\n".join(format_compact_tool(t) for t in tools_to_show)

            descriptions = []
            for tool in tools_to_show:
                # 描述参数信息
                params_desc = []
                props = tool.parameters.get("properties", {})
                required = tool.parameters.get("required", [])

                for param_name, param_info in props.items():
                    # 标注是否必填
                    req_mark = "（必填）" if param_name in required else "（可选）"
                    # 参数类型
                    param_type = param_info.get("type", "string")
                    # 参数说明
                    param_desc = param_info.get("description", "")
                    # 拼成一行
                    params_desc.append(f"      - {param_name} ({param_type}){req_mark}: {param_desc}")

                param_str = "\n".join(params_desc) if params_desc else "      无参数"

                desc = (
                    f"  ▶ {tool.name}\n"
                    f"    描述: {tool.description}\n"
                    f"    参数:\n{param_str}\n"
                )
                descriptions.append(desc)

            return "\n".join(descriptions)

    def get_openai_tools_format(self) -> List[Dict]:
        """
        获取 OpenAI 函数调用（Function Calling）格式的工具列表

        如果你使用的 LLM API 支持原生的 function calling，
        可以直接用这个格式调用。

        返回:
            OpenAI tools 参数格式的列表
        """
        with self._lock:
            tools = list(self._tools.values())
        return [tool.to_openai_tool_format()
                for tool in tools if self._is_active(tool.name)]

    def get_skill_tool_descriptions(self) -> str:
        """仅生成技能工具的文本描述"""
        with self._lock:
            skill_tools = [self._tools[n] for n in self._skill_tool_names if n in self._tools]
        if not skill_tools:
            return ""

        parts = []
        for tool in skill_tools:
            params_desc = []
            props = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            for pname, pinfo in props.items():
                req_mark = "（必填）" if pname in required else "（可选）"
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
                params_desc.append(f"      - {pname} ({ptype}){req_mark}: {pdesc}")

            param_str = "\n".join(params_desc) if params_desc else "      无参数"
            parts.append(
                f"  ▶ {tool.name}\n"
                f"    描述: {tool.description}\n"
                f"    参数:\n{param_str}\n"
            )

        return "\n".join(parts)

    def get_mcp_tool_descriptions(self) -> str:
        """仅生成 MCP 工具的文本描述"""
        with self._lock:
            mcp_tools = [self._tools[n] for n in self._mcp_tool_names if n in self._tools]
        if not mcp_tools:
            return ""

        parts = []
        for tool in mcp_tools:
            params_desc = []
            props = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            for pname, pinfo in props.items():
                req_mark = "（必填）" if pname in required else "（可选）"
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
                params_desc.append(f"      - {pname} ({ptype}){req_mark}: {pdesc}")

            param_str = "\n".join(params_desc) if params_desc else "      无参数"
            parts.append(
                f"  ▶ {tool.name}\n"
                f"    描述: {tool.description}\n"
                f"    参数:\n{param_str}\n"
            )

        return "\n".join(parts)

    # ==================== 工具方法 ====================

    def count(self) -> int:
        """返回已注册的工具数量"""
        return len(self._tools)

    def clear(self):
        """清空所有工具"""
        with self._lock:
            self._tools.clear()

    def set_active_tools(self, names: Optional[List[str]]) -> None:
        """Phase 4：设置用户可选工具子集。

        None / 未调用 = 全部工具；设置后 get_provider_tools / get_tool_descriptions
        / get_catalog 三方一致过滤。系统保留工具恒为 active。
        """
        self._active_tools = set(names) if names is not None else None

    def _is_active(self, name: str) -> bool:
        if self._active_tools is None:
            return True
        if name in SYSTEM_RESERVED_TOOLS:
            return True
        return name in self._active_tools

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """支持 'tool_name' in registry 语法"""
        return name in self._tools

    def __str__(self) -> str:
        tool_names = ", ".join(self._tools.keys())
        return f"🧰 ToolRegistry（共 {len(self._tools)} 个工具: {tool_names}）"

    def __repr__(self) -> str:
        return f"<ToolRegistry tools={list(self._tools.keys())}>"
