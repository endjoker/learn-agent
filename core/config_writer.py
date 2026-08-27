# -*- coding: utf-8 -*-
"""
config 写盘安全套件 —— 由 init_wizard 提升为公共模块（P3a）

init_wizard 与 WebUI ConfigService 共用：
  - read_raw_config：裸读（绕过 load_config，防 env 注入的密钥被写回文件）
  - backup_file：写前备份（轮转保留最近 BACKUP_KEEP=3 份）
  - write_config：tmp + os.replace 原子写
  - mask_key / mask_dict：展示与 GET 响应脱敏（统一 <masked:N> 样式，
    不保留任何明文片段）
"""

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("jk_agent")

# 需脱敏的键名（大小写不敏感，子串匹配）。
# 在 api_key/token/secret/password/authorization 基础上扩充常见变体：
#   - encrypt_key / encryption_key（存储加密密钥）
#   - app_key / appkey（云厂商 SDK 键名）
#   - access_key（OSS/AWS 风格）、private_key
#   - 分隔符兼容连字符与无分隔写法（api-key / apikey / refresh-token 等，
#     后者由 token 子串覆盖）
# 注意：不收录裸 "key"——sort_key/primary_key 等普通键会被误伤。
_SECRET_KEY_RE = re.compile(
    r"(api[-_]?key"                 # api_key / api-key / apikey
    r"|app[-_]?key"                 # app_key / appkey
    r"|access[-_]?key"              # access_key / access-key
    r"|encrypt(?:ion|ed)?[-_]?key"  # encrypt_key / encryption_key / encrypted_key
    r"|private[-_]?key"             # private_key
    r"|token|secret|password|authorization)",
    re.IGNORECASE)


def is_secret_key_name(key: str) -> bool:
    """键名是否命中密钥脱敏模式（mask_dict 与 ConfigService 共用的唯一判定源）。"""
    return bool(_SECRET_KEY_RE.search(str(key)))

# mask_dict 递归深度上限：12 层不足以覆盖深层嵌套配置，提高上限；
# 超限后不再递归，但该层仍做顶层打码（不原样透出，见 mask_dict）
_MASK_MAX_DEPTH = 64

# 脱敏时直通的占位符（非真实密钥）
_MASK_PASSTHROUGH = ("not-needed", "ollama", "lm_studio", "YOUR_API_KEY_HERE")

# 写盘备份轮转保留数（P2：原为 10 且从不按需收紧，长期运行会无限累积
# config.json.bak-*；收敛为最近 N=3 份，调整此常量即可改变策略）。
BACKUP_KEEP = 3


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
    """备份现有文件 → {name}.bak-YYYYmmdd-HHMMSS-ffffff，轮转保留最近 keep 份。

    时间戳含微秒避免同秒覆盖；万一仍冲突（如同名文件已被手动创建）追加
    计数后缀。keep=0 表示只保留本次备份（清空全部旧备份）。
    轮转按 mtime 排序删除最旧的（P2 修复：文件名时间戳可能被手动改动，
    mtime 才是真实写入顺序），上限由常量 BACKUP_KEEP（默认 3）控制。
    备份失败（如权限不足）仅告警并返回 None，不影响后续主写入。
    """
    path = path or default_config_path()
    if not path.exists():
        return None
    try:
        base_ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        bak = path.parent / f"{path.name}.bak-{base_ts}"
        counter = 2
        while bak.exists():
            bak = path.parent / f"{path.name}.bak-{base_ts}-{counter}"
            counter += 1
        shutil.copy2(path, bak)
    except OSError as e:
        # 备份失败不中断主写入：告警并返回 None，调用方继续写配置
        logger.warning("备份 %s 失败: %s，继续主写入", path, e)
        return None
    # 清理超出保留数的旧备份；keep=0 → 清空全部旧备份（本次新建的保留）
    try:
        baks = list(path.parent.glob(f"{path.name}.bak-*"))
        baks.sort(key=lambda p: p.stat().st_mtime_ns)
    except OSError:
        return bak
    stale = baks[:-keep] if keep > 0 else baks[:-1]
    for old in stale:
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
    """密钥打码：固定样式 <masked:N>（N 为原值长度）。

    P1 修复：旧实现保留前 4 后 4 明文片段（sk-f1…d412），对中短密钥
    （≤12 位）等于泄露大半原文；现统一不保留任何明文片段，仅暴露长度
    供 UI 展示判断是否已配置。空值显示 "(空)"，演示占位值原样直通。
    """
    if not key or key in _MASK_PASSTHROUGH:
        return key or "(空)"
    return f"<masked:{len(key)}>"


def mask_dict(data, _depth: int = 0):
    """深脱敏：键名命中密钥模式的字符串值过 mask_key。

    返回新对象，不修改入参；递归深度上限 _MASK_MAX_DEPTH（64）层防环。
    深度超限时不再递归，但该层 dict 的顶层字符串值仍按密钥模式打码，
    避免深层嵌套结构中的密钥原样透出。
    """
    if _depth > _MASK_MAX_DEPTH:
        # 深度超限：浅打码后返回，而不是原样透出
        if isinstance(data, dict):
            return {
                k: mask_key(v) if isinstance(v, str) and is_secret_key_name(k) else v
                for k, v in data.items()
            }
        return data
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if isinstance(v, str) and is_secret_key_name(k):
                out[k] = mask_key(v)
            else:
                out[k] = mask_dict(v, _depth + 1)
        return out
    if isinstance(data, list):
        return [mask_dict(v, _depth + 1) for v in data]
    return data


def is_masked_placeholder(value) -> bool:
    """写入保留规则助手：空值/脱敏占位符 → 应保留原值。

    识别新旧两种占位样式：
      - 新：<masked:N>（mask_key 统一输出）
      - 旧：含省略号（sk-f1…d412）或 ****（≤8 位旧掩码）——
        兼容历史浏览器会话中缓存的旧掩码值回传，保证原密钥不被旧样式
        占位符覆盖写盘。
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if (not v) or (v == "****"):
        return True
    if v.startswith("<masked:") and v.endswith(">"):
        return True
    return "…" in v
