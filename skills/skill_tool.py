# -*- coding: utf-8 -*-
"""
技能工具适配器 —— 将 Skill 包装为 BaseTool

两个工具：
  - SkillTool: 执行已有技能（返回指令文本，LLM 自行按步骤执行）
  - CreateSkillTool: LLM 运行时创建新技能（写磁盘 + 注册 + 重建 prompt）
"""

import logging
from typing import Optional

from tools.base_tool import BaseTool
from core.permission import ALLOW
from .skill import Skill
from .manager import SkillManager

logger = logging.getLogger("jk_agent")


class SkillTool(BaseTool):
    """
    技能执行工具 —— 将 Skill 包装为 BaseTool

    与普通工具不同，execute() 返回技能指令文本而不是直接执行代码。
    LLM 收到指令后自行按步骤执行，可调用其他工具。
    """

    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}, "required": []}

    def __init__(self, skill: Skill):
        self.name = skill.name
        self.description = (
            f"【技能】{skill.description} "
            f"注意：这是一个学习型技能，调用后返回操作指令，"
            f"请按照指令逐步执行。"
        )
        self.parameters = skill.parameters or {
            "type": "object", "properties": {}, "required": [],
        }
        self._skill = skill

    def execute(self, **kwargs) -> str:
        parts = [
            f"【技能执行】你正在调用技能 '{self._skill.name}'",
            f"说明：{self._skill.description}",
            "",
            "调用参数：",
        ]
        if kwargs:
            for k, v in kwargs.items():
                parts.append(f"  {k}: {v}")
        else:
            parts.append("  （无参数）")

        parts.extend([
            "",
            "【技能指令】",
            self._skill.instruction,
            "",
            "请严格遵循上述指令逐步执行。需要工具时使用运行时提供的原生 function calling；完成后直接给出用户可读的自然语言或 Markdown 结果。",
        ])

        return "\n".join(parts)


class CreateSkillTool(BaseTool):
    """
    创建技能工具 —— LLM 运行时创建新技能

    LLM 发现用户需要重复执行的流程时，可以调用此工具将其保存为技能，
    供后续复用。
    """

    name: str = "create_skill"
    description: str = (
        "创建新的学习型技能。"
        "当用户需要重复执行某个流程、或者你发现某个操作序列可以被封装为可复用能力时，"
        "使用此工具创建技能。创建后技能会持久化保存，后续可直接调用。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能名称（仅允许字母、数字、-、_），如 'code-review'、'deploy-check'",
            },
            "description": {
                "type": "string",
                "description": "简短描述，说明这个技能的作用和适用场景",
            },
            "instruction": {
                "type": "string",
                "description": "完整的操作指令，告诉 LLM 执行此技能时需要按哪些步骤操作。"
                               "指令清晰具体，可包含调用其他工具的步骤。",
            },
            "parameters": {
                "type": "object",
                "description": "可选，JSON Schema 定义调用此技能时可传入的参数",
            },
        },
        "required": ["name", "description", "instruction"],
    }

    def __init__(self, skill_manager: SkillManager):
        self._manager = skill_manager
        # 注册后通过 set_tool_registry 注入
        self._registry = None

    def set_tool_registry(self, registry):
        """注入 ToolRegistry 引用（注册时由外部调用）"""
        self._registry = registry

    def set_agent_ref(self, agent):
        """注入 Agent 引用（用于重建 system prompt）"""
        self._agent_ref = agent

    def execute(
        self,
        name: str,
        description: str,
        instruction: str,
        parameters: dict = None,
    ) -> str:
        if self._manager is None:
            return "❌ 技能系统未就绪"

        try:
            skill = self._manager.create_skill(
                name=name,
                description=description,
                instruction=instruction,
                parameters=parameters,
            )
        except ValueError as e:
            return f"❌ 创建失败: {e}"
        except OSError as e:
            return f"❌ 文件写入失败: {e}"

        # 注册为工具
        if self._registry:
            tool = SkillTool(skill)
            try:
                self._registry.register_skill_tool(tool)
            except ValueError as e:
                logger.warning(f"注册技能工具失败: {e}")

        # 重建 system prompt + 允许该技能免确认调用（技能只返回指令文本，无副作用）
        if hasattr(self, "_agent_ref") and self._agent_ref:
            try:
                self._agent_ref.permission.set_rule(name, ALLOW)
            except Exception as e:
                logger.warning(f"设置技能 '{name}' 权限规则失败: {e}")
            try:
                self._agent_ref._rebuild_system_prompt()
            except Exception as e:
                logger.error(f"重建 system prompt 失败: {e}")

        return (
            f"✅ 技能 '{name}' 创建成功！\n"
            f"说明: {description}\n"
            f"指令长度: {len(instruction)} 字\n"
            f"\n"
            f"该技能已保存到 SKILLS/{name}/ 目录，并注册为可用工具。\n"
            f"后续对话中可以直接调用此技能来执行相同流程。"
        )
