"""P3-7 拆分模块：命令/网络/计算工具（由 builtin_tools.py 迁出，builtin_tools.py 负责 re-export 兼容）。"""

import os
import platform
import re
import json
import sys
import ast
import operator as op
import logging
import subprocess
import threading
import tempfile
import requests
from pathlib import Path
from typing import List, Any

logger = logging.getLogger('jk_agent')

# 超长 bash 输出落盘目录（temp_cleanup 同名清理；LLM 可 read 续读）
_SPILL_DIR = str(Path(tempfile.gettempdir()) / 'jk-tool-spill')
from .base_tool import BaseTool
from ._tool_helpers import (
    _path_within_roots, _collect_allowed_roots, _check_workspace_boundary,
    _read_boundary_err, _mutation_boundary_err, _format_size, _safe_stat,
)
from core.safe_http import UnsafeUrl, request as safe_http_request
from core.sandbox import SandboxExecutor

logger = logging.getLogger('jk_agent')

# 超长 bash 输出落盘目录（temp_cleanup 同名清理；LLM 可 read 续读）
_SPILL_DIR = str(Path(tempfile.gettempdir()) / 'jk-tool-spill')
from core.sandbox.guard import sanitize_output


def _subprocess_default_timeout(default: int = 1200) -> int:
    """子进程默认超时（秒）。优先 config.json 的 agent_runtime.subprocess_timeout_seconds。

    供 bash / python 等 exec 工具作为 execute(timeout=...) 的缺省值；与 ToolRuntime 的
    subprocess 宽限保持一致，避免两层上限互相打架（默认 1200s = 20 分钟）。
    """
    try:
        from core.config_loader import load_config
        v = int((load_config().get("agent_runtime") or {}).get(
            "subprocess_timeout_seconds", default) or 0)
        return max(1, v)
    except Exception:
        return default

def _clamp_timeout(timeout, default: int = 1200, maximum: int = 3600) -> int:
    """将用户传入的超时钳制到 [1, maximum] 秒；非法值回退默认。

    与 ToolRuntime 的 subprocess 宽限配合，防止超长 timeout 导致工具线程
    长时间占用工具池（P0-2）。
    """
    try:
        value = int(timeout) if timeout is not None else 0
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        value = _subprocess_default_timeout(default)
    return max(1, min(value, maximum))

def _bash_output_cap_bytes(default_kb: int = 1024) -> int:
    """沙箱关闭路径（run_killable）的输出上限字节数。

    默认 1MB，可配 config.json → agent_runtime.bash_output_cap_kb；
    <=0 表示不设上限（不推荐：会把全部输出塞进 LLM 上下文）。
    """
    try:
        from core.config_loader import load_config
        v = int((load_config().get("agent_runtime") or {}).get(
            "bash_output_cap_kb", default_kb) or default_kb)
        return max(0, v) * 1024
    except Exception:
        return max(0, default_kb) * 1024

# HttpTool POST 请求体凭据检测（复用 guard.py SECRET_PATTERNS 的核心模式）
_CREDENTIAL_LEAK_RE = re.compile(
    r'(sk-[a-zA-Z0-9]{20,})'           # API Key（OpenAI / Anthropic）
    r'|(-----BEGIN\s+(RSA |EC |DSA )?PRIVATE KEY-----)',  # 私钥块
    re.DOTALL,
)


def _contains_secrets(data: str) -> bool:
    """检查字符串中是否包含疑似凭据（API Key / 私钥）。"""
    return bool(_CREDENTIAL_LEAK_RE.search(data or ""))

class BashTool(BaseTool):
    """
    Shell 命令执行工具

    功能：在本地执行 Shell 命令并返回输出结果。
    适用于运行命令行工具、执行脚本、查看系统状态等。

    【跨平台适配】
    自动检测当前操作系统，选择合适的 shell 执行命令：
    - Windows  → PowerShell（支持单引号、管道、Unix 风格别名 ls/cat/cp/mv 等）
    - macOS    → zsh/bash（支持 ls、grep、find 等）
    - Linux    → bash/sh（同 macOS）
    - Git Bash → 可运行 Linux 命令（如果在 Windows 上安装了 Git Bash）

    ⚠️ 安全提示：此工具可以执行任意命令，请谨慎使用。
    """

    name: str = "bash"
    capabilities = ("exec:shell",)
    description: str = ("在本地执行 Shell 命令。适用于运行脚本、安装包、使用 git、启动服务等系统级操作。"
                        "注意：下发命令时请自动适配当前操作系统的系统级操作（Windows/macOS/Linux）。"
                        "避免裸 `sleep N` 长等待（N>10 会被停止看门视为卡死）：等待服务就绪等场景请用"
                        "短 sleep 轮询（如 `for i in $(seq 1 30); do check && break; sleep 2; done`）或直接读日志/端口。")
    parameters: dict = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 Shell 命令",
            },
            "timeout": {
                "type": "integer",
                "description": "命令超时时间（秒）。不传时使用 agent_runtime.subprocess_timeout_seconds（默认 1200 秒）。",
            },
        },
        "required": ["command"],
    }

    def __init__(self):
        super().__init__()
        self._sandbox: SandboxExecutor | None = None
        self._policy = None
        self._workspace_roots: tuple = ()

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

    def set_policy(self, policy):
        """注入 PolicyEngine（allowed_roots 备选 cwd 来源）"""
        self._policy = policy

    def set_workspace_roots(self, roots):
        """显式注入允许的工作区根（首个根作为非沙箱路径的子进程 cwd）"""
        self._workspace_roots = tuple(roots or ())

    def _resolve_cwd(self) -> str | None:
        """解析 bash 子进程的工作目录（P2 env/cwd 泄露修复）。

        优先级：沙箱工作区 > 显式 workspace_roots 首项 > PolicyEngine
        .allowed_roots 首项；都没有时返回 None（继承父进程目录）。
        """
        sandbox_ws = getattr(self._sandbox, "_workspace", None) if self._sandbox else None
        if sandbox_ws:
            return str(sandbox_ws)
        roots = list(self._workspace_roots or ())
        if not roots:
            policy = getattr(self, "_policy", None)
            if policy is not None:
                try:
                    roots = list(policy.allowed_roots or ())
                except Exception:
                    roots = []
        return str(roots[0]) if roots else None

    # ============ 系统检测 ============

    # 当前操作系统
    SYSTEM = platform.system().lower()  # 'windows', 'darwin'(macOS), 'linux'

    # 是否在 Windows 上运行
    IS_WINDOWS = SYSTEM == "windows"

    # Shell 提示符符号
    PROMPT_SYMBOL = ">" if IS_WINDOWS else "$"

    # Shell 名称（仅用于显示）
    SHELL_NAME = "PowerShell" if IS_WINDOWS else f"{Path(os.environ.get('SHELL', '/bin/bash')).name}"

    # ============ Linux → Windows 命令映射（用于错误提示） ============
    # PowerShell 内置了大部分 Linux 命令别名（ls/cat/cp/mv/rm/pwd/mkdir），
    # 仅少数命令需要提示替代写法。

    COMMAND_SUGGESTIONS = {
        "ls":     {"win": "ls (PowerShell 内置)",     "desc": "列出目录内容"},
        "pwd":    {"win": "pwd (PowerShell 内置)",    "desc": "显示当前目录"},
        "cat":    {"win": "cat (PowerShell 内置)",    "desc": "查看文件内容"},
        "cp":     {"win": "cp (PowerShell 内置)",     "desc": "复制文件"},
        "mv":     {"win": "mv (PowerShell 内置)",     "desc": "移动/重命名文件"},
        "rm":     {"win": "rm (PowerShell 内置)",     "desc": "删除文件"},
        "find":   {"win": "Get-ChildItem -Recurse",   "desc": "查找文件"},
        "grep":   {"win": "Select-String",            "desc": "搜索文本"},
        "touch":  {"win": "New-Item -ItemType File",  "desc": "创建空文件"},
        "mkdir":  {"win": "mkdir",                    "desc": "创建目录（相同）"},
        "clear":  {"win": "cls",                      "desc": "清屏"},
        "whoami": {"win": "whoami",                   "desc": "当前用户名（相同）"},
        "diff":   {"win": "Compare-Object",           "desc": "比较文件"},
        "sort":   {"win": "Sort-Object",              "desc": "排序"},
    }

    # ============ 危险命令黑名单（从 guard.py 获取，唯一来源） ============

    # ============ 执行命令 ============

    def execute(self, command: str, timeout: int | None = None) -> str:
        """
        执行 Shell 命令

        参数:
            command: 要执行的命令
            timeout: 超时时间（秒）；缺省用 agent_runtime.subprocess_timeout_seconds（默认 1200）

        返回:
            命令的标准输出和标准错误
        """
        # --- 安全检查（B4 去重） ---
        # 闸门链（L1 PolicyEngine + L2 guard）已对 exec:shell 命令做过
        # _match_dangerous 判定；仅当未注入沙箱（非闸门调用方，如直连
        # tool.execute 的 CLI/测试路径）时保留本地兜底，避免重复扫描。
        if self._sandbox is None:
            from core.sandbox.guard import _match_dangerous
            dangerous = _match_dangerous(command.lower())
            if dangerous:
                return (
                    f"⛔ 安全限制：命令包含危险操作，已阻止执行\n"
                    f"   命令: {command}\n"
                    f"   匹配到危险模式: {dangerous}"
                )
        # 超时参数校验：钳制到 [1, 3600] 秒，非法值回退默认（默认 1200s）
        timeout = _clamp_timeout(timeout)

        # --- 提取命令名（用于后续的智能提示） ---
        cmd_name = command.strip().split()[0].lower() if command.strip() else ""

        # --- 沙箱检查（如果注入） ---
        if self._sandbox:
            from core.shell import shell_command
            import inspect
            shell_cmd = shell_command(command)
            run_kwargs = {"tool_name": "bash"}
            # timeout 透传：executor.run 支持可选 timeout 形参时传入（默认行为不变）
            if "timeout" in inspect.signature(self._sandbox.run).parameters:
                run_kwargs["timeout"] = timeout
            result = self._sandbox.run(shell_cmd[0], shell_cmd[1:], **run_kwargs)
            if result.blocked:
                return f"⛔ 沙箱拦截: {result.block_reason}"
            if result.interrupted:
                # 用户停止：进程树已被杀，当前输出丢弃（转录配对完整由上层保证）
                return "⏹️ 用户停止，工具已中断（当前输出已丢弃）"
            if result.timeout:
                return f"⏰ 命令执行超时\n   命令: {command}"

            stdout = result.stdout or ""
            stderr = result.stderr or ""
            # 构造输出（复用下方格式化逻辑）；spill 落盘路径透传，由
            # _format_output 附“完整输出已落盘: <path>”供 LLM read 续读。
            return self._format_output(command, stdout, stderr, 0, cmd_name,
                                       full_output_path=result.full_output_path or "")

        try:
            # --- 执行命令 ---
            # Windows 用 PowerShell（单引号/管道/Unix 别名均正常），Linux/Mac 用 bash
            # env 由 run_killable 默认 sanitize_env 脱敏（不泄露网关凭据）；
            # cwd 固定到工作区根，避免落在网关安装目录（P2 泄露修复）。
            from core.shell import shell_command, run_killable
            result = run_killable(
                shell_command(command),
                timeout=timeout,
                cwd=self._resolve_cwd(),
            )
            if getattr(result, "user_interrupted", False):
                return "⏹️ 用户停止，工具已中断（当前输出已丢弃）"
            # 沙箱关闭路径：输出上限（默认 1MB 可配）+ 超限落盘 /tmp/jk-tool-spill/
            stdout, stderr, spill_path = self._cap_and_spill(
                result.stdout or "", result.stderr or "")
            return self._format_output(command, stdout, stderr, result.returncode,
                                       cmd_name, full_output_path=spill_path)

        except subprocess.TimeoutExpired:
            return f"⏰ 命令执行超时（{timeout} 秒）\n   命令: {command}"
        except subprocess.CalledProcessError as e:
            return f"❌ 命令执行失败（退出码 {e.returncode}）: {command}\n{e}"
        except FileNotFoundError as e:
            return f"❌ 命令未找到: {e}"
        except OSError as e:
            return f"❌ 系统错误: {e}"
        except Exception as e:
            logger.error(f"命令执行异常: {e}", exc_info=True)
            return f"❌ 命令执行异常: {type(e).__name__}: {e}"

    def _cap_and_spill(self, stdout: str, stderr: str) -> tuple[str, str, str]:
        """沙箱关闭路径的输出上限 + 超限落盘（默认 1MB，可配
        config.json → agent_runtime.bash_output_cap_kb）。

        返回 (截断后的 stdout, 截断后的 stderr, spill 路径或 "")：
        - 未超限：原样返回，无落盘；
        - 超限：完整输出写入 /tmp/jk-tool-spill/（LLM 可 read 续读），
          内存只留尾部（stdout 8192 / stderr 4096），落盘提示由
          _format_output 附“完整输出已落盘: <path>”。
        """
        cap = _bash_output_cap_bytes()
        if cap <= 0 or (len(stdout) + len(stderr)) <= cap:
            return stdout, stderr, ""
        try:
            os.makedirs(_SPILL_DIR, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="jk-bash-", suffix=".log", dir=_SPILL_DIR)
            with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
                if stdout:
                    fh.write(stdout)
                if stderr:
                    fh.write("\n[stderr]\n" + stderr)
        except OSError:
            return stdout, stderr, ""
        out = stdout[-8192:] if len(stdout) > 8192 else stdout
        err = stderr[-4096:] if len(stderr) > 4096 else stderr
        return out, err, path

    def _format_output(self, command: str, stdout: str, stderr: str,
                        returncode: int, cmd_name: str,
                        full_output_path: str = "") -> str:
        """统一格式化命令输出

        C6：先截断再统一 sanitize_output（幂等安全）；有落盘（spill）时
        附“完整输出已落盘: <path>”供 LLM read 续读。
        """
        os_display = {
            "windows": "Windows",
            "darwin": "macOS",
            "linux": "Linux",
        }.get(self.SYSTEM, self.SYSTEM)

        parts = [f"⚡ [{os_display} | {self.SHELL_NAME}] {self.PROMPT_SYMBOL} {command}"]
        parts.append(f"   ➡ 退出码: {returncode}")
        parts.append("")

        if stdout:
            output = sanitize_output(stdout.rstrip())
            if len(output) > 8000:
                output = output[:8000] + f"\n\n……（输出过长，已截断，共 {len(stdout)} 字符）"
            parts.append(f"📤 输出:\n{output}")

        if stderr:
            err = sanitize_output(stderr.rstrip())
            if len(err) > 3000:
                err = err[:3000] + f"\n\n……（错误输出过长，已截断，共 {len(stderr)} 字符）"
            parts.append(f"📕 错误:\n{err}")

            if self.IS_WINDOWS and cmd_name in self.COMMAND_SUGGESTIONS:
                suggestion = self.COMMAND_SUGGESTIONS[cmd_name]
                parts.append(
                    f"   💡 提示：在 Windows 上请尝试使用 '{suggestion['win']}' "
                    f"替代 '{cmd_name}'（{suggestion['desc']}）"
                )

        if not stdout and not stderr:
            parts.append("（命令执行完毕，无输出）")

        if full_output_path:
            parts.append(f"完整输出已落盘: {full_output_path}")

        return "\n".join(parts)

class CalculatorTool(BaseTool):
    """
    数学计算器工具

    用 Python 安全地执行数学运算，比 LLM 自己算更准确。
    支持 + - * / 以及 math 模块中的函数。
    使用 ast 安全解析，不会执行任意代码。
    """

    name: str = "calculate"
    parallel_safe: bool = True   # B5：纯计算（AST 求值），无共享可变状态
    description: str = "执行数学计算。当需要精确的数值计算时使用，如加减乘除、平方根、三角函数等。比 LLM 自己算更准确。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，如 2 + 3 * 4、sqrt(16)、sin(pi/2)",
            },
        },
        "required": ["expression"],
    }

    # 支持的安全运算
    _ALLOWED_OPS = {
        ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
        ast.Div: op.truediv, ast.FloorDiv: op.floordiv,
        ast.Mod: op.mod, ast.Pow: op.pow,
        ast.USub: op.neg, ast.UAdd: op.pos,
    }

    def execute(self, expression: str) -> str:
        import math
        # 额外允许的函数和常量
        _ALLOWED_FUNCS = {
            "abs": abs, "round": round, "int": int, "float": float,
            "len": len, "str": str, "min": min, "max": max, "sum": sum,
            "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
            "tan": math.tan, "log": math.log, "log10": math.log10,
            "exp": math.exp, "ceil": math.ceil, "floor": math.floor,
            "pi": math.pi, "e": math.e,
        }

        try:
            tree = ast.parse(expression.strip(), mode="eval")
            result = self._safe_eval(tree.body, _ALLOWED_FUNCS)
            text = str(result)
            # 结果长度上限（≤1e6 字符），防止字符串重复等操作 OOM
            if len(text) > 1_000_000:
                return f"❌ 计算结果过长（{len(text)} 字符，上限 100 万字符），已拒绝"
            return f"🧮 {expression} = {result}"
        except SyntaxError:
            return f"❌ 表达式语法错误: {expression}"
        except Exception as e:
            logger.error(f"计算失败: {e}", exc_info=True)
            return f"❌ 计算失败: {e}"

    def _safe_eval(self, node, funcs):
        """安全地执行 AST 节点"""
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.BinOp):
            op_type = type(node.op)
            left = self._safe_eval(node.left, funcs)
            # 幂指数上限（≤10000），防止超大整数 OOM
            if op_type is ast.Pow:
                right = self._safe_eval(node.right, funcs)
                if isinstance(right, (int, float)) and abs(right) > 10000:
                    raise ValueError(f"幂指数过大（>10000）: {right}")
                return self._ALLOWED_OPS[op_type](left, right)
            # 字符串重复结果长度上限（≤1e6），防止 str * 巨数 OOM
            if op_type is ast.Mult and isinstance(left, str):
                right = self._safe_eval(node.right, funcs)
                if isinstance(right, (int, float)) and abs(right) > 1_000_000:
                    raise ValueError(f"字符串重复次数过大: {right}")
                return left * right
            return self._ALLOWED_OPS[op_type](left, self._safe_eval(node.right, funcs))
        elif isinstance(node, ast.UnaryOp):
            return self._ALLOWED_OPS[type(node.op)](
                self._safe_eval(node.operand, funcs),
            )
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name not in funcs:
                raise ValueError(f"不允许的函数: {name}")
            args = [self._safe_eval(arg, funcs) for arg in node.args]
            return funcs[name](*args)
        elif isinstance(node, ast.Name):
            if node.id in funcs:
                return funcs[node.id]
            raise ValueError(f"不允许的名称: {node.id}")
        elif isinstance(node, ast.List):
            return [self._safe_eval(el, funcs) for el in node.elts]
        elif isinstance(node, ast.Tuple):
            return tuple(self._safe_eval(el, funcs) for el in node.elts)
        elif isinstance(node, ast.Set):
            return {self._safe_eval(el, funcs) for el in node.elts}
        elif isinstance(node, ast.Dict):
            return {
                self._safe_eval(k, funcs): self._safe_eval(v, funcs)
                for k, v in zip(node.keys, node.values)
            }
        elif isinstance(node, ast.Subscript):
            value = self._safe_eval(node.value, funcs)
            if isinstance(node.slice, ast.Constant):
                return value[node.slice.value]
            elif isinstance(node.slice, ast.Slice):
                lower = self._safe_eval(node.slice.lower, funcs) if node.slice.lower else None
                upper = self._safe_eval(node.slice.upper, funcs) if node.slice.upper else None
                step = self._safe_eval(node.slice.step, funcs) if node.slice.step else None
                return value[lower:upper:step]
            else:
                return value[self._safe_eval(node.slice, funcs)]
        else:
            raise ValueError(f"不支持的表达式类型: {type(node).__name__}")

class DateTimeTool(BaseTool):
    """
    时间日期工具

    获取当前的日期、时间、星期等信息。
    Agent 没有时间概念，这个工具告诉它"现在是何时"。
    """

    name: str = "datetime"
    parallel_safe: bool = True   # B5：纯读时钟，无共享可变状态
    description: str = "获取当前的日期、时间、星期等信息。当需要知道现在是什么时候、今天的日期、当前时间时使用。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "description": "输出格式：date（仅日期）、time（仅时间）、full（完整，默认）",
            },
        },
        "required": [],
    }

    def execute(self, format: str = "full") -> str:
        from datetime import datetime
        now = datetime.now()

        weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday = weekdays_cn[now.weekday()]

        if format == "date":
            return f"📅 {now.strftime('%Y-%m-%d')} {weekday}"
        elif format == "time":
            return f"🕐 {now.strftime('%H:%M:%S')}"
        else:
            return (
                f"📅 {now.strftime('%Y-%m-%d')} {weekday}\n"
                f"🕐 {now.strftime('%H:%M:%S')}"
            )

class NoteTool(BaseTool):
    """
    临时笔记工具 —— 当前会话内的键值暂存

    数据仅保存在内存中，agent 重启后全部丢失。
    适合存临时上下文（如"用户本次要求的输出格式"），不适合存持久信息。
    """

    name: str = "notes"
    description: str = (
        "临时键值暂存（仅当前会话有效，重启后丢失）。"
        "适合保存本次对话中的临时上下文，如用户临时指定的格式要求、中间变量等。"
        "操作：save 保存、read 读取、list 列出所有、delete 删除。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作类型：save（保存）、read（读取）、list（列出所有）、delete（删除）",
            },
            "key": {
                "type": "string",
                "description": "笔记的键名，如 'user_name'、'project_info'",
            },
            "value": {
                "type": "string",
                "description": "笔记内容（仅 save 时需要）",
            },
            "session_id": {
                "type": "string",
                "description": "会话标识（可选）。不同 session_id 的笔记相互隔离，每个会话最多 200 条",
            },
        },
        "required": ["action", "key"],
    }

    MAX_ENTRIES_PER_SESSION = 200
    # P3-2：单条笔记大小上限——笔记是常驻内存的进程内存储，
    # 不设限的单条百 MB 字符串会直接撑爆网关内存
    MAX_VALUE_CHARS = 256 * 1024

    def __init__(self):
        super().__init__()
        # 按 session_id 键控的实例级存储（session_id -> {key: value}），
        # 替代类级共享字典，避免跨会话串数据；单会话条目数受限防内存膨胀。
        self._store: dict = {}
        self._lock = threading.Lock()

    def execute(self, action: str, key: str, value: str = None, session_id: str = "") -> str:
        action = action.lower()
        session_id = session_id or ""

        with self._lock:
            if action == "save":
                if value is None:
                    return "❌ save 操作需要提供 value 参数"
                if len(value) > self.MAX_VALUE_CHARS:
                    return (
                        f"❌ 笔记内容过大（{len(value)} 字符 > 上限 {self.MAX_VALUE_CHARS}）\n"
                        f"   💡 大文本请落盘为文件（write 工具），笔记只存摘要与文件路径"
                    )
                session_store = self._store.setdefault(session_id, {})
                if len(session_store) >= self.MAX_ENTRIES_PER_SESSION and key not in session_store:
                    return (
                        f"❌ 笔记数量已达上限（每个会话 {self.MAX_ENTRIES_PER_SESSION} 条），"
                        f"请先删除部分笔记"
                    )
                session_store[key] = value
                return f"✅ 已保存笔记: {key}（{len(value)} 字）"

            session_store = self._store.get(session_id, {})

            if action == "read":
                content = session_store.get(key)
                if content is None:
                    return f"❌ 未找到笔记: {key}"
                return f"📝 [{key}]\n{content}"

            elif action == "list":
                if not session_store:
                    return "📝 暂无笔记"
                lines = [f"📝 共 {len(session_store)} 条笔记（session: {session_id or '默认'}）:"]
                for k, v in sorted(session_store.items()):
                    lines.append(f"  - {k}: {v[:50]}{'...' if len(v) > 50 else ''}")
                return "\n".join(lines)

            elif action == "delete":
                if key in session_store:
                    del session_store[key]
                    return f"🗑️ 已删除笔记: {key}"
                return f"❌ 未找到笔记: {key}"

            else:
                return f"❌ 未知操作: {action}（可选: save/read/list/delete）"

class PythonTool(BaseTool):
    """
    Python 代码执行工具

    在沙箱环境中运行 Python 代码，返回 stdout 输出。
    适合 Agent 写代码后直接验证结果。
    注意：不隔离文件系统和网络，谨慎使用。
    """

    name: str = "python"
    capabilities = ("exec:code",)
    description: str = "执行 Python 代码并返回输出结果。当需要运行代码片段来验证逻辑或计算结果时使用。代码会实际执行，请注意安全。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的 Python 代码",
            },
        },
        "required": ["code"],
    }

    def __init__(self):
        super().__init__()
        self._sandbox: SandboxExecutor | None = None

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

    def execute(self, code: str, timeout: int | None = None) -> str:
        # --- AST 级代码审查：无论沙箱是否启用都强制执行本地检查 ---
        from core.sandbox.guard import check_python_code
        is_safe, reason = check_python_code(code)
        if not is_safe:
            return (
                f"⛔ {reason}\n"
                f"   python 工具禁止导入系统模块（os/subprocess/socket 等）和调用 eval/exec/open。\n"
                f"   如需执行系统操作（文件管理、运行命令等），请改用 bash 工具。"
            )

        # 超时限制：钳制到 [1, 3600] 秒，非法值回退默认
        timeout = _clamp_timeout(timeout)

        from core.sandbox.guard import sanitize_env
        try:
            # 环境净化：剥离 API Key / Token / 密码等敏感环境变量
            env = sanitize_env(os.environ.copy())
            # run_killable：进程组管理（超时/停止杀树）+ 流式限界输出 +
            # 停止直通（stop_check 轮询，用户停止立即中断）——替代裸
            # subprocess.run（原实现超时不杀子进程树、无法响应停止）。
            from core.shell import run_killable
            # P3-3：超长代码走临时文件——`python -c code` 的 code 作为单个
            # argv 传参，超过 ~100KB 逼近 ARG_MAX/单参数上限会启动失败
            if len(code) > 100 * 1024:
                tmp_file = tempfile.NamedTemporaryFile(
                    "w", suffix=".py", delete=False, encoding="utf-8")
                try:
                    tmp_file.write(code)
                    tmp_file.close()
                    result = run_killable(
                        [sys.executable, tmp_file.name],
                        timeout=timeout,
                        stdin=subprocess.DEVNULL,
                        env=env,
                    )
                finally:
                    try:
                        os.unlink(tmp_file.name)
                    except OSError:
                        pass
            else:
                result = run_killable(
                    [sys.executable, "-c", code],
                    timeout=timeout,
                    stdin=subprocess.DEVNULL,
                    env=env,
                )
            if getattr(result, "user_interrupted", False):
                return "⏹️ 用户停止，工具已中断（当前输出已丢弃）"
            parts = [f"🐍 Python 执行（退出码 {result.returncode}）"]
            # D6：输出过 sanitize_output（先截断再脱敏，与 _format_output 同一 C6 模式）
            if result.stdout:
                parts.append(f"\n📤 输出:\n{sanitize_output(result.stdout.rstrip()[:3000])}")
            if result.stderr:
                parts.append(f"\n📕 错误:\n{sanitize_output(result.stderr.rstrip()[:2000])}")
            if not result.stdout and not result.stderr:
                parts.append("\n（执行完毕，无输出）")
            return "\n".join(parts)
        except subprocess.TimeoutExpired:
            return f"⏰ Python 执行超时（{timeout} 秒）"
        except Exception as e:
            logger.error(f"Python 执行失败: {e}", exc_info=True)
            return f"❌ Python 执行失败: {e}"

class HttpTool(BaseTool):
    """
    HTTP 请求工具

    发送 HTTP 请求到指定 URL，支持 GET 和 POST。
    适合调用外部 API、获取 JSON 数据等。
    与 web_fetch 的区别：可以自定义请求方式、头信息等。
    """

    name: str = "http"
    capabilities = ("net:egress",)
    description: str = ("发送 HTTP 请求。需要调用外部 REST API、获取 JSON 数据、或与 Web 服务交互时使用。"
                        "支持 GET / POST / PUT / DELETE / PATCH 方法。"
                        "注意：响应体上限 1MB（超长截断），本工具面向 API JSON 响应，不用于下载文件。")
    parameters: dict = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "请求的 URL",
            },
            "method": {
                "type": "string",
                "description": "请求方法: GET（默认）/ POST / PUT / DELETE / PATCH",
            },
            "data": {
                "type": "string",
                "description": "请求体 JSON 数据（POST/PUT/PATCH 时发送）",
            },
        },
        "required": ["url"],
    }

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        "Accept": "application/json, text/plain, */*",
    }
    # P1-2（修正版）：safe_http.request 默认 10MB 流式上限已保证响应体有界
    # （审查报告"完整响应体载入内存"的前提不成立）；本工具最终只保留 3000
    # 字符，这里把上限进一步收紧到 1MB，避免无意义的带宽/内存浪费。
    MAX_RESPONSE_BYTES = 1024 * 1024
    # 携带请求体的方法（P3-5：原仅 POST，放开 REST 全族）
    BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self):
        super().__init__()
        self._sandbox: SandboxExecutor | None = None

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

    def execute(self, url: str, method: str = "GET", data: str = None) -> str:
        if not url.startswith(("http://", "https://")):
            return f"❌ 无效 URL: {url}"

        # --- DLP: 完整 URL（含查询串）与请求体不得包含疑似凭据（API Key / 私钥） ---
        if _contains_secrets(url):
            return "⛔ 安全拦截: 请求 URL（含查询参数）中包含疑似 API Key 或私钥，已阻止外发"
        if method.upper() in self.BODY_METHODS and data:
            if _contains_secrets(data):
                return "⛔ 安全拦截: 请求体中包含疑似 API Key 或私钥，已阻止外发"

        # --- 沙箱检查：外发目标黑名单（如果注入） ---
        if self._sandbox:
            is_safe, reason = self._sandbox.check_egress(url)
            if not is_safe:
                return f"⛔ 沙箱拦截: {reason}"

        try:
            method = method.upper()
            kwargs = {"headers": self.HEADERS, "timeout": 15,
                      "max_response_bytes": self.MAX_RESPONSE_BYTES}

            if method in self.BODY_METHODS and data:
                kwargs["json"] = json.loads(data) if isinstance(data, str) else data

            resp = safe_http_request(method, url, **kwargs)

            resp.raise_for_status()

            # 尝试格式化 JSON 输出
            try:
                body = json.dumps(resp.json(), ensure_ascii=False, indent=2)
            except Exception:
                body = resp.text

            if len(body) > 3000:
                body = body[:3000] + f"\n……（截断，共 {len(body)} 字符）"

            truncated_note = ""
            if getattr(resp, "truncated", False):
                truncated_note = f"\n   ⚠️ 响应体超过 {self.MAX_RESPONSE_BYTES // 1024}KB 上限已截断"

            # 响应体脱敏后再返回（API Key / 私钥 → ****）
            body = sanitize_output(body)

            return (
                f"🌐 {method} {url}\n"
                f"   状态: {resp.status_code}{truncated_note}\n\n"
                f"{body}"
            )

        except requests.Timeout:
            return f"⏰ 请求超时: {url}"
        except requests.HTTPError as e:
            return f"❌ HTTP {e.response.status_code}: {url}"
        except requests.ConnectionError:
            return f"❌ 无法连接: {url}"
        except UnsafeUrl as e:
            return f"⛔ 安全拦截: {e}"
        except json.JSONDecodeError:
            return f"❌ JSON 解析失败: {url}"
        except Exception as e:
            logger.error(f"请求失败: {e}", exc_info=True)
            return f"❌ 请求失败: {type(e).__name__}: {e}"
