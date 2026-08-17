# -*- coding: utf-8 -*-
r"""
PathValidator —— 工作区路径安全校验（Phase 3）。

覆盖：
- 路径清洗 / expanduser / 绝对化 / resolve
- Windows casefold / `\\?\` 防绕过
- UNC 开关（allow_unc，默认关闭）
- 系统路径 blocked（不枚举其他系统目录）
- 读写性、文件/目录
- Junction/Symlink 以最终 resolve 目标判断
- 敏感文件名称扫描（不读内容）
返回 PathValidationResult。
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import List, Optional

from gateway.webui.workspace_models import PathValidationResult

_IS_WINDOWS = os.name == "nt"

# 默认敏感文件名模式（仅名称匹配，不读内容）
DEFAULT_SENSITIVE_PATTERNS = [
    re.compile(r"^\.env($|\.)", re.IGNORECASE),
    re.compile(r"^\.git/config$", re.IGNORECASE),
    re.compile(r"^id_rsa$", re.IGNORECASE),
    re.compile(r"^id_ed25519$", re.IGNORECASE),
    re.compile(r"^credentials\.json$", re.IGNORECASE),
    re.compile(r"\.pem$", re.IGNORECASE),
    re.compile(r"\.key$", re.IGNORECASE),
]

# Windows 系统路径（大小写不敏感；resolve 后比较）
_SYSTEM_PATHS_WIN = [
    r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
    r"C:\System32", r"C:\Users\Default", r"C:\ProgramData",
]
# POSIX 系统路径
_SYSTEM_PATHS_POSIX = [
    "/etc", "/usr", "/boot", "/sys", "/proc", "/var", "/bin", "/sbin",
    "/lib", "/opt",
]


class PathValidator:
    """Windows/跨平台路径校验器。"""

    def __init__(self, *, allow_unc: bool = False,
                 sensitive_patterns: Optional[List[re.Pattern]] = None,
                 block_system_paths: bool = True):
        self.allow_unc = bool(allow_unc)
        self.sensitive_patterns = (
            list(sensitive_patterns) if sensitive_patterns
            else list(DEFAULT_SENSITIVE_PATTERNS))
        self.block_system_paths = block_system_paths

    # ---------- 核心校验 ----------

    def validate(self, raw_path: str, *, purpose: str = "project_root",
                 must_be_directory: bool = True,
                 base: Optional[Path] = None,
                 allowed_roots: Optional[List[Path]] = None,
                 risk_confirmed: bool = False) -> PathValidationResult:
        """校验路径，返回 PathValidationResult。

        purpose: project_root / working_directory / extra_root
        allowed_roots: working_directory 校验时用于判断是否越界
        """
        result = PathValidationResult(path=raw_path or "", normalized="")
        if not raw_path or not str(raw_path).strip():
            result.blocked = True
            result.status = "blocked"
            result.risk_level = "high"
            result.reasons.append("路径不能为空")
            return result

        cleaned = self._clean(str(raw_path))
        try:
            p = self._to_absolute(cleaned, base=base)
        except (ValueError, OSError) as exc:
            result.blocked = True
            result.status = "blocked"
            result.risk_level = "high"
            result.reasons.append(f"路径解析失败: {exc}")
            return result

        result.normalized = str(p)
        result.exists = p.exists()
        result.is_directory = p.is_dir() if result.exists else False
        if must_be_directory and result.exists and not result.is_directory:
            result.blocked = True
            result.status = "blocked"
            result.risk_level = "high"
            result.reasons.append("路径存在但不是目录")
            return result

        # UNC 检查
        if self._is_unc(p) and not self.allow_unc:
            result.blocked = True
            result.status = "blocked"
            result.risk_level = "high"
            result.reasons.append("UNC 路径被禁用（allow_unc=false）")
            return result

        # 系统路径
        if self.block_system_paths and self._is_system_path(p):
            result.blocked = True
            result.status = "blocked"
            result.risk_level = "high"
            result.reasons.append("系统路径不可作为工作区")
            return result

        # 工作目录越界
        if purpose == "working_directory" and allowed_roots:
            if not any(self._within(p, root) for root in allowed_roots):
                result.blocked = True
                result.status = "blocked"
                result.risk_level = "high"
                result.reasons.append("working_directory 越界：不在 project/extra roots 内")
                return result

        # 读写性
        if result.exists:
            result.readable = self._is_readable(p)
            result.writable = self._is_writable(p)
            if purpose in ("project_root", "working_directory", "extra_root"):
                if not result.readable:
                    result.blocked = True
                    result.status = "blocked"
                    result.risk_level = "high"
                    result.reasons.append("目录不可读")
                    return result
                if not result.writable:
                    result.warnings.append("目录不可写（只读工作区）")
                    result.risk_level = "medium"

        # 不存在：按 purpose 决定 warning/blocked
        if not result.exists:
            result.warnings.append("路径不存在；创建策略由创建向导确认")
            result.risk_level = "medium"

        # 敏感文件扫描（名称匹配，不读内容）
        sensitive = self._scan_sensitive(p)
        if sensitive:
            result.warnings.append(
                f"路径包含敏感文件/目录名: {', '.join(sensitive[:5])}")
            result.risk_level = "high"

        # risk 未确认
        if result.risk_level in ("high", "medium") and not risk_confirmed:
            result.status = "warning"
            result.reasons.append("存在风险项，需确认后才能创建")
        else:
            result.status = "ok"
        return result

    # ---------- 内部方法 ----------

    def _clean(self, raw: str) -> str:
        """清洗：去引号、strip、展开 ~。"""
        s = raw.strip().strip("\"'")
        if s.startswith("~"):
            s = str(Path(s).expanduser())
        return s

    def _to_absolute(self, path_str: str, base: Optional[Path] = None) -> Path:
        p = Path(path_str)
        if not p.is_absolute():
            base_p = (base or Path.cwd()).resolve()
            p = (base_p / p)
        # resolve 前去除 \\?\ 前缀（Windows 长路径），统一判定
        s = str(p)
        if _IS_WINDOWS and s.startswith("\\\\?\\"):
            s = s[4:]
        p = Path(s)
        return p.resolve()

    @staticmethod
    def _is_unc(p: Path) -> bool:
        s = str(p)
        if _IS_WINDOWS:
            # \\server\share 或 \\?\UNC\server\share
            return s.startswith("\\\\") or "UNC\\" in s.upper()
        return s.startswith("//")

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _is_system_path(self, p: Path) -> bool:
        s = str(p).replace("\\", "/")
        if s.startswith("/"):  # POSIX
            for sys_p in _SYSTEM_PATHS_POSIX:
                sp = sys_p.rstrip("/")
                if s == sp or s.startswith(sp + "/"):
                    return True
            return False
        if _IS_WINDOWS:
            try:
                norm = os.path.normpath(str(p)).lower()
            except Exception:
                norm = str(p).lower()
            for sys_p in _SYSTEM_PATHS_WIN:
                sp = os.path.normpath(sys_p).lower()
                if norm == sp or norm.startswith(sp + os.sep):
                    return True
        return False

    @staticmethod
    def _is_readable(p: Path) -> bool:
        try:
            if p.is_dir():
                os.scandir(p)
                return True
            return os.access(p, os.R_OK)
        except (OSError, PermissionError):
            return False

    @staticmethod
    def _is_writable(p: Path) -> bool:
        try:
            if p.is_dir():
                # 目录可写：尝试创建临时探测（不落盘）
                return os.access(p, os.W_OK | os.X_OK)
            return os.access(p, os.W_OK)
        except (OSError, PermissionError):
            return False

    def _scan_sensitive(self, p: Path) -> List[str]:
        """沿路径段 + 目录条目名做敏感名称匹配（只读文件名，不读内容）。"""
        hits = []
        try:
            parts = list(p.parts)
        except Exception:
            return hits
        for part in parts:
            if not part:
                continue
            name = part.rstrip("/\\")
            for pattern in self.sensitive_patterns:
                if pattern.search(name):
                    hits.append(name)
                    break
        # 扫描目录直接条目名（不递归、不读内容）
        if p.is_dir():
            try:
                for entry in os.scandir(p):
                    ename = entry.name
                    for pattern in self.sensitive_patterns:
                        if pattern.search(ename):
                            hits.append(ename)
                            break
            except (OSError, PermissionError):
                pass
        return hits


def default_validator() -> PathValidator:
    """从 config 构造默认校验器。"""
    try:
        from core.config_loader import load_config
        cfg = load_config().get("workspace", {})
        allow_unc = bool(cfg.get("allow_unc", False))
        patterns = [
            re.compile(str(x), re.IGNORECASE)
            for x in cfg.get("sensitive_file_patterns", [])
        ] or None
        return PathValidator(allow_unc=allow_unc, sensitive_patterns=patterns)
    except Exception:
        return PathValidator()
