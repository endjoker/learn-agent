# -*- coding: utf-8 -*-
"""builtin-tools-review.md 修复回归：P0/P1/P2/P3 各项。

覆盖：
- P0  WriteTool/EditTool 工作区边界校验（读写防护对称）
- P2-1 EditTool 空 old_string 显式拒绝
- P1-1 FileManagerTool copy/move 的 dest 先 resolve 再校验
- P2-2 FileManagerTool 批量删除单项异常隔离 / ls 容错
- P2-3 GrepTool 默认扫描剪枝（node_modules 等不再进入）
- P1-2(修正) HttpTool 响应体上限收紧到 1MB（safe_http 本就有 10MB 流式上限）
- P3-4 BaseTool.parallel_safe 显式默认 False + configure() 统一注入
- P3-5 HttpTool 支持 PUT/DELETE/PATCH
- P3-2 NoteTool 单条笔记大小上限
"""
import json
import shutil

import pytest

from tools.base_tool import BaseTool
from tools.builtin_tools import (
    SCAN_EXCLUDED_DIRS,
    EditTool,
    FileManagerTool,
    GrepTool,
    HttpTool,
    NoteTool,
    WriteTool,
    register_all_tools,
)
from tools.registry import ToolRegistry


# ============================================================
# P0：写工具工作区边界校验
# ============================================================

class TestWriteEditBoundary:
    def test_write_outside_workspace_rejected(self, tmp_path):
        tool = WriteTool()
        tool.set_workspace_roots([tmp_path])
        outside = tmp_path.parent / "outside-secret.txt"
        out = tool.execute(str(outside), "evil")
        assert "路径超出允许的工作区边界" in out
        assert not outside.exists()

    def test_write_inside_workspace_ok(self, tmp_path):
        tool = WriteTool()
        tool.set_workspace_roots([tmp_path])
        target = tmp_path / "ok.txt"
        out = tool.execute(str(target), "hello")
        assert "✅" in out
        assert target.read_text(encoding="utf-8") == "hello"

    def test_write_without_roots_keeps_backward_compat(self, tmp_path):
        """未配置工作区根时不拦截（_check_workspace_boundary 向后兼容语义）。"""
        tool = WriteTool()
        target = tmp_path / "free.txt"
        out = tool.execute(str(target), "x")
        assert "✅" in out

    def test_edit_outside_workspace_rejected(self, tmp_path):
        tool = EditTool()
        tool.set_workspace_roots([tmp_path])
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("hello world", encoding="utf-8")
        out = tool.execute(str(outside), "hello", "bye")
        assert "路径超出允许的工作区边界" in out
        assert outside.read_text(encoding="utf-8") == "hello world"

    def test_edit_inside_workspace_ok(self, tmp_path):
        tool = EditTool()
        tool.set_workspace_roots([tmp_path])
        target = tmp_path / "code.txt"
        target.write_text("hello world", encoding="utf-8")
        out = tool.execute(str(target), "hello", "bye")
        assert "✅" in out
        assert target.read_text(encoding="utf-8") == "bye world"


# ============================================================
# P0 修正：写边界必须遵守四档权限（ask/allow/unreviewed 交还裁决）
#
# PolicyEngine 对 fs:write 的语义：readonly→DENY；ask→全部 ASK；
# allow→界外 ASK；unreviewed→ALLOW。授权层先确认、后执行——工具层
# 硬边界若在确认后一票否决，就是"用户确认了也写不进去"的权限违背。
# ============================================================

from types import SimpleNamespace  # noqa: E402


def _perm(mode):
    return SimpleNamespace(permission_mode=mode) if mode else None


class TestMutationBoundaryRespectsPermissionLadder:
    """写边界 × 四档模式矩阵（write/edit/file_mgr 写类 action 一致）。"""

    @pytest.mark.parametrize("mode,expect_ok", [
        ("ask", True),          # 授权层已 ASK 并确认 → 执行必须可达
        ("allow", True),        # 界外写按档位 ASK，确认后放行
        ("unreviewed", True),   # 免审模式放行
        ("readonly", False),    # 与 DENY 一致，保持硬边界
        (None, False),          # 未注入权限的直调路径：硬边界是唯一防线
    ])
    def test_write_outside_by_mode(self, tmp_path, mode, expect_ok):
        tool = WriteTool()
        tool.set_workspace_roots([tmp_path])
        tool.set_permission(_perm(mode))
        outside = tmp_path.parent / f"out-{mode or 'none'}.txt"
        out = tool.execute(str(outside), "data")
        assert ("✅" in out) == expect_ok, out
        assert outside.exists() is expect_ok

    def test_edit_outside_allowed_mode_executes(self, tmp_path):
        tool = EditTool()
        tool.set_workspace_roots([tmp_path])
        tool.set_permission(_perm("allow"))
        outside = tmp_path.parent / "edit-allow.txt"
        outside.write_text("hello world", encoding="utf-8")
        out = tool.execute(str(outside), "hello", "bye")
        assert "✅" in out
        assert outside.read_text(encoding="utf-8") == "bye world"

    def test_edit_outside_readonly_still_rejected(self, tmp_path):
        tool = EditTool()
        tool.set_workspace_roots([tmp_path])
        tool.set_permission(_perm("readonly"))
        outside = tmp_path.parent / "edit-ro.txt"
        outside.write_text("keep", encoding="utf-8")
        out = tool.execute(str(outside), "keep", "gone")
        assert "路径超出允许的工作区边界" in out
        assert outside.read_text(encoding="utf-8") == "keep"

    def test_file_mgr_delete_outside_allowed_mode_executes(self, tmp_path):
        tool = FileManagerTool()
        tool.set_workspace_roots([tmp_path])
        tool.set_permission(_perm("allow"))
        outside = tmp_path.parent / "fmgr-del.txt"
        outside.write_text("x", encoding="utf-8")
        out = tool.execute(action="delete", path=str(outside), confirm=True)
        assert "🗑️" in out
        assert not outside.exists()

    def test_file_mgr_ls_outside_defers_to_ladder_when_permission_injected(self, tmp_path):
        """读边界四档对齐：注入权限后 ls 界外交还裁决（fs:read 全模式 ALLOW）。"""
        tool = FileManagerTool()
        tool.set_workspace_roots([tmp_path])
        tool.set_permission(_perm("allow"))
        outside_dir = tmp_path.parent / "fmgr-ls-dir"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "note.txt").write_text("x", encoding="utf-8")
        out = tool.execute(action="ls", path=str(outside_dir))
        assert "✅" in out or "📁" in out
        assert "路径超出允许的工作区边界" not in out

    def test_file_mgr_ls_outside_still_rejected_without_permission(self, tmp_path):
        """未注入权限的直调路径：读硬边界是唯一防线，保持默认拒绝。"""
        tool = FileManagerTool()
        tool.set_workspace_roots([tmp_path])
        outside_dir = tmp_path.parent / "fmgr-ls-dir2"
        outside_dir.mkdir(exist_ok=True)
        out = tool.execute(action="ls", path=str(outside_dir))
        assert "路径超出允许的工作区边界" in out


# ============================================================
# 读边界四档对齐：read/grep/glob 注入权限后交还裁决
# （PolicyEngine 对 fs:read 全模式无条件 ALLOW；系统路径 ASK 由授权层把关）
# ============================================================

class TestReadBoundaryLadderAlignment:
    def test_read_outside_allowed_with_permission(self, tmp_path):
        from tools.builtin_tools import ReadTool
        outside = tmp_path.parent / "outside-read.txt"
        outside.write_text("secret? no — ladder allows reads", encoding="utf-8")
        tool = ReadTool()
        tool.set_workspace_roots([tmp_path])
        tool.set_permission(_perm("ask"))
        out = tool.execute(str(outside))
        assert "✅" in out or "ladder allows reads" in out
        assert "路径超出允许的工作区边界" not in out

    def test_read_outside_rejected_without_permission(self, tmp_path):
        from tools.builtin_tools import ReadTool
        outside = tmp_path.parent / "outside-read2.txt"
        outside.write_text("x", encoding="utf-8")
        tool = ReadTool()
        tool.set_workspace_roots([tmp_path])
        out = tool.execute(str(outside))
        assert "路径超出允许的工作区边界" in out

    def test_grep_outside_allowed_with_permission(self, tmp_path):
        outside_dir = tmp_path.parent / "outside-grep"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "z.py").write_text("NEEDLE = 1\n", encoding="utf-8")
        tool = GrepTool()
        tool.set_workspace_roots([tmp_path])
        tool.set_permission(_perm("unreviewed"))
        out = tool.execute(pattern="NEEDLE", path=str(outside_dir))
        assert "NEEDLE" in out
        assert "路径超出允许的工作区边界" not in out

    def test_glob_outside_allowed_with_permission(self, tmp_path):
        outside_dir = tmp_path.parent / "outside-glob"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "f.txt").write_text("x", encoding="utf-8")
        tool = GrepTool  # noqa: F841  (占位说明下方直接构造 Glob)
        from tools.builtin_tools import GlobTool
        tool = GlobTool()
        tool.set_workspace_roots([tmp_path])
        tool.set_permission(_perm("allow"))
        out = tool.execute(pattern="*.txt", path=str(outside_dir))
        assert "f.txt" in out
        assert "路径超出允许的工作区边界" not in out

    def test_register_all_tools_reads_defer_when_permission_injected(self, tmp_path):
        """端到端：主链路注入 permission 后，读工具界外读不再被工具层拦截。"""
        registry = ToolRegistry()
        register_all_tools(registry, workspace_roots=[str(tmp_path)],
                           permission=_perm("allow"))
        reader = registry.get_tool("read")
        outside = tmp_path.parent / "e2e-read.txt"
        outside.write_text("ok", encoding="utf-8")
        out = reader.execute(str(outside))
        assert "路径超出允许的工作区边界" not in out

    def test_register_all_tools_forwards_permission(self, tmp_path):
        registry = ToolRegistry()
        register_all_tools(registry, workspace_roots=[str(tmp_path)],
                           permission=_perm("allow"))
        writer = registry.get_tool("write")
        outside = tmp_path.parent / "leak-allow.txt"
        # allow 模式：界外写交还裁决（单测无授权层 → 直接执行）
        assert "✅" in writer.execute(str(outside), "x")
        # 切 readonly（同一 checker 引用，模式即时生效）→ 硬边界恢复
        writer._permission.permission_mode = "readonly"
        assert "路径超出允许的工作区边界" in writer.execute(str(outside), "y")


# ============================================================
# P2-1：EditTool 空 old_string
# ============================================================

class TestEditEmptyOldString:
    def test_empty_old_string_rejected_even_with_replace_all(self, tmp_path):
        tool = EditTool()
        target = tmp_path / "f.txt"
        target.write_text("hello", encoding="utf-8")
        out = tool.execute(str(target), "", "X", replace_all=True)
        assert "old_string 不能为空" in out
        # 文件未被破坏（修复前会变成 'XhXeXlXlXoX'）
        assert target.read_text(encoding="utf-8") == "hello"


# ============================================================
# P1-1：FileManager copy/move dest resolve
# ============================================================

class TestFileMgrDestResolve:
    def _tool(self, tmp_path):
        tool = FileManagerTool()
        tool.set_workspace_roots([tmp_path])
        return tool

    def test_move_relative_dest_rejected_when_cwd_outside_roots(self, tmp_path, monkeypatch):
        """相对 dest resolve 后落在进程 cwd（不在边界内）→ 明确拒绝，
        而不是把文件移到不可预期位置。"""
        tool = self._tool(tmp_path)
        src = tmp_path / "a.txt"
        src.write_text("data", encoding="utf-8")
        out = tool.execute(action="move", path=str(src), dest="relative-out.txt")
        assert "路径超出允许的工作区边界" in out
        assert src.exists()  # 未被移走

    def test_move_absolute_dest_inside_roots_ok(self, tmp_path):
        tool = self._tool(tmp_path)
        src = tmp_path / "a.txt"
        src.write_text("data", encoding="utf-8")
        dst = tmp_path / "sub" / "b.txt"
        out = tool.execute(action="move", path=str(src), dest=str(dst))
        assert "✅" in out
        assert not src.exists()
        assert dst.read_text(encoding="utf-8") == "data"

    def test_copy_relative_dest_rejected_when_cwd_outside_roots(self, tmp_path):
        tool = self._tool(tmp_path)
        src = tmp_path / "a.txt"
        src.write_text("data", encoding="utf-8")
        out = tool.execute(action="copy", path=str(src), dest="relative-copy.txt")
        assert "路径超出允许的工作区边界" in out


# ============================================================
# P2-2：批量删除单项隔离
# ============================================================

class TestBatchDeleteIsolation:
    def test_one_failure_does_not_abort_batch(self, tmp_path, monkeypatch):
        tool = FileManagerTool()
        f1 = tmp_path / "1.txt"
        f3 = tmp_path / "3.txt"
        for f in (f1, f3):
            f.write_text("x", encoding="utf-8")
        victim = tmp_path / "2.txt"
        victim.write_text("x", encoding="utf-8")

        real_rmtree = shutil.rmtree
        def flaky_unlink(self, *a, **kw):
            if self == victim:
                raise PermissionError("simulated EACCES")
            return orig_unlink(self, *a, **kw)
        orig_unlink = type(victim).unlink
        monkeypatch.setattr(type(victim), "unlink", flaky_unlink)

        out = tool.execute(action="delete", paths=[str(f1), str(victim), str(f3)],
                           confirm=True)
        assert "✅ 2 成功" in out and "❌ 1 失败" in out
        assert f1.exists() is False
        assert f3.exists() is False
        assert victim.exists()  # 失败项保留，其余不受影响

    def test_ls_survives_stat_race(self, tmp_path):
        """ls 用 _safe_stat：条目 stat 失败按 0 处理而非整体崩溃。"""
        tool = FileManagerTool()
        (tmp_path / "x.txt").write_text("data", encoding="utf-8")
        out = tool.execute(action="ls", path=str(tmp_path))
        assert "x.txt" in out


# ============================================================
# P2-3：Grep 默认扫描剪枝
# ============================================================

class TestGrepPruning:
    def _make_tree(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("NEEDLE = 1\n", encoding="utf-8")
        deps = tmp_path / "node_modules" / "pkg"
        deps.mkdir(parents=True)
        (deps / "x.py").write_text("NEEDLE = 2\n", encoding="utf-8")
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "y.py").write_text("NEEDLE = 3\n", encoding="utf-8")

    def test_default_scan_skips_excluded_dirs(self, tmp_path):
        self._make_tree(tmp_path)
        tool = GrepTool()
        out = tool.execute(pattern="NEEDLE", path=str(tmp_path))
        assert "src/a.py" in out
        assert "node_modules" not in out
        assert ".venv" not in out

    def test_include_branch_also_filters_excluded_dirs(self, tmp_path):
        self._make_tree(tmp_path)
        tool = GrepTool()
        out = tool.execute(pattern="NEEDLE", path=str(tmp_path), include="**/*.py")
        assert "src/a.py" in out
        assert "node_modules" not in out

    def test_excluded_dirs_constant(self):
        assert {"node_modules", ".venv", "__pycache__", ".git"} <= SCAN_EXCLUDED_DIRS


# ============================================================
# P3-4：BaseTool parallel_safe 显式默认 + configure 统一注入
# ============================================================

class TestBaseToolConfigure:
    def test_parallel_safe_defaults_to_false(self):
        assert BaseTool.parallel_safe is False

    def test_configure_dispatches_to_setters(self):
        class T(BaseTool):
            name = "t"
            description = "t"

            def __init__(self):
                self.got = {}

            def set_sandbox(self, sandbox):
                self.got["sandbox"] = sandbox

            def set_workspace_roots(self, roots):
                self.got["roots"] = roots

        marker = object()
        t = T()
        t.configure(sandbox=marker, workspace_roots=("r",), policy=object())
        assert t.got == {"sandbox": marker, "roots": ("r",)}

    def test_register_all_tools_injects_roots_into_write_edit(self, tmp_path):
        """P2-4 端到端：register_all_tools 统一 configure 后，
        Write/Edit 自动获得边界校验能力（P0 漏配不再可能）。"""
        registry = ToolRegistry()
        register_all_tools(registry, workspace_roots=[str(tmp_path)])
        writer = registry.get_tool("write")
        editor = registry.get_tool("edit")
        assert writer is not None and editor is not None
        outside = tmp_path.parent / "leak.txt"
        assert "路径超出允许的工作区边界" in writer.execute(str(outside), "x")
        assert "路径超出允许的工作区边界" in editor.execute(str(outside), "a", "b")


# ============================================================
# P3-5 / P1-2：HttpTool 方法族与响应上限
# ============================================================

class TestHttpTool:
    def _run(self, monkeypatch, method="GET", data=None):
        captured = {}
        class FakeResp:
            status_code = 200
            truncated = False
            def raise_for_status(self): pass
            def json(self): return {"ok": True}
            @property
            def text(self): return json.dumps({"ok": True})
        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["kwargs"] = kwargs
            return FakeResp()
        monkeypatch.setattr("tools.exec_tools.safe_http_request", fake_request)
        tool = HttpTool()
        return tool.execute("https://example.com/api", method=method, data=data), captured

    def test_put_patch_delete_attach_json_body(self, monkeypatch):
        for method in ("PUT", "PATCH", "DELETE"):
            out, captured = self._run(monkeypatch, method=method, data='{"k":1}')
            assert "🌐" in out
            assert captured["method"] == method
            assert captured["kwargs"]["json"] == {"k": 1}

    def test_get_has_no_body(self, monkeypatch):
        out, captured = self._run(monkeypatch, method="GET")
        assert "json" not in captured["kwargs"]

    def test_response_cap_tightened_to_1mb(self, monkeypatch):
        _, captured = self._run(monkeypatch, method="GET")
        # safe_http 本身默认 10MB 流式上限；本工具收紧为 1MB（只用 3000 字符）
        assert captured["kwargs"]["max_response_bytes"] == 1024 * 1024


# ============================================================
# P3-2：NoteTool 单条大小上限
# ============================================================

class TestNoteToolClamp:
    def test_oversized_value_rejected(self):
        tool = NoteTool()
        big = "x" * (tool.MAX_VALUE_CHARS + 1)
        out = tool.execute(action="save", key="big", value=big)
        assert "❌" in out and "过大" in out
        # 正常大小可存
        out = tool.execute(action="save", key="ok", value="small")
        assert "✅" in out
