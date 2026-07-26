# -*- coding: utf-8 -*-
"""
配置档管理 —— 从 config.json 的 sandbox section 加载配置，提供运行时可切换的配置档
"""

import copy
import logging
from typing import Any

logger = logging.getLogger("hello_agent")

# 默认配置（config.json 无 sandbox section 时使用）
_DEFAULT_CONFIG = {
    "enabled": True,
    "default_profile": "agent",
    "idle_timeout_seconds": 300,   # 长驻进程空闲上限（5 分钟无 read/send → kill），供 ProcessManager 用
    "network": {
        "blocked_domains": [
            "*.xyz",
            "*.tk",
            "*.ml",
            "*.ga",
            "*.cf",
            "*.gq",
            "*.top",
            "*.loan",
            "*.date",
            "*.men",
        ],
        "blocked_ips": [
            "10.0.0.0/8",
            "172.16.0.0/12"
        ],
    },
    "profiles": {
        "agent": {
            "memory_mb": 512,
            "cpu_seconds": 30,
            "timeout_seconds": 60,
            "max_processes": 50,
            "max_output_mb": 10,
            "max_files": 256,
            "stack_mb": 8,
            "core_dump": False,
            "network": True,
        },
        "restricted": {
            "memory_mb": 64,
            "cpu_seconds": 5,
            "timeout_seconds": 10,
            "max_processes": 5,
            "max_output_mb": 1,
            "max_files": 64,
            "stack_mb": 2,
            "core_dump": False,
            "network": False,
        },
        "permissive": {
            "memory_mb": 2048,
            "cpu_seconds": 120,
            "timeout_seconds": 300,
            "max_processes": 200,
            "max_output_mb": 100,
            "max_files": 1024,
            "stack_mb": 32,
            "core_dump": False,
            "network": True,
        },
    },
}



def load_config() -> dict[str, Any]:
    """
    加载沙箱配置 —— 从 config.json 的 sandbox section 读取，
    无配置时返回默认值。
    """
    try:
        from core.config_loader import load_config as _load_unified
        unified = _load_unified()
        sandbox_cfg = unified.get("sandbox", {})
        if sandbox_cfg:
            merged = _deep_merge(copy.deepcopy(_DEFAULT_CONFIG), sandbox_cfg)
            logger.info("从 config.json 加载了沙箱配置")
            return merged
    except Exception:
        pass

    return _DEFAULT_CONFIG


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def get_profile(config: dict, name: str) -> dict | None:
    """获取指定配置档，不存在返回 None"""
    return config.get("profiles", {}).get(name)


def list_profiles(config: dict) -> list[str]:
    """列出所有可用配置档名"""
    return list(config.get("profiles", {}).keys())


def get_network_config(config: dict) -> dict:
    """获取网络控制配置"""
    return config.get("network", _DEFAULT_CONFIG["network"])
