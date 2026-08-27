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
    9. L2 规则热更（config.json 的 sandbox 段 mtime 校验，WebUI 保存后自动重载）
"""

import ast
import os
import re
import shlex
from pathlib import Path
from functools import lru_cache

# ================================================================
# 敏感文件列表（禁止写入 / 编辑）
# ================================================================

# Legacy compatibility symbol. Ordinary project files are not permission-sensitive;
# the unified PolicyEngine only treats SYSTEM_PATHS_* as approval-sensitive paths.
SENSITIVE_FILES: list[str] = []

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
    "/etc", "/sys", "/proc", "/var/lib", "/boot",
]
SYSTEM_PATHS_MAC = [
    "/System", "/Library", "/Applications",
]


def _path_prefix_hit(s: str, sys_paths: list) -> bool:
    """路径串是否命中系统路径前缀（段边界匹配）。"""
    for sp in sys_paths:
        sp = sp.replace("\\", "/").rstrip("/")
        if s == sp or s.startswith(sp + "/"):
            return True
    return False


def _is_system_path(path: str | Path) -> bool:
    """检查路径是否在系统关键路径下（路径段边界匹配）。

    对 POSIX 绝对路径（以 / 开头）先做字面前缀匹配（避免 Windows 上
    Path.resolve() 把 /etc 解析成 D:\\etc 导致的漏检），再做 normpath
    规范化（/./etc、/a/../etc、//etc）与 $VAR 展开（$PWD/../etc）复检，
    最后做符号链接展开（realpath）复检，防规范化绕过。Windows 绝对路径
    （以盘符开头）走 resolve + normpath 比较。
    """
    raw = str(path)
    s = raw.replace("\\", "/")
    posix_sys = SYSTEM_PATHS_LINUX + SYSTEM_PATHS_MAC
    # POSIX 绝对路径（含 $VAR 前缀，如 $PWD/../etc）
    if s.startswith("/") or raw.startswith("$"):
        if _path_prefix_hit(s, posix_sys):
            return True
        # 规范化复检：/./etc、/a/../etc、//etc、$PWD/../etc
        expanded = os.path.expandvars(raw)
        norm = os.path.normpath(expanded.replace("\\", "/"))
        if norm.startswith("/") and _path_prefix_hit(norm, posix_sys):
            return True
        # 符号链接展开复检（失败静默，依赖前两轮）
        try:
            resolved = os.path.realpath(expanded).replace("\\", "/")
        except Exception:
            resolved = ""
        if resolved.startswith("/") and _path_prefix_hit(resolved, posix_sys):
            return True
        return False
    # Windows 绝对路径（盘符开头）或相对路径：resolve 后同时比较
    # Windows 系统路径与 POSIX 系统路径（../../etc 之类相对路径逃逸）
    try:
        p_norm = os.path.normpath(str(Path(path).resolve())).lower()
    except Exception:
        return False
    for sys_path in SYSTEM_PATHS_WIN:
        sp = os.path.normpath(sys_path).lower()
        if p_norm == sp or p_norm.startswith(sp + os.sep):
            return True
    if _path_prefix_hit(p_norm.replace("\\", "/"), posix_sys):
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

# 单词型危险命令：作为剥离 env/sudo 前缀后的首令牌才命中
# 避免 "format" 误伤 PowerShell 的 "format-table"、参数误匹配（如 grep format）
DANGEROUS_WORDS = ["format", "shutdown", "reboot", "halt"]

# 破坏性命令：目标命中关键系统目录即拦（P0-2）
DESTRUCTIVE_COMMANDS = ("rm", "chmod", "chown", "dd", "mkfs")

# 关键系统目录（rm/chmod/chown/dd/mkfs 目标命中即拦；~ 与 $HOME 本体视为关键）
CRITICAL_DIRS = (
    "/bin", "/sbin", "/boot", "/etc", "/usr", "/var", "/lib",
    "/root", "/dev", "/sys", "/proc",
)


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
    # 变体（无空格/不同空白）：:(){:|:&};: 等
    ("fork bomb", re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|?\s*:?\s*&\s*\}")),
    ("> /dev/sda", re.compile(r">\s*/dev/(?:sda|mmcblk\d+)(?:\d+)?(?=\s|$|[;&|])", re.IGNORECASE)),
]


def _tokenize_command(command: str) -> list[str]:
    """shlex 分词；引号不闭合等异常时回退到空白切分。"""
    try:
        return shlex.split(command)
    except ValueError:
        return re.findall(r"[^\s]+", command)


def _split_subcommands(command: str) -> list[str]:
    """按 ;  &&  ||  |  及换行拆分子命令（空段丢弃）。

    逐段检测避免整串正则对复合命令的粘合误判，也让 env/sudo 前缀剥离
    和首令牌判定按段独立进行。
    """
    return [seg for seg in re.split(r"[;&|\n]+", command) if seg.strip()]


def _strip_privilege_prefix(tokens: list[str]) -> list[str]:
    """剥离 env 前缀赋值（FOO=bar ...）与 sudo / sudo -u USER / sudo -g GROUP。

    返回新列表，不修改入参。'env FOO=1 cmd' 形式的 env 命令一并剥离。
    """
    tokens = list(tokens)
    while tokens and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])
                      or tokens[0] == "env"):
        tokens.pop(0)
    while tokens and tokens[0] == "sudo":
        tokens.pop(0)
        while tokens and tokens[0] in ("-u", "-g", "--user", "--group"):
            tokens.pop(0)
            if tokens and not tokens[0].startswith("-"):
                tokens.pop(0)
    return tokens


# rm 全量删除目标（-rf 变体：/ . .. ./* /* 等）
_RM_WIPE_TARGETS = frozenset({".", "..", "/*", "./*", "../*", "*"})


def _is_critical_target(token: str) -> bool:
    """令牌是否命中关键系统目录（含 /、~ 与 $HOME 本体）。"""
    t = token.strip("'\"")
    if t == "/":
        return True
    norm = t.rstrip("/")
    if norm in ("~", "$home", "${home}"):
        return True
    for d in CRITICAL_DIRS:
        if norm == d or norm.startswith(d + "/"):
            return True
    return False


def _destructive_target_hit(tokens: list[str]) -> str | None:
    """破坏性命令目标命中关键目录时返回命中的目标令牌（None 表示安全）。"""
    cmd = tokens[0].strip("'\"")
    args = tokens[1:]
    # rm：直接检查全部非选项目标（覆盖 . .. ./* /* 及关键目录）
    if cmd == "rm":
        for a in args:
            if a == "--" or a.startswith("-"):
                continue
            t = a.strip("'\"")
            norm = t.rstrip("/") if t != "/" else t
            if norm in _RM_WIPE_TARGETS or _is_critical_target(t):
                return t
        return None
    # chmod / chown：跳过选项与权限/属主说明符，检查路径型参数
    if cmd in ("chmod", "chown"):
        for a in args:
            if a == "--" or a.startswith("-"):
                continue
            t = a.strip("'\"")
            if not t.startswith(("/", "~", "$", ".")):
                continue  # 777 / a+x / user:group 等非路径参数
            if _is_critical_target(t):
                return t
        return None
    # dd：of= 写入目标
    if cmd == "dd":
        for a in args:
            m = re.match(r"of=(.+)$", a)
            if m and _is_critical_target(m.group(1)):
                return a
        return None
    # mkfs / mkfs.ext4 ...
    if cmd.startswith("mkfs"):
        for a in args:
            if a.startswith("-"):
                continue
            if _is_critical_target(a.strip("'\"")):
                return a
        return None
    return None


@lru_cache(maxsize=512)
def _match_dangerous(command_lower: str) -> str | None:
    """返回命中的危险模式（None 表示安全）。供 L2 硬拦使用。

    三层检测（对 command.lower() 之后的文本）：
      1. 整串正则模式 —— fork bomb / Windows 命令 / 设备重定向等，避免被切碎漏检
      2. 按 ;  &&  ||  |  及换行拆分子命令，逐段剥离 env/sudo 前缀后做令牌级检测
         —— 单词型危险命令（format/shutdown/reboot/halt）与
            rm/chmod/chown/dd/mkfs 关键目录目标

    模块级 verdict LRU（key=lower(command)，maxsize 512）：permission.py /
    policy_engine.py / builtin_tools.py / check_command_safety 多闸门共用同一判定
    缓存（多闸门去重，L3 配合项）。DANGEROUS_WORDS 等配置变化（apply_guard_config
    调用或 mtime 热更）时整体 cache_clear()；仅在缓存未命中时校验 config.json
    mtime（命中路径零额外开销）。
    """
    # L2 规则热更：缓存未命中才校验 config.json mtime（命中直接返回）
    _maybe_reload_guard_config()
    for label, pattern in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command_lower):
            return label
    words = {w.lower() for w in DANGEROUS_WORDS}
    for segment in _split_subcommands(command_lower):
        tokens = _strip_privilege_prefix(_tokenize_command(segment))
        if not tokens:
            continue
        cmd = tokens[0].strip("'\"")
        # bash/sh -c '嵌套命令'：递归检测内层，堵住引号包裹绕过（P0-2）
        if cmd in ("bash", "sh", "dash", "zsh", "ksh") and "-c" in tokens:
            idx = tokens.index("-c")
            if idx + 1 < len(tokens):
                inner = tokens[idx + 1].strip("'\"")
                if inner:
                    inner_hit = _match_dangerous(inner)
                    if inner_hit is not None:
                        return "bash -c 嵌套: " + inner_hit
        if cmd.lower() in words:
            return cmd
        if cmd in DESTRUCTIVE_COMMANDS or cmd.startswith("mkfs"):
            hit = _destructive_target_hit(tokens)
            if hit is not None:
                return f"{cmd} 目标命中关键目录: {hit}"
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

# 硬编码兜底默认值：仅当 config.json 未提供对应键时使用；
# config.json 一旦提供某键（如 forbidden_calls），以配置为准（整体替换，
# 不与默认集叠加——否则配置放宽的项会被默认集悄悄加回，形成"残留"）。
_DEFAULT_FORBIDDEN_IMPORTS = {"subprocess", "ctypes", "socket", "importlib"}
_DEFAULT_FORBIDDEN_CALLS = {"eval", "exec", "__import__", "compile", "open", "getattr", "setattr"}
_DEFAULT_FORBIDDEN_QUALIFIED = {
    "os.system", "os.popen",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.remove", "os.unlink", "os.rmdir",
    "os.kill", "os.killpg",
    "shutil.rmtree",
    "builtins.open", "builtins.eval", "builtins.exec",
    "builtins.compile", "builtins.__import__",
    "builtins.getattr", "builtins.setattr",
}
# 运行时缓存（带 config.json mtime 校验，热更自动重载）
_python_rules: dict | None = None

# 最近一次生效的 config.json mtime_ns；None 表示尚未加载/文件缺失。
# mtime 变化时自动重载模块级安全参数与 python_rules，
# WebUI 保存配置后下一次检查自动生效（L2 规则热更）。
_guard_config_mtime_ns: int | None = None

# config.json 路径缓存（基于 core.config_loader._find_project_root，进程内稳定）
_guard_config_path: Path | None = None


def _config_file_mtime_ns() -> int | None:
    """config.json 的 mtime_ns；文件缺失/不可读返回 None。"""
    global _guard_config_path
    if _guard_config_path is None:
        try:
            from core.config_loader import _find_project_root
            _guard_config_path = _find_project_root() / "config.json"
        except Exception:
            # 兜底：guard.py 位于 <根>/core/sandbox/，向上三级即项目根
            _guard_config_path = Path(__file__).resolve().parents[2] / "config.json"
    try:
        return _guard_config_path.stat().st_mtime_ns
    except OSError:
        return None


def _invalidate_guard_caches() -> None:
    """整体失效 verdict LRU 与 python_rules 缓存（配置可能变化，旧判定作废）。

    DANGEROUS_WORDS 等模块级参数变化会改变 _match_dangerous 判定结果，
    因此 apply_guard_config 调用 / mtime 热更重载时必须整体 cache_clear()。
    """
    global _python_rules
    _python_rules = None
    _match_dangerous.cache_clear()


def _apply_sandbox_config(config: dict) -> None:
    """把 sandbox 段配置应用到模块级安全参数（列表整体替换，保持引用不变）。"""
    if "sensitive_files" in config:
        SENSITIVE_FILES[:] = config["sensitive_files"]

    sp = config.get("system_paths", {})
    if isinstance(sp, dict):
        if "windows" in sp:
            SYSTEM_PATHS_WIN[:] = sp["windows"]
        if "linux" in sp:
            SYSTEM_PATHS_LINUX[:] = sp["linux"]
        if "mac" in sp:
            SYSTEM_PATHS_MAC[:] = sp["mac"]

    if "dangerous_words" in config:
        DANGEROUS_WORDS[:] = config["dangerous_words"]


def _maybe_reload_guard_config() -> bool:
    """config.json mtime 变化时自动重载模块级安全参数与 python_rules。

    WebUI 保存配置后下一次检查自动生效；返回是否发生重载。
    mtime 不可得（config.json 缺失）时不重载，沿用已生效配置（fail-safe）。
    """
    global _guard_config_mtime_ns
    mtime = _config_file_mtime_ns()
    if mtime is None or mtime == _guard_config_mtime_ns:
        return False
    _guard_config_mtime_ns = mtime
    try:
        # force_reload：config_loader 的进程内缓存也必须同步到磁盘最新内容
        from core.config_loader import load_config
        config = load_config(force_reload=True).get("sandbox", {})
    except Exception:
        return False
    _invalidate_guard_caches()
    _apply_sandbox_config(config)
    return True


def _get_python_rules() -> dict:
    """从 config.json 读取 python 安全规则，失败则用默认值

    规则缓存带 config.json mtime 校验：mtime 变化自动重载
    （WebUI 保存配置后下一次检查自动生效）。
    """
    global _python_rules
    _maybe_reload_guard_config()
    if _python_rules is not None:
        return _python_rules
    try:
        from core.config_loader import load_config
        cfg = load_config()
        rules = cfg.get("sandbox", {}).get("python", {})
        _python_rules = _merge_python_rules(rules)
    except Exception:
        _python_rules = _merge_python_rules({})
    return _python_rules


def _merge_python_rules(rules: dict) -> dict:
    """config 的 sandbox.python 段 → 运行时黑名单。

    语义：config.json 提供了某键（含显式空列表）→ 以配置为准（整体替换）；
    未提供该键 → 用硬编码兜底默认。修复"配置放宽后默认集叠加回来说明残留"
    的问题（如配置移除 open，运行时却仍拦截）。
    """
    merged: dict = {}
    for key, default, source_key in (
        ("forbidden_imports", _DEFAULT_FORBIDDEN_IMPORTS, "forbidden_imports"),
        ("forbidden_calls", _DEFAULT_FORBIDDEN_CALLS, "forbidden_calls"),
        ("forbidden_qualified", _DEFAULT_FORBIDDEN_QUALIFIED, "forbidden_qualified_calls"),
    ):
        value = rules.get(source_key)
        merged[key] = set(value) if value is not None else set(default)
    return merged

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
    # AWS Access Key ID（AKIA + 16 位大写字母数字）
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA****"),
    # GCP API Key（AIza + 35 位）
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "AIza****"),
    # GitHub classic PAT（ghp_ + 36 位）与 fine-grained PAT（github_pat_ + 82 位）
    (re.compile(r"\bghp_[0-9A-Za-z]{36}\b"), "ghp_****"),
    (re.compile(r"\bgithub_pat_[0-9A-Za-z_]{82}\b"), "github_pat_****"),
    # Slack bot/user token（xoxb- / xoxp- / xoxa- / xoxr- / xoxs-）
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), "xox*-****"),
    # Google OAuth access token
    (re.compile(r"\bya29\.[0-9A-Za-z_-]+\b"), "ya29.****"),
]


# 凭据泄漏检测正则（供 DLP 复用：POST 数据 / 命令内容中的密钥泄漏）。
# 覆盖 OpenAI/Anthropic sk-、AWS AKIA、GCP AIza、GitHub ghp_/github_pat_、
# Slack xox*-、Google OAuth ya29. 及私钥块。
_CREDENTIAL_LEAK_RE = re.compile(
    r"(?:sk-[a-zA-Z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|ghp_[0-9A-Za-z]{36}"
    r"|github_pat_[0-9A-Za-z_]{82}"
    r"|xox[baprs]-[0-9A-Za-z-]{10,}"
    r"|ya29\.[0-9A-Za-z_-]+"
    r"|-----BEGIN\s+(?:RSA |EC |DSA )?PRIVATE KEY-----)",
    re.DOTALL,
)


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
# 组合式密钥变量名（段匹配抓不到的命名，如 AWS_ACCESS_KEY_ID / MYSQL_PWD）
_SECRET_ENV_COMPOUND = (
    "ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "MYSQL_PWD",
)
_SECRET_ENV_RE = re.compile(
    r"(?:^|[_\-])(" + "|".join(SECRET_ENV_MARKERS) + r")(?:$|[_\-])"
    r"|(?:^|[_\-])(?:[A-Za-z0-9_]+_)?(" + "|".join(_SECRET_ENV_COMPOUND) + r")(?:$|[_\-])"
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


def check_write_content(file_path: str, content: str, *,
                        check_policy_paths: bool = True) -> tuple[bool, str]:
    """
    检查写入文件的内容是否安全

    参数:
        file_path: 目标文件路径
        content: 写入内容

    返回:
        (is_safe, reason_or_None)
    """
    # L2 规则热更：config.json mtime 变化时自动重载（sensitive_files / 系统路径等）
    _maybe_reload_guard_config()

    # 1. 敏感路径检查（按路径段边界匹配，避免 endswith 误伤 subagent.py）
    p_norm = os.path.normpath(file_path).lower()
    if check_policy_paths:
        for sensitive in SENSITIVE_FILES:
            s_norm = os.path.normpath(sensitive).lower()
            if p_norm == s_norm or p_norm.endswith(os.sep + s_norm):
                return False, f"禁止修改关键文件: {sensitive}"

    # System paths are classified by PolicyEngine as ASK, not hard-denied here.
    if check_policy_paths and _is_system_path(file_path):
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


def _check_command_for_paths(command: str, sensitive_files: list | None = None) -> tuple[bool, str]:
    """检查命令字符串中引用的路径是否命中系统路径或敏感文件。

    从命令中提取绝对路径并调用 _is_system_path()；同时检测命令引用的
    token 是否命中 SENSITIVE_FILES 基名。
    sensitive_files 可传入自定义列表（如 unreviewed 模式只查密钥/版本库），
    默认使用模块级 SENSITIVE_FILES。
    返回 (is_blocked, reason)。
    """
    # 1. 绝对路径 → 系统路径检查
    for m in _CMD_ABS_PATH_RE.finditer(command):
        p = m.group(1)
        if _is_system_path(p):
            return True, f"检测到系统路径操作，已拦截: {p}"

    # 2. 敏感文件引用（按文件名 token 检测，如 rm agent.py / cat .env / > core/…）
    cmd_lower = command.lower()
    sf_list = SENSITIVE_FILES if sensitive_files is None else sensitive_files
    for sf in sf_list:
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
    *,
    check_policy_paths: bool = True,
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
    # L2 规则热更：config.json mtime 变化时自动重载（危险词 / 敏感文件 / 系统路径）
    _maybe_reload_guard_config()

    # ===== 0. 危险命令（OS 级，最后硬拦——最先检查，不可被白名单绕过）=====
    dangerous = _match_dangerous(command.lower())
    if dangerous:
        return False, f"检测到危险命令，已拦截: {dangerous}"

    # Shell path policy may be handled by PolicyEngine; sensitive-file
    # references are hard L2 only when the configured list is active.
    if check_policy_paths:
        blocked, reason = _check_command_for_paths(command)
        if blocked:
            return False, reason
    else:
        # Preserve hard system-path detection, but do not reject ordinary
        # project files merely because their basename is listed as sensitive.
        for match in _CMD_ABS_PATH_RE.finditer(command):
            if _is_system_path(match.group(1)):
                return False, f"检测到系统路径操作，已拦截: {match.group(1)}"

    # ===== 1. 白名单检查 =====
    # 仅对无分隔符/换行的简单命令允许前缀早退；多行/复合命令必须继续对
    # 全文做 DLP 与注入扫描，防止 `git status; curl -d @/x …` 被前缀白名单跳过。
    if not re.search(r"[;&|\n]", command):
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


# 从统一配置加载安全参数（mtime 热更缓存）
# ================================================================


def apply_guard_config(config: dict = None):
    """
    从统一配置（config.json 的 sandbox section）更新模块级安全参数。

    调用时机：模块导入后、Agent 初始化前；也可在运行期调用以强制刷新。
    未提供的 key 保留硬编码默认值。

    每次调用都会整体失效 verdict LRU 与 python_rules 缓存（L3 配合项：
    配置可能变化，旧判定一律作废）。config=None 时强制从磁盘重新加载
    （load_config(force_reload=True)），传 sandbox dict 时直接应用。

    参数:
        config: sandbox section 的 dict。None 时自动从 config_loader 加载。
    """
    global _guard_config_mtime_ns

    _invalidate_guard_caches()

    if config is None:
        try:
            from core.config_loader import load_config
            cfg = load_config(force_reload=True)
            config = cfg.get("sandbox", {})
        except Exception:
            return

    _guard_config_mtime_ns = _config_file_mtime_ns()

    if not config:
        return

    _apply_sandbox_config(config)


# 模块导入时自动加载
try:
    apply_guard_config()
except Exception:
    pass  # 静默失败，使用硬编码默认值