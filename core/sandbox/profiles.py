# -*- coding: utf-8 -*-
"""
配置档管理 —— 加载 config/sandbox.json，提供运行时可切换的配置
"""

import json
import os
import copy
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("hello_agent")

# 默认配置（config/sandbox.json 不存在时使用）
_DEFAULT_CONFIG = {
    "enabled": True,
    "default_profile": "agent",
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

_CONFIG_PATH = Path("config") / "sandbox.json"


def _find_config() -> Path | None:
    """在标准路径查找 sandbox.json"""
    candidates = [
        Path.cwd() / "config" / "sandbox.json",
        Path.cwd() / "sandbox.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def load_config() -> dict[str, Any]:
    """
    加载 config/sandbox.json，失败时返回默认配置
    """
    cfg_path = _find_config()
    if not cfg_path:
        return _DEFAULT_CONFIG

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        # 合并：用户配置覆盖默认（深拷贝默认配置，避免就地修改污染模块级 _DEFAULT_CONFIG）
        merged = _deep_merge(copy.deepcopy(_DEFAULT_CONFIG), user_cfg)
        logger.info("从 %s 加载了沙箱配置", cfg_path.name)
        return merged
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("加载沙箱配置失败 %s: %s，使用默认配置", cfg_path, e)
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
