# -*- coding: utf-8 -*-
"""
内置工具 —— 统一注册入口与命名空间 shim（P3-7 拆分后保持兼容）。

工具实现已按职责拆到：
  tools/_tool_helpers.py  共享辅助函数（路径边界/文本探测/glob 展开等）
  tools/fs_tools.py       文件系统工具（read/write/edit/grep/glob/file_mgr）
  tools/exec_tools.py     命令/网络/计算工具（bash/python/http/calculator/...）

本文件保留公开命名空间（所有工具类/辅助函数/常量/register_all_tools/
BUILTIN_TOOLS），任何 `from tools.builtin_tools import ...` 或
`import tools.builtin_tools` 的行为与拆分前完全一致。
"""

import logging

from ._tool_helpers import (  # noqa: F401 —— helpers 公开命名空间兼容
    _PERMISSION_LADDER_MODES, TEXT_EXTENSIONS, TEXT_FILENAMES, SCAN_EXCLUDED_DIRS,
    _path_within_roots, _collect_allowed_roots, _check_workspace_boundary,
    _mutation_boundary_err, _read_boundary_err, _format_size, _safe_stat,
    _is_text_file, _iter_files_pruned, _in_excluded_dir, _expand_glob_braces,
)
from .fs_tools import (  # noqa: F401
    _MAX_READ_BYTES, _LARGE_FILE_LINE_CAP,
    ReadTool, WriteTool, EditTool, GrepTool, GlobTool, FileManagerTool,
)
from .exec_tools import (  # noqa: F401
    _subprocess_default_timeout, _clamp_timeout, _bash_output_cap_bytes,
    _contains_secrets, BashTool, CalculatorTool, DateTimeTool, NoteTool,
    PythonTool, HttpTool,
)
from .memory_tools import MemorySearchTool, MemoryUpdateTool
from .base_tool import BaseTool  # noqa: F401

logger = logging.getLogger("jk_agent")


# 工具列表 —— 拆分后仍按原顺序（readabilit 与注册顺序不变）
BUILTIN_TOOLS = [
    ReadTool,
    WriteTool,
    EditTool,
    GrepTool,
    GlobTool,
    BashTool,
    CalculatorTool,
    DateTimeTool,
    NoteTool,
    FileManagerTool,
    PythonTool,
    HttpTool,
    MemorySearchTool,
    MemoryUpdateTool,
]


def register_all_tools(registry, memory_manager=None, sandbox=None, process_manager=None,
                        policy=None, workspace_roots=None, permission=None):
    """
    一键注册所有内置工具到注册表

    参数:
        registry: ToolRegistry 实例
        memory_manager: MemoryManager 实例（可选），注入到记忆工具中
        sandbox: SandboxExecutor 实例（可选），注入到 bash/python/write/edit/read/http 工具中
        process_manager: ProcessManager 实例（可选），注入到 proc_* 工具中
        policy: PolicyEngine 实例（可选），注入到文件路径类工具做 allowed_roots 边界校验
        workspace_roots: 允许的工作区根列表（可选），显式注入边界校验用
        permission: PermissionChecker 实例（可选），注入到写类工具做四档权限感知
                    边界（ask/allow/unreviewed 下写边界交还权限裁决，见
                    _mutation_boundary_err）；主 Agent 链路必须传入

    使用方式：
        from tools import ToolRegistry
        from tools.builtin_tools import register_all_tools

        registry = ToolRegistry()
        register_all_tools(registry)
        register_all_tools(registry, memory_manager=mm)  # 启用记忆系统
        register_all_tools(registry, sandbox=sb)         # 启用沙箱
        register_all_tools(registry, process_manager=pm) # 启用长驻进程工具
        register_all_tools(registry, policy=engine)      # 启用工作区边界校验
    """
    from .registry import ToolRegistry

    if not isinstance(registry, ToolRegistry):
        raise TypeError("参数必须是 ToolRegistry 实例")

    for tool_cls in BUILTIN_TOOLS:
        tool = tool_cls()
        # 统一注入入口（P2-4）：BaseTool.configure 按子类实现的 set_* setter
        # 分发，未实现自动跳过——不再逐工具 if 判断（P0 的 Write/Edit 漏配
        # 边界校验注入正是旧模式的漏网之鱼）。
        tool.configure(sandbox=sandbox, policy=policy,
                       workspace_roots=workspace_roots,
                       memory_manager=memory_manager,
                       permission=permission)
        registry.register_tool(tool)

    # 长驻子进程工具（proc_*）
    if process_manager is not None:
        from .process_tools import register_process_tools
        register_process_tools(registry, process_manager=process_manager)

    # 定时任务工具（cron_*）—— 无额外依赖
    from tools.cron_tools import (
        CronAddJobTool, CronDeleteJobTool, CronListJobsTool, CronRunJobTool)
    registry.register_tool(CronAddJobTool())
    registry.register_tool(CronDeleteJobTool())
    registry.register_tool(CronListJobsTool())
    registry.register_tool(CronRunJobTool())

    return registry
