# -*- coding: utf-8 -*-
"""
config 写盘安全套件 —— 由 init_wizard 提升为公共模块（P3a）

init_wizard 与 WebUI ConfigService 共用：
  - read_raw_config：裸读（绕过 load_config，防 env 注入的密钥被写回文件）
  - backup_file：写前备份（保留最近 10 份）
  - write_config：tmp + os.replace 原子写
  - mask_key / mask_dict：展示与 GET 响应脱敏
"""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# 需脱敏的键名（大小写不敏感）
_SECRET_KEY_RE = re.compile(
    r"(api_key|token|secret|password|authorization)", re.IGNORECASE)

# 脱敏时直通的占位符（非真实密钥）
_MASK_PASSTHROUGH = ("not-needed", "ollama", "lm_studio", "YOUR_API_KEY_HERE")

BACKUP_KEEP = 10


def default_config_path() -> Path:
    """项目根 config.json 路径"""
    from core.config_loader import _find_project_root
    return _find_project_root() / "config.json"


def read_raw_config(path: Optional[Path] = None) -> Tuple[dict, str]:
    """
    裸读 config.json（不合并默认值、不叠加环境变量）。
    返回 (data, status)，status ∈ "new" / "loaded" / "corrupt"
    """
    path = path or default_config_path()
    if not path.exists():
        return {}, "new"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, "corrupt"
        return data, "loaded"
    except (json.JSONDecodeError, OSError):
        return {}, "corrupt"


def backup_file(path: Optional[Path] = None,
                keep: int = BACKUP_KEEP) -> Optional[Path]:
    """备份现有文件 → {name}.bak-YYYYmmdd-HHMMSS，保留最近 keep 份"""
    path = path or default_config_path()
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.parent / f"{path.name}.bak-{ts}"
    shutil.copy2(path, bak)
    # 清理超出保留数的旧备份
    baks = sorted(path.parent.glob(f"{path.name}.bak-*"))
    for old in baks[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass
    return bak


def write_config(path: Optional[Path], data: dict) -> None:
    """原子写入：tmp + fsync + os.replace（复用 core.atomic_io）"""
    from core.atomic_io import atomic_write_bytes
    path = path or default_config_path()
    content = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        atomic_write_bytes(path, content, prefix=f".{path.name}.")
    except OSError as e:
        raise OSError(f"写入 {path.name} 失败: {e}") from e


def mask_key(key: str) -> str:
    """API Key 打码：sk-f1…d412，≤8 位全掩"""
    if not key or key in _MASK_PASSTHROUGH:
        return key or "(空)"
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def mask_dict(data, _depth: int = 0):
    """深脱敏：键名命中密钥模式的字符串值过 mask_key。

    返回新对象，不修改入参；限制递归深度防环。
    """
    if _depth > 12:
        return data
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if isinstance(v, str) and _SECRET_KEY_RE.search(str(k)):
                out[k] = mask_key(v)
            else:
                out[k] = mask_dict(v, _depth + 1)
        return out
    if isinstance(data, list):
        return [mask_dict(v, _depth + 1) for v in data]
    return data


def is_masked_placeholder(value) -> bool:
    """写入保留规则助手：新值为空/脱敏占位（含 … 或 ****）→ 应保留原值"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    return (not v) or ("…" in v) or (v == "****")
