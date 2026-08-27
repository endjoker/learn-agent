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
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .skill import Skill

logger = logging.getLogger("jk_agent")

# 技能名允许的字符
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9\-_]+$")

# ============================================================
# B8 类级 mtime 签名缓存
#   key   : str(resolved skills_dir)
#   value : (聚合签名, {name: Skill} 快照)
#   磁盘上 {skill.json / instruction.md} 的 (mtime_ns, size) 聚合签名
#   未变化时，load_all 直接复用内存解析快照，跳过读盘 + JSON 解析（快速路径）。
#   线程安全：所有缓存读写均在 _SKILL_LOAD_LOCK 内完成（简单 Lock）。
# ============================================================
_SIG_DIR_MISSING = ("missing",)
_SKILL_LOAD_CACHE: dict = {}
_SKILL_LOAD_LOCK = threading.Lock()


class SkillManager:
    """技能管理器：扫描 / 创建 / 删除 / 更新 SKILLS 目录下的技能"""

    def __init__(self, skills_dir: str = ""):
        if skills_dir:
            self._skills_dir = Path(skills_dir).resolve()
        else:
            # 基于项目根目录定位，不受 os.chdir 影响
            self._skills_dir = Path(__file__).resolve().parent.parent / "SKILLS"
        self._skills: dict[str, Skill] = {}

    # ============================================================
    # 加载
    # ============================================================

    def load_all(self) -> List[Skill]:
        """扫描 SKILLS/ 目录，加载所有技能。

        B8 快速路径：{skill.json / instruction.md} 的 mtime_ns+size 聚合签名
        未变化时，直接复用类级缓存中的内存解析快照（免读盘 + 免 JSON 解析）。
        """
        # 目录不存在时保持原语义：创建目录（签名按缺失处理，必然走慢路径）
        if self._compute_signature() == _SIG_DIR_MISSING:
            self._skills_dir.mkdir(parents=True, exist_ok=True)

        signature = self._compute_signature()
        key = str(self._skills_dir)

        with _SKILL_LOAD_LOCK:
            cached = _SKILL_LOAD_CACHE.get(key)
            if cached is not None and cached[0] == signature:
                # 快速路径：磁盘未变化，复用快照（浅拷贝，实例级变更不外泄）
                self._skills = dict(cached[1])
                return list(self._skills.values())

            # 慢路径：磁盘发生变化（或首次加载），全量解析并重建缓存
            skills = self._parse_all()
            _SKILL_LOAD_CACHE[key] = (signature, dict(skills))
            self._skills = skills
            return list(self._skills.values())

    def _compute_signature(self) -> tuple:
        """聚合签名：目录存在性 + 每个候选技能子目录下
        {skill.json / instruction.md} 的 (mtime_ns, size)。

        任一文件被修改/增删、目录增删都会改变签名 → 缓存失效。
        """
        if not self._skills_dir.exists():
            return _SIG_DIR_MISSING
        parts = ["dir"]
        try:
            folders = sorted(f for f in self._skills_dir.iterdir() if f.is_dir())
        except OSError:
            # 目录不可读时按缺失处理：走慢路径并让原有异常语义上抛
            return _SIG_DIR_MISSING
        for folder in folders:
            parts.append(folder.name)
            for fname in ("skill.json", "instruction.md"):
                try:
                    st = (folder / fname).stat()
                    parts.append((fname, st.st_mtime_ns, st.st_size))
                except OSError:
                    parts.append((fname, None))
        return tuple(parts)

    def _parse_all(self) -> dict:
        """从磁盘全量解析技能（B8 慢路径）。返回 {name: Skill}。"""
        skills: dict = {}
        if not self._skills_dir.exists():
            return skills

        # 内置工具名（与技能工具注册进同一注册表，重名会冲突）
        try:
            from tools.builtin_tools import BUILTIN_TOOLS
            builtin_names = {t.name for t in BUILTIN_TOOLS}
        except Exception:
            builtin_names = set()

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
                # 技能名合法性校验（与 create_skill 的 _VALID_NAME_RE 一致）
                if not _VALID_NAME_RE.match(skill.name):
                    logger.warning(
                        f"跳过技能 '{skill.name}': 名称不合法（仅允许字母、数字、-、_）")
                    continue
                # 与内置工具重名冲突校验
                if skill.name in builtin_names:
                    logger.warning(
                        f"跳过技能 '{skill.name}': 与内置工具重名冲突，无法注册")
                    continue
                skills[skill.name] = skill
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.warning(f"加载技能失败 '{folder.name}': {e}")
                continue

        return skills

    # ============================================================
    # 查询
    # ============================================================

    def get_skill(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def get_all_skills(self) -> List[Skill]:
        return list(self._skills.values())

    def skill_count(self) -> int:
        return len(self._skills)

    def get_catalog(self) -> list:
        """只读 Catalog API（Phase 2）：不实例化执行工具，只返回元数据。

        每项含 id/name/description/version/available/source；缓存按文件 mtime 由调用方刷新。
        """
        out = []
        for skill in self._skills.values():
            out.append({
                "id": skill.name,
                "name": skill.name,
                "description": skill.description,
                "version": skill.version,
                "available": True,
                "source": "skills",
            })
        return out

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
