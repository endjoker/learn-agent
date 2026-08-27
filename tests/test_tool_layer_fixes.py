# -*- coding: utf-8 -*-
"""工具层问题修复的针对性单测。

覆盖清单（与修复报告编号一致）：
1.  run_killable 流式限界读取（小输出正常 / 大输出受控 / 不提前杀进程）
2.  run_killable env 脱敏 + cwd；BashTool cwd 固定到工作区
3.  Grep include 花括号展开
4.  Edit 多处匹配拒绝 / replace_all / 非 UTF-8 友好提示
5.  Read 空文件明确提示，offset 越界才报越界
6.  Glob stat 容错 + max_results 校验
7.  SYSTEM_RESERVED_TOOLS 含全部 cron_* 工具
8.  register_tool 重名检查在锁内（并发注册不静默覆盖）
9.  web_tools Content-Type 判断（非文本跳过解析）
10. memory_update action 枚举校验
"""

import subprocess
import sys
import threading

import pytest

from core.shell import run_killable, _BoundedStreamBuffer
from tools.builtin_tools import (
    BashTool,
    EditTool,
    GlobTool,
    GrepTool,
    ReadTool,
    _expand_glob_braces,
    _safe_stat,
)
from tools.memory_tools import MemoryUpdateTool
from tools.registry import SYSTEM_RESERVED_TOOLS, ToolRegistry
from tools.web_tools import WebFetchTool, WebSearchTool, _is_textual_content_type


# ============================================================
# 1. run_killable 流式限界读取
# ============================================================

class TestRunKillableStreaming:
    def test_small_output_normal(self):
        result = run_killable(["bash", "-c", "echo hi"])
        assert result.returncode == 0
        assert result.stdout == "hi\n"
        assert isinstance(result.stdout, str) and isinstance(result.stderr, str)

    def test_large_output_bounded_and_not_killed_early(self, tmp_path):
        # 产生 ~20MB 输出后正常退出（退出码 0 证明没有因超量被提前杀死）；
        # 头部与尾部都保留在结果里。
        script = (
            "import sys\n"
            "print('HEAD_MARKER')\n"
            "sys.stdout.write('x' * (20 * 1024 * 1024))\n"
            "print()\n"
            "print('TAIL_MARKER')\n"
        )
        result = run_killable(
            [sys.executable, "-c", script], max_output_bytes=2 * 1024 * 1024
        )
        assert result.returncode == 0
        # 单流环形上限 2MB + 省略标记 → 结果远小于 20MB 全量
        assert len(result.stdout) < 3 * 1024 * 1024
        assert "HEAD_MARKER" in result.stdout      # 头部保留
        assert "TAIL_MARKER" in result.stdout      # 尾部保留
        assert "中间输出过长已省略" in result.stdout  # 中段丢弃有标记

    def test_timeout_still_kills_group_and_raises(self):
        with pytest.raises(subprocess.TimeoutExpired):
            run_killable(["bash", "-c", "sleep 5"], timeout=1)

    def test_stderr_bounded_too(self):
        script = "import sys\nsys.stderr.write('e' * (8 * 1024 * 1024))\n"
        result = run_killable(
            [sys.executable, "-c", script], max_output_bytes=512 * 1024
        )
        assert result.returncode == 0
        assert 0 < len(result.stderr) <= 512 * 1024 + 200  # 上限 + 标记余量

    def test_unlimited_escape_hatch_keeps_all(self):
        n = 3 * 1024 * 1024
        result = run_killable(
            ["bash", "-c", f"head -c {n} /dev/zero | tr '\\0' 'a'"],
            max_output_bytes=0,  # <=0 不设上限
        )
        assert len(result.stdout) == n


class TestBoundedStreamBuffer:
    def test_under_limit_no_elision(self):
        buf = _BoundedStreamBuffer(100)
        buf.append(b"hello")
        assert buf.value() == b"hello"
        assert not buf.dropped_middle

    def test_over_limit_keeps_head_and_tail(self):
        buf = _BoundedStreamBuffer(10)
        buf.append(b"A" * 6)   # 头部容量 5 → 前 5 进头部，1 字节进尾部
        buf.append(b"B" * 50)
        value = buf.value()
        assert value.startswith(b"A" * 5)
        assert value.endswith(b"B" * 5)
        assert buf.dropped_middle


# ============================================================
# 2. env 脱敏 + cwd
# ============================================================

class TestEnvAndCwd:
    def test_default_env_strips_secret_token(self, monkeypatch):
        monkeypatch.setenv("WEBUI_AUTH_TOKEN", "super-secret-token-value")
        result = run_killable(["bash", "-c", "printf '%s' \"$WEBUI_AUTH_TOKEN\""])
        assert "super-secret-token-value" not in (result.stdout or "")

    def test_default_env_keeps_plain_var(self, monkeypatch):
        monkeypatch.setenv("JK_PLAIN_VAR", "plain-ok")
        result = run_killable(["bash", "-c", "printf '%s' \"$JK_PLAIN_VAR\""])
        assert result.stdout == "plain-ok"

    def test_explicit_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("WEBUI_AUTH_TOKEN", "super-secret-token-value")
        import os
        result = run_killable(
            ["bash", "-c", "printf '%s' \"$WEBUI_AUTH_TOKEN\""],
            env=dict(os.environ),  # 显式传入则尊重调用方
        )
        assert result.stdout == "super-secret-token-value"

    def test_cwd_param(self, tmp_path):
        result = run_killable(["bash", "-c", "pwd"], cwd=str(tmp_path))
        assert result.stdout.strip() == str(tmp_path)

    def test_bashtool_runs_in_workspace_root(self, tmp_path):
        tool = BashTool()
        tool.set_workspace_roots([tmp_path])
        out = tool.execute("pwd")
        assert str(tmp_path) in out
        assert "❌" not in out and "⛔" not in out

    def test_bashtool_env_sanitized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WEBUI_AUTH_TOKEN", "super-secret-token-value")
        tool = BashTool()
        tool.set_workspace_roots([tmp_path])
        out = tool.execute("printf '%s' \"$WEBUI_AUTH_TOKEN\"")
        assert "super-secret-token-value" not in out

    def test_bashtool_prefers_sandbox_workspace(self, tmp_path):
        class FakeSandbox:
            _workspace = tmp_path
            _extra_workspace_roots = ()

        tool = BashTool()
        tool.set_sandbox(FakeSandbox())
        tool.set_workspace_roots(["/other/root"])
        assert tool._resolve_cwd() == str(tmp_path)


# ============================================================
# 3. Grep include 花括号展开
# ============================================================

class TestGrepBraceExpansion:
    @pytest.mark.parametrize("pattern,expected", [
        ("*.py", ["*.py"]),
        ("*.{ts,tsx}", ["*.ts", "*.tsx"]),
        ("**/*.{json,yml}", ["**/*.json", "**/*.yml"]),
        ("{a,b}{1,2}", ["a1", "a2", "b1", "b2"]),
        ("{a,a}.py", ["a.py"]),  # 去重保序
    ])
    def test_expand(self, pattern, expected):
        assert _expand_glob_braces(pattern) == expected

    def test_grep_finds_ts_and_tsx_only(self, tmp_path):
        for name, body in [
            ("a.ts", "const x = NEEDLE_TS;"),
            ("b.tsx", "const y = NEEDLE_TSX;"),
            ("c.js", "var z = NEEDLE_JS;"),
        ]:
            (tmp_path / name).write_text(body, encoding="utf-8")

        tool = GrepTool()
        out = tool.execute(pattern="NEEDLE", path=str(tmp_path),
                           include="*.{ts,tsx}")
        assert "❌" not in out
        assert "a.ts:" in out.replace("\\", "/") or "a.ts" in out
        assert "b.tsx" in out
        assert "NEEDLE_TS" in out and "NEEDLE_TSX" in out
        assert "NEEDLE_JS" not in out and "c.js" not in out


# ============================================================
# 4. Edit replace_all + 非 UTF-8 提示
# ============================================================

class TestEditTool:
    def test_schema_has_replace_all(self):
        props = EditTool.parameters["properties"]
        assert "replace_all" in props
        assert props["replace_all"]["type"] == "boolean"
        assert "replace_all" not in EditTool.parameters["required"]

    def test_single_match_replaces_once(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("hello world", encoding="utf-8")
        out = EditTool().execute(str(f), "world", "there")
        assert "✅" in out and "已替换" in out
        assert f.read_text(encoding="utf-8") == "hello there"

    def test_multi_match_refuses_by_default(self, tmp_path):
        f = tmp_path / "g.txt"
        original = "aaa\nbbb\naaa\naaa\n"
        f.write_text(original, encoding="utf-8")
        out = EditTool().execute(str(f), "aaa", "zzz")
        assert "未执行替换" in out and "3 处" in out
        assert "replace_all=true" in out          # 提示扩大或显式全换
        assert "old_string" in out
        assert f.read_text(encoding="utf-8") == original  # 文件未被改动

    def test_replace_all_true_replaces_everywhere(self, tmp_path):
        f = tmp_path / "h.txt"
        f.write_text("aaa bbb aaa bbb aaa", encoding="utf-8")
        out = EditTool().execute(str(f), "aaa", "z", replace_all=True)
        assert "✅" in out and "3" in out and "全部替换" in out
        assert f.read_text(encoding="utf-8") == "z bbb z bbb z"

    def test_non_utf8_file_friendly_error(self, tmp_path):
        f = tmp_path / "gbk.txt"
        f.write_bytes("中文内容，GBK 编码".encode("gbk"))
        out = EditTool().execute(str(f), "中文", "XX")
        assert "非 UTF-8 编码文件，无法编辑" in out
        assert "UnicodeDecodeError" not in out    # 不再透出原始异常文本
        assert f.read_bytes() == "中文内容，GBK 编码".encode("gbk")


# ============================================================
# 5. Read 空文件 / offset 越界
# ============================================================

class TestReadEmptyAndOffset:
    def test_empty_file_clear_hint(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        out = ReadTool().execute(str(f))
        assert "(空文件)" in out
        assert "超出文件总行数" not in out

    def test_empty_file_with_offset_still_clear_hint(self, tmp_path):
        f = tmp_path / "empty2.txt"
        f.write_bytes(b"")
        out = ReadTool().execute(str(f), offset=3)
        assert "(空文件)" in out

    def test_nonempty_offset_out_of_range_reports_bounds(self, tmp_path):
        f = tmp_path / "five.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
        out = ReadTool().execute(str(f), offset=99)
        assert "超出文件总行数" in out and "5 行" in out

    def test_nonempty_normal_read(self, tmp_path):
        f = tmp_path / "ok.txt"
        f.write_text("alpha\nbeta\n", encoding="utf-8")
        out = ReadTool().execute(str(f))
        assert "alpha" in out and "beta" in out


# ============================================================
# 6. Glob stat 容错 + max_results 校验
# ============================================================

class TestGlobRobustness:
    def test_safe_stat_returns_zero_on_oserror(self):
        class VanishedFile:
            def stat(self):
                raise OSError(2, "No such file or directory")

        assert _safe_stat(VanishedFile()) == (0, 0.0)

    def test_glob_normal_listing(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
        out = GlobTool().execute("*.txt", path=str(tmp_path))
        assert "共找到 5 个文件" in out

    def test_negative_max_results_falls_back_to_default(self, tmp_path):
        for i in range(35):
            (tmp_path / f"g{i}.log").write_text("x", encoding="utf-8")
        tool = GlobTool()
        out_neg = tool.execute("*.log", path=str(tmp_path), max_results=-5)
        out_zero = tool.execute("*.log", path=str(tmp_path), max_results=0)
        out_bad = tool.execute("*.log", path=str(tmp_path), max_results="abc")
        # 非法值回退默认 30：显示前 30 个并提示还有 5 个未显示
        for out in (out_neg, out_zero, out_bad):
            assert "还有 5 个文件未显示" in out
            assert "❌ 文件查找失败" not in out

    def test_positive_max_results_respected(self, tmp_path):
        for i in range(10):
            (tmp_path / f"p{i}.md").write_text("x", encoding="utf-8")
        out = GlobTool().execute("*.md", path=str(tmp_path), max_results=3)
        assert "共找到 10 个文件（显示前 3 个）" in out
        assert "还有 7 个文件未显示" in out


# ============================================================
# 7. SYSTEM_RESERVED_TOOLS 补 cron_list_jobs
# ============================================================

class TestReservedToolsIncludeAllCron:
    CRON_TOOLS = ("cron_add_job", "cron_delete_job",
                  "cron_list_jobs", "cron_run_job")

    def test_all_cron_tools_reserved(self):
        for name in self.CRON_TOOLS:
            assert name in SYSTEM_RESERVED_TOOLS, \
                f"系统保留清单缺少 {name}，会暴露进 Catalog 且被 allowlist 过滤掉"

    def test_registered_names_match_reserved_list(self):
        from tools.cron_tools import (
            CronAddJobTool, CronDeleteJobTool, CronListJobsTool, CronRunJobTool)
        instances = [CronAddJobTool(), CronDeleteJobTool(),
                     CronListJobsTool(), CronRunJobTool()]
        for inst in instances:
            assert inst.name in SYSTEM_RESERVED_TOOLS

    def test_cron_tools_hidden_from_catalog_even_when_active(self):
        from tools.cron_tools import CronListJobsTool
        reg = ToolRegistry()
        reg.register_tool(CronListJobsTool())
        reg.set_active_tools(["cron_list_jobs"])  # 即使显式激活也不进 Catalog
        catalog_names = [item["name"] for item in reg.get_catalog()]
        assert "cron_list_jobs" not in catalog_names
        # 但运行装配 allowlist 过滤后仍保留在注册表内
        assert reg.get_tool("cron_list_jobs") is not None
        assert reg.get_available_names(["cron_list_jobs"]) == []


# ============================================================
# 8. register_tool TOCTOU：重名检查移入锁内
# ============================================================

class TestRegisterToolThreadSafety:
    def _make_tool(self, name):
        from tools.base_tool import BaseTool

        class T(BaseTool):
            pass

        t = T()
        t.name = name
        return t

    def test_sequential_duplicate_still_raises_and_keeps_original(self):
        reg = ToolRegistry()
        first = self._make_tool("dup")
        reg.register_tool(first)
        with pytest.raises(ValueError):
            reg.register_tool(self._make_tool("dup"))
        assert reg.get_tool("dup") is first

    def test_concurrent_same_name_registers_exactly_one(self):
        reg = ToolRegistry()
        n_threads, barrier = 16, threading.Barrier(16)
        winners, failures = [], []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            try:
                reg.register_tool(self._make_tool("raced"))
                with lock:
                    winners.append(1)
            except ValueError:
                with lock:
                    failures.append(1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(winners) == 1
        assert len(failures) == n_threads - 1
        assert reg.count() == 1


# ============================================================
# 9. web_tools Content-Type 判断
# ============================================================

class TestWebContentType:
    @pytest.mark.parametrize("ct,expected", [
        ("text/html; charset=utf-8", True),
        ("text/plain", True),
        ("application/json", True),
        ("application/xhtml+xml", True),
        ("application/atom+xml", True),
        ("application/javascript", True),
        ("application/zip", False),
        ("application/octet-stream", False),
        ("image/png", False),
        ("binary/octet-stream", False),
        ("", True),  # 未声明类型保守按文本处理
    ])
    def test_matrix(self, ct, expected):
        assert _is_textual_content_type(ct) is expected

    @staticmethod
    def _fake_response(content_type, text="<html><body>Hello Web Body</body></html>"):
        class FakeResponse:
            headers = {"Content-Type": content_type} if content_type else {}
            apparent_encoding = "utf-8"
            encoding = None
            status_code = 200

            def raise_for_status(self):
                return None

        resp = FakeResponse()
        resp.text = text
        return resp

    def test_fetch_binary_content_skipped(self, monkeypatch):
        import tools.web_tools as wt
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["url"] = url
            return self._fake_response("application/zip")

        monkeypatch.setattr(wt, "safe_request", fake_request)
        monkeypatch.setattr(wt, "validate_url", lambda url: None)
        out = WebFetchTool().execute("https://example.com/file.zip")
        assert "非文本内容(application/zip)" in out
        assert "已跳过解析" in out

    def test_fetch_html_still_parsed(self, monkeypatch):
        import tools.web_tools as wt

        monkeypatch.setattr(
            wt, "safe_request",
            lambda method, url, **kwargs: self._fake_response("text/html"))
        monkeypatch.setattr(wt, "validate_url", lambda url: None)
        out = WebFetchTool().execute("https://example.com/article")
        assert "Hello Web Body" in out

    def test_bing_search_skips_binary_response(self, monkeypatch):
        import tools.web_tools as wt

        monkeypatch.setattr(
            wt, "safe_request",
            lambda method, url, **kwargs: self._fake_response("application/pdf"))
        monkeypatch.setattr(wt, "validate_url", lambda url: None)
        assert WebSearchTool._search_bing("q", 5) == []

        # 文本响应仍走解析路径（无匹配项时返回空列表但确实尝试了解析）
        monkeypatch.setattr(
            wt, "safe_request",
            lambda method, url, **kwargs: self._fake_response(
                "text/html", "<html><body></body></html>"))
        assert WebSearchTool._search_bing("q", 5) == []


# ============================================================
# 10. memory_update action 枚举校验
# ============================================================

class TestMemoryUpdateEnumValidation:
    class StubManager:
        def __init__(self):
            self.calls = []

        def update_weight(self, memory_id, delta):
            self.calls.append((memory_id, delta))
            return True

    def test_invalid_action_rejected_without_manager_call(self):
        mgr = self.StubManager()
        tool = MemoryUpdateTool()
        tool.set_memory_manager(mgr)
        out = tool.execute(memory_id=1, action="helpful")  # 枚举外的似是而非值
        assert "非法" in out and "action" in out
        assert "useful" in out and "not_useful" in out  # 提示合法取值
        assert mgr.calls == []  # 未误当 not_useful 执行权重-1

    @pytest.mark.parametrize("bad_action", ["HELPFUL", "Useful", "", "delete", "up"])
    def test_various_invalid_actions(self, bad_action):
        mgr = self.StubManager()
        tool = MemoryUpdateTool()
        tool.set_memory_manager(mgr)
        out = tool.execute(memory_id=2, action=bad_action)
        assert "非法" in out
        assert mgr.calls == []

    def test_valid_actions_map_to_delta(self):
        mgr = self.StubManager()
        tool = MemoryUpdateTool()
        tool.set_memory_manager(mgr)
        tool.execute(memory_id=3, action="useful")
        tool.execute(memory_id=4, action="not_useful")
        assert mgr.calls == [(3, 1), (4, -1)]
