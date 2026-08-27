"""P3-7 拆分模块：文件系统工具（由 builtin_tools.py 迁出，builtin_tools.py 负责 re-export 兼容）。"""

import os
import re
from pathlib import Path
from typing import List
import shutil
from ._tool_helpers import (
    SCAN_EXCLUDED_DIRS, TEXT_EXTENSIONS,
    TEXT_FILENAMES, _path_within_roots, _collect_allowed_roots,
    _check_workspace_boundary, _mutation_boundary_err, _read_boundary_err,
    _format_size, _safe_stat, _is_text_file, _iter_files_pruned,
    _in_excluded_dir, _expand_glob_braces,
)
from core.atomic_io import atomic_write_bytes as _atomic_write_bytes
from .base_tool import BaseTool
import logging
from core.sandbox.guard import sanitize_output
logger = logging.getLogger('jk_agent')
from core.sandbox import SandboxExecutor


_MAX_READ_BYTES = 8 * 1024 * 1024

_LARGE_FILE_LINE_CAP = 5000

class ReadTool(BaseTool):
    """
    读取文件内容工具

    功能：读取指定文件的内容，支持行号显示和行数范围控制。
    适用于查看代码文件、配置文件、日志文件等文本内容。
    """

    name: str = "read"
    capabilities = ("fs:read",)
    parallel_safe: bool = True   # B5：纯读（只读文件+注入的只读引用），无共享可变状态
    description: str = "【首选文件读取工具】读取指定文件的内容，支持行号显示和行数范围控制。查看代码、配置文件、日志等内容时请优先使用此工具，而不是用 bash 执行 cat/type 命令。跨平台兼容，自动处理编码。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要读取的文件路径（绝对路径或相对于当前目录的相对路径）",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（从 1 开始计数）。不传则从文件开头读取",
            },
            "limit": {
                "type": "integer",
                "description": "最多读取的行数。不传则读取到文件末尾",
            },
        },
        "required": ["file_path"],
    }

    def __init__(self):
        super().__init__()
        self._sandbox: SandboxExecutor | None = None
        self._policy = None
        self._workspace_roots: tuple = ()
        self._permission = None

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

    def set_policy(self, policy):
        """注入 PolicyEngine（读路径 allowed_roots 边界校验用）"""
        self._policy = policy

    def set_workspace_roots(self, roots):
        """显式注入允许的工作区根"""
        self._workspace_roots = tuple(roots or ())

    def set_permission(self, permission):
        """注入 PermissionChecker（读边界四档权限感知，见 _read_boundary_err）"""
        self._permission = permission

    def execute(self, file_path: str, offset: int = None, limit: int = None) -> str:
        """
        读取文件内容并返回带行号的结果

        参数:
            file_path: 要读取的文件路径
            offset: 起始行号（从 1 开始），默认从头开始
            limit: 最多读取行数，默认读取全部

        返回:
            格式化后的文件内容，每行带行号前缀
        """
        try:
            path = Path(file_path).resolve()

            # --- 工作区边界校验（P0-3；四档权限感知：注入权限后读边界
            #     交还裁决——PolicyEngine 对 fs:read 全模式 ALLOW） ---
            boundary_err = _read_boundary_err(self, path)
            if boundary_err:
                return boundary_err

            # --- 检查文件是否存在 ---
            if not path.exists():
                return f"❌ 错误：文件不存在 -> {path}"
            if not path.is_file():
                return f"❌ 错误：路径不是文件 -> {path}"

            # --- 图片文件检测 ---
            from core.protocols.vision import is_image_file
            if is_image_file(str(path)):
                size = path.stat().st_size
                return (
                    f"🖼️ 图片文件: {path} ({_format_size(size)})\n"
                    f"[IMAGE:path={path}]"
                )

            # --- 确定要显示的行范围 ---
            start = (offset - 1) if (offset is not None and offset > 0) else 0
            start = max(start, 0)
            end = start + limit if limit else None

            # --- 空文件：明确提示，不走"起始行越界"分支（P3-2） ---
            size = path.stat().st_size
            if size == 0:
                return f"📄 文件: {path}\n   (空文件)\n   共 0 字节，无内容可读取"

            # --- 读取文件（>8MB 大文件流式读取头部，避免整文件载入内存） ---
            large_file = size > _MAX_READ_BYTES

            if large_file:
                lines: List[str] = []
                scanned = 0
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if end is not None and scanned >= end:
                            break
                        if scanned >= start:
                            lines.append(line)
                            if end is None and len(lines) >= _LARGE_FILE_LINE_CAP:
                                break
                        scanned += 1
                total_lines = None  # 大文件不做全量行数统计
                end = scanned  # 大文件已消费行数即绝对结束行号（窗口不重复切片）
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                total_lines = len(lines)
                end = min(end if end is not None else total_lines, total_lines)

            if not lines or start >= end:
                if total_lines is None:
                    return f"⚠️ 未读取到内容（文件过大，流式读取窗口为空）"
                return f"⚠️ 起始行 {offset} 超出文件总行数（共 {total_lines} 行）"

            # --- 提取选中的行（大文件 lines 即窗口，不再二次切片） ---
            if large_file:
                selected_lines = lines
            else:
                selected_lines = lines[start:end]

            # --- 构造输出 ---
            if total_lines is not None:
                parts = [f"📄 文件: {path}  （共 {total_lines} 行）"]
            else:
                parts = [f"📄 文件: {path}  （大文件 {_format_size(size)}，流式读取）"]
            if offset or limit or large_file:
                parts.append(f"   显示范围: 第 {start + 1} ~ {end} 行")
            parts.append("")

            # 行号占位宽度（对齐用）
            line_num_width = len(str(end))

            for i, line in enumerate(selected_lines, start=start + 1):
                # 去掉行尾换行符，但保留空格缩进
                content = line.rstrip("\n").rstrip("\r")
                parts.append(f"  {i:>{line_num_width}} │ {content}")

            # 如果后面还有更多行，提示一下
            if total_lines is not None and end < total_lines:
                parts.append(f"\n  …… 还有 {total_lines - end} 行未显示（使用 offset={end + 1} 继续查看）")
            elif large_file and end >= _LARGE_FILE_LINE_CAP:
                parts.append(f"\n  …… 文件超过 {_format_size(_MAX_READ_BYTES)}，已截断（使用 offset={end + 1} 继续查看）")

            result = "\n".join(parts)

            # 输出脱敏（API Key / Token / 私钥 → ****）
            result = sanitize_output(result)

            return result

        except PermissionError:
            return f"❌ 权限不足：无法读取文件 -> {file_path}"
        except Exception as e:
            logger.error(f"读取文件失败: {e}", exc_info=True)
            return f"❌ 读取文件失败: {type(e).__name__}: {e}"

class WriteTool(BaseTool):
    """
    写入文件工具

    功能：创建新文件或覆盖已有文件的内容。
    注意：此操作会覆盖文件原有内容，不可恢复！
    """

    name: str = "write"
    capabilities = ("fs:write",)
    description: str = "【首选文件写入工具】创建新文件或覆盖已有文件的内容。会自动创建父目录。需要新建或完全替换文件时请使用此工具，而不是用 bash 的 echo/重定向。跨平台兼容。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要写入的文件路径（绝对路径或相对路径）",
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容",
            },
        },
        "required": ["file_path", "content"],
    }

    def __init__(self):
        super().__init__()
        self._sandbox: SandboxExecutor | None = None
        self._policy = None
        self._workspace_roots: tuple = ()
        self._permission = None

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

    def set_policy(self, policy):
        """注入 PolicyEngine（写路径 allowed_roots 边界校验用，P0 读写防护对称）"""
        self._policy = policy

    def set_workspace_roots(self, roots):
        """显式注入允许的工作区根"""
        self._workspace_roots = tuple(roots or ())

    def set_permission(self, permission):
        """注入 PermissionChecker（写边界按四档模式感知，见 _mutation_boundary_err）"""
        self._permission = permission

    def execute(self, file_path: str, content: str) -> str:
        """
        写入文件

        参数:
            file_path: 文件路径
            content: 文件内容

        返回:
            操作结果描述（包含路径、行数、字符数）
        """
        try:
            path = Path(file_path).resolve()

            # --- 工作区边界校验（P0：写防护与读防护对称；四档模式感知，
            #     ask/allow/unreviewed 下边界交还权限裁决，避免"确认后仍被拒"） ---
            boundary_err = _mutation_boundary_err(self, path)
            if boundary_err:
                return boundary_err

            # --- 沙箱检查（如果注入） ---
            if self._sandbox:
                is_safe, reason = self._sandbox.check_write_file(str(path), content)
                if not is_safe:
                    return f"⛔ 沙箱拦截: {reason}"

            # 检查是否试图写入已存在的文件（安全提示）
            file_exists = path.exists()

            # 自动创建父目录
            path.parent.mkdir(parents=True, exist_ok=True)

            # 写入文件（原子写：tmp + os.replace，避免半截文件）
            _atomic_write_bytes(path, content.encode("utf-8"))

            # 统计信息
            line_count = content.count("\n") + (1 if content else 0)
            char_count = len(content)

            action = "覆盖" if file_exists else "新建"
            return (
                f"✅ 文件{action}成功\n"
                f"   路径: {path}\n"
                f"   行数: {line_count} 行\n"
                f"   字符: {char_count} 字符"
            )

        except PermissionError:
            return f"❌ 权限不足：无法写入文件 -> {file_path}"
        except IsADirectoryError:
            return f"❌ 错误：{file_path} 是一个目录，无法写入"
        except Exception as e:
            logger.error(f"写入文件失败: {e}", exc_info=True)
            return f"❌ 写入文件失败: {type(e).__name__}: {e}"

class EditTool(BaseTool):
    """
    文件精确编辑工具

    功能：在文件中查找一段文本（old_string），替换为新文本（new_string）。
    适合对文件做局部修改，而不需要重写整个文件。

    注意：
    - old_string 必须精确匹配文件中的内容（包括缩进和空格）
    - 默认只替换第一次匹配到的内容；old_string 匹配多处时会拒绝执行，
      需扩大 old_string 保证唯一，或显式传 replace_all=true 全部替换
    """

    name: str = "edit"
    capabilities = ("fs:write",)
    description: str = "【首选文件编辑工具】对文件做精确的查找替换修改。当需要修改文件中某段代码或文本（如修bug、改配置）时使用，而不是重写整个文件或使用 bash 的 sed。精确匹配：唯一匹配时替换该处；匹配多处时拒绝执行（需扩大 old_string 或传 replace_all=true）。跨平台兼容。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要修改的文件路径",
            },
            "old_string": {
                "type": "string",
                "description": "要被替换的旧文本（必须精确匹配文件中的内容，包括缩进和空格）",
            },
            "new_string": {
                "type": "string",
                "description": "替换后的新文本",
            },
            "replace_all": {
                "type": "boolean",
                "description": "是否替换全部匹配处。默认 false：匹配多处时拒绝执行并提示；true 时替换所有匹配并在结果中注明替换次数",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def __init__(self):
        super().__init__()
        self._sandbox: SandboxExecutor | None = None
        self._policy = None
        self._workspace_roots: tuple = ()
        self._permission = None

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

    def set_policy(self, policy):
        """注入 PolicyEngine（写路径 allowed_roots 边界校验用，P0 读写防护对称）"""
        self._policy = policy

    def set_workspace_roots(self, roots):
        """显式注入允许的工作区根"""
        self._workspace_roots = tuple(roots or ())

    def set_permission(self, permission):
        """注入 PermissionChecker（写边界按四档模式感知，见 _mutation_boundary_err）"""
        self._permission = permission

    def execute(self, file_path: str, old_string: str, new_string: str,
                replace_all: bool = False) -> str:
        """
        在文件中执行精确的查找替换

        参数:
            file_path: 文件路径
            old_string: 要被替换的旧文本
            new_string: 替换后的新文本
            replace_all: 匹配多处时是否全部替换（默认 false：多处匹配拒绝执行）

        返回:
            操作结果描述
        """
        try:
            # --- 空 old_string 防护（P2-1）："".replace 语义会在每个字符间
            #     插入 new_string（"hello".replace("", "X") → 'XhXeXlXlXoX'），
            #     replace_all=true 时直接毁掉整个文件，必须显式拒绝 ---
            if not old_string:
                return (
                    "❌ 错误：old_string 不能为空\n"
                    "   💡 空字符串会在文件每个位置匹配；新建文件请用 write 工具"
                )

            path = Path(file_path).resolve()

            # --- 工作区边界校验（P0：写防护与读防护对称；四档模式感知） ---
            boundary_err = _mutation_boundary_err(self, path)
            if boundary_err:
                return boundary_err

            # --- 检查文件 ---
            if not path.exists():
                return f"❌ 错误：文件不存在 -> {path}"
            if not path.is_file():
                return f"❌ 错误：路径不是文件 -> {path}"

            # --- 读取原内容（非 UTF-8 给出结构化友好提示，P2-2） ---
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError as e:
                logger.warning(f"编辑失败（非 UTF-8 编码）: {path}: {e}")
                return (
                    f"❌ 检测到非 UTF-8 编码文件，无法编辑: {path}\n"
                    f"   💡 请先用 bash 工具转换编码后重试，例如:\n"
                    f"      iconv -f GBK -t UTF-8 '{path}' -o '{path}.utf8'"
                )

            # --- 检查 old_string 是否存在 ---
            if old_string not in content:
                # 尝试给出模糊提示
                return (
                    f"❌ 错误：未找到要替换的内容\n"
                    f"   文件: {path}\n"
                    f"   查找: '{old_string[:60]}'\n"
                    f"   💡 提示：old_string 必须与文件中内容精确匹配"
                    f"（包括缩进、空格和换行）"
                )

            # --- 统计匹配次数 ---
            occurrences = content.count(old_string)

            # --- 执行替换 ---
            # 唯一匹配：直接替换该处；匹配多处且未显式确认：拒绝执行（P2-2，
            # 修复原先静默只改第一处导致文件处于半改状态的问题）
            if occurrences > 1 and not replace_all:
                return (
                    f"⚠️ 未执行替换：old_string 在文件中匹配到 {occurrences} 处\n"
                    f"   文件: {path}\n"
                    f"   查找: '{old_string[:50]}'\n"
                    f"   💡 请扩大 old_string 上下文使其在文件中唯一后重试；\n"
                    f"      若确实要全部替换，请传参数 replace_all=true"
                )
            replace_count = occurrences if replace_all else 1
            if replace_all:
                new_content = content.replace(old_string, new_string)
            else:
                new_content = content.replace(old_string, new_string, 1)

            # --- 沙箱检查（如果注入） ---
            if self._sandbox:
                is_safe, reason = self._sandbox.check_write_file(str(path), new_content)
                if not is_safe:
                    return f"⛔ 沙箱拦截: {reason}"

            # --- 写回文件（原子写：tmp + os.replace） ---
            _atomic_write_bytes(path, new_content.encode("utf-8"))

            scope_note = (
                f"匹配次数: {occurrences} 处（replace_all=true，已全部替换）"
                if replace_count > 1
                else "匹配次数: 1 处（已替换）"
            )
            return (
                f"✅ 文件修改成功\n"
                f"   文件: {path}\n"
                f"   替换内容: '{old_string[:50]}' → '{new_string[:50]}'\n"
                f"   {scope_note}"
            )

        except PermissionError:
            return f"❌ 权限不足：无法修改文件 -> {file_path}"
        except Exception as e:
            logger.error(f"编辑文件失败: {e}", exc_info=True)
            return f"❌ 编辑文件失败: {type(e).__name__}: {e}"

class GrepTool(BaseTool):
    """
    内容搜索工具

    功能：在文件中搜索指定的文本模式（支持正则表达式）。
    相当于命令行中的 grep 命令。

    适用场景：
    - 查找某个函数在哪里定义
    - 搜索某个变量在哪里被使用
    - 查找包含特定关键词的文件
    """

    name: str = "grep"
    capabilities = ("fs:read",)
    parallel_safe: bool = True   # B5：纯读（只读扫描文件），无共享可变状态
    description: str = "【首选文本搜索工具】在文件或目录中搜索文本内容。当需要查找某个函数、变量、关键词在哪里出现时使用。支持正则表达式和文件类型过滤。跨平台兼容，比用 bash 执行 grep/findstr 命令更可靠。"

    _policy = None
    _workspace_roots: tuple = ()

    def set_policy(self, policy):
        """注入 PolicyEngine（读路径 allowed_roots 边界校验用）"""
        self._policy = policy

    def set_workspace_roots(self, roots):
        """显式注入允许的工作区根"""
        self._workspace_roots = tuple(roots or ())

    def set_permission(self, permission):
        """注入 PermissionChecker（读边界四档权限感知，见 _read_boundary_err）"""
        self._permission = permission

    parameters: dict = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "要搜索的文本模式（支持 Python 正则表达式）",
            },
            "path": {
                "type": "string",
                "description": "搜索路径（文件或目录），默认为当前目录",
            },
            "include": {
                "type": "string",
                "description": "文件类型过滤，如 '*.py' 只搜索 Python 文件，'*.{ts,tsx}' 搜索 TypeScript。支持花括号多模式展开（{a,b} 拆成 a、b 两个模式分别匹配后合并）",
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回结果数，默认 20。设为 0 视为默认值",
            },
        },
        "required": ["pattern"],
    }

    def execute(self, pattern: str, path: str = ".", include: str = None, max_results: int = 20) -> str:
        """
        在文件中搜索内容

        参数:
            pattern: 搜索模式（正则表达式）
            path: 搜索路径
            include: 文件类型过滤
            max_results: 最大结果数（0 表示不限制）

        返回:
            匹配结果列表（带文件名、行号、匹配内容）
        """
        try:
            search_path = Path(path).resolve()

            # --- 工作区边界校验（P0-3；四档权限感知：注入权限后读边界
            #     交还裁决——PolicyEngine 对 fs:read 全模式 ALLOW） ---
            boundary_err = _read_boundary_err(self, path)
            if boundary_err:
                return boundary_err

            if not search_path.exists():
                return f"❌ 错误：路径不存在 -> {path}"

            # --- 正则长度上限（防 ReDoS/超长输入） ---
            if len(pattern) > 500:
                return f"❌ 正则表达式过长（{len(pattern)} 字符 > 500），请简化后重试"

            # --- 编译正则 ---
            try:
                regex = re.compile(pattern)
            except re.error as e:
                return f"❌ 正则表达式错误: {e}"

            # max_results=0 视为默认值 20
            try:
                max_results = int(max_results)
            except (TypeError, ValueError):
                max_results = 20
            if max_results <= 0:
                max_results = 20

            # --- 收集要搜索的文件 ---
            files_to_search: List[Path] = []

            if search_path.is_file():
                # 直接搜单个文件
                files_to_search = [search_path]
            else:
                # 递归搜索目录
                if include:
                    # 花括号展开后逐一 rglob 合并去重（P2-1：rglob 原生不支持 {a,b}）；
                    # rglob 无法剪枝，但命中排除目录的文件在文本探测前就按路径
                    # 剔除，避免对依赖目录做数万次 open 探测（P2-3）
                    seen_files: set = set()
                    files_to_search = []
                    for pat in _expand_glob_braces(include):
                        for f in search_path.rglob(pat):
                            if f in seen_files or _in_excluded_dir(f):
                                continue
                            seen_files.add(f)
                            files_to_search.append(f)
                else:
                    # 无 include：os.walk 剪枝枚举（P2-3）——node_modules/
                    # .venv 等目录根本不进入，替代原 rglob("*") 全量物化
                    files_to_search = list(_iter_files_pruned(search_path))

                # 只保留文本文件，排除二进制目录
                files_to_search = [f for f in files_to_search if f.is_file() and _is_text_file(f)]

            # --- 扫描上限（文件数 + 总字节量，防止超大目录拖垮/占满内存） ---
            MAX_SCAN_FILES = 2000
            MAX_SCAN_BYTES = 32 * 1024 * 1024
            files_cap_hit = len(files_to_search) > MAX_SCAN_FILES
            if files_cap_hit:
                files_to_search = files_to_search[:MAX_SCAN_FILES]

            # --- 执行搜索 ---
            results = []
            total_matches = 0
            scanned_bytes = 0
            byte_cap_hit = False

            for file in files_to_search:
                if len(results) >= max_results:
                    break
                if byte_cap_hit:
                    break
                try:
                    scanned_bytes += file.stat().st_size
                except OSError:
                    pass
                if scanned_bytes > MAX_SCAN_BYTES:
                    byte_cap_hit = True
                    break

                try:
                    with open(file, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line in enumerate(f, 1):
                            match = regex.search(line)
                            if match:
                                total_matches += 1
                                if len(results) < max_results:
                                    # 截断过长的行
                                    content = line.rstrip("\n").rstrip("\r")
                                    if len(content) > 250:
                                        # 在匹配位置附近截断
                                        match_pos = match.start()
                                        start = max(0, match_pos - 50)
                                        end = min(len(content), match_pos + 100)
                                        if start > 0:
                                            content = "..." + content[start:end]
                                        else:
                                            content = content[:end]
                                        if end < len(line):
                                            content += "..."
                                    results.append({
                                        "file": str(file.resolve()),
                                        "line": line_no,
                                        "content": content,
                                        "match": match.group(),
                                    })
                except Exception:
                    continue  # 跳过无法读取的文件

            # --- 构造输出 ---
            if not results:
                return f"🔍 在 {search_path} 中未找到匹配 '{pattern}' 的内容"

            parts = [
                f"🔍 搜索: '{pattern}'",
                f"   路径: {search_path}",
                f"   共扫描 {len(files_to_search)} 个文件",
                f"   匹配 {total_matches} 处（显示 {len(results)} 处）",
                "",
            ]

            if files_cap_hit or byte_cap_hit:
                note = "⚠️ 扫描已达上限，结果可能不完整"
                if files_cap_hit:
                    note += f"（文件数上限 {MAX_SCAN_FILES}）"
                if byte_cap_hit:
                    note += f"（扫描字节上限 {_format_size(MAX_SCAN_BYTES)}）"
                parts.append(f"   {note}")
                parts.append("")

            for i, r in enumerate(results, 1):
                parts.append(f"  [{i}] {r['file']}:{r['line']}")
                parts.append(f"       {r['content']}")
                parts.append("")

            if total_matches > max_results:
                parts.append(f"  …… 还有 {total_matches - max_results} 处未显示")

            return sanitize_output("\n".join(parts))

        except Exception as e:
            logger.error(f"搜索失败: {e}", exc_info=True)
            return f"❌ 搜索失败: {type(e).__name__}: {e}"

class GlobTool(BaseTool):
    """
    文件查找工具

    功能：使用通配符模式查找文件（如 `**/*.py`、`src/**/*`）。
    相当于命令行中的 find 命令。

    常用模式示例：
    - **/*.py         所有 Python 文件
    - src/**/*.ts      src 目录下所有 TypeScript 文件
    - **/*.{json,yml} 所有 JSON 或 YAML 文件
    """

    name: str = "glob"
    capabilities = ("fs:read",)
    parallel_safe: bool = True   # B5：纯读（rglob + stat），无共享可变状态
    description: str = "【首选文件查找工具】使用通配符模式查找文件，如 **/*.py 查找所有 Python 文件。当需要查看目录结构、查找符合模式的文件、统计文件数量时使用。跨平台兼容，比用 bash 执行 ls/find/dir 命令更可靠。"

    _policy = None
    _workspace_roots: tuple = ()

    def set_policy(self, policy):
        """注入 PolicyEngine（读路径 allowed_roots 边界校验用）"""
        self._policy = policy

    def set_workspace_roots(self, roots):
        """显式注入允许的工作区根"""
        self._workspace_roots = tuple(roots or ())

    def set_permission(self, permission):
        """注入 PermissionChecker（读边界四档权限感知，见 _read_boundary_err）"""
        self._permission = permission

    parameters: dict = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "通配符模式。如 **/*.py 查找所有 Python 文件，**/*.json 查找所有 JSON 文件",
            },
            "path": {
                "type": "string",
                "description": "搜索的根目录，默认为当前目录",
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回结果数，默认 30。非法或非正值按默认值处理",
            },
        },
        "required": ["pattern"],
    }

    def execute(self, pattern: str, path: str = ".", max_results: int = 30) -> str:
        """
        按通配符模式查找文件

        参数:
            pattern: 通配符模式
            path: 搜索根目录
            max_results: 最大结果数（非法或非正值回退默认 30）

        返回:
            匹配的文件列表（带大小和修改时间）
        """
        try:
            search_path = Path(path).resolve()

            # --- 工作区边界校验（P0-3；四档权限感知：注入权限后读边界
            #     交还裁决——PolicyEngine 对 fs:read 全模式 ALLOW） ---
            boundary_err = _read_boundary_err(self, path)
            if boundary_err:
                return boundary_err

            if not search_path.exists():
                return f"❌ 错误：路径不存在 -> {path}"

            # --- max_results 校验（P3-8）：非法/非正值回退默认，避免负数被当不限量 ---
            try:
                max_results = int(max_results)
            except (TypeError, ValueError):
                max_results = 30
            if max_results <= 0:
                max_results = 30

            # --- 执行 glob 搜索（P3-1：物化硬上限——`**/*` 十万级目录
            #     不再无界物化，超限提示缩小 pattern） ---
            _GLOB_HARD_CAP = 50000
            matched_files: List[Path] = []
            glob_cap_hit = False
            for f in search_path.rglob(pattern):
                matched_files.append(f)
                if len(matched_files) >= _GLOB_HARD_CAP:
                    glob_cap_hit = True
                    break

            # 只保留文件类型（排除目录）
            files = [f for f in matched_files if f.is_file()]

            # 排除 .git 目录
            files = [f for f in files if ".git" not in f.parts]

            # 按修改时间排序（最新的在前；已消失的文件 mtime 按 0 处理沉底，
            # stat 失败不让整个 glob 失败，P3-8）
            files.sort(key=lambda f: _safe_stat(f)[1], reverse=True)

            if not files:
                return f"🔍 在 {search_path} 中未找到匹配 '{pattern}' 的文件"

            total_found = len(files)

            # 限制显示数量
            if max_results > 0 and len(files) > max_results:
                files = files[:max_results]

            # 构造输出
            from datetime import datetime
            parts = [
                f"🔍 查找: '{pattern}'",
                f"   路径: {search_path}",
                f"   共找到 {total_found} 个文件（显示前 {len(files)} 个）",
            ]
            if glob_cap_hit:
                parts.append("   ⚠️ 匹配数超过 50000 上限已截断，请缩小 pattern 或指定更深的子目录")
            parts.append("")

            for i, f in enumerate(files, 1):
                # 相对路径显示
                try:
                    rel_path = f.relative_to(search_path)
                except ValueError:
                    rel_path = f.name

                # stat 失败容错：文件可能在排序后、展示前被删除（P3-8）
                size, mtime_val = _safe_stat(f)
                try:
                    mtime = datetime.fromtimestamp(mtime_val).strftime("%Y-%m-%d %H:%M")
                except (OSError, OverflowError, ValueError):
                    mtime = "?"

                parts.append(f"  [{i}] {rel_path}")
                parts.append(f"       📏 {_format_size(size)}  ⏱ {mtime}")

            if total_found > max_results:
                parts.append(f"\n  …… 还有 {total_found - max_results} 个文件未显示")

            return sanitize_output("\n".join(parts))

        except Exception as e:
            logger.error(f"文件查找失败: {e}", exc_info=True)
            return f"❌ 文件查找失败: {type(e).__name__}: {e}"

class FileManagerTool(BaseTool):
    """
    文件管理工具

    安全的文件操作：复制、移动、删除、创建目录等。
    比用 bash 执行 cp/mv/rm 更安全（有路径检查和确认）。
    """

    name: str = "file_mgr"
    capabilities = ("fs:write", "fs:delete", "fs:move")
    description: str = "文件管理操作：复制(copy)、移动(move)、删除(delete)、创建目录(mkdir)、列出目录(ls)。比用 bash 执行系统命令更安全。支持批量删除（传 paths 数组）。删除操作需要 confirm=true 二次确认。"

    _policy = None
    _workspace_roots: tuple = ()
    _permission = None

    def set_policy(self, policy):
        """注入 PolicyEngine（读路径 allowed_roots 边界校验用）"""
        self._policy = policy

    def set_workspace_roots(self, roots):
        """显式注入允许的工作区根"""
        self._workspace_roots = tuple(roots or ())

    def set_permission(self, permission):
        """注入 PermissionChecker（写类 action 边界按四档模式感知）"""
        self._permission = permission

    def resolve_capabilities(self, params: dict) -> tuple:
        """按 action 决定能力：ls 是读，delete/copy/move/mkdir 是写"""
        action = (params.get("action") or "").lower()
        if action == "ls":
            return ("fs:read",)
        if action in ("delete", "copy", "cp", "move", "mv", "rename", "mkdir"):
            return ("fs:write",)
        return ("fs:write",)  # 未知 action 保守按写处理

    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作: copy（复制）、move（移动/重命名）、delete（删除文件/目录）、mkdir（创建目录）、ls（列出目录）",
            },
            "path": {
                "type": "string",
                "description": "操作的目标路径（单路径操作时使用）",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "路径列表（批量删除时使用，会依次删除所有路径；单路径操作请用 path）",
            },
            "dest": {
                "type": "string",
                "description": "目标路径（copy/move 时需要）",
            },
            "confirm": {
                "type": "boolean",
                "description": "删除操作二次确认：delete 必须传 confirm=true 才会真正执行，否则只返回确认提示",
            },
        },
        "required": ["action"],
    }

    def execute(self, action: str, path: str = None, paths: list = None, dest: str = None, confirm: bool = False) -> str:
        # 局部导入遮蔽模块级 shutil 属既有写法；加 noqa 消除 F811 重定义告警
        import shutil  # noqa: F811

        action = (action or "").lower()
        confirmed = str(confirm).lower() in ("true", "1", "yes", "y")

        # ===== 删除操作二次确认（confirm=true 才执行） =====
        if action == "delete" and not confirmed:
            return (
                "⚠️ 删除是不可逆操作，需要二次确认。\n"
                "   如确认删除，请重新调用并传入 confirm=true 参数。"
            )

        # ===== 批量操作 =====
        if paths and action == "delete":
            results = []
            for p_str in paths:
                # 用户可控路径过工作区边界校验（P0-3；写类操作四档权限感知）
                boundary_err = _mutation_boundary_err(self, p_str)
                if boundary_err:
                    results.append(f"  ❌ {boundary_err}")
                    continue
                p = Path(p_str).resolve()
                # 单项异常隔离（P2-2）：一个路径失败不让整批中断——
                # 否则前面已删、后面未删，结果不可预期
                try:
                    if not p.exists():
                        results.append(f"  ❌ 路径不存在: {p}")
                    elif p.is_file():
                        p.unlink()
                        results.append(f"  ✅ 已删除文件: {p}")
                    else:
                        shutil.rmtree(p)
                        results.append(f"  ✅ 已删除目录: {p}")
                except OSError as exc:
                    results.append(f"  ❌ 删除失败: {p} ({exc})")
            ok = sum(1 for r in results if r.startswith("  ✅"))
            fail = len(results) - ok
            summary = f"🗑️ 批量删除完成：共 {len(results)} 项，✅ {ok} 成功"
            if fail:
                summary += f"，❌ {fail} 失败"
            return summary + "\n" + "\n".join(results)

        # ===== 单路径操作 =====
        if not path:
            return "❌ 请提供 path 参数（单操作）或 paths 参数（批量删除）"

        # 用户可控路径过工作区边界校验（P0-3）：四档权限感知——写类 action
        # 走 _mutation_boundary_err（ask/allow/unreviewed 交还裁决）；
        # ls 是读，走 _read_boundary_err（注入权限后读边界完全交还裁决）
        boundary_err = (_read_boundary_err if action == "ls"
                        else _mutation_boundary_err)(self, path)
        if boundary_err:
            return boundary_err

        p = Path(path).resolve()

        if action == "mkdir":
            p.mkdir(parents=True, exist_ok=True)
            return f"✅ 目录已创建: {p}"

        elif action == "ls":
            if not p.exists():
                return f"❌ 路径不存在: {p}"
            items = list(p.iterdir()) if p.is_dir() else [p]
            if not items:
                return f"📁 {p} 为空目录"
            lines = [f"📁 {p}（共 {len(items)} 项）"]
            for item in sorted(items, key=lambda x: (not x.is_dir(), x.name)):
                suffix = "/" if item.is_dir() else ""
                # _safe_stat 容错（P2-2）：竞态删除不再崩掉整个 ls
                size = _format_size(_safe_stat(item)[0]) if item.is_file() else ""
                lines.append(f"  {'📄' if item.is_file() else '📁'} {item.name}{suffix}  {size}")
            return "\n".join(lines)

        elif action == "delete":
            if not p.exists():
                return f"❌ 路径不存在: {p}"
            if p.is_file():
                p.unlink()
                return f"🗑️ 已删除文件: {p}"
            else:
                shutil.rmtree(p)
                return f"🗑️ 已删除目录（含所有内容）: {p}"

        elif action in ("copy", "cp"):
            if not p.exists():
                return f"❌ 源路径不存在: {p}"
            if not dest:
                return "❌ copy 操作需要提供 dest 目标路径"
            # P1-1：先 resolve 再校验——相对 dest 原样传给 copy2/rename 时
            # 相对的是网关进程 cwd（安装目录）而非工作区根，且与边界检查
            # 所见路径不一致；resolve 后校验与落盘同一坐标。
            dst = Path(dest).resolve()
            dst_err = _mutation_boundary_err(self, dst)
            if dst_err:
                return dst_err
            if p.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
            else:
                shutil.copytree(p, dst, dirs_exist_ok=True)
            return f"✅ 已复制: {p} → {dst}"

        elif action in ("move", "mv", "rename"):
            if not p.exists():
                return f"❌ 源路径不存在: {p}"
            if not dest:
                return "❌ move 操作需要提供 dest 目标路径"
            # P1-1：同 copy——先 resolve 再校验（原实现校验原始字符串、
            # rename 用未 resolve 路径，两者可能指向不同位置）
            dst = Path(dest).resolve()
            dst_err = _mutation_boundary_err(self, dst)
            if dst_err:
                return dst_err
            dst.parent.mkdir(parents=True, exist_ok=True)
            p.rename(dst)
            return f"✅ 已移动: {p} → {dst}"

        else:
            return f"❌ 未知操作: {action}（可选: copy/move/delete/mkdir/ls）"
