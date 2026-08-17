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
    "cron_run_job",
    "proc_start",
    "proc_send",
    "proc_read",
    "proc_list",
    "proc_stop",
})

# 工具风险标记（供 Catalog 展示；实际执行仍由 SecurityGate 拦截）
_TOOL_RISK = {
    "read": "low", "grep": "low", "glob": "low", "search": "low",
    "web_fetch": "low", "datetime": "low", "calculate": "low",
    "write": "medium", "edit": "medium", "file_mgr": "medium",
    "bash": "high", "python": "high", "http": "medium",
}


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

    def register_skill_tool(self, tool: BaseTool) -> "ToolRegistry":
        """注册技能工具（单独追踪，与普通工具分开展示）"""
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

        # 重名检查
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
        """OpenAI-compatible schemas plus provider-name -> internal-name mapping."""
        mapping: dict[str, str] = {}
        tools: list[dict] = []
        for tool in self.list_tools():
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
        return list(self._tools.values())

    def list_tool_names(self) -> List[str]:
        """列出所有已注册的工具名（供模糊匹配等场景使用）"""
        return list(self._tools.keys())

    def get_catalog(self) -> List[dict]:
        """用户可选工具 Catalog（Phase 2）。

        排除系统保留工具（create_skill/memory_*/cron_*/proc_*）与
        Skill/MCP 工具（它们有单独区域）。
        """
        out = []
        for name, tool in self._tools.items():
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
        selected = [n for n in (names or []) if n in self._tools
                    and n not in SYSTEM_RESERVED_TOOLS]
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
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    # ==================== 为 LLM 生成描述 ====================

    def get_tool_descriptions(self) -> str:
        """
        生成内置工具的文本描述（排除技能工具和 MCP 工具，它们有单独区域）

        返回:
            格式化的工具描述文本
        """
        tools_to_show = [
            t for n, t in self._tools.items()
            if n not in self._skill_tool_names and n not in self._mcp_tool_names
            and self._is_active(n)
        ]

        if not tools_to_show:
            return "（当前没有可用工具）"

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
        return [tool.to_openai_tool_format()
                for tool in self._tools.values() if self._is_active(tool.name)]

    def get_skill_tool_descriptions(self) -> str:
        """仅生成技能工具的文本描述"""
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
