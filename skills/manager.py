# -*- coding: utf-8 -*-
"""
技能管理器 —— 管理 SKILLS/ 目录下技能的磁盘 I/O 和生命周期

目录结构：
    SKILLS/
        code-review/
            skill.json          # 元数据
            instruction.md      # 核心指令
        research/
            ...
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .skill import Skill

logger = logging.getLogger("hello_agent")

# 技能名允许的字符
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9\-_]+$")


class SkillManager:
    """技能管理器：扫描 / 创建 / 删除 / 更新 SKILLS 目录下的技能"""

    def __init__(self, skills_dir: str = "SKILLS"):
        self._skills_dir = Path(skills_dir).resolve()
        self._skills: dict[str, Skill] = {}

    # ============================================================
    # 加载
    # ============================================================

    def load_all(self) -> List[Skill]:
        """扫描 SKILLS/ 目录，加载所有技能"""
        self._skills = {}
        if not self._skills_dir.exists():
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            return []

        for folder in sorted(self._skills_dir.iterdir()):
            if not folder.is_dir():
                continue
            json_path = folder / "skill.json"
            instr_path = folder / "instruction.md"
            if not json_path.exists() or not instr_path.exists():
                logger.warning(f"跳过不完整的技能目录: {folder.name}")
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                instruction = instr_path.read_text(encoding="utf-8")
                data["instruction"] = instruction
                skill = Skill.from_dict(data, source_dir=str(folder))
                self._skills[skill.name] = skill
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.warning(f"加载技能失败 '{folder.name}': {e}")
                continue

        return list(self._skills.values())

    # ============================================================
    # 查询
    # ============================================================

    def get_skill(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def get_all_skills(self) -> List[Skill]:
        return list(self._skills.values())

    def skill_count(self) -> int:
        return len(self._skills)

    def skill_exists(self, name: str) -> bool:
        return name in self._skills

    # ============================================================
    # 创建
    # ============================================================

    def create_skill(
        self,
        name: str,
        description: str,
        instruction: str,
        parameters: Optional[dict] = None,
        tags: Optional[List[str]] = None,
    ) -> Skill:
        """
        创建一个新技能（文件夹 + skill.json + instruction.md）

        异常:
            ValueError: 名称不合法或已存在
            OSError: 目录创建/文件写入失败
        """
        if not _VALID_NAME_RE.match(name):
            raise ValueError(f"技能名 '{name}' 不合法（仅允许字母、数字、-、_）")

        if name in self._skills:
            raise ValueError(f"技能 '{name}' 已存在")

        folder = self._skills_dir / name
        if folder.exists():
            raise ValueError(f"目录 SKILLS/{name}/ 已存在")

        now = datetime.now().isoformat(timespec="seconds")
        skill = Skill(
            name=name,
            description=description,
            instruction=instruction,
            parameters=parameters or {"type": "object", "properties": {}, "required": []},
            version=1,
            created_at=now,
            tags=tags or [],
            source_dir=str(folder),
        )

        # 写磁盘
        folder.mkdir(parents=True)
        try:
            with open(folder / "skill.json", "w", encoding="utf-8") as f:
                json.dump(skill.to_dict(), f, ensure_ascii=False, indent=2)
            (folder / "instruction.md").write_text(
                instruction.strip() + "\n", encoding="utf-8"
            )
        except Exception:
            # 清理不完整的目录
            import shutil
            shutil.rmtree(folder, ignore_errors=True)
            raise

        self._skills[name] = skill
        return skill

    # ============================================================
    # 删除
    # ============================================================

    def delete_skill(self, name: str) -> bool:
        """删除技能文件夹及其所有内容"""
        skill = self._skills.get(name)
        if not skill:
            return False

        folder = Path(skill.source_dir) if skill.source_dir else self._skills_dir / name
        if folder.exists():
            import shutil
            shutil.rmtree(folder, ignore_errors=True)

        self._skills.pop(name, None)
        return True

    # ============================================================
    # 更新
    # ============================================================

    def update_skill(
        self,
        name: str,
        *,
        description: Optional[str] = None,
        instruction: Optional[str] = None,
        parameters: Optional[dict] = None,
        tags: Optional[List[str]] = None,
    ) -> Optional[Skill]:
        """更新技能的元数据和/或指令"""
        skill = self._skills.get(name)
        if not skill:
            return None

        folder = Path(skill.source_dir) if skill.source_dir else self._skills_dir / name

        if description is not None:
            skill.description = description
        if parameters is not None:
            skill.parameters = parameters
        if tags is not None:
            skill.tags = tags
        if instruction is not None:
            skill.instruction = instruction
            (folder / "instruction.md").write_text(instruction.strip() + "\n", encoding="utf-8")

        skill.version += 1
        with open(folder / "skill.json", "w", encoding="utf-8") as f:
            json.dump(skill.to_dict(), f, ensure_ascii=False, indent=2)

        self._skills[name] = skill
        return skill

    # ============================================================
    # System Prompt 文本
    # ============================================================

    def get_skill_descriptions(self) -> str:
        """生成技能列表文本（供 SystemPrompt 使用）"""
        if not self._skills:
            return ""

        lines = []
        for skill in sorted(self._skills.values(), key=lambda s: s.name):
            lines.append(f"  ▶ {skill.name}")
            lines.append(f"    描述: {skill.description}")
            params = skill.parameters or {}
            props = params.get("properties", {})
            required = set(params.get("required", []) or [])
            if props:
                lines.append("    参数:")
                for pname, pinfo in props.items():
                    typ = pinfo.get("type", "string")
                    req = "必填" if pname in required else "可选"
                    desc = pinfo.get("description", "")
                    lines.append(f"      - {pname} ({typ})({req}): {desc}")
            lines.append("")
        return "\n".join(lines)
