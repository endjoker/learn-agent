# -*- coding: utf-8 -*-
"""
ConfigService —— config 读写服务（P3c 建，P3d 复用）

写盘流水线（复用 core.config_writer）：
  asyncio.Lock 串行 → 段白名单校验 → base_rev 乐观并发 →
  裸读（绕过 load_config，防 env 注入密钥写回）→ 合并 →
  密钥保留规则 → backup_file → write_config（原子）→ force_reload

白名单（保守版，用户决策）：llm / gateway.sessions / prompt / workspace；
mcp.servers 走专用端点 update_mcp_servers（整表替换）。
permission / sandbox / hooks / gateway 主段 不开放 UI 编辑。
"""

import asyncio
import copy

from core.config_writer import (
    read_raw_config, write_config, backup_file, mask_dict,
    is_masked_placeholder, default_config_path,
)

# 通用 PATCH 可编辑段（mcp.servers 除外，走专用端点）
EDITABLE_SECTIONS = {"llm", "prompt", "workspace", "gateway.sessions"}

_SECRET_KEYS = ("api_key", "token", "secret", "password", "authorization")


class ConfigConflictError(Exception):
    """base_rev 不符（并发写冲突）"""


class ConfigService:
    def __init__(self):
        self._lock = asyncio.Lock()

    # ---------- 读 ----------

    @staticmethod
    def _rev() -> int:
        p = default_config_path()
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0

    def read_masked(self):
        """返回 (masked_config, rev, status)；corrupt 时 config 为 None"""
        data, status = read_raw_config()
        if status == "corrupt":
            return None, 0, status
        return mask_dict(data), self._rev(), status

    # ---------- 写：通用段 ----------

    async def patch_section(self, section: str, patch: dict,
                            base_rev: int = None) -> int:
        """PATCH 白名单段。返回新 rev。冲突抛 ConfigConflictError。"""
        if section not in EDITABLE_SECTIONS:
            raise PermissionError(
                f"段 {section} 不开放 UI 编辑（白名单: "
                f"{sorted(EDITABLE_SECTIONS)}）")
        if not isinstance(patch, dict):
            raise ValueError("patch 必须是对象")

        async with self._lock:
            if base_rev is not None and base_rev != self._rev():
                raise ConfigConflictError("config 已被修改，请刷新后重试")
            data, status = read_raw_config()
            if status == "corrupt":
                raise ValueError("config.json 损坏，请人工修复后重试")

            target = self._ensure_path(data, section)
            self._merge(target, patch)

            backup_file()
            write_config(None, data)
            self._force_reload()
            return self._rev()

    # ---------- 写：mcp.servers 整表 ----------

    async def update_mcp_servers(self, servers: list) -> int:
        """整表替换 mcp.servers（_deep_merge 对 list 整体替换的语义）"""
        if not isinstance(servers, list):
            raise ValueError("servers 必须是数组")
        async with self._lock:
            data, status = read_raw_config()
            if status == "corrupt":
                raise ValueError("config.json 损坏，请人工修复后重试")
            data.setdefault("mcp", {})["servers"] = servers
            backup_file()
            write_config(None, data)
            self._force_reload()
            return self._rev()

    # ---------- 工具 ----------

    @staticmethod
    def _force_reload():
        from core.config_loader import load_config
        load_config(force_reload=True)

    @staticmethod
    def _ensure_path(data: dict, dotted: str) -> dict:
        node = data
        for part in dotted.split("."):
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        return node

    def _merge(self, target: dict, patch: dict):
        """dict 递归合并；list 整体替换；密钥占位符保留原值"""
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(target.get(k), dict):
                self._merge(target[k], v)
            elif isinstance(v, str) and self._is_secret_key(k) \
                    and is_masked_placeholder(v):
                continue  # 保留原密钥
            else:
                target[k] = copy.deepcopy(v)

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        lk = str(key).lower()
        return any(s in lk for s in _SECRET_KEYS)
