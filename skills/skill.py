# -*- coding: utf-8 -*-
"""
技能数据模型 —— Skill dataclass
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Skill:
    """内存中的技能表示"""
    name: str
    description: str
    instruction: str           # instruction.md 内容
    parameters: dict           # JSON Schema
    version: int = 1
    created_at: str = ""       # ISO 8601
    tags: List[str] = field(default_factory=list)
    source_dir: str = ""       # 文件夹路径（加载时自动设置）

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "created_at": self.created_at,
            "tags": self.tags,
            "parameters": self.parameters,
        }

    @staticmethod
    def from_dict(data: dict, source_dir: str = "") -> "Skill":
        return Skill(
            name=data["name"],
            description=data.get("description", ""),
            instruction=data.get("instruction", ""),
            parameters=data.get("parameters", {"type": "object", "properties": {}, "required": []}),
            version=data.get("version", 1),
            created_at=data.get("created_at", ""),
            tags=data.get("tags", []),
            source_dir=source_dir,
        )
