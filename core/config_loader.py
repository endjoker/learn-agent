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
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("jk_agent")

# ============================================================
# 默认配置（所有 section 的硬编码 fallback）
# ============================================================

_DEFAULT_CONFIG: dict[str, Any] = {
    "agent_runtime": {
        "native_tool_streaming": True,
        "max_tool_result_chars": 12000,
        "max_tool_calls": 600,
        "max_parallel_tools": 4,
    },
    "goal": {"enabled": True, "max_active_per_session": 1},
    "subagent": {"enabled": True, "max_children": 4, "one_level_only": True},
    "retention": {"enabled": True, "terminal_days": 30, "artifact_days": 30, "interval_seconds": 3600},
    "runtime_store": {
        "backend": "sqlite",
        "path": "./workspace/.agent/state/runtime.db",
        "wal": True,
        "busy_timeout_ms": 5000,
    },
    # Feature flag: dispatcher integration is introduced incrementally.
    "task_runtime": {
        "enabled": True,
        "max_global_concurrency": 4,
        "default_timeout_seconds": 1200,
        "cancel_grace_seconds": 10,
        "zombie_max_seconds": 300,
        "max_attempts": 2,
        "channel_replay_max_attempts": 3,
    },
    "artifacts": {
        "root": "./workspace/.agent/artifacts",
        "max_file_bytes": 52428800,
        "retention_days": 30,
    },
    "llm": {
        "model_id": "",
        "timeout": 60,
        # provider_default means the field is omitted from API requests.
        "reasoning": {"level": "provider_default"},
        "models": {},
    },
    "permission": {
        "workspace": "./workspace",
        # Project root: enables source/config edits while paths outside this
        # repository remain outside the permission boundary.
        "extra_workspaces": ["."],
        "default_mode": "ask",
    },
    "hooks": {
        "enabled": True,
        "hooks": {
            "pre_tool": [{"matcher": "bash|write|file_mgr", "hooks": []}],
            "post_tool": [], "user_prompt": [], "notification": [],
            "denied": [], "stop": [],
            "session_start": [],
            "plan_approved": [],
        },
    },
    "mcp": {
        "servers": [],
    },
    "sandbox": {
        # L2 沙箱硬闸门默认关闭：安全兜底由 4 档权限（readonly/ask/allow/
        # unreviewed）承担；需要硬拦截时由 config.json → sandbox.enabled=true 开启。
        "enabled": False,
        "idle_timeout_seconds": 300,
        # 不默认保护项目文件；危险命令、系统路径等其他安全规则仍保留。
        "sensitive_files": [],
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
                "/etc", "/sys", "/proc", "/var/lib", "/boot",
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
        "python": {
            "forbidden_imports": ["ctypes", "socket"],
            "forbidden_calls": ["eval", "exec", "__import__"],
            "forbidden_qualified_calls": [
                "os.system", "os.popen", "os.execv", "os.execve",
                "os.execvp", "os.execvpe", "os.remove", "os.unlink",
                "os.rmdir", "os.kill", "os.killpg", "shutil.rmtree",
            ],
        },
    },
    "gateway": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 9120,
        "worker_pool_size": 4,
        "agent": {
            "model": "",
            "max_steps": 100,
            "permission_mode": "allow",
            "auto_approve_plan": True,
            "quiet": True,
        },
        "sessions": {
            "max_sessions": 50,
            "idle_timeout_minutes": 60,
            "persist": True,
            "soft_timeout_seconds": 90,
            "hard_timeout_seconds": 1200,
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
            "main_session": {"tools": None, "skills": None, "mcp_servers": None},
        },
    },
    "prompt": {
        "bootstrap_max_chars_per_file": 8000,
        "bootstrap_max_chars_total": 32000,
        "truncation_warning": "once",
    },
    "workspace": {
        "path": "./workspace",
        "dirs": ["ref", "scripts", "downloads", "tmp", "output"],
        "allow_unc": False, "block_system_paths": True, "list_limit": 100, "list_limit_max": 1000,
        "max_json_body_bytes": 1048576, "path_confirmation_required": True,
        "sensitive_file_patterns": [], "snapshot_retention_days": 30, "snapshot_retention_per_workspace": 20,
    },
}

def is_enabled(value, default: bool = True) -> bool:
    """解析配置开关值。

    兼容 bool 与字符串写法："false"/"0"/"no"/"off"（不区分大小写）视为关闭，
    其他非空字符串视为开启；None 与空/纯空白字符串视为"未设置"，回退到 default。
    边界说明：空字符串不视为开启（避免 "enabled": "" 意外启用某项功能）。
    """
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        return stripped.lower() not in ("false", "0", "no", "off")
    return bool(value)


# 缓存（load_config 并发安全：force_reload 与首次加载均在锁内完成）
_config_cache: Optional[dict] = None
_config_loaded: bool = False
_config_lock = threading.Lock()


# ============================================================
# 查找项目根目录
# ============================================================

def _find_project_root() -> Path:
    """从当前文件向上查找项目根目录（含 .git 或 agent.py 的目录）。

    fallback 说明：仅检查当前目录与其父目录两层——core/ 位于项目根下，
    core/config_loader.py 的 parent（即 core/ 的上级）应为项目根；两层都未
    命中（例如以第三方包方式安装进 site-packages）时回退到 current.parent 并
    照常返回。调用方仅用该路径定位 config.json / config.example.json，
    未命中时文件不存在，自然走默认配置分支，不产生其他副作用。
    """
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

    # 快路径：无锁读缓存（deepcopy 结果由调用方独享，安全）
    if _config_loaded and not force_reload:
        return copy.deepcopy(_config_cache)

    with _config_lock:
        # 双重检查：等待锁期间可能已被其他线程加载；
        # force_reload 在锁内重新加载并整体替换缓存，保证并发安全
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
    except json.JSONDecodeError as e:
        # JSON 损坏不再静默降级：显式报错，并把损坏文件改名留证
        logger.error("config.json 解析失败: %s；已改名为 .corrupt-<时间戳> 留证，使用默认配置", e)
        _quarantine_corrupt(path)
        return copy.deepcopy(_DEFAULT_CONFIG)
    except OSError as e:
        logger.error("config.json 读取失败: %s，使用默认配置", e)
        return copy.deepcopy(_DEFAULT_CONFIG)

    if not isinstance(user_cfg, dict):
        # 顶层非对象（如 JSON 数组）同样视为损坏，避免 _deep_merge 对非 dict 崩溃
        logger.error("config.json 顶层必须是 JSON 对象，实际为 %s；已改名为 .corrupt-<时间戳> 留证，使用默认配置",
                     type(user_cfg).__name__)
        _quarantine_corrupt(path)
        return copy.deepcopy(_DEFAULT_CONFIG)

    return _deep_merge(copy.deepcopy(_DEFAULT_CONFIG), user_cfg)


def _quarantine_corrupt(path: Path) -> None:
    """把损坏的 config.json 改名 .corrupt-<ts> 留证；改名失败仅告警不中断。"""
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"{path.name}.corrupt-{ts}")
        path.rename(target)
    except OSError as e:
        logger.warning("损坏的 config.json 改名失败: %s", e)


def _apply_env_overrides(config: dict) -> dict:
    """环境变量覆盖（API Key 敏感信息优先从环境变量读取）"""
    llm = config.get("llm")
    # 类型守卫：llm 段被配置文件写成非 dict（如字符串/数组）时跳过，不再 AttributeError 崩溃
    if not isinstance(llm, dict):
        if llm is not None:
            logger.warning("config.llm 不是字典（%s），跳过环境变量覆盖", type(llm).__name__)
        return config

    models = llm.get("models")
    if models is not None and not isinstance(models, dict):
        logger.warning("config.llm.models 不是字典（%s），跳过模型级环境变量覆盖", type(models).__name__)
    else:
        # ANTHROPIC_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY 覆盖模型配置。
        # openai / anthropic / gemini 语义对称：环境变量非空即覆盖（环境变量优先级最高），
        # 与模块优先级注释（config.json → 环境变量 → 默认值）一致。
        for model_name, model_cfg in (models or {}).items():
            if not isinstance(model_cfg, dict):
                logger.warning("config.llm.models.%s 不是字典（%s），跳过该模型的环境变量覆盖",
                               model_name, type(model_cfg).__name__)
                continue
            protocol = model_cfg.get("protocol", "openai")
            if protocol == "anthropic":
                env_key = os.getenv("ANTHROPIC_API_KEY", "")
            elif protocol == "gemini":
                env_key = os.getenv("GEMINI_API_KEY", "")
            elif protocol == "openai":
                env_key = os.getenv("OPENAI_API_KEY", "")
            else:
                env_key = ""
            if env_key:
                model_cfg["api_key"] = env_key
                logger.debug("环境变量 %s 覆盖 llm.models.%s.api_key",
                             _ENV_KEY_FOR_PROTOCOL.get(protocol, "?"), model_name)

    # LLM_PROTOCOL 全局覆盖（models 缺失时创建；非 dict 时跳过并告警）
    env_protocol = os.getenv("LLM_PROTOCOL", "")
    if env_protocol:
        current_model = llm.get("model_id", "")
        if current_model:
            models_holder = llm.get("models")
            if models_holder is None:
                models_holder = {}
                llm["models"] = models_holder
            if isinstance(models_holder, dict):
                target = models_holder.setdefault(current_model, {})
                if isinstance(target, dict):
                    target["protocol"] = env_protocol
                else:
                    logger.warning("config.llm.models.%s 不是字典，跳过 LLM_PROTOCOL 覆盖", current_model)
            else:
                logger.warning("config.llm.models 不是字典，跳过 LLM_PROTOCOL 覆盖")

    # Optional operational override.  It intentionally applies globally so
    # deployments can tune latency/cost without rewriting model credentials.
    env_reasoning = os.getenv("LLM_REASONING_EFFORT", "")
    if env_reasoning:
        reasoning = llm.get("reasoning")
        if reasoning is None:
            reasoning = {}
            llm["reasoning"] = reasoning
        if isinstance(reasoning, dict):
            reasoning["level"] = env_reasoning
        else:
            logger.warning("config.llm.reasoning 不是字典，跳过 LLM_REASONING_EFFORT 覆盖")

    return config


# 协议 → 环境变量名（仅用于日志）
_ENV_KEY_FOR_PROTOCOL = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


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
