# -*- coding: utf-8 -*-
"""
L2-A 内容安全拦截 —— 在命令/文件操作执行前做内容层面安全检查

功能:
    1. 防数据外发（curl/wget/git push 等）
    2. 保护敏感文件（.env / permission.py / agent.py 等）
    3. 防护系统路径（/etc / C:\\Windows 等）
    4. 防危险命令注入（rm -rf / / @reboot 等）
    5. Python 代码 AST 级安全检查
    6. 输出脱敏（API Key / 私钥 / 密码）
    7. 安全操作白名单（跳过 L2-A 检查）
    8. 网络域名黑名单检查（外发过滤）
"""

import ast
import os
import re
from pathlib import Path

# ================================================================
# 敏感文件列表（禁止写入 / 编辑）
# ================================================================

SENSITIVE_FILES = [
    ".env",
    ".git/config",
    "core/permission.py",
    "core/llm_client.py",
    "agent.py",
    "requirements.txt",
]

# ================================================================
# 系统关键路径（各平台）
# ================================================================

SYSTEM_PATHS_WIN = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\System32",
]
SYSTEM_PATHS_LINUX = [
    "/etc", "/usr", "/boot", "/sys", "/proc",
    "/var/log", "/var/lib",
]
SYSTEM_PATHS_MAC = [
    "/System", "/Library", "/Applications",
]


def _is_system_path(path: str | Path) -> bool:
    """检查路径是否在系统关键路径下"""
    p = str(Path(path).resolve())
    p_lower = p.lower()

    # Windows
    for sys_path in SYSTEM_PATHS_WIN:
        if p_lower.startswith(sys_path.lower()):
            return True

    # Linux / macOS
    for sys_path in SYSTEM_PATHS_LINUX + SYSTEM_PATHS_MAC:
        if p_lower.startswith(sys_path):
            return True

    return False


def _is_within_workspace(path: str | Path, workspace: str | Path) -> bool:
    """检查路径是否在工作区内"""
    try:
        Path(path).resolve().relative_to(Path(workspace).resolve())
        return True
    except ValueError:
        return False


# ================================================================
# 外发数据检测模式（数据泄露防护）
# ================================================================

DATA_LEAK_PATTERNS = [
    # curl/wget 外发文件内容
    re.compile(r"curl\s+.*?-d\s+.*?\$\(?", re.IGNORECASE),
    re.compile(r"curl\s+.*?--data\s+.*?\$\(?", re.IGNORECASE),
    re.compile(r"curl\s+.*?--data-raw\s+.*?\$\(?", re.IGNORECASE),
    re.compile(r"wget\s+.*?--post-data\s+.*?\$\(?", re.IGNORECASE),
    # git 推送到非预期远程
    re.compile(r"git\s+push\s+https?://", re.IGNORECASE),
    # netcat/telnet 反向 shell
    re.compile(r"(nc|netcat)\s+-e\s+", re.IGNORECASE),
    re.compile(r"bash\s+-i\s+>&?\s+/dev/tcp/", re.IGNORECASE),
    # base64 编码敏感文件
    re.compile(r"base64\s+(\.env|id_rsa|token|\.git/config)", re.IGNORECASE),
    # 疑似外发环境变量
    re.compile(r"curl\s+.*?\$API_KEY", re.IGNORECASE),
    re.compile(r"curl\s+.*?\$TOKEN", re.IGNORECASE),
    re.compile(r"curl\s+.*?\$SECRET", re.IGNORECASE),
]

# ================================================================
# 可执行注入模式
# ================================================================

EXEC_INJECTION_PATTERNS = [
    # cron 持久化
    re.compile(r"@reboot\s+", re.IGNORECASE),
    # Windows 计划任务
    re.compile(r"schtasks\s+/create", re.IGNORECASE),
    # 注册表启动项
    re.compile(r"reg\s+add\s+.*\\CurrentVersion\\Run", re.IGNORECASE),
    # systemd 开机启动
    re.compile(r"systemctl\s+enable\s+", re.IGNORECASE),
    # 创建系统用户
    re.compile(r"(sudo\s+)?useradd\s+", re.IGNORECASE),
    re.compile(r"(sudo\s+)?adduser\s+", re.IGNORECASE),
    # 开放系统目录
    re.compile(r"chmod\s+777\s+/", re.IGNORECASE),
    # PowerShell 远程执行
    re.compile(r"Invoke-Command\s+-ComputerName", re.IGNORECASE),
    re.compile(r"Invoke-Expression\s+", re.IGNORECASE),
    re.compile(r"IEX\s+", re.IGNORECASE),
    # 中文：创建系统用户（PowerShell）
    re.compile(r"net\s+user\s+.*/add", re.IGNORECASE),
    # 中文命令（PowerShell 本地化）
    re.compile(r"新增.*用户", re.IGNORECASE),
    re.compile(r"新建.*任务", re.IGNORECASE),
    re.compile(r"添加.*计划", re.IGNORECASE),
    re.compile(r"关闭.*防火墙", re.IGNORECASE),
    re.compile(r"停止.*服务", re.IGNORECASE),
]

# ================================================================
# 安全操作白名单（命中后直接绕过 L2-A 检查）
# ================================================================

SAFE_PATTERNS = [
    # 安全 git 操作（只读 / 本地）
    re.compile(r"^git\s+(status|log|diff|show|branch|stash|config|remote)\b"),
    re.compile(r"^git\s+add\b"),
    re.compile(r"^git\s+commit\b"),
    # 文件查看
    re.compile(r"^(ls|dir|pwd|echo|cat|type|more|less|head|tail|find|findstr|grep|where|which)\s"),
    # 版本检查
    re.compile(r"^(python|python3|node|npm|pip|git|go|rustc|cargo)\s+--version$"),
    re.compile(r"^(python|python3|node|npm|pip|git|go|rustc|cargo)\s+-v$"),
    # 目录创建（安全范围）
    re.compile(r"^mkdir\s+-p\s+\.?(/|\\)"),
    re.compile(r"^mkdir\s+\.?(/|\\)"),
    # pip 安装（通常安全）
    re.compile(r"^pip\s+(install|list|show)\s"),
    re.compile(r"^npm\s+(install|list|show)\s"),
]

# ================================================================
# Python 代码 AST 检查
# ================================================================

FORBIDDEN_IMPORTS = {"os", "subprocess", "ctypes", "socket", "sys"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open"}


def check_python_code(code: str) -> tuple[bool, str]:
    """
    AST 级 Python 代码安全检查

    返回:
        (is_safe, reason_or_None)
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Python 语法错误: {e}"

    for node in ast.walk(tree):
        # import os → 禁止
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_IMPORTS:
                    return False, f"Python 安全: 禁止导入模块 '{alias.name}'"
        # from os import * → 禁止
        if isinstance(node, ast.ImportFrom):
            if node.module in FORBIDDEN_IMPORTS:
                return False, f"Python 安全: 禁止导入模块 '{node.module}'"
        # eval() / exec() → 禁止
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                return False, f"Python 安全: 禁止调用 '{node.func.id}'"

    return True, ""


# ================================================================
# 输出脱敏（API Key / 私钥 / 密码）
# ================================================================

SECRET_PATTERNS = [
    # OpenAI / Anthropic API Key
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "sk-****"),
    (re.compile(r"sk-ant-[a-zA-Z0-9]{20,}"), "sk-ant-****"),
    # 私钥块
    (
        re.compile(
            r"-----BEGIN\s+(RSA |EC |DSA )?PRIVATE KEY-----"
            r".*?-----END\s+(RSA |EC |DSA )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[私钥已脱敏]",
    ),
    # 通用凭据 key=value 或 key: value
    (
        re.compile(
            r"(api[_-]?key|apikey|secret|password|token)"
            r'[=:]["\']?\w{8,}["\']?',
            re.IGNORECASE,
        ),
        r"\1: ****",
    ),
]


def sanitize_output(text: str) -> str:
    """对工具输出做脱敏处理（API Key / 私钥 / 密码 → ****）"""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ================================================================
# 环境变量清理
# ================================================================

SANITIZE_ENV_PREFIXES = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def sanitize_env(env: dict) -> dict:
    """剥离敏感环境变量"""
    return {
        k: v
        for k, v in env.items()
        if not any(k.upper().startswith(p) for p in SANITIZE_ENV_PREFIXES)
    }


# ================================================================
# 文件写入安全检查
# ================================================================


def check_write_content(file_path: str, content: str) -> tuple[bool, str]:
    """
    检查写入文件的内容是否安全

    参数:
        file_path: 目标文件路径
        content: 写入内容

    返回:
        (is_safe, reason_or_None)
    """
    # 1. 敏感路径检查
    for sensitive in SENSITIVE_FILES:
        if sensitive in file_path or file_path.endswith(sensitive):
            return False, f"禁止修改关键文件: {file_path}"

    # 2. 系统路径检查
    if _is_system_path(file_path):
        return False, f"禁止写入系统路径: {file_path}"

    # 3. 可执行注入检查
    for pattern in EXEC_INJECTION_PATTERNS:
        if pattern.search(content):
            return False, f"检测到可疑系统操作: {pattern.pattern[:50]}"

    return True, ""


# ================================================================
# 命令安全检查（主入口）
# ================================================================


def check_command_safety(
    command: str,
    tool_name: str = "bash",
    workspace: str | None = None,
) -> tuple[bool, str]:
    """
    检查命令内容安全性

    参数:
        command:   要执行的命令文本
        tool_name: 工具名称（bash / python）
        workspace: 工作区路径（可选）

    返回:
        (is_safe, reason) — True 表示安全，False 表示被拦截
    """
    # ===== 0. 白名单检查 =====
    for pattern in SAFE_PATTERNS:
        if pattern.search(command):
            return True, ""

    # ===== 1. 防外发数据检测 =====
    for pattern in DATA_LEAK_PATTERNS:
        if pattern.search(command):
            return (
                False,
                f"检测到疑似数据外发行为，已拦截: {command[:100]}",
            )

    # ===== 2. 防危险操作检测 =====
    for pattern in EXEC_INJECTION_PATTERNS:
        if pattern.search(command):
            return (
                False,
                f"检测到可疑系统操作，已拦截: {pattern.pattern[:50]}",
            )

    return True, ""


# ================================================================
# 网络域名黑名单检查
# ================================================================


def check_network_target(
    url: str,
    blocked_domains: list[str] | None = None,
    blocked_ips: list[str] | None = None,
) -> tuple[bool, str]:
    """
    检查网络请求目标是否在黑名单中

    参数:
        url:           请求 URL
        blocked_domains: 黑名单域名列表（支持通配符）
        blocked_ips:    黑名单 IP 段列表

    返回:
        (is_safe, reason) — True 表示安全，False 表示被拦截
    """
    if not blocked_domains:
        blocked_domains = []
    if not blocked_ips:
        blocked_ips = []

    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
    except Exception:
        return True, ""

    # 域名黑名单检查（支持 *.xyz 通配符）
    for pattern in blocked_domains:
        if pattern.startswith("*."):
            # *.xyz → 匹配 anything.xyz
            suffix = pattern[1:]  # .xyz
            if hostname.endswith(suffix):
                return False, f"请求目标在黑名单中: {hostname}（匹配 {pattern}）"
        else:
            if hostname == pattern or hostname.endswith("." + pattern):
                return False, f"请求目标在黑名单中: {hostname}（匹配 {pattern}）"

    return True, ""
