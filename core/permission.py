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

# 哨兵：区分"未传 extra_workspaces"与"显式空列表"（Phase 0 工作区模式）
_UNSET = object()


# ============================================================
# bash 命令分类（可从 config.json 覆盖）
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

# 危险命令检测 —— 统一从 guard.py 获取，避免重复定义
from core.sandbox.guard import (SENSITIVE_FILES, _is_system_path,
                                _match_dangerous, _check_command_for_paths)


# unreviewed 模式下仍需拦截的敏感文件（密钥与版本库元数据）。
# 核心代码文件（agent.py / core/*.py / requirements.txt 等）在 unreviewed
# 下放行——用户可在免审模式编辑项目自身；ask 模式仍走完整 SENSITIVE_FILES。
_UNREVIEWED_SENSITIVE_FILES = [".env", ".git"]


def classify_bash_command(command: str) -> str:
    """
    分析 bash 命令，返回权限级别

    先检查是否危险命令 → deny
    再检查是否只读命令 → allow
    其余写命令 → ask
    """
    cmd_lower = command.strip().lower()

    # 危险命令 —— 从 guard.py 获取（唯一来源）
    if _match_dangerous(cmd_lower):
        return DENY

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

    def __init__(self, workspace: str = None, config: dict = None,
                 extra_workspaces: object = _UNSET):
        """
        初始化权限检查器

        参数:
            workspace: 项目工作区路径（默认：config → permission.workspace → 当前目录）
            config:    permission section 的配置 dict（None 时自动从 config_loader 加载）
            extra_workspaces: 显式额外白名单根列表。
                传 None / _UNSET 时继承 config.permission.extra_workspaces（默认 ["."]）；
                传显式空列表 [] 时表示"没有任何额外根"（工作区模式，不回退全局 ["."]）。
        """
        # 从统一配置读取
        if config is None:
            try:
                from core.config_loader import load_config
                config = load_config().get("permission", {})
            except Exception:
                config = {}

        # workspace: 参数 > config > 当前目录
        # 注意：必须锚定项目根解析（不再依赖 os.chdir，见 Phase 0）
        try:
            from core.config_loader import _find_project_root
            _root = _find_project_root()
        except Exception:
            _root = Path.cwd()
        if workspace:
            self.workspace = Path(workspace).resolve()
        elif config.get("workspace"):
            ws = config["workspace"]
            self.workspace = (_root / ws).resolve() if ws != "." else _root.resolve()
        else:
            self.workspace = (_root / "workspace").resolve()

        # 额外白名单根（#8：默认含项目根，允许 LLM 修改根目录配置文件）
        # Phase 0：工作区模式必须显式替换，显式空列表不回退全局 ["."]。
        if extra_workspaces is not _UNSET and extra_workspaces is not None:
            extra = list(extra_workspaces)
        else:
            extra = config.get("extra_workspaces", ["."])
        self._extra_roots = []
        for x in extra:
            self._extra_roots.append((_root / x).resolve() if x != "." else _root.resolve())

        # 工具权限规则表
        self._rules: Dict[str, Union[str, Callable]] = {}

        # 工作区信任标志
        self._workspace_trusted = False
        # unreviewed：普通工具和路径不受白名单/常规规则限制；高危命令和
        # 敏感路径仍返回 ASK，由上层审批桥确认。
        self._permission_mode = "ask"

        # 加载默认规则（从 config 或硬编码）
        self._init_default_rules(config)

    def _within(self, p: Path) -> bool:
        """路径是否在 workspace 或任一额外白名单根内（#8 多根判断）"""
        if is_within_workspace(p, self.workspace):
            return True
        return any(is_within_workspace(p, r) for r in self._extra_roots)

    def _resolve(self, path_str: str) -> Path:
        """解析路径：workspace 相对优先，回退额外根（相对 config.json 等根文件）"""
        p = resolve_path(path_str, self.workspace)
        if not p.exists() and not Path(path_str).is_absolute():
            for r in self._extra_roots:
                cand = resolve_path(path_str, r)
                if cand.exists():
                    return cand
        return p

    def _init_default_rules(self, config: dict = None):
        """设置各工具的默认权限规则（可从 config.json 的 permission.tool_rules 覆盖）"""
        cfg = config or {}
        tool_rules = cfg.get("tool_rules", {})

        # 从 config 或硬编码默认值读取每个工具的权限
        def _rule(tool_name: str, default: Union[str, Callable]) -> Union[str, Callable]:
            if tool_name in tool_rules:
                val = tool_rules[tool_name]
                if val in ("allow", "ask", "deny"):
                    return val
            return default

        # ===== allow：安全、高频、只读 =====
        self.set_rule("grep", _rule("grep", ALLOW))
        self.set_rule("datetime", _rule("datetime", ALLOW))
        self.set_rule("calculate", _rule("calculate", ALLOW))
        self.set_rule("notes", _rule("notes", ALLOW))
        self.set_rule("memory_search", _rule("memory_search", ALLOW))
        self.set_rule("memory_update", _rule("memory_update", ALLOW))
        self.set_rule("search", _rule("search", ALLOW))
        self.set_rule("web_fetch", _rule("web_fetch", ALLOW))
        self.set_rule("create_skill", _rule("create_skill", ALLOW))

        # ===== 路径敏感的操作 =====
        self.set_rule("read", _rule("read", self._check_file_path_allow))
        self.set_rule("glob", _rule("glob", self._check_file_path_allow))
        self.set_rule("write", _rule("write", self._check_file_path_ask))
        self.set_rule("edit", _rule("edit", self._check_file_path_ask))
        self.set_rule("file_mgr", _rule("file_mgr", self._check_file_mgr))

        # ===== bash：根据命令内容动态判断 =====
        self.set_rule("bash", _rule("bash", self._check_bash_command))

        # ===== ask：有副作用的操作 =====
        self.set_rule("python", _rule("python", ASK))
        self.set_rule("http", _rule("http", ASK))

        # ---- 加载 bash 命令分类（可从 config 覆盖） ----
        bash_cfg = cfg.get("bash_commands", {})
        if "readonly" in bash_cfg:
            READONLY_COMMANDS[:] = bash_cfg["readonly"]
        if "write" in bash_cfg:
            WRITE_COMMANDS[:] = bash_cfg["write"]

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
                  - 回调函数 (tool_name, params) → "allow"|"ask"|"deny"
        """
        self._rules[tool_name] = rule

    def get_rule(self, tool_name: str) -> Optional[Union[str, Callable]]:
        """获取工具的权限规则"""
        return self._rules.get(tool_name)

    # ============================================================
    # 规则列举（WebUI 权限档位展示用）
    # ============================================================

    # 标准探针参数集：对每个工具求值，刻画当前规则的实际效果
    _PROBE_PARAMS = [
        ("empty_args", {}),
        ("workspace_path", {"file_path": "probe.txt", "path": "probe.txt"}),
        ("bash_readonly", {"command": "ls"}),
        ("bash_write", {"command": "git commit -m probe"}),
    ]

    def describe_rules(self) -> dict:
        """列举每个工具的规则类型与标准探针求值结果。

        返回:
            {"tools": {tool: {"rule_type": fixed|dynamic,
                              "rule": 固定值或 "callable",
                              "probes": {探针名: allow|ask|deny}}},
             "meta": {"workspace": ..., "workspace_trusted": ...}}

        探针只求值 check()，无副作用（dynamic 规则应为纯函数）。
        """
        tools = {}
        for tool, rule in self._rules.items():
            if callable(rule):
                info = {"rule_type": "dynamic", "rule": "callable"}
            else:
                info = {"rule_type": "fixed", "rule": rule}
            probes = {}
            for name, params in self._PROBE_PARAMS:
                try:
                    probes[name] = self.check(tool, dict(params))
                except Exception:
                    probes[name] = "error"
            info["probes"] = probes
            tools[tool] = info
        return {
            "tools": tools,
            "meta": {
                "workspace": str(self.workspace),
                "workspace_trusted": self._workspace_trusted,
            },
        }


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

        path = self._resolve(path_str)
        if self._within(path):
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
        # 已信任工作区（plan/A 键）时，白名单根内写操作直接放行（#8）
        if self._workspace_trusted and self._within(self._resolve(path_str)):
            return ALLOW
        return ASK  # 未信任时写操作都需确认

    def _check_file_path_delete(self, tool_name: str, params: dict) -> str:  # noqa: ARG001
        """
        删除类工具的路径检查规则
        工作区内 → ask，工作区外 → deny
        """
        path_str = self._extract_path(params)
        if not path_str:
            return ASK

        path = self._resolve(path_str)
        if self._within(path):
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
            path = self._resolve(path_str)
            return ALLOW if self._within(path) else ASK

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
                path = self._resolve(cmd_path)
                if path.exists() and not self._within(path):
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

    def set_permission_mode(self, mode: str) -> None:
        """设置会话权限档位；仅接受已知档位，非法值回退为 ask。"""
        self._permission_mode = mode if mode in ("readonly", "ask", "allow", "unreviewed") else "ask"

    def is_unreviewed_mode(self) -> bool:
        """当前是否为“普通操作免审，敏感/高危例外审批”模式。"""
        return self._permission_mode == "unreviewed"

    def _is_sensitive_path(self, path: Path) -> bool:
        """判断路径是否为配置的敏感文件，或 .git 这类敏感目录。"""
        norm = os.path.normpath(str(path)).lower()
        for sensitive in SENSITIVE_FILES:
            item = os.path.normpath(str(sensitive)).lower()
            if norm == item or norm.endswith(os.sep + item):
                return True
            # .git/config 的整个 .git 目录都应视为敏感；其他带目录的
            # 关键文件仍仅匹配自身，避免把整个 core/ 误判为敏感目录。
            if item == os.path.normpath(".git/config"):
                # Treat the entire .git directory as sensitive, including nested
                # paths such as /project/.git/HEAD.
                if ".git" in {part.lower() for part in path.parts}:
                    return True
        return False

    @staticmethod
    def _classify_unreviewed_path(path: Path) -> str:
        """unreviewed 模式下对路径分类，返回 "allow"（放行）或 "sensitive"（需审批）。

        规则（顺序敏感）：
        - .git/config 精确路径：放行（用户此前要求免审改 git 配置）
        - .env（任意位置）与 .git 目录其余部分：敏感，需审批
        - 其他：放行
        """
        # .git/config 精确路径 → 放行
        try:
            if path.name.lower() == "config" and path.parent.name.lower() == ".git":
                return "allow"
        except Exception:
            pass
        # .env 密钥文件（任意位置）
        norm = os.path.normpath(str(path)).lower()
        if norm == ".env" or norm.endswith(os.sep + ".env"):
            return "sensitive"
        # .git 目录其余部分（含 .git/HEAD、.git/objects 等）
        if ".git" in {part.lower() for part in path.parts}:
            return "sensitive"
        return "allow"

    def _unreviewed_exception(self, tool_name: str, params: dict) -> str:
        """返回 unreviewed 下仍需人工审批的风险说明，空字符串表示普通操作。"""
        if tool_name == "bash":
            command = str(params.get("command") or "")
            dangerous = _match_dangerous(command.lower())
            if dangerous:
                return f"高危命令需审批: {dangerous}"
            # unreviewed 下仅拦：系统路径 + 密钥文件 + .git；核心代码文件放行
            blocked, reason = _check_command_for_paths(
                command, sensitive_files=_UNREVIEWED_SENSITIVE_FILES)
            if blocked:
                return f"敏感路径操作需审批: {reason}"
        for key in ("file_path", "path", "dest"):
            value = params.get(key)
            if value:
                resolved = self._resolve(str(value))
                if _is_system_path(resolved):
                    return f"系统路径操作需审批: {value}"
                if self._classify_unreviewed_path(resolved) == "sensitive":
                    return f"敏感文件操作需审批: {value}"
        values = params.get("paths")
        if isinstance(values, list):
            for value in values:
                if value:
                    resolved = self._resolve(str(value))
                    if _is_system_path(resolved):
                        return f"系统路径操作需审批: {value}"
                    if self._classify_unreviewed_path(resolved) == "sensitive":
                        return f"敏感文件操作需审批: {value}"
        return ""

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

    def _is_operation_external(self, params: dict) -> bool:
        """
        检查操作是否涉及工作区外的路径。
        检查 params 中所有可能的路径参数（file_path/path/dest/paths），
        只要有一个在工作区外，就返回 True。
        """
        for key in ("file_path", "path", "dest"):
            val = params.get(key)
            if val:
                p = self._resolve(val)
                if not self._within(p):
                    return True
        paths_list = params.get("paths", [])
        if isinstance(paths_list, list):
            for p_str in paths_list:
                p = self._resolve(p_str)
                if not self._within(p):
                    return True
        return False

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

        # unreviewed 不做普通工具规则、工作区边界或额外根检查；仅将
        # 高危 shell 命令和敏感文件/目录操作转交审批桥。
        if self.is_unreviewed_mode():
            exception = self._unreviewed_exception(tool_name, params)
            return (ASK if exception else ALLOW)

        rule = self._rules.get(tool_name)

        # 未设置规则 → 默认 ask（安全优先）
        if rule is None:
            return ALLOW if self._workspace_trusted else ASK

        # 固定权限
        if isinstance(rule, str):
            if self._workspace_trusted and rule == ASK:
                return ALLOW  # 无路径的固定 ask（如 python/http）升为 allow
            return rule

        # 动态规则（回调函数）
        if callable(rule):
            level = rule(tool_name, params)
            # 工作区信任：ask 升级时区分路径是否在工作区内
            if self._workspace_trusted and level == ASK:
                if self._is_operation_external(params):
                    return ASK  # 工作区外操作仍需确认
                return ALLOW  # 工作区内操作放行
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
