# -*- coding: utf-8 -*-
"""
内置工具实现 —— Agent CLI 基础工具集

包含 Agent 操作本地文件系统所需的 6 个核心工具：

  工具名称     功能                      适用场景
  ────────    ──────                    ────────
  read        读取文件内容               查看代码、配置、日志
  write       创建或覆盖文件             新建文件、修改配置
  edit        精确查找替换修改文件        局部修改代码、修复bug
  grep        在文件中搜索文本           查找函数定义、搜索关键词
  glob        按通配符查找文件           找某类文件、浏览项目结构
  bash        执行 Shell 命令           运行程序、查看目录、git操作

【跨平台说明】
  read / write / edit / grep / glob → ✅ 完全跨平台
      底层使用纯 Python 实现（open()、re、pathlib 等），
      不依赖任何系统命令，Windows / macOS / Linux 均可运行。

  bash → ⚠️ 自动适配当前系统
      Linux/macOS → 使用 bash/sh，提示符 $
      Windows     → 使用 cmd.exe，提示符 >
      安装了 Git Bash / WSL 时也可运行 Linux 命令。
      工具会自动检测系统，智能提示命令替代方案。

每个工具都继承 BaseTool，遵循统一的接口规范：
  - name:       工具名称
  - description: 工具描述（给 LLM 看）
  - parameters:  参数定义（JSON Schema）
  - execute():   执行逻辑
"""

import os
import re
import json
import sys
import platform
import shutil
import subprocess
import logging
import requests
from pathlib import Path
from typing import List
from .base_tool import BaseTool
from .memory_tools import MemorySearchTool, MemoryUpdateTool
from core.sandbox.guard import sanitize_output

# 沙箱导入（可选，无 sandbox 时自动降级）
try:
    from core.sandbox import SandboxExecutor, SandboxResult
except ImportError:
    SandboxExecutor = None  # type: ignore
    SandboxResult = None  # type: ignore

logger = logging.getLogger('hello_agent')


# ============================================================
# 辅助函数
# ============================================================

def _format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def _is_text_file(file_path: Path) -> bool:
    """判断一个文件是否是文本文件（跳过二进制文件）"""

    # 跳过 .git 目录
    if ".git" in file_path.parts:
        return False

    # 常见的文本文件扩展名
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

    # 检查扩展名
    if file_path.suffix.lower() in TEXT_EXTENSIONS:
        return True

    # 无扩展名的常见文件名
    if file_path.name in {
        "Dockerfile", "Makefile", "Makefile", "CHANGELOG", "LICENSE",
        "README", "CONTRIBUTING", "Gemfile", "Rakefile", "Procfile",
    }:
        return True

    # 通过尝试读取来判断是否是文本文件
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            f.read(1024)
        return True
    except (UnicodeDecodeError, Exception):
        return False


# ============================================================
# 1. ReadTool —— 读取文件内容
# ============================================================

class ReadTool(BaseTool):
    """
    读取文件内容工具

    功能：读取指定文件的内容，支持行号显示和行数范围控制。
    适用于查看代码文件、配置文件、日志文件等文本内容。
    """

    name: str = "read"
    capabilities = ("fs:read",)
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

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

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

            # --- 检查文件是否存在 ---
            if not path.exists():
                return f"❌ 错误：文件不存在 -> {path}"
            if not path.is_file():
                return f"❌ 错误：路径不是文件 -> {path}"

            # --- 读取文件 ---
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)

            # --- 确定要显示的行范围 ---
            start = (offset - 1) if (offset is not None and offset > 0) else 0
            end = start + limit if limit else total_lines
            # 防止越界
            end = min(end, total_lines)
            start = max(start, 0)

            if start >= total_lines or start >= end:
                return f"⚠️ 起始行 {offset} 超出文件总行数（共 {total_lines} 行）"

            # --- 提取选中的行 ---
            selected_lines = lines[start:end]

            # --- 构造输出 ---
            parts = [f"📄 文件: {path}  （共 {total_lines} 行）"]
            if offset or limit:
                parts.append(f"   显示范围: 第 {start + 1} ~ {end} 行")
            parts.append("")

            # 行号占位宽度（对齐用）
            line_num_width = len(str(end))

            for i, line in enumerate(selected_lines, start=start + 1):
                # 去掉行尾换行符，但保留空格缩进
                content = line.rstrip("\n").rstrip("\r")
                parts.append(f"  {i:>{line_num_width}} │ {content}")

            # 如果后面还有更多行，提示一下
            if end < total_lines:
                parts.append(f"\n  …… 还有 {total_lines - end} 行未显示（使用 offset={end + 1} 继续查看）")

            result = "\n".join(parts)

            # 输出脱敏（API Key / Token / 私钥 → ****）
            result = sanitize_output(result)

            return result

        except PermissionError:
            return f"❌ 权限不足：无法读取文件 -> {file_path}"
        except Exception as e:
            logger.error(f"读取文件失败: {e}", exc_info=True)
            return f"❌ 读取文件失败: {type(e).__name__}: {e}"


# ============================================================
# 2. WriteTool —— 创建或覆盖文件
# ============================================================

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

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

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

            # --- 沙箱检查（如果注入） ---
            if self._sandbox:
                is_safe, reason = self._sandbox.check_write_file(str(path), content)
                if not is_safe:
                    return f"⛔ 沙箱拦截: {reason}"

            # 检查是否试图写入已存在的文件（安全提示）
            file_exists = path.exists()

            # 自动创建父目录
            path.parent.mkdir(parents=True, exist_ok=True)

            # 写入文件
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

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


# ============================================================
# 3. EditTool —— 精确查找替换修改文件
# ============================================================

class EditTool(BaseTool):
    """
    文件精确编辑工具

    功能：在文件中查找一段文本（old_string），替换为新文本（new_string）。
    适合对文件做局部修改，而不需要重写整个文件。

    注意：
    - old_string 必须精确匹配文件中的内容（包括缩进和空格）
    - 默认只替换第一次匹配到的内容（更安全）
    """

    name: str = "edit"
    capabilities = ("fs:write",)
    description: str = "【首选文件编辑工具】对文件做精确的查找替换修改。当需要修改文件中某段代码或文本（如修bug、改配置）时使用，而不是重写整个文件或使用 bash 的 sed。精确匹配，默认只替换第一次。跨平台兼容。"
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
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def __init__(self):
        super().__init__()
        self._sandbox: SandboxExecutor | None = None

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

    def execute(self, file_path: str, old_string: str, new_string: str) -> str:
        """
        在文件中执行精确的查找替换

        参数:
            file_path: 文件路径
            old_string: 要被替换的旧文本
            new_string: 替换后的新文本

        返回:
            操作结果描述
        """
        try:
            path = Path(file_path).resolve()

            # --- 检查文件 ---
            if not path.exists():
                return f"❌ 错误：文件不存在 -> {path}"
            if not path.is_file():
                return f"❌ 错误：路径不是文件 -> {path}"

            # --- 读取原内容 ---
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

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

            # --- 执行替换（只替换第一次，更安全） ---
            new_content = content.replace(old_string, new_string, 1)

            # --- 沙箱检查（如果注入） ---
            if self._sandbox:
                is_safe, reason = self._sandbox.check_write_file(str(path), new_content)
                if not is_safe:
                    return f"⛔ 沙箱拦截: {reason}"

            # --- 写回文件 ---
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)

            return (
                f"✅ 文件修改成功\n"
                f"   文件: {path}\n"
                f"   替换内容: '{old_string[:50]}' → '{new_string[:50]}'\n"
                f"   匹配次数: {occurrences} 处（已替换第 1 处）"
            )

        except PermissionError:
            return f"❌ 权限不足：无法修改文件 -> {file_path}"
        except Exception as e:
            logger.error(f"编辑文件失败: {e}", exc_info=True)
            return f"❌ 编辑文件失败: {type(e).__name__}: {e}"


# ============================================================
# 4. GrepTool —— 在文件中搜索内容
# ============================================================

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
    description: str = "【首选文本搜索工具】在文件或目录中搜索文本内容。当需要查找某个函数、变量、关键词在哪里出现时使用。支持正则表达式和文件类型过滤。跨平台兼容，比用 bash 执行 grep/findstr 命令更可靠。"
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
                "description": "文件类型过滤，如 '*.py' 只搜索 Python 文件，'*.{ts,tsx}' 搜索 TypeScript",
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回结果数，默认 20。设为 0 则不限制",
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
            if not search_path.exists():
                return f"❌ 错误：路径不存在 -> {path}"

            # --- 编译正则 ---
            try:
                regex = re.compile(pattern)
            except re.error as e:
                return f"❌ 正则表达式错误: {e}"

            # --- 收集要搜索的文件 ---
            files_to_search: List[Path] = []

            if search_path.is_file():
                # 直接搜单个文件
                files_to_search = [search_path]
            else:
                # 递归搜索目录
                if include:
                    # 使用 glob 过滤文件类型
                    files_to_search = list(search_path.rglob(include))
                else:
                    files_to_search = list(search_path.rglob("*"))

                # 只保留文本文件，排除二进制和 .git 目录
                files_to_search = [f for f in files_to_search if f.is_file() and _is_text_file(f)]

            # --- 执行搜索 ---
            results = []
            total_matches = 0

            for file in files_to_search:
                if max_results > 0 and len(results) >= max_results:
                    break

                try:
                    with open(file, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line in enumerate(f, 1):
                            match = regex.search(line)
                            if match:
                                total_matches += 1
                                if max_results == 0 or len(results) < max_results:
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

            for i, r in enumerate(results, 1):
                parts.append(f"  [{i}] {r['file']}:{r['line']}")
                parts.append(f"       {r['content']}")
                parts.append("")

            if max_results > 0 and total_matches > max_results:
                parts.append(f"  …… 还有 {total_matches - max_results} 处未显示")

            return sanitize_output("\n".join(parts))

        except Exception as e:
            logger.error(f"搜索失败: {e}", exc_info=True)
            return f"❌ 搜索失败: {type(e).__name__}: {e}"


# ============================================================
# 5. GlobTool —— 按通配符查找文件
# ============================================================

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
    description: str = "【首选文件查找工具】使用通配符模式查找文件，如 **/*.py 查找所有 Python 文件。当需要查看目录结构、查找符合模式的文件、统计文件数量时使用。跨平台兼容，比用 bash 执行 ls/find/dir 命令更可靠。"
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
                "description": "最大返回结果数，默认 30。设为 0 则不限制",
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
            max_results: 最大结果数（0 表示不限制）

        返回:
            匹配的文件列表（带大小和修改时间）
        """
        try:
            search_path = Path(path).resolve()
            if not search_path.exists():
                return f"❌ 错误：路径不存在 -> {path}"

            # --- 执行 glob 搜索 ---
            matched_files = list(search_path.rglob(pattern))

            # 只保留文件类型（排除目录）
            files = [f for f in matched_files if f.is_file()]

            # 排除 .git 目录
            files = [f for f in files if ".git" not in f.parts]

            # 按修改时间排序（最新的在前）
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

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
                "",
            ]

            for i, f in enumerate(files, 1):
                # 相对路径显示
                try:
                    rel_path = f.relative_to(search_path)
                except ValueError:
                    rel_path = f.name

                size = f.stat().st_size
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

                parts.append(f"  [{i}] {rel_path}")
                parts.append(f"       📏 {_format_size(size)}  ⏱ {mtime}")

            if max_results > 0 and total_found > max_results:
                parts.append(f"\n  …… 还有 {total_found - max_results} 个文件未显示")

            return sanitize_output("\n".join(parts))

        except Exception as e:
            logger.error(f"文件查找失败: {e}", exc_info=True)
            return f"❌ 文件查找失败: {type(e).__name__}: {e}"


# ============================================================
# 6. BashTool —— 执行 Shell 命令
# ============================================================

class BashTool(BaseTool):
    """
    Shell 命令执行工具

    功能：在本地执行 Shell 命令并返回输出结果。
    适用于运行命令行工具、执行脚本、查看系统状态等。

    【跨平台适配】
    自动检测当前操作系统，选择合适的 shell 执行命令：
    - Windows  → cmd.exe（支持 dir、type、findstr 等原生命令）
    - macOS    → zsh/bash（支持 ls、grep、find 等）
    - Linux    → bash/sh（同 macOS）
    - Git Bash → 可运行 Linux 命令（如果在 Windows 上安装了 Git Bash）

    ⚠️ 安全提示：此工具可以执行任意命令，请谨慎使用。
    """

    name: str = "bash"
    capabilities = ("exec:shell",)
    description: str = "在本地执行 Shell 命令。适用于运行脚本、安装包、使用 git、启动服务等系统级操作。注意：下发命令时请自动适配当前操作系统的系统级操作（Windows/macOS/Linux）。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 Shell 命令",
            },
            "timeout": {
                "type": "integer",
                "description": "命令超时时间（秒），默认 30 秒",
            },
        },
        "required": ["command"],
    }

    def __init__(self):
        super().__init__()
        self._sandbox: SandboxExecutor | None = None

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

    # ============ 系统检测 ============

    # 当前操作系统
    SYSTEM = platform.system().lower()  # 'windows', 'darwin'(macOS), 'linux'

    # 是否在 Windows 上运行
    IS_WINDOWS = SYSTEM == "windows"

    # Shell 提示符符号
    PROMPT_SYMBOL = ">" if IS_WINDOWS else "$"

    # Shell 名称（仅用于显示）
    SHELL_NAME = "cmd.exe" if IS_WINDOWS else f"{Path(os.environ.get('SHELL', '/bin/bash')).name}"

    # ============ Linux → Windows 命令映射（用于错误提示） ============

    COMMAND_SUGGESTIONS = {
        "ls":     {"win": "dir",     "desc": "列出目录内容"},
        "pwd":    {"win": "cd",      "desc": "显示当前目录"},
        "cat":    {"win": "type",    "desc": "查看文件内容"},
        "cp":     {"win": "copy",    "desc": "复制文件"},
        "mv":     {"win": "move",    "desc": "移动/重命名文件"},
        "rm":     {"win": "del",     "desc": "删除文件"},
        "find":   {"win": "dir /s",  "desc": "查找文件"},
        "grep":   {"win": "findstr", "desc": "搜索文本"},
        "touch":  {"win": "type nul >", "desc": "创建空文件"},
        "mkdir":  {"win": "mkdir",   "desc": "创建目录（相同）"},
        "clear":  {"win": "cls",     "desc": "清屏"},
        "whoami": {"win": "whoami",  "desc": "当前用户名（相同）"},
        "diff":   {"win": "fc",      "desc": "比较文件"},
        "sort":   {"win": "sort",    "desc": "排序（相同）"},
    }

    # ============ 危险命令黑名单 ============

    DANGEROUS_COMMANDS_WIN = [
        "del /f /s", "rd /s /q", "format ", "diskpart",
        "shutdown /r", "shutdown /s", "taskkill /f",
    ]

    DANGEROUS_COMMANDS_UNIX = [
        "rm -rf /", "rm -rf ~", "rm -rf .", "mkfs",
        "dd if=", ":(){ :|:& };:",  # fork 炸弹
        "chmod 0", "chown -r", "> /dev/sda",
    ]

    @property
    def _dangerous_commands(self) -> list:
        """根据当前系统返回合适的危险命令列表"""
        if self.IS_WINDOWS:
            return self.DANGEROUS_COMMANDS_UNIX + self.DANGEROUS_COMMANDS_WIN
        else:
            return self.DANGEROUS_COMMANDS_UNIX

    # ============ 执行命令 ============

    def execute(self, command: str, timeout: int = 30) -> str:
        """
        执行 Shell 命令

        参数:
            command: 要执行的命令
            timeout: 超时时间（秒）

        返回:
            命令的标准输出和标准错误
        """
        # --- 安全检查 ---
        for dangerous in self._dangerous_commands:
            if dangerous in command.lower():
                return (
                    f"⛔ 安全限制：命令包含危险操作，已阻止执行\n"
                    f"   命令: {command}\n"
                    f"   匹配到危险模式: {dangerous}"
                )

        # --- 提取命令名（用于后续的智能提示） ---
        cmd_name = command.strip().split()[0].lower() if command.strip() else ""

        # --- 沙箱检查（如果注入） ---
        if self._sandbox:
            result = self._sandbox.run(
                "cmd.exe" if self.IS_WINDOWS else "bash",
                ["/c", command] if self.IS_WINDOWS else ["-c", command],
                tool_name="bash",
            )
            if result.blocked:
                return f"⛔ 沙箱拦截: {result.block_reason}"
            if result.timeout:
                return f"⏰ 命令执行超时\n   命令: {command}"

            stdout = result.stdout or ""
            stderr = result.stderr or ""
            # 构造输出（复用下方格式化逻辑）
            return self._format_output(command, stdout, stderr, 0, cmd_name)

        try:
            # --- 执行命令 ---
            # 注意：shell=True 在 Windows 上用 cmd.exe，在 Linux/Mac 上用 bash
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )

            return self._format_output(command, result.stdout, result.stderr, result.returncode, cmd_name)

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

    def _format_output(self, command: str, stdout: str, stderr: str,
                        returncode: int, cmd_name: str) -> str:
        """统一格式化命令输出"""
        os_display = {
            "windows": "Windows",
            "darwin": "macOS",
            "linux": "Linux",
        }.get(self.SYSTEM, self.SYSTEM)

        parts = [f"⚡ [{os_display} | {self.SHELL_NAME}] {self.PROMPT_SYMBOL} {command}"]
        parts.append(f"   ➡ 退出码: {returncode}")
        parts.append("")

        if stdout:
            output = stdout.rstrip()
            if len(output) > 8000:
                output = output[:8000] + f"\n\n……（输出过长，已截断，共 {len(stdout)} 字符）"
            parts.append(f"📤 输出:\n{output}")

        if stderr:
            err = stderr.rstrip()
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

        return "\n".join(parts)


# ============================================================
# 7. CalculatorTool —— 数学计算器
# ============================================================

import ast
import operator as op

class CalculatorTool(BaseTool):
    """
    数学计算器工具

    用 Python 安全地执行数学运算，比 LLM 自己算更准确。
    支持 + - * / 以及 math 模块中的函数。
    使用 ast 安全解析，不会执行任意代码。
    """

    name: str = "calculate"
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
            return self._ALLOWED_OPS[type(node.op)](
                self._safe_eval(node.left, funcs),
                self._safe_eval(node.right, funcs),
            )
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


# ============================================================
# 8. DateTimeTool —— 时间日期
# ============================================================

class DateTimeTool(BaseTool):
    """
    时间日期工具

    获取当前的日期、时间、星期等信息。
    Agent 没有时间概念，这个工具告诉它"现在是何时"。
    """

    name: str = "datetime"
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


# ============================================================
# 9. NoteTool —— 笔记/记忆
# ============================================================

class NoteTool(BaseTool):
    """
    笔记工具 —— 在对话间记住关键信息

    可以保存笔记（save）、读取笔记（read）、列出笔记（list）、
    删除笔记（delete）。笔记存储在内存中，重启后丢失。
    """

    name: str = "notes"
    description: str = "存储和检索笔记/记忆。当需要记住用户的关键信息（如名字、偏好、项目信息），或在后续对话中回忆之前说过的内容时使用。操作：save 保存、read 读取、list 列出、delete 删除。"
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
        },
        "required": ["action", "key"],
    }

    # 类级别共享存储（同进程内所有实例共享）
    _store: dict = {}

    def execute(self, action: str, key: str, value: str = None) -> str:
        action = action.lower()

        if action == "save":
            if value is None:
                return "❌ save 操作需要提供 value 参数"
            self._store[key] = value
            return f"✅ 已保存笔记: {key}（{len(value)} 字）"

        elif action == "read":
            content = self._store.get(key)
            if content is None:
                return f"❌ 未找到笔记: {key}"
            return f"📝 [{key}]\n{content}"

        elif action == "list":
            if not self._store:
                return "📝 暂无笔记"
            lines = [f"📝 共 {len(self._store)} 条笔记:"]
            for k, v in sorted(self._store.items()):
                lines.append(f"  - {k}: {v[:50]}{'...' if len(v) > 50 else ''}")
            return "\n".join(lines)

        elif action == "delete":
            if key in self._store:
                del self._store[key]
                return f"🗑️ 已删除笔记: {key}"
            return f"❌ 未找到笔记: {key}"

        else:
            return f"❌ 未知操作: {action}（可选: save/read/list/delete）"


# ============================================================
# 10. FileManagerTool —— 文件管理
# ============================================================

class FileManagerTool(BaseTool):
    """
    文件管理工具

    安全的文件操作：复制、移动、删除、创建目录等。
    比用 bash 执行 cp/mv/rm 更安全（有路径检查和确认）。
    """

    name: str = "file_mgr"
    capabilities = ("fs:write", "fs:delete", "fs:move")
    description: str = "文件管理操作：复制(copy)、移动(move)、删除(delete)、创建目录(mkdir)、列出目录(ls)。比用 bash 执行系统命令更安全。支持批量删除（传 paths 数组）。"

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
        },
        "required": ["action"],
    }

    def execute(self, action: str, path: str = None, paths: list = None, dest: str = None) -> str:
        import shutil

        action = action.lower()

        # ===== 批量操作 =====
        if paths and action == "delete":
            results = []
            for p_str in paths:
                p = Path(p_str).resolve()
                if not p.exists():
                    results.append(f"  ❌ 路径不存在: {p}")
                elif p.is_file():
                    p.unlink()
                    results.append(f"  ✅ 已删除文件: {p}")
                else:
                    shutil.rmtree(p)
                    results.append(f"  ✅ 已删除目录: {p}")
            ok = sum(1 for r in results if r.startswith("  ✅"))
            fail = len(results) - ok
            summary = f"🗑️ 批量删除完成：共 {len(results)} 项，✅ {ok} 成功"
            if fail:
                summary += f"，❌ {fail} 失败"
            return summary + "\n" + "\n".join(results)

        # ===== 单路径操作 =====
        if not path:
            return "❌ 请提供 path 参数（单操作）或 paths 参数（批量删除）"

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
                size = _format_size(item.stat().st_size) if item.is_file() else ""
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
            dst = Path(dest)
            if p.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
            else:
                shutil.copytree(p, dst, dirs_exist_ok=True)
            return f"✅ 已复制: {p} → {dst}"

        elif action in ("move", "mv", "rename"):
            if not p.exists():
                return f"❌ 源路径不存在: {p}"
            dst = Path(dest)
            dst.parent.mkdir(parents=True, exist_ok=True)
            p.rename(dst)
            return f"✅ 已移动: {p} → {dst}"

        else:
            return f"❌ 未知操作: {action}（可选: copy/move/delete/mkdir/ls）"


# ============================================================
# 11. PythonTool —— 执行 Python 代码
# ============================================================

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

    def execute(self, code: str) -> str:
        # --- 沙箱检查：AST 级代码审查（如果注入） ---
        if self._sandbox:
            is_safe, reason = self._sandbox.check_python(code)
            if not is_safe:
                return f"⛔ 沙箱拦截: {reason}"

        import subprocess
        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True,
                timeout=15, encoding="utf-8", errors="replace",
            )
            parts = [f"🐍 Python 执行（退出码 {result.returncode}）"]
            if result.stdout:
                parts.append(f"\n📤 输出:\n{result.stdout.rstrip()[:3000]}")
            if result.stderr:
                parts.append(f"\n📕 错误:\n{result.stderr.rstrip()[:2000]}")
            if not result.stdout and not result.stderr:
                parts.append("\n（执行完毕，无输出）")
            return "\n".join(parts)
        except subprocess.TimeoutExpired:
            return "⏰ Python 执行超时（15 秒）"
        except Exception as e:
            logger.error(f"Python 执行失败: {e}", exc_info=True)
            return f"❌ Python 执行失败: {e}"


# HttpTool POST data 凭据检测（复用 guard.py SECRET_PATTERNS 的核心模式）
_CREDENTIAL_LEAK_RE = re.compile(
    r'(sk-[a-zA-Z0-9]{20,})'           # API Key（OpenAI / Anthropic）
    r'|(-----BEGIN\s+(RSA |EC |DSA )?PRIVATE KEY-----)',  # 私钥块
    re.DOTALL,
)


def _contains_secrets(data: str) -> bool:
    """检查字符串中是否包含疑似凭据（API Key / 私钥）。"""
    return bool(_CREDENTIAL_LEAK_RE.search(data or ""))


# ============================================================
# 12. HttpTool —— HTTP 请求
# ============================================================

class HttpTool(BaseTool):
    """
    HTTP 请求工具

    发送 HTTP 请求到指定 URL，支持 GET 和 POST。
    适合调用外部 API、获取 JSON 数据等。
    与 web_fetch 的区别：可以自定义请求方式、头信息等。
    """

    name: str = "http"
    capabilities = ("net:egress",)
    description: str = "发送 HTTP 请求。需要调用外部 REST API、获取 JSON 数据、或与 Web 服务交互时使用。支持 GET 和 POST 方法。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "请求的 URL",
            },
            "method": {
                "type": "string",
                "description": "请求方法: GET（默认）或 POST",
            },
            "data": {
                "type": "string",
                "description": "POST 时发送的 JSON 数据",
            },
        },
        "required": ["url"],
    }

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(self):
        super().__init__()
        self._sandbox: SandboxExecutor | None = None

    def set_sandbox(self, sandbox: SandboxExecutor):
        """注入沙箱执行器"""
        self._sandbox = sandbox

    def execute(self, url: str, method: str = "GET", data: str = None) -> str:
        if not url.startswith(("http://", "https://")):
            return f"❌ 无效 URL: {url}"

        # --- DLP: POST 数据不得包含疑似凭据（API Key / 私钥） ---
        if method.upper() == "POST" and data:
            if _contains_secrets(data):
                return "⛔ 安全拦截: POST 数据中包含疑似 API Key 或私钥，已阻止外发"

        # --- 沙箱检查：外发目标黑名单（如果注入） ---
        if self._sandbox:
            is_safe, reason = self._sandbox.check_egress(url)
            if not is_safe:
                return f"⛔ 沙箱拦截: {reason}"

        try:
            method = method.upper()
            kwargs = {"headers": self.HEADERS, "timeout": 15}

            if method == "POST" and data:
                kwargs["json"] = json.loads(data) if isinstance(data, str) else data

            if method == "POST":
                resp = requests.post(url, **kwargs)
            else:
                resp = requests.get(url, **kwargs)

            resp.raise_for_status()

            # 尝试格式化 JSON 输出
            try:
                body = json.dumps(resp.json(), ensure_ascii=False, indent=2)
            except Exception:
                body = resp.text

            if len(body) > 3000:
                body = body[:3000] + f"\n……（截断，共 {len(body)} 字符）"

            return (
                f"🌐 {method} {url}\n"
                f"   状态: {resp.status_code}\n\n"
                f"{body}"
            )

        except requests.Timeout:
            return f"⏰ 请求超时: {url}"
        except requests.HTTPError as e:
            return f"❌ HTTP {e.response.status_code}: {url}"
        except requests.ConnectionError:
            return f"❌ 无法连接: {url}"
        except json.JSONDecodeError:
            return f"❌ JSON 解析失败: {url}"
        except Exception as e:
            logger.error(f"请求失败: {e}", exc_info=True)
            return f"❌ 请求失败: {type(e).__name__}: {e}"


# ============================================================
# 工具列表 —— 方便批量导入
# ============================================================

# 所有基础工具的列表，方便一次性全部注册
BUILTIN_TOOLS = [
    ReadTool,
    WriteTool,
    EditTool,
    GrepTool,
    GlobTool,
    BashTool,
    CalculatorTool,
    DateTimeTool,
    NoteTool,
    FileManagerTool,
    PythonTool,
    HttpTool,
    MemorySearchTool,
    MemoryUpdateTool,
]

def register_all_tools(registry, memory_manager=None, sandbox=None, process_manager=None):
    """
    一键注册所有内置工具到注册表

    参数:
        registry: ToolRegistry 实例
        memory_manager: MemoryManager 实例（可选），注入到记忆工具中
        sandbox: SandboxExecutor 实例（可选），注入到 bash/python/write/edit/read/http 工具中
        process_manager: ProcessManager 实例（可选），注入到 proc_* 工具中

    使用方式：
        from tools import ToolRegistry
        from tools.builtin_tools import register_all_tools

        registry = ToolRegistry()
        register_all_tools(registry)
        register_all_tools(registry, memory_manager=mm)  # 启用记忆系统
        register_all_tools(registry, sandbox=sb)         # 启用沙箱
        register_all_tools(registry, process_manager=pm) # 启用长驻进程工具
    """
    from .registry import ToolRegistry

    if not isinstance(registry, ToolRegistry):
        raise TypeError("参数必须是 ToolRegistry 实例")

    for tool_cls in BUILTIN_TOOLS:
        tool = tool_cls()
        # 注入 MemoryManager（记忆工具）
        if memory_manager and hasattr(tool, 'set_memory_manager'):
            tool.set_memory_manager(memory_manager)
        # 注入 SandboxExecutor（沙箱工具）
        if sandbox and hasattr(tool, 'set_sandbox'):
            tool.set_sandbox(sandbox)
        registry.register_tool(tool)

    # 长驻子进程工具（proc_*）
    if process_manager is not None:
        from .process_tools import register_process_tools
        register_process_tools(registry, process_manager=process_manager)

    return registry
