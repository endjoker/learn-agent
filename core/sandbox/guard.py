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
    """检查路径是否在系统关键路径下（路径段边界匹配）。

    对 POSIX 绝对路径（以 / 开头）直接用字符串前缀匹配，避免 Windows 上
    Path.resolve() 把 /etc 解析成 D:\\etc 导致的漏检。Windows 绝对路径
    （以盘符开头）走 resolve + normpath 比较。
    """
    s = str(path).replace("\\", "/")
    # POSIX 绝对路径：直接前缀匹配
    if s.startswith("/"):
        for sys_path in SYSTEM_PATHS_LINUX + SYSTEM_PATHS_MAC:
            sp = sys_path.replace("\\", "/").rstrip("/")
            if s == sp or s.startswith(sp + "/"):
                return True
        return False
    # Windows 绝对路径（盘符开头）
    try:
        p_norm = os.path.normpath(str(Path(path).resolve())).lower()
    except Exception:
        return False
    for sys_path in SYSTEM_PATHS_WIN:
        sp = os.path.normpath(sys_path).lower()
        if p_norm == sp or p_norm.startswith(sp + os.sep):
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
# 危险命令模式（OS 级危害，L2 最后硬拦）
# 与 permission.DANGEROUS 一致；bash 与 proc_* 在 L2 统一拦死，
# 不依赖 L1 规则是否设了 ALLOW。permission 的 DANGEROUS 保留作 L1 二级防线。
# ================================================================

# 单词型危险命令：必须作为独立令牌（前后空白/行首行尾）才命中
# 避免 "format" 误伤 PowerShell 的 "format-table"
# 限制在行首、空白后或命令分隔符后（; / && / || / |），防止参数中的误匹配（如 ls --format）
DANGEROUS_WORDS = ["format", "shutdown", "reboot", "halt"]


def _compile_words_re(words: list) -> re.Pattern:
    """从危险词列表编译正则（模块级定义和配置热更新共用）"""
    return re.compile(
        r"(?:^|\s|;\s*|\|\|\s*|&&\s*|\|\s*)(" + "|".join(words) + r")(?=\s|$)",
        re.IGNORECASE,
    )


_DANGEROUS_WORDS_RE = _compile_words_re(DANGEROUS_WORDS)

# 危险命令显示名称（保留供配置和提示文本使用）。不能再以子串方式对它们做安全判断：
# ``rm -rf /tmp/file`` 含有 ``rm -rf /``，但并不是删除根目录。
DANGEROUS_SUBSTRINGS = [
    "rm -rf /", "rm -rf ~", "rm -rf .",
    "del /f /s", "rd /s /q",
    "diskpart",              # Windows 磁盘分区工具
    "taskkill /f",           # 强制杀进程
    "chmod 0",               # Unix 权限清零（与 chmod 777 / 互补覆盖）
    "chown -r",              # 递归改所有者
    "mkfs",
    "dd if=",
    ":(){ :|:& };:",         # fork 炸弹
    "> /dev/sda", "> /dev/mmc",
]


_DANGEROUS_COMMAND_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Require the destructive target to be its own shell token.  ``/*`` is
    # included because it is equivalent to deleting the root's contents.
    ("rm -rf /", re.compile(
        r"(?:^|[;&|]\s*)rm(?:\s+(?:-[A-Za-z]*|--(?:force|recursive)))+"
        r"\s+(?:--\s+)?/(?:\*|(?=\s|$|[;&|]))", re.IGNORECASE)),
    ("rm -rf ~", re.compile(
        r"(?:^|[;&|]\s*)rm(?:\s+(?:-[A-Za-z]*|--(?:force|recursive)))+"
        r"\s+(?:--\s+)?~(?=\s|$|[;&|])", re.IGNORECASE)),
    ("rm -rf .", re.compile(
        r"(?:^|[;&|]\s*)rm(?:\s+(?:-[A-Za-z]*|--(?:force|recursive)))+"
        r"\s+(?:--\s+)?\.(?=\s|$|[;&|])", re.IGNORECASE)),
    ("del /f /s", re.compile(r"(?:^|[;&|]\s*)del\s+(?=[^\r\n]*\s/f\b)(?=[^\r\n]*\s/s\b)", re.IGNORECASE)),
    ("rd /s /q", re.compile(r"(?:^|[;&|]\s*)(?:rd|rmdir)\s+(?=[^\r\n]*\s/s\b)(?=[^\r\n]*\s/q\b)", re.IGNORECASE)),
    ("diskpart", re.compile(r"(?:^|[;&|]\s*)diskpart(?:\s|$)", re.IGNORECASE)),
    ("taskkill /f", re.compile(r"(?:^|[;&|]\s*)taskkill\s+[^\r\n]*\s/f\b", re.IGNORECASE)),
    # 0 / 00 / 000 are mode values; do not mistake chmod 0644 for chmod 0.
    ("chmod 0", re.compile(r"(?:^|[;&|]\s*)chmod\s+0{1,4}(?=\s|$)", re.IGNORECASE)),
    ("chown -r", re.compile(r"(?:^|[;&|]\s*)chown\s+(?:-[A-Za-z]*r[A-Za-z]*|--recursive)\s+[^\r\n]*\s/(?:\*|(?=\s|$|[;&|]))", re.IGNORECASE)),
    ("mkfs", re.compile(r"(?:^|[;&|]\s*)mkfs(?:\.[A-Za-z0-9_+-]+)?(?:\s|$)", re.IGNORECASE)),
    ("dd if=", re.compile(r"(?:^|[;&|]\s*)dd\s+[^\r\n]*\bif=", re.IGNORECASE)),
    (":(){ :|:& };:", re.compile(re.escape(":(){ :|:& };:"))),
    ("> /dev/sda", re.compile(r">\s*/dev/(?:sda|mmcblk\d+)(?:\d+)?(?=\s|$|[;&|])", re.IGNORECASE)),
]


def _match_dangerous(command_lower: str) -> str | None:
    """返回命中的危险模式（None 表示安全）。供 L2 硬拦使用。"""
    for label, pattern in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command_lower):
            return label
    m = _DANGEROUS_WORDS_RE.search(command_lower)
    if m:
        return m.group(1)
    return None


# ================================================================
# proc_send 内容检查（投喂到 REPL 的 input 可能是 shell/python 危险调用）
# ================================================================

# Python 危险调用模式（投喂到 python REPL 时）
_PROC_PYTHON_DANGER = [
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bos\.(remove|unlink|rmdir|kill)\s*\("),
    re.compile(r"\bshutil\.rmtree\s*\("),
    re.compile(r"\bsubprocess\b"),
    re.compile(r"\b__import__\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"\bctypes\b"),
]


def check_proc_send_input(text: str) -> tuple[bool, str]:
    """proc_send 的 L2 内容检查。

    投喂到 REPL 的 input 可能是 shell 命令或 python 代码，统一查两类危险模式：
    - shell 危险：data-leak / exec-injection / DANGEROUS（复用 check_command_safety 的模式）
    - python 危险：os.system / subprocess / eval / exec / __import__ / shutil.rmtree / ctypes

    返回 (is_safe, reason)。诚实声明：字符串模式启发，覆盖常见 OS-harm 向量；
    混淆/编码输入无法完全覆盖，靠 L1 区外 ASK + idle-timeout 兜底。
    """
    # shell 危险（直接复用 check_command_safety，它已含 DANGEROUS）
    safe, reason = check_command_safety(text, "bash")
    if not safe:
        return False, f"send 内容命中 shell 危险模式: {reason}"
    # python 危险
    for p in _PROC_PYTHON_DANGER:
        if p.search(text):
            return False, f"send 内容命中 Python 危险调用: {p.pattern}"
    return True, ""


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
# Python 代码 AST 检查（配置化）
# ================================================================

# 硬编码兜底默认值（config.json 不可用时使用）
_DEFAULT_FORBIDDEN_IMPORTS = {"subprocess", "ctypes", "socket"}
_DEFAULT_FORBIDDEN_CALLS = {"eval", "exec", "__import__"}
_DEFAULT_FORBIDDEN_QUALIFIED = {
    "os.system", "os.popen",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.remove", "os.unlink", "os.rmdir",
    "os.kill", "os.killpg",
    "shutil.rmtree",
}

# 运行时缓存（load_config 只读一次）
_python_rules: dict | None = None


def _get_python_rules() -> dict:
    """从 config.json 读取 python 安全规则，失败则用默认值"""
    global _python_rules
    if _python_rules is not None:
        return _python_rules
    try:
        from core.config_loader import load_config
        cfg = load_config()
        rules = cfg.get("sandbox", {}).get("python", {})
        _python_rules = {
            "forbidden_imports": set(rules.get("forbidden_imports", list(_DEFAULT_FORBIDDEN_IMPORTS))),
            "forbidden_calls": set(rules.get("forbidden_calls", list(_DEFAULT_FORBIDDEN_CALLS))),
            "forbidden_qualified": set(rules.get("forbidden_qualified_calls", list(_DEFAULT_FORBIDDEN_QUALIFIED))),
        }
    except Exception:
        _python_rules = {
            "forbidden_imports": _DEFAULT_FORBIDDEN_IMPORTS,
            "forbidden_calls": _DEFAULT_FORBIDDEN_CALLS,
            "forbidden_qualified": _DEFAULT_FORBIDDEN_QUALIFIED,
        }
    return _python_rules


def reload_python_rules():
    """清除缓存，下次 check 时重新读 config"""
    global _python_rules
    _python_rules = None


def _top_module(dotted: str | None) -> str:
    """取点分模块名的顶层模块（os.path → os），用于拦截子模块导入。"""
    if not dotted:
        return ""
    return dotted.split(".")[0]


def _qualified_name(node: ast.Call) -> str | None:
    """提取调用的限定名（如 os.system），用于 forbidden_qualified 匹配。"""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return None


def check_python_code(code: str) -> tuple[bool, str]:
    """
    AST 级 Python 代码安全检查（规则从 config.json 的 sandbox.python 段读取）

    检查三类：
      1. forbidden_imports — 整个模块禁止导入
      2. forbidden_calls — 裸函数调用禁止（如 eval()）
      3. forbidden_qualified_calls — 限定调用禁止（如 os.system()）

    返回:
        (is_safe, reason_or_None)
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Python 语法错误: {e}"

    rules = _get_python_rules()
    forbidden_imports = rules["forbidden_imports"]
    forbidden_calls = rules["forbidden_calls"]
    forbidden_qualified = rules["forbidden_qualified"]

    for node in ast.walk(tree):
        # import os / import os.path → 禁止（按顶层模块判断）
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _top_module(alias.name) in forbidden_imports:
                    return False, f"Python 安全: 禁止导入模块 '{alias.name}'"
        # from os import * / from subprocess import run → 禁止（按顶层模块判断）
        if isinstance(node, ast.ImportFrom):
            if _top_module(node.module) in forbidden_imports:
                return False, f"Python 安全: 禁止导入模块 '{node.module}'"
        # 函数调用检查
        if isinstance(node, ast.Call):
            # 裸调用：eval() / exec()
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                return False, f"Python 安全: 禁止调用 '{node.func.id}'"
            # 限定调用：os.system() / shutil.rmtree()
            qname = _qualified_name(node)
            if qname and qname in forbidden_qualified:
                return False, f"Python 安全: 禁止调用 '{qname}'"

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

# 敏感标记：名字中作为独立段（分隔符边界）出现即视为密钥变量
# 覆盖 OPENAI_API_KEY / GITHUB_TOKEN / DB_PASSWORD / ANTHROPIC_API_KEY 等
# 厂商前缀命名，避免仅匹配 "以 TOKEN 开头" 抓不到的情况
SECRET_ENV_MARKERS = (
    "API_KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
)
_SECRET_ENV_RE = re.compile(
    r"(?:^|[_\-])(" + "|".join(SECRET_ENV_MARKERS) + r")(?:$|[_\-])"
)


def _is_secret_env_key(name: str) -> bool:
    """判断环境变量名是否为敏感凭据（按段匹配，避免误伤 TOKENIZER 等）"""
    return _SECRET_ENV_RE.search(name.upper()) is not None


def sanitize_env(env: dict) -> dict:
    """剥离敏感环境变量（API Key / Token / 密码等）"""
    return {k: v for k, v in env.items() if not _is_secret_env_key(k)}


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
    # 1. 敏感路径检查（按路径段边界匹配，避免 endswith 误伤 subagent.py）
    p_norm = os.path.normpath(file_path).lower()
    for sensitive in SENSITIVE_FILES:
        s_norm = os.path.normpath(sensitive).lower()
        if p_norm == s_norm or p_norm.endswith(os.sep + s_norm):
            return False, f"禁止修改关键文件: {sensitive}"

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


# 从命令中提取候选绝对路径（Unix: /…  Windows: X:\…）
_CMD_ABS_PATH_RE = re.compile(r'(/[^\s;|&<>`\'"(){}\[\]]{2,})')


def _check_command_for_paths(command: str) -> tuple[bool, str]:
    """检查命令字符串中引用的路径是否命中系统路径或敏感文件。

    从命令中提取绝对路径并调用 _is_system_path()；同时检测命令引用的
    token 是否命中 SENSITIVE_FILES 基名。
    返回 (is_blocked, reason)。
    """
    # 1. 绝对路径 → 系统路径检查
    for m in _CMD_ABS_PATH_RE.finditer(command):
        p = m.group(1)
        if _is_system_path(p):
            return True, f"检测到系统路径操作，已拦截: {p}"

    # 2. 敏感文件引用（按文件名 token 检测，如 rm agent.py / cat .env / > core/…）
    cmd_lower = command.lower()
    for sf in SENSITIVE_FILES:
        sf_base = os.path.basename(sf).lower()
        if not sf_base:
            continue
        # 用正则做 token 级匹配，避免 .env 误伤 .env.example
        if re.search(r'(?:^|[\s;|&<>`\'"(){}\[\]])(?:[./]*)?'
                     + re.escape(sf_base)
                     + r'(?:[\s;|&<>`\'"(){}\[\]$]|$)', cmd_lower):
            return True, f"检测到敏感文件操作，已拦截: {sf}"

    return False, ""


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
    # ===== 0. 危险命令（OS 级，最后硬拦——最先检查，不可被白名单绕过）=====
    dangerous = _match_dangerous(command.lower())
    if dangerous:
        return False, f"检测到危险命令，已拦截: {dangerous}"

    # ===== 0.5. 系统路径 & 敏感文件检测（在白名单之前，ls /etc 也需拦截）=====
    blocked, reason = _check_command_for_paths(command)
    if blocked:
        return False, reason

    # ===== 1. 白名单检查 =====
    for pattern in SAFE_PATTERNS:
        if pattern.search(command):
            return True, ""

    # ===== 2. 防外发数据检测 =====
    for pattern in DATA_LEAK_PATTERNS:
        if pattern.search(command):
            return (
                False,
                f"检测到疑似数据外发行为，已拦截: {command[:100]}",
            )

    # ===== 3. 防危险操作检测 =====
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

    # IP/CIDR 黑名单检查（如 10.0.0.0/8 内网段）
    # 仅对 IP 字面量的 hostname 生效；域名需 DNS 解析，不在静态黑名单范围
    if blocked_ips:
        import ipaddress

        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None  # 域名而非 IP 字面量，跳过 IP 检查
        if ip is not None:
            for entry in blocked_ips:
                try:
                    if "/" in entry:
                        net = ipaddress.ip_network(entry, strict=False)
                    else:
                        net = ipaddress.ip_network(entry + "/32", strict=False)
                except ValueError:
                    continue  # 非法条目，跳过
                if ip in net:
                    return False, f"请求目标在黑名单中: {hostname}（匹配 {entry}）"

    return True, ""


# ================================================================
# 从统一配置加载安全参数
# ================================================================

_guard_config_loaded = False


def apply_guard_config(config: dict = None):
    """
    从统一配置（config.json 的 sandbox section）更新模块级安全参数。

    调用时机：模块导入后、Agent 初始化前。
    未提供的 key 保留硬编码默认值。

    参数:
        config: sandbox section 的 dict。None 时自动从 config_loader 加载。
    """
    global SENSITIVE_FILES, SYSTEM_PATHS_WIN, SYSTEM_PATHS_LINUX, SYSTEM_PATHS_MAC
    global DANGEROUS_SUBSTRINGS, DANGEROUS_WORDS, _DANGEROUS_WORDS_RE
    global _guard_config_loaded

    if config is None:
        try:
            from core.config_loader import load_config
            config = load_config().get("sandbox", {})
        except Exception:
            return

    if not config or _guard_config_loaded:
        return

    # --- sensitive_files ---
    if "sensitive_files" in config:
        SENSITIVE_FILES[:] = config["sensitive_files"]

    # --- system_paths ---
    sp = config.get("system_paths", {})
    if isinstance(sp, dict):
        if "windows" in sp:
            SYSTEM_PATHS_WIN[:] = sp["windows"]
        if "linux" in sp:
            SYSTEM_PATHS_LINUX[:] = sp["linux"]
        if "mac" in sp:
            SYSTEM_PATHS_MAC[:] = sp["mac"]

    # --- dangerous_commands ---
    if "dangerous_commands" in config:
        DANGEROUS_SUBSTRINGS[:] = config["dangerous_commands"]

    # --- dangerous_words（需要重建正则） ---
    if "dangerous_words" in config:
        DANGEROUS_WORDS[:] = config["dangerous_words"]
        _DANGEROUS_WORDS_RE = _compile_words_re(DANGEROUS_WORDS)

    _guard_config_loaded = True


# 模块导入时自动加载
try:
    apply_guard_config()
except Exception:
    pass  # 静默失败，使用硬编码默认值
