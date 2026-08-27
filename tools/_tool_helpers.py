"""P3-7 拆分模块：共享工具辅助函数（由 builtin_tools.py 迁出，builtin_tools.py 负责 re-export 兼容）。"""

import os
import re
from pathlib import Path
from typing import List


_PERMISSION_LADDER_MODES = {"ask", "allow", "unreviewed"}

TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".md", ".txt", ".json",
    ".yaml", ".yml", ".toml", ".cfg", ".conf", ".ini", ".env",
    ".css", ".html", ".htm", ".xml", ".svg",
    ".sh", ".bat", ".ps1", ".bash", ".zsh",
    ".sql", ".rb", ".go", ".rs", ".java", ".cpp", ".c", ".h",
    ".hpp", ".cs", ".swift", ".kt", ".scala",
    ".vue", ".svelte", ".astro", ".php", ".r",
    ".mjs", ".cjs", ".mts", ".cts",
    ".csv", ".tsv", ".log",
    ".gradle", ".sbt", ".cmake",
    ".tex", ".rst", ".adoc",
    ".dockerfile", ".Makefile",
    ".pl", ".pm", ".lua", ".hs",
    ".ml", ".mli", ".scm", ".clj",
    ".dart", ".groovy", ".erl",
}

TEXT_FILENAMES = {
    "Dockerfile", "Makefile", "CHANGELOG", "LICENSE",
    "README", "CONTRIBUTING", "Gemfile", "Rakefile", "Procfile",
}

SCAN_EXCLUDED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}

def _path_within_roots(path: Path, roots) -> bool:
    """路径是否落在任一允许根内（resolve + relative_to 双保险）。"""
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = path.absolute()
    for root in roots:
        try:
            root_path = Path(root).resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        try:
            resolved.relative_to(root_path)
            return True
        except ValueError:
            continue
    return False

def _collect_allowed_roots(tool) -> tuple:
    """按优先级收集允许的工作区根（去重保序）：

    1. 注入的 PolicyEngine（.allowed_roots 公开属性）
    2. 显式注入的 workspace_roots
    3. 沙箱执行器携带的工作区根（未注入 policy 时的兼容来源）
    """
    roots: list = []
    policy = getattr(tool, "_policy", None)
    if policy is not None:
        try:
            roots.extend(policy.allowed_roots)
        except Exception:
            pass
    roots.extend(getattr(tool, "_workspace_roots", None) or ())
    sandbox = getattr(tool, "_sandbox", None)
    if sandbox is not None:
        ws = getattr(sandbox, "_workspace", None)
        if ws:
            roots.append(ws)
        roots.extend(getattr(sandbox, "_extra_workspace_roots", ()) or ())

    seen = set()
    out = []
    for r in roots:
        try:
            key = str(Path(r).resolve(strict=False))
        except (OSError, RuntimeError):
            key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return tuple(out)

def _check_workspace_boundary(tool, path_value) -> str | None:
    """校验用户可控路径是否落在工作区边界内。

    返回 None 表示通过；否则返回结构化越界错误文本。
    未配置任何工作区根时不拦截（保持向后兼容）。
    """
    if not path_value:
        return None
    roots = _collect_allowed_roots(tool)
    if not roots:
        return None
    if _path_within_roots(Path(path_value), roots):
        return None
    return f"路径超出允许的工作区边界: {path_value}"

def _mutation_boundary_err(tool, path_value) -> str | None:
    """写类工具（fs:write）的工作区边界校验——四档权限感知版。

    PolicyEngine 对 fs:write 的四档语义（core/policy_engine.py）：
      readonly → DENY；ask → 全部 ASK；allow → 界外 ASK；unreviewed → ALLOW。
    主链路中授权层（_dispatch_native_call 的 gate）先于工具执行完成裁决与
    用户确认——工具层硬边界若在这些模式下一票否决，会出现「用户确认了也
    写不进去」的权限违背。因此：
      - 注入了权限且模式 ∈ {ask, allow, unreviewed}：边界交还四档裁决
        （界外写按档位 ASK / 放行，确认后的执行必须可达）；
      - readonly：保持硬边界（与 DENY 一致，纵深防御）；
      - 未注入权限的直调路径（工作区运行时 / 系统 API 等无裁决层场景）：
        硬边界是唯一防线，保持默认拒绝。
    """
    permission = getattr(tool, "_permission", None)
    if permission is not None:
        mode = str(getattr(permission, "permission_mode", "") or "")
        if mode in _PERMISSION_LADDER_MODES:
            return None
    return _check_workspace_boundary(tool, path_value)

def _read_boundary_err(tool, path_value) -> str | None:
    """读类工具（fs:read）的工作区边界校验——四档权限感知版。

    PolicyEngine 对 fs:read 的四档语义：**全模式无条件 ALLOW**（唯一的读
    限制是系统路径在非 unreviewed 下 ASK，由授权层的 gate 裁决）。项目权限
    只遵循四项基本权限——工具层不得对读追加四档之外的限制，因此注入了
    PermissionChecker 后读边界完全交还裁决。硬边界仅保留给未注入权限的
    直调路径（工作区运行时 / 系统 API / catalog 等无裁决层场景）。
    """
    if getattr(tool, "_permission", None) is not None:
        return None
    return _check_workspace_boundary(tool, path_value)

def _format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"

def _safe_stat(f) -> tuple[int, float]:
    """容错版 Path.stat（P3-8）：文件可能在遍历后被删除，stat 抛 OSError
    时返回 (size=0, mtime=0)，不让整个 glob 失败。"""
    try:
        st = f.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return 0, 0.0

def _is_text_file(file_path: Path) -> bool:
    """判断一个文件是否是文本文件（跳过二进制文件）"""

    # 跳过 .git 目录
    if ".git" in file_path.parts:
        return False

    # 检查扩展名
    if file_path.suffix.lower() in TEXT_EXTENSIONS:
        return True

    # 无扩展名的常见文件名
    if file_path.name in TEXT_FILENAMES:
        return True

    # 通过尝试读取来判断是否是文本文件
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            f.read(1024)
        return True
    except (OSError, UnicodeDecodeError):
        return False

def _iter_files_pruned(root: Path):
    """os.walk 剪枝版文件枚举：进入子目录前剪掉排除目录（P2-3）。

    相比 `list(root.rglob("*"))` 后过滤，node_modules 等目录根本不会被
    遍历（rglob 版仍会完整走一遍目录树）。不跟随符号链接。
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SCAN_EXCLUDED_DIRS]
        for name in filenames:
            yield Path(dirpath) / name

def _in_excluded_dir(file_path: Path) -> bool:
    """路径的任一组成部分命中排除目录名（include 分支 rglob 结果过滤用）。"""
    return any(part in SCAN_EXCLUDED_DIRS for part in file_path.parts)

def _expand_glob_braces(pattern: str) -> List[str]:
    """展开通配符中的简单花括号选择（P2-1）。

    ``*.{ts,tsx}`` → ``['*.ts', '*.tsx']``；支持多个花括号组（笛卡尔积），
    不支持嵌套花括号。无花括号时原样返回单元素列表。
    """
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]
    expanded: List[str] = []
    for alt in match.group(1).split(","):
        expanded.extend(
            _expand_glob_braces(pattern[:match.start()] + alt + pattern[match.end():])
        )
    # 去重且保持顺序
    return list(dict.fromkeys(expanded))
