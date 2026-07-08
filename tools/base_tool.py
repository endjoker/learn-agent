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
