# -*- coding: utf-8 -*-
"""
工具基类模块 —— 所有工具的"模板"和"合同"

每个工具都必须继承 BaseTool，并实现以下内容：
1. name:       工具名称（LLM 调用时用的唯一标识）
2. description: 工具描述（告诉 LLM 什么时候用这个工具）
3. parameters:  参数定义（JSON Schema 格式，描述需要什么参数）
4. execute():   执行方法（工具的核心逻辑）

设计理念：
    - 统一接口：所有工具都遵循相同的调用方式
    - 自描述：工具自己能说清楚"我做什么、需要什么参数"
    - 易扩展：新增工具只需继承 BaseTool 并实现 execute()
"""

from typing import Any, Dict


class BaseTool:
    """
    工具基类 —— 所有工具的标准接口

    子类需要重写以下类属性：
        name: str        — 工具名称，如 "read_file"
        description: str — 工具描述，如 "读取文件内容"
        parameters: dict — 参数定义的 JSON Schema

    子类必须实现以下方法：
        execute(**kwargs) -> str  — 工具的执行逻辑
    """

    # ============ 类属性（子类必须重写） ============

    name: str = ""
    """工具名称 —— LLM 使用这个名字来调用工具，如 "read"、"write"、"bash" """

    description: str = ""
    """工具描述 —— 告诉 LLM 这个工具做什么、什么时候使用它"""

    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    """
    参数定义的 JSON Schema

    格式示例：
    {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径",
            },
            "content": {
                "type": "string",
                "description": "文件内容",
            },
        },
        "required": ["file_path", "content"],
    }
    """

    # ============ 安全能力标签（供 SecurityGate 决定跑哪些 L2 检查） ============

    capabilities: tuple = ()
    """
    工具的安全能力标签。可选值：
        fs:read / fs:write / fs:delete / fs:move  — 文件读写
        net:egress                                 — 网络外发
        exec:shell / exec:code                     — 命令/代码执行
        remote:call                                — 远程工具（MCP）

    无风险能力的工具（datetime/notes 等纯计算/查询）留空 () 即可，
    SecurityGate 会跳过 L2、走 L1 权限。
    """

    parallel_safe: bool = False
    """
    是否可与其他工具并行执行（默认 False = 默认拒绝并行，P3-4 显式化）。

    此前本属性未在基类声明，ToolBatchExecutor._is_parallel_safe 靠
    getattr(tool, "parallel_safe", False) 隐式默认拒绝——方向安全但不可见，
    新增工具作者容易不知道存在这个开关。只有纯读、无共享可变状态、且经过
    审计的工具才应显式声明 True（如 read/glob/grep/clock/calculator）。
    """

    _permission = None
    """注入的 PermissionChecker（四档权限感知边界用，见 set_permission）。

    项目权限只遵循四项基本权限（readonly/ask/allow/unreviewed 经
    PolicyEngine 裁决）：注入后，工具层的工作区边界对读完全交还裁决、
    对写按 readonly 之外的档位交还裁决；未注入（无裁决层的直调路径）
    时工具层硬边界是唯一防线。"""

    def configure(self, *, sandbox=None, policy=None, workspace_roots=None,
                  memory_manager=None, permission=None) -> None:
        """统一注入接口（P2-4）：register_all_tools 对所有工具调用同一入口。

        按子类实际实现的 set_* setter 分发（hasattr 探测），未实现的注入项
        自动跳过——新增工具只需实现需要的 setter，不再依赖 register_all_tools
        里逐工具 if 判断（Write/Edit 漏配边界校验注入正是这样漏掉的）。
        默认实现无状态、可安全重复调用。
        """
        if sandbox is not None and hasattr(self, "set_sandbox"):
            self.set_sandbox(sandbox)
        if policy is not None and hasattr(self, "set_policy"):
            self.set_policy(policy)
        if workspace_roots is not None and hasattr(self, "set_workspace_roots"):
            self.set_workspace_roots(workspace_roots)
        if memory_manager is not None and hasattr(self, "set_memory_manager"):
            self.set_memory_manager(memory_manager)
        if permission is not None and hasattr(self, "set_permission"):
            self.set_permission(permission)

    def resolve_capabilities(self, params: dict) -> tuple:
        """
        返回本次调用的安全能力（默认返回类属性 capabilities）。

        子类可重写以按参数动态决定能力，例如 file_mgr 的 action=ls 是读、
        action=delete 是写。默认实现直接返回静态 capabilities。
        """
        return self.capabilities

    # ============ 实例方法 ============

    def execute(self, **kwargs) -> str:
        """
        执行工具的核心逻辑

        这个方法是工具的"灵魂"—— 子类必须重写它。
        方法接收关键字参数（由 parameters 定义），返回字符串结果。

        参数:
            **kwargs: 根据 parameters 定义传入的具名参数
                     （比如 ReadTool 会收到 file_path、offset、limit 等）

        返回:
            str: 工具执行的结果文本（纯文本格式，方便 LLM 阅读）

        异常:
            NotImplementedError: 如果子类没有重写此方法
        """
        raise NotImplementedError(
            f"工具 '{self.name}' 未实现 execute() 方法。"
            f"请在子类中重写 execute() 并实现具体逻辑。"
        )

    def to_openai_tool_format(self) -> Dict[str, Any]:
        """
        转换为 OpenAI 函数调用（Function Calling）格式

        这样就能兼容使用 OpenAI 的 tool_choice / function calling API，
        让 LLM 直接返回结构化的工具调用指令。

        返回:
            {
                "type": "function",
                "function": {
                    "name": "...",
                    "description": "...",
                    "parameters": {...},
                }
            }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }

    def __str__(self) -> str:
        """友好地打印工具信息"""
        return f"[工具] {self.name}: {self.description[:40]}..."

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name='{self.name}')>"
