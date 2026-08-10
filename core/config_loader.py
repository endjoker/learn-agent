# -*- coding: utf-8 -*-
"""
统一配置加载器 —— 从 config.json 读取所有配置

优先级:
  1. config.json（项目根目录）—— 统一配置文件
  2. 环境变量覆盖（API Key 等敏感信息）
  3. 硬编码默认值

用法:
    from core.config_loader import load_config
    config = load_config()
    llm_cfg = config["llm"]
"""

import json
import os
import copy
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hello_agent")

# ============================================================
# 默认配置（所有 section 的硬编码 fallback）
# ============================================================

_DEFAULT_CONFIG: dict[str, Any] = {
    "agent_runtime": {
        "response_protocol": "auto",
        "legacy_execute": False,
        "protocol_retry_limit": 1,
        "raw_response_audit": "errors",
        "raw_response_max_chars": 20000,
        "native_tool_streaming": False,
    },
    "llm": {
        "model_id": "",
        "timeout": 60,
        "models": {},
    },
    "permission": {
        "workspace": "./workspace",
        "default_mode": "ask",
        "tool_rules": {
            # 注：只列出有固定权限的工具。bash/read/write/edit/glob/file_mgr
            # 使用动态回调（如路径检查、命令分类），不在 tool_rules 中配置固定值。
            # 如需覆盖，可在 config.json 中显式设置 "bash": "allow" 等。
            "grep": "allow", "datetime": "allow", "calculate": "allow",
            "notes": "allow", "memory_search": "allow", "memory_update": "allow",
            "search": "allow", "web_fetch": "allow", "create_skill": "allow",
            "python": "ask", "http": "ask",
        },
        "bash_commands": {
            "readonly": [
                "ls", "dir", "pwd", "echo", "cat", "type", "more", "less",
                "head", "tail", "findstr", "where", "which",
                "git status", "git log", "git diff", "git show", "git branch",
                "git stash list", "git remote -v", "git config",
                "pip list", "pip show",
                "python --version", "python3 --version",
                "node --version", "npm --version",
            ],
            "write": [
                "rm", "del", "rd", "rmdir",
                "mv", "move", "rename",
                "cp", "copy", "xcopy", "robocopy",
                "mkdir", "md",
                "git add", "git commit", "git push", "git pull",
                "git merge", "git rebase", "git reset",
                "git checkout -b", "git branch -d", "git tag",
                "pip install", "pip uninstall", "pip update",
                "npm install", "npm uninstall",
                "chmod", "chown",
                "taskkill", "kill -9",
            ],
        },
    },
    "hooks": {
        "enabled": True,
        "hooks": {
            "pre_tool": [{"matcher": "bash|write|file_mgr", "hooks": []}],
            "post_tool": [], "user_prompt": [], "notification": [],
            "denied": [], "stop": [], "pre_llm": [], "post_llm": [],
            "session_start": [], "session_end": [],
            "plan_approved": [], "task_complete": [],
        },
    },
    "mcp": {
        "servers": [],
    },
    "sandbox": {
        "enabled": True,
        "idle_timeout_seconds": 300,
        "sensitive_files": [
            ".env", ".git/config",
            "core/permission.py", "core/llm_client.py",
            "agent.py", "requirements.txt",
        ],
        "dangerous_commands": [
            "rm -rf /", "rm -rf ~", "rm -rf .",
            "del /f /s", "rd /s /q",
            "diskpart", "taskkill /f",
            "chmod 0", "chown -r",
            "mkfs", "dd if=",
            ":(){ :|:& };:",
            "> /dev/sda", "> /dev/mmc",
        ],
        "dangerous_words": ["format", "shutdown", "reboot", "halt"],
        "system_paths": {
            "windows": [
                "C:\\Windows", "C:\\Program Files",
                "C:\\Program Files (x86)", "C:\\System32",
            ],
            "linux": [
                "/etc", "/usr", "/boot", "/sys", "/proc",
                "/var/log", "/var/lib",
            ],
            "mac": ["/System", "/Library", "/Applications"],
        },
        "network": {
            "blocked_domains": [
                "*.xyz", "*.tk", "*.ml", "*.ga", "*.cf",
                "*.gq", "*.top", "*.loan", "*.date", "*.men",
            ],
            "blocked_ips": ["10.0.0.0/8", "172.16.0.0/12"],
        },
    },
    "gateway": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 9120,
        "worker_pool_size": 4,
        "agent": {
            "model": "",
            "max_steps": 30,
            "permission_mode": "allow",
            "auto_approve_plan": True,
            "quiet": True,
        },
        "sessions": {
            "max_sessions": 50,
            "idle_timeout_minutes": 60,
            "persist": True,
            "soft_timeout_seconds": 90,
            "hard_timeout_seconds": 600,
        },
        "scheduler": {
            "enabled": True,
            "max_concurrent": 2,
            "misfire_policy": "skip",
            "history_limit": 50,
            "jobs": [],
        },
        "heartbeat": {
            "enabled": True,
            "every": "30m",
            "session_key": "heartbeat:main",
            "active_hours": "08:00-22:00",
            "prompt_file": "workspace/HEARTBEAT.md",
            "deliver": {"mode": "none"},
            "defer_when_busy": True,
        },
        "channels": {
            "debug": {"enabled": True},
            "webui": {"enabled": True},
            "feishu": {
                "enabled": False,
                "mode": "websocket",
                "app_id": "",
                "app_secret": "",
                "encrypt_key": "",
                "verification_token": "",
            },
            "weixin": {
                "enabled": False,
                "credentials_file": "gateway/creds/weixin.json",
                "allow_from": [],
                "reply_format": "markdown",
            },
        },
        "webui": {
            "allow_non_loopback": False,
            "auth_token": "",
            "allowed_ips": [],
        },
    },
}

def is_enabled(value, default: bool = True) -> bool:
    """解析配置开关值。

    兼容 bool 与字符串写法：`"false"/"0"/"no"/"off"`（不区分大小写）视为关闭，
    其他非空字符串视为开启；None 使用默认值。
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


# 缓存
_config_cache: Optional[dict] = None
_config_loaded: bool = False


# ============================================================
# 查找项目根目录
# ============================================================

def _find_project_root() -> Path:
    """从当前文件向上查找项目根目录（含 .git 或 agent.py 的目录）"""
    current = Path(__file__).resolve().parent
    for parent in [current, current.parent]:
        if (parent / "agent.py").exists() or (parent / ".git").exists():
            return parent
    return current.parent  # fallback


# ============================================================
# 主加载函数
# ============================================================

def load_config(config_path: Optional[str] = None, force_reload: bool = False) -> dict:
    """
    加载统一配置。

    查找顺序:
      1. 指定的 config_path
      2. 项目根目录的 config.json
      3. 硬编码默认值

    参数:
        config_path:  配置文件路径（可选）
        force_reload: 强制重新加载（忽略缓存）

    返回:
        完整配置字典（deepcopy，安全修改）
    """
    global _config_cache, _config_loaded

    if _config_loaded and not force_reload:
        return copy.deepcopy(_config_cache)

    project_root = _find_project_root()

    # ---- 尝试加载 config.json ----
    if config_path:
        cfg_path = Path(config_path)
    else:
        cfg_path = project_root / "config.json"

    if cfg_path.exists():
        config = _load_json_config(cfg_path)
        logger.info("从 %s 加载了统一配置", cfg_path)
    else:
        logger.info("未找到 config.json，使用默认配置")
        config = copy.deepcopy(_DEFAULT_CONFIG)

    # ---- 环境变量覆盖 ----
    config = _apply_env_overrides(config)

    # ---- 缓存 ----
    _config_cache = config
    _config_loaded = True

    return copy.deepcopy(config)


def _load_json_config(path: Path) -> dict:
    """从 config.json 加载，用默认值填补缺失的 section/key"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("config.json 解析失败: %s，使用默认配置", e)
        return copy.deepcopy(_DEFAULT_CONFIG)

    return _deep_merge(copy.deepcopy(_DEFAULT_CONFIG), user_cfg)


def _apply_env_overrides(config: dict) -> dict:
    """环境变量覆盖（API Key 敏感信息优先从环境变量读取）"""
    # ANTHROPIC_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY 覆盖模型配置
    for model_name, model_cfg in config.get("llm", {}).get("models", {}).items():
        protocol = model_cfg.get("protocol", "openai")
        if protocol == "anthropic":
            env_key = os.getenv("ANTHROPIC_API_KEY", "")
            if env_key:
                model_cfg["api_key"] = env_key
        elif protocol == "gemini":
            env_key = os.getenv("GEMINI_API_KEY", "")
            if env_key:
                model_cfg["api_key"] = env_key
        elif protocol == "openai" and not model_cfg.get("api_key"):
            env_key = os.getenv("OPENAI_API_KEY", "")
            if env_key:
                model_cfg["api_key"] = env_key

    # LLM_PROTOCOL 全局覆盖
    env_protocol = os.getenv("LLM_PROTOCOL", "")
    if env_protocol:
        current_model = config.get("llm", {}).get("model_id", "")
        if current_model:
            config["llm"].setdefault("models", {}).setdefault(current_model, {})["protocol"] = env_protocol

    return config


# ============================================================
# 辅助
# ============================================================

def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 覆盖 base"""
    for key, value in override.items():
        if (key in base and isinstance(base[key], dict)
                and isinstance(value, dict)):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def export_config(path: Optional[str] = None) -> str:
    """导出当前配置为 JSON 字符串（用于迁移）"""
    config = load_config()
    if path:
        cfg_path = Path(path)
        # 如果指定了路径且 config.json 使用方需要相对路径，我们这里直接保存
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        logger.info("配置已导出到 %s", path)
    return json.dumps(config, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # 独立测试
    import sys
    cfg = load_config()
    if "--export" in sys.argv:
        print(export_config())
    else:
        for section in ["llm", "permission", "hooks", "mcp", "sandbox"]:
            print(f"[{section}] {'OK' if section in cfg else 'MISSING'}")
        print(f"\n当前模型: {cfg['llm']['model_id']}")
        print(f"已配置模型: {list(cfg['llm']['models'].keys())}")
