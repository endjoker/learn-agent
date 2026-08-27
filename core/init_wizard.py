# -*- coding: utf-8 -*-
"""
交互式初始化向导 —— python agent.py init

引导用户配置 LLM 模型、MCP 服务器、Hooks 等核心选项，
可选配置 permission / sandbox 高级选项。
配置写入 config.json，下次启动生效。

设计要点:
  - 裸读 config.json（不用 load_config），避免默认值膨胀和环境变量密钥泄露
  - 全程只收集 fragments，最后确认 → 备份 → 原子写，Ctrl+C 零副作用
  - 稀疏写入：只固化用户显式选择/确认的值，默认值留在代码里随版本演进
  - MCP servers 返回完整列表（_deep_merge 对 list 是整体替换）
"""

import copy
import getpass
import ipaddress
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.config_loader import _deep_merge, _find_project_root, _DEFAULT_CONFIG, is_enabled
from core.reasoning import REASONING_LEVELS

# ============================================================
# 常量（数值与 llm_client.LOCAL_PROVIDERS / protocols 保持一致）
# ============================================================

_PROVIDER_DEFAULTS = {
    "ollama": {
        "name": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "desc": "最易用的本地模型运行工具",
    },
    "lm_studio": {
        "name": "LM Studio",
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm_studio",
        "desc": "图形化本地模型管理",
    },
    "vllm": {
        "name": "vLLM",
        "base_url": "http://localhost:8000/v1",
        "api_key": "not-needed",
        "desc": "高性能推理引擎",
    },
    "llama_cpp": {
        "name": "llama.cpp",
        "base_url": "http://localhost:8080/v1",
        "api_key": "not-needed",
        "desc": "轻量级 C++ 推理",
    },
}

_CLOUD_PROTOCOLS = [
    ("openai", "OpenAI 及兼容服务", "https://api.openai.com/v1"),
    ("anthropic", "Anthropic（Claude 系列）", "https://api.anthropic.com"),
    ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta"),
]

_MCP_TRANSPORTS = [
    ("stdio", "本地进程（command + args）"),
    ("streamable", "Streamable HTTP"),
    ("http+sse", "HTTP + SSE"),
]

# 云端协议对应的环境变量提示
_ENV_KEY_HINTS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


# ============================================================
# 输入原语（house style：中文提示、两格缩进、emoji）
# ============================================================

def _ask(prompt: str, default: str = "") -> str:
    """读取一行输入，回车返回 default"""
    suffix = f" (默认 {default})" if default else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val if val else default


def _ask_int(prompt: str, default: int) -> int:
    """读取整数，非法输入循环重问"""
    while True:
        val = input(f"  {prompt} (默认 {default}): ").strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            print(f"  ❗ 请输入整数，如 {default}")


def _as_int(value, default: int) -> int:
    """把配置值安全转 int；缺失/非法（旧配置中的字符串脏值等）回退默认值。

    向导把现有配置值当作输入默认值时使用，避免 int("abc") 直接崩溃。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_valid_host(value: str) -> bool:
    """监听地址基础校验：IP（v4/v6）或合法主机名。"""
    value = value.strip()
    if not value or any(ch.isspace() for ch in value):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    # 主机名：字母/数字/点/连字符/下划线，不以连字符开头
    return (not value.startswith("-")
            and all(ch.isalnum() or ch in ".-_" for ch in value))


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    """[Y/n] 或 [y/N] 确认，与 agent.py 的 yes 集合一致"""
    hint = "[Y/n]" if default else "[y/N]"
    val = input(f"  {prompt} {hint} ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "是")


def _ask_choice(title: str, options: list[tuple[str, str]], default: int = 1) -> str:
    """
    数字菜单。options = [(key, desc), ...]，返回选中的 key。
    回车选 default（1-indexed）。
    """
    print(f"  {title}")
    for i, (key, desc) in enumerate(options, 1):
        print(f"    {i}. {desc}")
    while True:
        val = input(f"  请选择 [1-{len(options)}] (默认{default}): ").strip()
        if not val:
            return options[default - 1][0]
        try:
            idx = int(val)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        except ValueError:
            pass
        print(f"  ❗ 请输入 1-{len(options)} 的数字")


def _ask_secret(prompt: str) -> str:
    """隐藏输入（API Key），非 tty 回退明文"""
    try:
        val = getpass.getpass(f"  {prompt}: ").strip()
    except Exception:
        print("  ⚠️ 当前终端不支持隐藏输入，将明文显示")
        val = input(f"  {prompt}: ").strip()
    return val


def _ask_kv_lines(prompt: str) -> dict:
    """循环读取 KEY=VAL 行，空行结束。用于 MCP env / headers"""
    print(f"  {prompt}（KEY=VAL 每行一条，空行结束）:")
    result = {}
    while True:
        line = input("    > ").strip()
        if not line:
            break
        if "=" not in line:
            print("    ❗ 格式: KEY=VAL")
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip()
    return result


# ============================================================
# 文件 I/O（实现已提升为 core.config_writer，此处保留别名兼容旧调用）
# ============================================================

from core.config_writer import (  # noqa: E402
    read_raw_config as _read_raw_config,
    backup_file as _backup_file,
    write_config as _write_config,
    mask_key as _mask_key,
)


def _handle_corrupt(path: Path) -> Optional[dict]:
    """损坏文件处理菜单。返回 {}（重命名后重来）或 None（中止）"""
    print(f"\n  ❌ config.json 解析失败，无法安全地增量修改")
    print(f"    1. 中止，我手动修复后重跑（推荐）")
    print(f"    2. 把损坏文件改名为 .broken-<时间戳>，按全新配置开始")
    while True:
        val = input("  请选择 [1/2] (默认1): ").strip()
        if val in ("", "1"):
            return None
        if val == "2":
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            broken = path.with_suffix(f".broken-{ts}")
            path.rename(broken)
            print(f"  📦 已改名: {broken.name}")
            return {}
        print("  ❗ 请输入 1 或 2")


def _overview(existing: dict) -> None:
    """打印当前配置概览"""
    llm = existing.get("llm", {})
    models = llm.get("models", {})
    model_id = llm.get("model_id", "(未设置)")
    print(f"  当前默认模型: {model_id}")
    if models:
        for name, cfg in models.items():
            key_hint = f", key {_mask_key(cfg.get('api_key', ''))}" if cfg.get("api_key") else ""
            url = cfg.get("base_url", "本地预设")
            print(f"    · {name} → {url}{key_hint}")
    else:
        print(f"    (暂无已配置模型)")

    mcp = existing.get("mcp", {})
    servers = mcp.get("servers", [])
    if servers:
        names = ", ".join(s.get("name", "?") for s in servers)
        print(f"  MCP 服务器:   {names}")
    else:
        print(f"  MCP 服务器:   (无)")

    hooks = existing.get("hooks", {})
    enabled = hooks.get("enabled", True)
    print(f"  Hooks:        {'✅ 已启用' if enabled else '❌ 已禁用'}")


# ============================================================
# 步骤 1：LLM 模型
# ============================================================

def _collect_one_model(existing_models: dict) -> Optional[tuple[str, dict]]:
    """收集单个模型配置，返回 (name, model_dict) 或 None"""
    print()
    name = _ask("模型名称 (如 qwen-max / claude-sonnet)")
    if not name:
        print("  ⚠️ 模型名不能为空，已跳过")
        return None
    if name in existing_models:
        if not _ask_yes_no(f"  模型 '{name}' 已存在，覆盖？", default=False):
            print("  已跳过")
            return None

    # 本地 or 云端
    model_type = _ask_choice("模型类型:", [
        ("local", "本地模型（Ollama / LM Studio / vLLM / llama.cpp）"),
        ("cloud", "云端 API（OpenAI 兼容 / Anthropic / Gemini）"),
    ], default=2)

    model_cfg: dict[str, Any] = {}

    if model_type == "local":
        # 选 provider
        provider_opts = [(k, f"{v['name']} — {v['desc']}") for k, v in _PROVIDER_DEFAULTS.items()]
        provider = _ask_choice("本地服务:", provider_opts, default=1)
        preset = _PROVIDER_DEFAULTS[provider]
        model_cfg["provider"] = provider

        url = _ask("Base URL", preset["base_url"])
        if url:
            model_cfg["base_url"] = url

        # 本地 key 通常无敏感性，明文即可
        key = _ask("API Key", preset["api_key"])
        if key:
            model_cfg["api_key"] = key

        ctx = _ask_int("上下文长度 (tokens)", 131072)
        if ctx:
            model_cfg["context_length"] = ctx

    else:
        # 云端：选协议
        proto_opts = [(p, d) for p, d, _ in _CLOUD_PROTOCOLS]
        protocol = _ask_choice("协议:", proto_opts, default=1)
        model_cfg["protocol"] = protocol

        # 默认 URL
        default_url = next(u for p, _, u in _CLOUD_PROTOCOLS if p == protocol)
        url = _ask("Base URL", default_url)
        if url:
            if "://" not in url:
                print("  ⚠️ URL 应包含 ://（如 https://...），已按原样保存")
            model_cfg["base_url"] = url

        # API Key（隐藏输入）
        env_hint = _ENV_KEY_HINTS.get(protocol, "")
        key_prompt = f"API Key（也可用环境变量 {env_hint} 提供）" if env_hint else "API Key"
        key = _ask_secret(key_prompt)
        if key:
            model_cfg["api_key"] = key
        else:
            print(f"  💡 未填写 Key，运行时将从环境变量 {env_hint} 读取" if env_hint
                  else "  💡 未填写 Key")

        ctx = _ask_int("上下文长度 (tokens)", 128000)
        if ctx:
            model_cfg["context_length"] = ctx

    reasoning_options = [
        ("provider_default", "跟随服务商默认（推荐）"),
        ("none", "关闭推理"),
        ("minimal", "极低"),
        ("low", "低"),
        ("medium", "中"),
        ("high", "高"),
        ("xhigh", "极高"),
        ("max", "最大"),
    ]
    # Keep this assertion next to the UI options so new public values are not
    # accidentally omitted from the initializer.
    assert tuple(key for key, _ in reasoning_options) == REASONING_LEVELS
    level = _ask_choice("推理等级:", reasoning_options, default=1)
    if level != "provider_default":
        model_cfg["reasoning"] = {"level": level}

    return name, model_cfg


def _step_llm(existing: dict) -> Optional[dict]:
    """LLM 配置步骤。返回 {"llm": {...}} 或 None（跳过）"""
    print("\n" + "═" * 20 + " 第 1/3 步 · LLM 模型 " + "═" * 20)

    llm_cfg = existing.get("llm", {})
    existing_models = llm_cfg.get("models", {})
    _overview(existing)

    changes: dict[str, Any] = {}  # 收集变更
    new_models: dict[str, Any] = {}

    while True:
        print()
        print("  1. 添加 / 修改模型")
        print("  2. 切换默认模型")
        print(f"  3. 修改请求超时（当前 {llm_cfg.get('timeout', 60)}s）")
        print("  4. 完成本步")
        print("  5. 跳过本步（不修改 LLM 配置）")
        choice = input("  请选择 [1-5] (默认4): ").strip()

        if choice == "1":
            result = _collect_one_model({**existing_models, **new_models})
            if result:
                name, cfg = result
                new_models[name] = cfg
                print(f"  ✅ 已收集模型: {name}")
                if _ask_yes_no(f"  将 {name} 设为默认模型？", default=True):
                    changes["model_id"] = name
                if not _ask_yes_no("  继续添加下一个模型？", default=False):
                    continue
        elif choice == "2":
            all_names = list({**existing_models, **new_models}.keys())
            if not all_names:
                print("  ⚠️ 暂无可用模型，请先添加")
                continue
            opts = [(n, n) for n in all_names]
            current = changes.get("model_id", llm_cfg.get("model_id", ""))
            print(f"  当前默认: {current or '(未设置)'}")
            picked = _ask_choice("选择默认模型:", opts, default=1)
            changes["model_id"] = picked
            print(f"  ✅ 默认模型 → {picked}")
        elif choice == "3":
            current_timeout = llm_cfg.get("timeout", 60)
            t = _ask_int("请求超时 (秒)", current_timeout)
            changes["timeout"] = t
            print(f"  ✅ 超时 → {t}s")
        elif choice in ("", "4"):
            break
        elif choice == "5":
            return None
        else:
            print("  ❗ 请输入 1-5")

    if new_models:
        changes["models"] = new_models
    if not changes:
        return None
    return {"llm": changes}


# ============================================================
# 步骤 2：MCP 服务器
# ============================================================

def _step_mcp(existing: dict) -> Optional[dict]:
    """MCP 配置步骤。返回 {"mcp": {"servers": 完整列表}} 或 None"""
    print("\n" + "═" * 20 + " 第 2/3 步 · MCP 服务器 " + "═" * 18)

    original = existing.get("mcp", {}).get("servers", [])
    # 深拷贝完整列表作为工作副本
    work_list = copy.deepcopy(original)

    if work_list:
        print("\n  已有服务器:")
        for i, s in enumerate(work_list, 1):
            transport = s.get("transport", "?")
            target = s.get("url") or s.get("command", "?")
            status = "✅" if is_enabled(s.get("enabled")) else "❌"
            print(f"    {i}. {status} {s.get('name', '?')} ({transport} → {target})")
    else:
        print("\n  (暂无 MCP 服务器)")

    changed = False
    while True:
        print()
        print("  1. 添加服务器")
        print("  2. 删除服务器")
        print("  3. 完成本步")
        print("  4. 跳过本步（不修改 MCP 配置）")
        choice = input("  请选择 [1-4] (默认3): ").strip()

        if choice == "1":
            server = _collect_one_server(work_list)
            if server:
                work_list.append(server)
                changed = True
                print(f"  ✅ 已添加: {server['name']}")
        elif choice == "2":
            if not work_list:
                print("  ⚠️ 没有可删除的服务器")
                continue
            print("  当前服务器:")
            for i, s in enumerate(work_list, 1):
                print(f"    {i}. {s.get('name', '?')}")
            idx_str = input(f"  删除第几个？[1-{len(work_list)}] (回车取消): ").strip()
            if idx_str:
                try:
                    idx = int(idx_str) - 1
                    if 0 <= idx < len(work_list):
                        removed = work_list.pop(idx)
                        changed = True
                        print(f"  🗑️ 已删除: {removed.get('name', '?')}")
                    else:
                        print("  ❗ 编号超出范围")
                except ValueError:
                    print("  ❗ 请输入数字")
        elif choice in ("", "3"):
            break
        elif choice == "4":
            return None
        else:
            print("  ❗ 请输入 1-4")

    if not changed:
        return None
    return {"mcp": {"servers": work_list}}


def _collect_one_server(existing_servers: list) -> Optional[dict]:
    """收集单个 MCP 服务器配置"""
    print()
    existing_names = {s.get("name", "") for s in existing_servers}
    while True:
        name = _ask("服务器名称 (如 web-search)")
        if not name:
            print("  ⚠️ 名称不能为空，已跳过")
            return None
        if name in existing_names:
            print(f"  ❗ 名称 '{name}' 已存在，请换一个")
            continue
        break

    transport = _ask_choice("传输方式:", [(t, d) for t, d in _MCP_TRANSPORTS], default=1)
    server: dict[str, Any] = {"name": name, "transport": transport}

    if transport == "stdio":
        cmd = _ask("启动命令 (如 npx / python / node)")
        if not cmd:
            print("  ⚠️ 命令不能为空，已跳过")
            return None
        server["command"] = cmd
        args_str = _ask("命令参数（空格分隔，回车跳过）")
        if args_str:
            server["args"] = args_str.split()
        env = _ask_kv_lines("环境变量")
        if env:
            server["env"] = env
    else:
        url = _ask("服务器 URL (如 http://localhost:3000/mcp)")
        if not url:
            print("  ⚠️ URL 不能为空，已跳过")
            return None
        if "://" not in url:
            print("  ⚠️ URL 应包含 ://（如 http://...），已按原样保存")
        server["url"] = url
        headers = _ask_kv_lines("自定义 Headers")
        if headers:
            server["headers"] = headers

    # 启用/禁用
    server["enabled"] = _ask_yes_no("立即启用此服务器？", default=True)
    server["trust"] = _ask_yes_no(
        "信任此服务器可提供可执行工具？", default=False)

    return server


# ============================================================
# 步骤 3：Hooks
# ============================================================

def _step_hooks(existing: dict) -> Optional[dict]:
    """Hooks 配置步骤。返回 {"hooks": {...}} 或 None（跳过）"""
    print("\n" + "═" * 20 + " 第 3/3 步 · Hooks " + "═" * 23)

    hooks_cfg = existing.get("hooks", {})
    current_enabled = hooks_cfg.get("enabled", True)

    if not _ask_yes_no("配置 hooks？", default=True):
        return None

    changes: dict[str, Any] = {}

    # 总开关
    enabled = _ask_yes_no("启用 hooks 系统？", default=current_enabled)
    if enabled != current_enabled:
        changes["enabled"] = enabled

    # 内置过滤器
    has_filters = bool(hooks_cfg.get("filters"))
    if _ask_yes_no("启用内置过滤器（敏感词拦截 + 危险命令模式）？", default=True):
        if not has_filters:
            # 从 config.example.json 读取默认 filters
            filters = _load_example_filters()
            if filters:
                changes["filters"] = filters
                print("  ✅ 已从模板加载默认过滤器（敏感词 + 危险命令）")
            else:
                print("  ⚠️ 未找到 config.example.json，跳过过滤器配置")
    elif has_filters:
        # 用户明确不要 filters，但原配置有 → 不主动删除（保持原样）
        print("  💡 已有过滤器配置保持不变，如需删除请手动编辑 config.json")

    print("  💡 8 个事件桶（pre_tool / stop / plan_approved …）可稍后在 config.json")
    print("     的 hooks 段手写脚本，交互模式下 /hook reload 即时生效")

    if not changes:
        return None
    return {"hooks": changes}


def _load_example_filters() -> Optional[dict]:
    """从 config.example.json 读取 hooks.filters 块"""
    try:
        root = _find_project_root()
        example_path = root / "config.example.json"
        if not example_path.exists():
            return None
        with open(example_path, "r", encoding="utf-8") as f:
            example = json.load(f)
        filters = example.get("hooks", {}).get("filters")
        if isinstance(filters, dict):
            # 去掉 _comment 字段
            return {k: v for k, v in filters.items() if not k.startswith("_")}
        return None
    except (json.JSONDecodeError, OSError):
        return None


# ============================================================
# 高级选项：permission / sandbox
# ============================================================

def _step_agent_runtime(existing: dict) -> Optional[dict]:
    """Configure the native tool-call event stream and retire text settings."""
    print("\n  Agent native tool calls and streaming events")
    runtime = existing.get("agent_runtime", {})
    native_stream = _ask_yes_no(
        "Enable native tool-call streaming events?",
        default=is_enabled(runtime.get("native_tool_streaming"), True),
    )
    max_result_chars = max(0, _ask_int(
        "Maximum characters retained from one tool result (0 disables truncation)",
        _as_int(runtime.get("max_tool_result_chars"), 10000),
    ))
    retired_keys = {
        "response_protocol", "legacy_execute", "protocol_retry_limit",
        "raw_response_audit", "raw_response_max_chars",
    }
    needs_migration = any(key in runtime for key in retired_keys)
    if (runtime.get("native_tool_streaming") is native_stream
            and _as_int(runtime.get("max_tool_result_chars"), 10000) == max_result_chars
            and not needs_migration):
        return None
    return {"agent_runtime": {
        "native_tool_streaming": native_stream,
        "max_tool_result_chars": max_result_chars,
    }}

def _step_task_runtime(existing: dict) -> Optional[dict]:
    """Configure the durable TaskRuntime feature flag and SQLite state store."""
    print("\n  持久任务运行时（TaskRuntime）")
    runtime = existing.get("task_runtime", {})
    store = existing.get("runtime_store", {})
    runtime_changes: dict[str, Any] = {}
    store_changes: dict[str, Any] = {}

    enabled = _ask_yes_no(
        "启用统一 TaskRuntime（消息先持久化，再由运行时执行）？",
        default=is_enabled(runtime.get("enabled"), True),
    )
    if enabled != is_enabled(runtime.get("enabled"), True):
        runtime_changes["enabled"] = enabled

    store_path = _ask("运行时 SQLite 路径", store.get("path", "./workspace/.agent/state/runtime.db"))
    if store_path != store.get("path", "./workspace/.agent/state/runtime.db"):
        store_changes["path"] = store_path
    if store.get("backend", "sqlite") != "sqlite":
        store_changes["backend"] = "sqlite"

    max_workers = _ask_int("TaskRuntime 最大并发会话数", _as_int(runtime.get("max_global_concurrency"), 4))
    if max_workers != runtime.get("max_global_concurrency", 4):
        runtime_changes["max_global_concurrency"] = max_workers
    timeout = _ask_int("TaskRuntime 默认超时（秒）", _as_int(runtime.get("default_timeout_seconds"), 1200))
    if timeout != runtime.get("default_timeout_seconds", 1200):
        runtime_changes["default_timeout_seconds"] = timeout
    grace = _ask_int("超时后取消等待时间（秒）", _as_int(runtime.get("cancel_grace_seconds"), 10))
    if grace != runtime.get("cancel_grace_seconds", 10):
        runtime_changes["cancel_grace_seconds"] = grace

    changes: dict[str, Any] = {}
    if runtime_changes:
        changes["task_runtime"] = runtime_changes
    if store_changes:
        changes["runtime_store"] = store_changes
    return changes or None


def _step_goal(existing: dict) -> Optional[dict]:
    """Configure the durable Goal runtime defaults."""
    print("\n  Goal（长期目标自动续跑）")
    goal = existing.get("goal", {})
    changes: dict[str, Any] = {}
    enabled = _ask_yes_no("启用 Goal 模块？", default=is_enabled(goal.get("enabled"), True))
    if enabled != is_enabled(goal.get("enabled"), True):
        changes["enabled"] = enabled
    max_active = _ask_int("每会话最大同时运行的 Goal 数",
                          _as_int(goal.get("max_active_per_session"), 1))
    if max_active >= 1 and max_active != goal.get("max_active_per_session", 1):
        changes["max_active_per_session"] = max_active
    return {"goal": changes} if changes else None


def _step_subagent(existing: dict) -> Optional[dict]:
    """Configure the Subagent delegation runtime defaults."""
    print("\n  Subagent（子 Agent 委派）")
    sub = existing.get("subagent", {})
    changes: dict[str, Any] = {}
    enabled = _ask_yes_no("启用 Subagent 模块？", default=is_enabled(sub.get("enabled"), True))
    if enabled != is_enabled(sub.get("enabled"), True):
        changes["enabled"] = enabled
    max_children = _ask_int("每 Goal 最大子 Agent 数", _as_int(sub.get("max_children"), 4))
    if max_children >= 1 and max_children != sub.get("max_children", 4):
        changes["max_children"] = max_children
    one_level = _ask_yes_no("子 Agent 仅一层（不可再派生）？",
                            default=is_enabled(sub.get("one_level_only"), True))
    if one_level != is_enabled(sub.get("one_level_only"), True):
        changes["one_level_only"] = one_level
    return {"subagent": changes} if changes else None


def _step_retention(existing: dict) -> Optional[dict]:
    """Configure the runtime record retention policy."""
    print("\n  Retention（运行时记录保留策略）")
    ret = existing.get("retention", {})
    changes: dict[str, Any] = {}
    enabled = _ask_yes_no("启用自动清理？", default=is_enabled(ret.get("enabled"), True))
    if enabled != is_enabled(ret.get("enabled"), True):
        changes["enabled"] = enabled
    terminal = _ask_int("终态任务保留天数", _as_int(ret.get("terminal_days"), 30))
    if terminal >= 1 and terminal != ret.get("terminal_days", 30):
        changes["terminal_days"] = terminal
    artifact = _ask_int("Artifact 保留天数", _as_int(ret.get("artifact_days"), 30))
    if artifact >= 1 and artifact != ret.get("artifact_days", 30):
        changes["artifact_days"] = artifact
    interval = _ask_int("清理间隔（秒）", _as_int(ret.get("interval_seconds"), 3600))
    if interval >= 1 and interval != ret.get("interval_seconds", 3600):
        changes["interval_seconds"] = interval
    return {"retention": changes} if changes else None


def _step_runtime_store(existing: dict) -> Optional[dict]:
    """Configure the unified SQLite runtime store (path / WAL / busy timeout).

    此前是向导唯一未覆盖的顶层段（2026-08 审计补齐）：统一会话/任务运行时
    的存储位置属用户可感知选项（备份/迁移/磁盘容量决策），给出显式入口。
    """
    print("\n  🗄️  运行时存储（统一会话 + 任务运行时，SQLite）")
    current = existing.get("runtime_store", {}) or {}
    changes: dict[str, Any] = {}
    path = _ask("SQLite 路径", current.get("path", "./workspace/.agent/state/runtime.db"))
    if path != current.get("path", "./workspace/.agent/state/runtime.db"):
        changes["path"] = path
    wal = _ask_yes_no("启用 WAL 模式（并发读写更稳，推荐）",
                      default=is_enabled(current.get("wal"), True))
    if wal != is_enabled(current.get("wal"), True):
        changes["wal"] = wal
    busy = _ask_int("busy_timeout 毫秒（写冲突等待）",
                    _as_int(current.get("busy_timeout_ms"), 5000))
    if busy != _as_int(current.get("busy_timeout_ms"), 5000):
        changes["busy_timeout_ms"] = busy
    return {"runtime_store": changes} if changes else None


def _step_artifacts(existing: dict) -> Optional[dict]:
    """Configure the durable ArtifactStore root and retention limits."""
    print("\n  产物存储（ArtifactStore）")
    artifacts = existing.get("artifacts", {})
    changes: dict[str, Any] = {}
    root = _ask("Artifact 根目录", artifacts.get("root", "./workspace/.agent/artifacts"))
    if root != artifacts.get("root", "./workspace/.agent/artifacts"):
        changes["root"] = root
    max_bytes = _ask_int("单个 Artifact 最大字节数", _as_int(artifacts.get("max_file_bytes"), 52428800))
    if max_bytes > 0 and max_bytes != artifacts.get("max_file_bytes", 52428800):
        changes["max_file_bytes"] = max_bytes
    retention = _ask_int("Artifact 保留天数", _as_int(artifacts.get("retention_days"), 30))
    if retention >= 1 and retention != artifacts.get("retention_days", 30):
        changes["retention_days"] = retention
    return {"artifacts": changes} if changes else None

def _step_gateway_runtime(existing: dict) -> Optional[dict]:
    """Configure gateway/session defaults that affect every channel."""
    gateway = existing.get("gateway", {})
    agent = gateway.get("agent", {})
    sessions = gateway.get("sessions", {})
    print("\n  Gateway / 会话运行参数")
    changes: dict[str, Any] = {}

    enabled = _ask_yes_no("启用 Gateway？", default=is_enabled(gateway.get("enabled"), True))
    if enabled != is_enabled(gateway.get("enabled"), True):
        changes["enabled"] = enabled
    while True:
        host = _ask("监听地址", str(gateway.get("host", "127.0.0.1")))
        if _is_valid_host(host):
            break
        print("  ❗ 无效的监听地址（应为 IP 地址或主机名）")
    if host != gateway.get("host", "127.0.0.1"):
        changes["host"] = host
    while True:
        port = _ask_int("监听端口", _as_int(gateway.get("port"), 9120))
        if 1 <= port <= 65535:
            break
        print("  ❗ 端口范围应为 1-65535")
    if port != gateway.get("port", 9120):
        changes["port"] = port
    workers = _ask_int("工作线程数", _as_int(gateway.get("worker_pool_size"), 4))
    if workers != gateway.get("worker_pool_size", 4):
        changes["worker_pool_size"] = workers

    agent_changes: dict[str, Any] = {}
    max_steps = _ask_int("单轮最大工具步骤", _as_int(agent.get("max_steps"), 100))
    if max_steps != agent.get("max_steps", 100):
        agent_changes["max_steps"] = max_steps
    # 权限默认值诚实化：全新配置实际合并写入的是 _DEFAULT_CONFIG 的默认值
    # （gateway.agent.permission_mode="allow"）；向导推荐 ask 时与之比对，
    # 确保写出的就是向导展示/推荐的值，消除"展示 ask 实际 allow"漂移。
    gateway_agent_defaults = _DEFAULT_CONFIG["gateway"]["agent"]
    permission_mode = _ask_choice("Gateway 默认权限:", [
        ("ask", "ask — 需要确认（推荐）"), ("allow", "allow — 全部允许"),
        ("unreviewed", "unreviewed — 不审查"),
    ], default=1)
    if permission_mode != agent.get("permission_mode", gateway_agent_defaults["permission_mode"]):
        agent_changes["permission_mode"] = permission_mode
    # ask 模式不自动批准 Plan（allow / unreviewed 才推荐自动批准）；
    # 无论选哪种模式都把 auto_approve_plan 与所选权限对齐写入，消除漂移
    auto_approve = _ask_yes_no(
        "自动批准 Plan（仅 allow / unreviewed 推荐）？",
        default=permission_mode != "ask")
    if auto_approve != is_enabled(agent.get("auto_approve_plan"), gateway_agent_defaults["auto_approve_plan"]):
        agent_changes["auto_approve_plan"] = auto_approve
    quiet = _ask_yes_no("静默运行（不向终端输出模型分片）？", default=is_enabled(agent.get("quiet"), True))
    if quiet != is_enabled(agent.get("quiet"), True):
        agent_changes["quiet"] = quiet
    if agent_changes:
        changes["agent"] = agent_changes

    session_changes: dict[str, Any] = {}
    for key, label, default in (
        ("max_sessions", "最大并发会话数", 32),
        ("idle_timeout_minutes", "会话空闲回收分钟数", 60),
        ("soft_timeout_seconds", "软超时秒数", 90),
        ("hard_timeout_seconds", "硬超时秒数", 1200),
    ):
        value = _ask_int(label, _as_int(sessions.get(key), default))
        if value != sessions.get(key, default):
            session_changes[key] = value
    persist = _ask_yes_no("持久化会话历史？", default=is_enabled(sessions.get("persist"), True))
    if persist != is_enabled(sessions.get("persist"), True):
        session_changes["persist"] = persist
    if session_changes:
        changes["sessions"] = session_changes

    channel_changes: dict[str, Any] = {}
    for channel_name in ("debug", "feishu", "weixin"):
        current = gateway.get("channels", {}).get(channel_name, {})
        current_enabled = is_enabled(current.get("enabled") if isinstance(current, dict) else current, False)
        enabled_value = _ask_yes_no(f"启用渠道 {channel_name}？", default=current_enabled)
        if enabled_value != current_enabled:
            channel_changes[channel_name] = {"enabled": enabled_value}
    if channel_changes:
        changes["channels"] = channel_changes
    return {"gateway": changes} if changes else None


def _step_workspace_and_prompt(existing: dict) -> Optional[dict]:
    """Configure workspace layout and prompt bootstrap budgets."""
    workspace = existing.get("workspace", {})
    permission = existing.get("permission", {})
    prompt = existing.get("prompt", {})
    print("\n  工作区与提示词预算")
    changes: dict[str, Any] = {}
    workspace_changes: dict[str, Any] = {}
    # permission.workspace is the runtime source of truth.  Keep the
    # workspace section in sync because it is exposed by the WebUI settings.
    # 回车不静默改写：仅当用户显式输入了不同于默认值的路径才记录变更。
    default_path = str(permission.get("workspace", workspace.get("path", "./workspace")))
    path = _ask("工作区路径", default_path)
    if path != default_path:
        if path != workspace.get("path", "./workspace"):
            workspace_changes["path"] = path
        if path != permission.get("workspace", "./workspace"):
            changes["permission"] = {"workspace": path}
    # Older configs stored descriptions as an object; use its directory names
    # as the editable list without silently replacing it unless the user edits.
    stored_dirs = workspace.get("dirs", []) or []
    dirs = list(stored_dirs) if isinstance(stored_dirs, dict) else stored_dirs
    raw_dirs = _ask("工作区子目录（逗号分隔）", ",".join(dirs))
    new_dirs = [item.strip() for item in raw_dirs.split(",") if item.strip()]
    if new_dirs != dirs:
        workspace_changes["dirs"] = new_dirs
    if workspace_changes:
        changes["workspace"] = workspace_changes

    prompt_changes: dict[str, Any] = {}
    for key, label, default in (
        ("bootstrap_max_chars_per_file", "单个引导文件最大字符数", 8000),
        ("bootstrap_max_chars_total", "引导文件总最大字符数", 32000),
    ):
        value = _ask_int(label, _as_int(prompt.get(key), default))
        if value != prompt.get(key, default):
            prompt_changes[key] = value
    warning = _ask_choice("提示词截断警告:", [
        ("once", "once — 每次会话提示一次"), ("always", "always — 每次截断提示"),
        ("never", "never — 不提示"),
    ], default=1)
    if warning != prompt.get("truncation_warning", "once"):
        prompt_changes["truncation_warning"] = warning
    if prompt_changes:
        changes["prompt"] = prompt_changes
    return changes or None


def _step_tool_security(existing: dict) -> Optional[dict]:
    """Optionally collect sandbox network deny lists (permission follows four-tier)."""
    if not _ask_yes_no("配置网络封锁策略？", default=False):
        return None
    sandbox = existing.get("sandbox", {})
    changes: dict[str, Any] = {}
    if _ask_yes_no("编辑网络封锁域名/IP 列表？", default=False):
        network = sandbox.get("network", {})
        domains = _ask("封锁域名（逗号分隔）", ",".join(network.get("blocked_domains", [])))
        ips = _ask("封锁 IP/CIDR（逗号分隔）", ",".join(network.get("blocked_ips", [])))
        changes["sandbox"] = {"network": {
            "blocked_domains": [x.strip() for x in domains.split(",") if x.strip()],
            "blocked_ips": [x.strip() for x in ips.split(",") if x.strip()],
        }}
    return changes or None


def _step_webui_security(existing: dict) -> Optional[dict]:
    """Collect remote WebUI allowlist settings without exposing a token."""
    gateway = existing.get("gateway", {})
    webui = gateway.get("webui", {})
    print("\n  🌐 WebUI 远程访问")

    current_remote = is_enabled(webui.get("allow_non_loopback"), False)
    allow_remote = _ask_yes_no("允许从非本机访问 WebUI？", default=current_remote)
    changes: dict[str, Any] = {}
    if allow_remote != current_remote:
        changes["allow_non_loopback"] = allow_remote

    if allow_remote:
        current_ips = webui.get("allowed_ips", []) or []
        if isinstance(current_ips, str):
            current_ips = [current_ips]
        default_ips = ", ".join(str(item) for item in current_ips)
        while True:
            raw = _ask("允许的客户端 IP/CIDR（逗号分隔）", default_ips)
            allowed_ips = [item.strip() for item in raw.split(",") if item.strip()]
            if not allowed_ips:
                print("  ❌ 远程访问必须至少配置一个允许的 IP 或 CIDR")
                continue
            try:
                for item in allowed_ips:
                    ipaddress.ip_network(item, strict=False)
            except ValueError:
                print(f"  ❌ 无效的 IP 或 CIDR：{item}")
                continue
            if allowed_ips != current_ips:
                changes["allowed_ips"] = allowed_ips
            break

    return {"gateway": {"webui": changes}} if changes else None

def _step_advanced(existing: dict) -> Optional[dict]:
    """高级配置步骤。返回 {"permission": ..., "sandbox": ...} 或 None"""
    print("\n" + "═" * 18 + " 高级选项 · Permission / Sandbox " + "═" * 8)

    changes: dict[str, Any] = {}

    # ---- Permission ----
    perm_cfg = existing.get("permission", {})
    current_mode = perm_cfg.get("default_mode", "ask")
    print(f"\n  📋 权限管理（当前: {current_mode}）")
    # 只提供运行时的四档真实模式（core/policy_engine.VALID_MODES）；
    # 旧菜单里的 deny 不是有效档位，运行时会被静默强制回退为 ask，属于展示漂移
    mode = _ask_choice("默认权限模式:", [
        ("ask", "ask — 每次询问（推荐）"),
        ("allow", "allow — 全部允许（⚠️ 不安全）"),
        ("readonly", "readonly — 只读，禁止写操作"),
        ("unreviewed", "unreviewed — 不审查"),
    ], default=1)
    perm_changes: dict[str, Any] = {}
    if mode != current_mode:
        perm_changes["default_mode"] = mode

    # The project root is an explicit extra root by default.  It lets the
    # agent edit project source/config files while keeping unrelated paths
    # outside the permission boundary.
    current_extra = perm_cfg.get("extra_workspaces", ["."])
    if not isinstance(current_extra, list):
        current_extra = ["."]
    extra_raw = _ask("额外允许访问的工作区根路径（逗号分隔，. 表示项目根目录）",
                     ",".join(str(item) for item in current_extra))
    extra_workspaces = [item.strip() for item in extra_raw.split(",") if item.strip()]
    if extra_workspaces != current_extra:
        perm_changes["extra_workspaces"] = extra_workspaces

    if perm_changes:
        changes["permission"] = perm_changes

    # ---- Sandbox ----
    sb_cfg = existing.get("sandbox", {})
    # L2 沙箱硬闸门默认关闭（与 sandbox 配置默认一致）
    current_enabled = sb_cfg.get("enabled", False)
    print(f"\n  🛡️ 沙箱（当前: {'开启' if current_enabled else '关闭'}）")
    sb_enabled = _ask_yes_no("启用沙箱？", default=current_enabled)
    sb_changes: dict[str, Any] = {}
    if sb_enabled != current_enabled:
        sb_changes["enabled"] = sb_enabled

    current_timeout = sb_cfg.get("idle_timeout_seconds", 300)
    sb_timeout = _ask_int("沙箱空闲超时 (秒)", current_timeout)
    if sb_timeout != current_timeout:
        sb_changes["idle_timeout_seconds"] = sb_timeout

    if sb_changes:
        changes["sandbox"] = sb_changes

    if not changes:
        return None
    return changes


# ============================================================
# 摘要预览
# ============================================================

def _print_summary(fragments: list[dict], existing: dict) -> None:
    """打印变更摘要（diff 风格，key 打码）"""
    print("\n" + "─" * 18 + " 写入预览 " + "─" * 18)

    for frag in fragments:
        for section, value in frag.items():
            if section == "llm":
                if "model_id" in value:
                    old = existing.get("llm", {}).get("model_id", "(无)")
                    print(f"  llm.model_id:    {old} → {value['model_id']}")
                if "timeout" in value:
                    old = existing.get("llm", {}).get("timeout", 60)
                    print(f"  llm.timeout:     {old} → {value['timeout']}")
                if "models" in value:
                    for name, cfg in value["models"].items():
                        proto = cfg.get("protocol", cfg.get("provider", "openai"))
                        key_hint = f", key {_mask_key(cfg['api_key'])}" if cfg.get("api_key") else ""
                        print(f"  llm.models:      + {name} ({proto}{key_hint})")
            elif section == "mcp":
                servers = value.get("servers", [])
                names = [s.get("name", "?") for s in servers]
                print(f"  mcp.servers:     {', '.join(names) if names else '(清空)'}")
            elif section == "hooks":
                parts = []
                if "enabled" in value:
                    parts.append(f"enabled={'✅' if value['enabled'] else '❌'}")
                if "filters" in value:
                    parts.append("+filters")
                print(f"  hooks:           {', '.join(parts) if parts else '保持'}")
            elif section == "permission":
                for k, v in value.items():
                    print(f"  permission.{k}: → {v}")
            elif section == "sandbox":
                for k, v in value.items():
                    print(f"  sandbox.{k}:  → {v}")
            elif section == "workspace":
                for k, v in value.items():
                    print(f"  workspace.{k}: → {v}")
            elif section == "prompt":
                for k, v in value.items():
                    print(f"  prompt.{k}:    → {v}")
            elif section == "gateway":
                for k, v in value.items():
                    if isinstance(v, dict):
                        for k2, v2 in v.items():
                            print(f"  gateway.{k}.{k2}: → {v2}")
                    else:
                        print(f"  gateway.{k}:   → {v}")

    # 未修改的 section（覆盖向导可能触及的全部顶层段）
    touched = {k for frag in fragments for k in frag}
    untouched = [s for s in (
        "llm", "mcp", "hooks", "permission", "sandbox", "gateway",
        "agent_runtime", "task_runtime", "runtime_store", "goal",
        "subagent", "retention", "artifacts", "workspace", "prompt",
    ) if s not in touched]
    if untouched:
        print(f"  {'/'.join(untouched)}: 不修改")


# ============================================================
# 横幅
# ============================================================

def _print_banner(status: str, existing: dict) -> None:
    """打印向导横幅和当前配置概览"""
    print("\n╔═══════════════════════════════════════════════╗")
    print("║   🛠️  JKagent 初始化向导                   ║")
    print("║   每一步回车 = 使用默认值，Ctrl+C 随时退出     ║")
    print("╚═══════════════════════════════════════════════╝")

    if status == "new":
        print("\n  ✨ 首次配置（config.json 不存在，将新建）")
    else:
        print("\n  📄 检测到已有配置 config.json（增量修改模式）")
        print()
        _overview(existing)


# ============================================================
# 配置补齐工具（嵌套键级，不覆盖已有值）
# ============================================================

def _nested_missing(defaults: dict, existing: dict) -> bool:
    """递归判断 existing 相对 defaults 是否有缺失的嵌套键。"""
    for key, value in defaults.items():
        if key not in existing:
            return True
        if isinstance(value, dict) and isinstance(existing.get(key), dict):
            if _nested_missing(value, existing[key]):
                return True
    return False


def _sections_needing_backfill(defaults: dict, existing: dict) -> list[str]:
    """返回需要补齐默认值的顶层 section。

    不仅包含完全缺失的 section，还包含内部新增了默认键的既有 section
    （例如 agent_runtime.max_parallel_tools、workspace 新增键），确保
    后续版本新增的配置在重新初始化时以本地现有值为主、缺失键补默认值。
    """
    need: list[str] = []
    for key, default in defaults.items():
        if key not in existing:
            need.append(key)
            continue
        if isinstance(default, dict) and isinstance(existing.get(key), dict):
            if _nested_missing(default, existing[key]):
                need.append(key)
    return need


def _strip_default_equal_leaves(node: dict, defaults: dict) -> dict:
    """剥离与 _DEFAULT_CONFIG 完全相同的键值，保留 section 骨架（稀疏写入）。

    规则：
      - 双方都是 dict → 递归；子级全部剥空时保留空 dict 占位（骨架可见）；
      - 叶子值（list 视为整体）与当前默认完全相等 → 剥离，运行期由代码内
        默认回落，日后版本调整默认值才能对老配置生效；
      - 其余（用户显式设置、版本新增/扩展键）原样保留。

    用户在向导中改过的值必然不同于当前默认，因此不会被误删。
    """
    result: dict = {}
    for key, value in node.items():
        default_value = defaults.get(key)
        if isinstance(value, dict):
            if isinstance(default_value, dict):
                result[key] = _strip_default_equal_leaves(value, default_value)
            else:
                # 默认配置中不是 dict（或缺失）：无法逐键比较，整块视为用户数据
                result[key] = copy.deepcopy(value)
        elif key in defaults and value == default_value:
            continue  # 与当前默认完全一致 → 不固化进文件
        else:
            result[key] = copy.deepcopy(value)
    return result


# ============================================================
# 公开入口
# ============================================================

def run_init_wizard() -> int:
    """
    交互式初始化向导主入口。
    返回退出码：0=成功/无修改，1=错误/中止，130=用户取消
    """
    try:
        root = _find_project_root()
        cfg_path = root / "config.json"

        # ---- 读取现有配置 ----
        existing, status = _read_raw_config(cfg_path)
        if status == "corrupt":
            existing = _handle_corrupt(cfg_path)
            if existing is None:
                return 1
            status = "new"

        _print_banner(status, existing)

        # ---- 收集各步变更 ----
        fragments: list[dict] = []
        for step in (_step_llm, _step_mcp, _step_hooks):
            frag = step(existing)
            if frag:
                fragments.append(frag)

        # ---- 可选：高级选项 ----
        if _ask_yes_no("\n⚙️  是否配置高级选项（安全 / runtime / Gateway / 工作区 / WebUI）？", default=False):
            frag = _step_advanced(existing)
            if frag:
                fragments.append(frag)
            frag = _step_agent_runtime(existing)
            if frag:
                fragments.append(frag)
            frag = _step_task_runtime(existing)
            if frag:
                fragments.append(frag)
            frag = _step_goal(existing)
            if frag:
                fragments.append(frag)
            frag = _step_subagent(existing)
            if frag:
                fragments.append(frag)
            frag = _step_retention(existing)
            if frag:
                fragments.append(frag)
            frag = _step_artifacts(existing)
            if frag:
                fragments.append(frag)
            frag = _step_runtime_store(existing)
            if frag:
                fragments.append(frag)
            frag = _step_gateway_runtime(existing)
            if frag:
                fragments.append(frag)
            frag = _step_workspace_and_prompt(existing)
            if frag:
                fragments.append(frag)
            frag = _step_tool_security(existing)
            if frag:
                fragments.append(frag)
            frag = _step_webui_security(existing)
            if frag:
                fragments.append(frag)

        # ---- 检查是否有新增配置需要补齐（顶层 section 或 section 内缺失默认键）----
        merged_with_defaults = copy.deepcopy(_DEFAULT_CONFIG)
        _deep_merge(merged_with_defaults, existing)
        missing_sections = _sections_needing_backfill(_DEFAULT_CONFIG, existing)

        # ---- 无修改且无缺失 ----
        if not fragments and not missing_sections:
            print("\n  😴 没有任何修改，config.json 保持不变")
            return 0

        # ---- 有缺失配置但无用户修改 → 提示补齐 ----
        if not fragments and missing_sections:
            print(f"\n  📋 检测到 config.json 缺少以下配置段或默认键: {', '.join(missing_sections)}")
            if not _ask_yes_no("自动补齐默认值（保留现有值）？", default=True):
                print("  ✋ 已放弃，未做任何修改")
                return 0
            # 将补齐视为一个 fragment；deep_merge 只填缺失键，绝不覆盖已有值
            fragments.append({k: _DEFAULT_CONFIG[k] for k in missing_sections})

        # ---- 预览 & 确认 ----
        _print_summary(fragments, existing)
        print()
        if not _ask_yes_no("写入 config.json？", default=True):
            print("  ✋ 已放弃，未做任何修改")
            return 0

        # ---- 备份 & 合并 & 写入 ----
        # 备份失败仅告警，不中断主写入
        try:
            bak = _backup_file(cfg_path)
        except OSError as e:
            print(f"  ⚠️ 备份失败（{e}），继续写入")
            bak = None
        # 稀疏写入：以用户现有 config 为底座（全新安装才用完整骨架），
        # 只叠加向导中用户实际输入/确认的 fragments，最后剥离一切与
        # _DEFAULT_CONFIG 完全相同的键值 —— 避免把当前版本的默认值固化进
        # config.json 导致日后版本新默认永不生效。用户改过的值必然不同于
        # 当前默认，不受剥离影响；缺失的键由运行期默认回落提供。
        if existing:
            final = copy.deepcopy(existing)
        else:
            final = copy.deepcopy(_DEFAULT_CONFIG)
        for frag in fragments:
            _deep_merge(final, frag)
        final = _strip_default_equal_leaves(final, _DEFAULT_CONFIG)

        # Normalize known values in place — 仅规范化已存在的键，不向稀疏
        # 配置注入默认值。Never reconstruct this mapping:
        # future runtime options and vendor extensions must survive init.
        runtime = final.get("agent_runtime")
        if isinstance(runtime, dict):
            if "native_tool_streaming" in runtime:
                runtime["native_tool_streaming"] = is_enabled(
                    runtime.get("native_tool_streaming"), True)
            if "max_tool_result_chars" in runtime:
                runtime["max_tool_result_chars"] = max(
                    0, _as_int(runtime.get("max_tool_result_chars"), 12000))

        try:
            _write_config(cfg_path, final)
        except OSError as e:
            print(f"\n  ❌ {e}")
            return 1

        if bak:
            print(f"\n  💾 已备份旧配置 → {bak.name}")
        change_count = sum(len(f) for f in fragments)
        print(f"  ✅ 已写入 config.json（{change_count} 处变更）")

        print(f"\n  🚀 下一步:")
        print(f"     jkagent-gateway run      启动 Gateway / WebUI（新配置生效）")
        print(f"     提示: config.json 已被 .gitignore 忽略，不会提交；")
        print(f"           云端 key 也可改用环境变量提供，避免落盘。")
        return 0

    except (KeyboardInterrupt, EOFError):
        print("\n\n  👋 初始化已取消，config.json 未被修改")
        return 130
