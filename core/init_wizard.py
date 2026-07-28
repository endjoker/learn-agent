# -*- coding: utf-8 -*-
"""
交互式初始化向导 —— python agent.py init

引导用户配置 LLM 模型、MCP 服务器、Hooks 等核心选项，
可选配置 permission / sandbox 高级选项。
配置写入 config.json，下次启动生效。

设计要点:
  - 裸读 config.json（不用 load_config），避免默认值膨胀和环境变量密钥泄露
  - 全程只收集 fragments，最后确认 → 备份 → 原子写，Ctrl+C 零副作用
  - MCP servers 返回完整列表（_deep_merge 对 list 是整体替换）
"""

import copy
import getpass
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.config_loader import _deep_merge, _find_project_root, _DEFAULT_CONFIG

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
# 文件 I/O
# ============================================================

def _read_raw_config(path: Path) -> tuple[dict, str]:
    """
    裸读 config.json（不合并默认值、不叠加环境变量）。
    返回 (data, status)，status ∈ "new" / "loaded" / "corrupt"
    """
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


def _backup_file(path: Path) -> Optional[Path]:
    """备份现有 config.json → config.json.bak-YYYYmmdd-HHMMSS"""
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.parent / f"config.json.bak-{ts}"
    shutil.copy2(path, bak)
    return bak


def _write_config(path: Path, data: dict) -> None:
    """原子写入：tmp → os.replace"""
    tmp = path.parent / "config.json.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        # 清理残留 tmp
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise OSError(f"写入 config.json 失败: {e}") from e


# ============================================================
# 展示辅助
# ============================================================

def _mask_key(key: str) -> str:
    """API Key 打码：sk-f1…d412，≤8 位全掩"""
    if not key or key in ("not-needed", "ollama", "lm_studio", "YOUR_API_KEY_HERE"):
        return key or "(空)"
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


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
            print(f"    {i}. {s.get('name', '?')} ({transport} → {target})")
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

    print("  💡 12 个事件桶（pre_tool / stop / task_complete …）可稍后在 config.json")
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

def _step_advanced(existing: dict) -> Optional[dict]:
    """高级配置步骤。返回 {"permission": ..., "sandbox": ...} 或 None"""
    print("\n" + "═" * 18 + " 高级选项 · Permission / Sandbox " + "═" * 8)

    changes: dict[str, Any] = {}

    # ---- Permission ----
    perm_cfg = existing.get("permission", {})
    current_mode = perm_cfg.get("default_mode", "ask")
    print(f"\n  📋 权限管理（当前: {current_mode}）")
    mode = _ask_choice("默认权限模式:", [
        ("ask", "ask — 每次询问（推荐）"),
        ("allow", "allow — 全部允许（⚠️ 不安全）"),
        ("deny", "deny — 全部拒绝（最严格）"),
    ], default=1)
    perm_changes: dict[str, Any] = {}
    if mode != current_mode:
        perm_changes["default_mode"] = mode

    current_ws = perm_cfg.get("workspace", ".")
    ws = _ask("工作区路径", current_ws)
    if ws != current_ws:
        perm_changes["workspace"] = ws

    if perm_changes:
        changes["permission"] = perm_changes

    # ---- Sandbox ----
    sb_cfg = existing.get("sandbox", {})
    current_enabled = sb_cfg.get("enabled", True)
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

    # 未修改的 section
    touched = {k for frag in fragments for k in frag}
    untouched = [s for s in ("llm", "mcp", "hooks", "permission", "sandbox") if s not in touched]
    if untouched:
        print(f"  {'/'.join(untouched)}: 不修改")


# ============================================================
# 横幅
# ============================================================

def _print_banner(status: str, existing: dict) -> None:
    """打印向导横幅和当前配置概览"""
    print("\n╔═══════════════════════════════════════════════╗")
    print("║   🛠️  HelloAgent 初始化向导                   ║")
    print("║   每一步回车 = 使用默认值，Ctrl+C 随时退出     ║")
    print("╚═══════════════════════════════════════════════╝")

    if status == "new":
        print("\n  ✨ 首次配置（config.json 不存在，将新建）")
    else:
        print("\n  📄 检测到已有配置 config.json（增量修改模式）")
        print()
        _overview(existing)


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
        if _ask_yes_no("\n⚙️  是否配置高级选项（permission / sandbox）？", default=False):
            frag = _step_advanced(existing)
            if frag:
                fragments.append(frag)

        # ---- 检查是否有新增 section 需要补齐 ----
        merged_with_defaults = copy.deepcopy(_DEFAULT_CONFIG)
        _deep_merge(merged_with_defaults, existing)
        missing_sections = [k for k in _DEFAULT_CONFIG if k not in existing]

        # ---- 无修改且无缺失 ----
        if not fragments and not missing_sections:
            print("\n  😴 没有任何修改，config.json 保持不变")
            return 0

        # ---- 有缺失 section 但无用户修改 → 提示补齐 ----
        if not fragments and missing_sections:
            print(f"\n  📋 检测到 config.json 缺少以下配置段: {', '.join(missing_sections)}")
            if not _ask_yes_no("自动补齐默认值？", default=True):
                print("  ✋ 已放弃，未做任何修改")
                return 0
            # 将补齐视为一个 fragment
            fragments.append({k: merged_with_defaults[k] for k in missing_sections})

        # ---- 预览 & 确认 ----
        _print_summary(fragments, existing)
        print()
        if not _ask_yes_no("写入 config.json？", default=True):
            print("  ✋ 已放弃，未做任何修改")
            return 0

        # ---- 备份 & 合并 & 写入 ----
        bak = _backup_file(cfg_path)
        # 以 _DEFAULT_CONFIG 为底座，确保所有 section 都有完整默认值
        # 用户已有配置覆盖默认值，向导收集的 fragments 最后覆盖
        final = copy.deepcopy(_DEFAULT_CONFIG)
        _deep_merge(final, existing)
        for frag in fragments:
            _deep_merge(final, frag)

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
        print(f"     python agent.py          启动交互模式（新配置生效）")
        print(f"     提示: config.json 已被 .gitignore 忽略，不会提交；")
        print(f"           云端 key 也可改用环境变量提供，避免落盘。")
        return 0

    except (KeyboardInterrupt, EOFError):
        print("\n\n  👋 初始化已取消，config.json 未被修改")
        return 130
