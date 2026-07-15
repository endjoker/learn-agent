# -*- coding: utf-8 -*-
"""
技能系统 —— AI 学习到的能力，持久化存储在 SKILLS/ 目录

提供 SkillManager（CRUD）、SkillTool（执行适配器）、CreateSkillTool（运行时创建）
"""

from .skill import Skill
from .manager import SkillManager
from .skill_tool import SkillTool, CreateSkillTool

__all__ = ["Skill", "SkillManager", "SkillTool", "CreateSkillTool"]
