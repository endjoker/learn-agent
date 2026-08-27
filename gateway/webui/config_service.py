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

注意（2026-08-26 设置页清理时核实）：GatewayServer 的 self.config 是
get_gateway_config() 返回的 **gateway 子段**（gateway/config.py），因此
"gateway.sessions" 就是会话参数的真实路径（sess_cfg = config.get("sessions")
读到的即 gateway.sessions）；workspace 段的 path 键当前无消费者（agent
工作区来自 permission.workspace），UI 已不再暴露该卡片，但段保留可编辑。
"""

import asyncio
import copy

from core.config_writer import (
    read_raw_config, write_config, backup_file, mask_dict,
    is_masked_placeholder, is_secret_key_name, default_config_path,
)

# 通用 PATCH 可编辑段（mcp.servers 除外，走专用端点）
EDITABLE_SECTIONS = {"llm", "prompt", "workspace", "gateway.sessions"}


class ConfigConflictError(Exception):
    """base_rev 不符（并发写冲突）。携带当前 rev 供客户端自动重试。"""

    def __init__(self, message: str, current_rev: int):
        super().__init__(message)
        self.current_rev = current_rev


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
            # 单用户本地部署：采用 last-write-wins，不再用 config.json mtime 做乐观锁。
            # mtime 每次写盘都变化，导致保存频繁 409；进程内 asyncio.Lock 已串行化并发写，
            # base_rev 仅保留参数兼容（忽略）。
            data, status = read_raw_config()
            if status == "corrupt":
                raise ValueError("config.json 损坏，请人工修复后重试")

            target = self._ensure_path(data, section)
            self._merge(target, patch)

            backup_file()
            write_config(None, data)
            self._force_reload()
            return self._rev()

    # ---------- 写：整表（持锁管线） ----------

    async def write_full(self, data: dict) -> int:
        """整表写盘（持锁管线：backup → write → force_reload）。

        供 api_settings.backup_and_write / main_session_put 等配置写盘统一走
        ConfigService 的 asyncio.Lock 串行化，避免绕过锁的并发写冲突。
        """
        async with self._lock:
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
        """密钥键名判定：复用 config_writer 的统一正则（含 encrypt_key/
        appkey/access_key 等扩充变体），保证读侧脱敏与写侧保留同源一致。"""
        return is_secret_key_name(key)
