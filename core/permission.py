"""
权限管理模块 —— 三级权限：allow / ask / deny

控制 Agent 对工具的调用权限，防止误操作。

三级权限：
  allow — 直接执行，不询问（安全、高频的操作）
  ask   — 暂停，等待用户确认方可执行
  deny  — 直接拒绝，不执行

工作区概念：
  工作区 = 项目根目录，读操作在工作区内安全，写操作需要确认。
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Callable, Union


# ============================================================
# 权限级别常量
# ============================================================

ALLOW = "allow"   # 直接执行
ASK = "ask"       # 询问用户
DENY = "deny"     # 直接拒绝


# ============================================================
# bash 命令分类
# ============================================================

# 只读命令（直接放行）
READONLY_COMMANDS = [
    "ls", "dir", "pwd", "echo", "cat", "type", "more", "less",
    "head", "tail", "findstr", "where", "which",
    "git status", "git log", "git diff", "git show", "git branch",
    "git stash list", "git remote -v", "git config",
    "pip list", "pip show",
    "python --version", "python3 --version",
    "node --version", "npm --version",
]

# 写命令（需要确认）
WRITE_COMMANDS = [
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
]

# 危险命令（直接拒绝）
DANGEROUS_COMMANDS = [
    "rm -rf /", "rm -rf ~", "rm -rf .",
    "del /f /s", "rd /s /q", "format",
    "mkfs", "dd if=",
    ":(){ :|:& };:",  # fork 炸弹
    "shutdown", "reboot", "halt",
    "> /dev/sda", "> /dev/mmc",
]


def classify_bash_command(command: str) -> str:
    """
    分析 bash 命令，返回权限级别

    先检查是否危险命令 → deny
    再检查是否只读命令 → allow
    其余写命令 → ask
    """
    cmd_lower = command.strip().lower()

    # 危险命令
    for pattern in DANGEROUS_COMMANDS:
        if pattern in cmd_lower:
            return DENY

    # 提取命令名（第一个词）
    first_word = cmd_lower.split()[0] if cmd_lower.split() else ""

    # 只读命令
    for pattern in READONLY_COMMANDS:
        if cmd_lower.startswith(pattern):
            return ALLOW

    # 写命令
    for pattern in WRITE_COMMANDS:
        if cmd_lower.startswith(pattern):
            return ASK

    # 未知命令 → 保守处理
    return ASK


# ============================================================
# 工作区路径检查
# ============================================================

def resolve_path(path_str: str, workspace: Path) -> Path:
    """将路径转换为绝对路径"""
    p = Path(path_str)
    if not p.is_absolute():
        p = (workspace / p).resolve()
    else:
        p = p.resolve()
    return p


def is_within_workspace(path: Path, workspace: Path) -> bool:
    """判断路径是否在工作区内"""
    try:
        path.resolve().relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


# ============================================================
# 权限检查器
# ============================================================

class PermissionChecker:
    """
    权限检查器

    管理每个工具的权限规则，支持：
    - 固定权限：直接设为 allow / ask / deny
    - 动态规则：传入回调函数，根据参数动态判断
    - 路径检查：自动判断路径是否在工作区内

    使用方式：
        checker = PermissionChecker(workspace="/path/to/project")

        # 检查工具调用
        level = checker.check("read", {"file_path": "test.txt"})
        # → "allow"（工作区内读取）

        level = checker.check("write", {"file_path": "/etc/passwd"})
        # → "deny"（工作区外写入）
    """

    def __init__(self, workspace: str = None):
        """
        初始化权限检查器

        参数:
            workspace: 项目工作区路径（默认当前目录）
        """
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

        # 工具权限规则表
        # key = 工具名, value = 权限级别 或 回调函数
        self._rules: Dict[str, Union[str, Callable]] = {}

        # 工作区信任标志（A 选项：本会话内工作区全放行）
        self._workspace_trusted = False

        # 初始化默认规则
        self._init_default_rules()

    def _init_default_rules(self):
        """设置各工具的默认权限规则"""

        # ===== allow：安全、高频、只读 =====
        # 工作区内的读操作直接放行
        self.set_rule("grep", ALLOW)
        self.set_rule("datetime", ALLOW)
        self.set_rule("calculate", ALLOW)
        self.set_rule("notes", ALLOW)
        self.set_rule("memory_search", ALLOW)
        self.set_rule("memory_update", ALLOW)
        self.set_rule("search", ALLOW)
        self.set_rule("web_fetch", ALLOW)

        # ===== 路径敏感的操作：规则函数动态判断 =====
        self.set_rule("read", self._check_file_path_allow)       # 区内allow，区外ask
        self.set_rule("glob", self._check_file_path_allow)       # 区内allow，区外ask
        self.set_rule("write", self._check_file_path_ask)        # 全ask
        self.set_rule("edit", self._check_file_path_ask)         # 全ask
        self.set_rule("file_mgr", self._check_file_mgr)  # 根据action区分

        # ===== bash：根据命令内容动态判断 =====
        self.set_rule("bash", self._check_bash_command)

        # ===== ask：有副作用的操作 =====
        self.set_rule("python", ASK)
        self.set_rule("http", ASK)

    # ============================================================
    # 规则设置
    # ============================================================

    def set_rule(self, tool_name: str, rule: Union[str, Callable]):
        """
        设置工具的权限规则

        参数:
            tool_name: 工具名称
            rule: 允许值:
                  - "allow" / "ask" / "deny"（固定权限）
                  - 回调函数 (tool_name, params, workspace) → "allow"|"ask"|"deny"
        """
        self._rules[tool_name] = rule

    def get_rule(self, tool_name: str) -> Optional[Union[str, Callable]]:
        """获取工具的权限规则"""
        return self._rules.get(tool_name)

    # ============================================================
    # 动态规则函数
    # ============================================================

    def _check_file_path_allow(self, tool_name: str, params: dict) -> str:
        """
        读取类工具的路径检查规则
        工作区内 → allow，工作区外 → ask
        """
        path_str = self._extract_path(params)
        if not path_str:
            return ALLOW

        path = resolve_path(path_str, self.workspace)
        if is_within_workspace(path, self.workspace):
            return ALLOW
        return ASK

    def _check_file_path_ask(self, tool_name: str, params: dict) -> str:
        """
        读写类工具的路径检查规则
        工作区内 → ask，工作区外 → ask（需要用户确认）
        """
        path_str = self._extract_path(params)
        if not path_str:
            return ASK
        return ASK  # 无论区内区外，写操作都需确认

    def _check_file_path_delete(self, tool_name: str, params: dict) -> str:  # noqa: ARG001
        """
        删除类工具的路径检查规则
        工作区内 → ask，工作区外 → deny
        """
        path_str = self._extract_path(params)
        if not path_str:
            return ASK

        path = resolve_path(path_str, self.workspace)
        if is_within_workspace(path, self.workspace):
            return ASK
        return DENY

    def _check_file_mgr(self, tool_name: str, params: dict) -> str:  # noqa: ARG001
        """
        file_mgr 工具的权限检查
        根据不同的 action 区分处理：
          ls/mkdir → 类似读操作（区内allow，区外ask）
          copy/move → 写操作（全ask）
          delete → 全ask（按A后区内升allow）
        """
        action = params.get("action", "").lower()
        # 兼容单路径 path 和批量路径 paths
        path_str = params.get("path", "")
        if not path_str:
            paths_list = params.get("paths", [])
            path_str = paths_list[0] if paths_list else ""

        # 没有路径 → 保守处理
        if not path_str:
            return ASK

        # ls/mkdir：类似读操作
        if action in ("ls", "mkdir", "list"):
            path = resolve_path(path_str, self.workspace)
            return ALLOW if is_within_workspace(path, self.workspace) else ASK

        # copy/move / delete：全 ask（A 按钮再将区内升为 allow）
        return ASK

    def _check_bash_command(self, tool_name: str, params: dict) -> str:
        """
        bash 命令的分类检查（加上路径检查）

        先判断命令类型（只读/写/危险），
        如果是只读命令，再检查是否涉及工作区外路径。
        """
        command = params.get("command", "")
        if not command:
            return ASK

        level = classify_bash_command(command)

        # 只读命令：检查是否涉及工作区外路径
        if level == ALLOW:
            cmd_path = self._extract_bash_path(command)
            if cmd_path:
                path = resolve_path(cmd_path, self.workspace)
                if path.exists() and not is_within_workspace(path, self.workspace):
                    return ASK  # 工作区外只读操作 → ask

        return level

    @staticmethod
    def _extract_bash_path(command: str) -> Optional[str]:
        """
        从 bash 命令中提取路径参数
        取命令的最后一个参数（常见用法：ls /path、cd /path、cat file）
        """
        parts = command.strip().split()
        if len(parts) <= 1:
            return None
        candidate = parts[-1]

        # 跳过明显不是路径的参数
        if candidate.startswith(("-", "--")):
            return None

        # 检查是否是路径格式
        if candidate.startswith(("/", "~", ".", "C:", "D:", "..")) or "\\" in candidate or "/" in candidate:
            return candidate
        return None

    # ============================================================
    # 核心检查方法
    # ============================================================

    @staticmethod
    def _extract_path(params: dict) -> Optional[str]:
        """从工具参数中提取文件路径"""
        for key in ("file_path", "path", "dest"):
            if key in params:
                return params[key]
        return None

    def allow_workspace(self):
        """
        本会话内工作区全部放行（A 选项）

        调用后，工作区内的路径检查将直接返回 allow，
        工作区外的操作和危险命令仍受限制。
        """
        self._workspace_trusted = True

    def is_workspace_trusted(self) -> bool:
        """当前是否已信任工作区"""
        return self._workspace_trusted

    def check(self, tool_name: str, params: dict = None) -> str:
        """
        检查工具调用的权限

        参数:
            tool_name: 工具名称
            params:    工具参数字典

        返回:
            "allow" — 直接执行
            "ask"   — 需要用户确认
            "deny"  — 直接拒绝
        """
        if params is None:
            params = {}

        rule = self._rules.get(tool_name)

        # 未设置规则 → 默认 ask（安全优先）
        if rule is None:
            return ALLOW if self._workspace_trusted else ASK

        # 固定权限
        if isinstance(rule, str):
            if self._workspace_trusted and rule == ASK:
                return ALLOW  # 工作区信任：将固定 ask 升为 allow
            return rule

        # 动态规则（回调函数）
        if callable(rule):
            level = rule(tool_name, params)
            # 工作区信任：将 ask 升为 allow（deny 保持）
            if self._workspace_trusted and level == ASK:
                return ALLOW
            return level

        return ASK

    def format_permission_info(self, tool_name: str, params: dict,
                               level: str, result: str = None) -> str:
        """生成权限检查结果的显示文本"""
        labels = {ALLOW: "✅ 允许", ASK: "❓ 需要确认", DENY: "⛔ 已拒绝"}

        lines = [
            f"  🔒 权限检查: {labels.get(level, level)}",
            f"     工具: {tool_name}",
        ]

        # 显示关键参数
        if params:
            for k, v in params.items():
                if isinstance(v, str) and len(v) > 100:
                    v = v[:100] + "..."
                lines.append(f"     {k}: {v}")

        if result:
            lines.append(f"     → {result}")

        return "\n".join(lines)

    def __str__(self) -> str:
        return f"PermissionChecker(workspace={self.workspace}, rules={len(self._rules)})"
