# -*- coding: utf-8 -*-
"""
memory/manager.py 修复回归测试

覆盖：
- P1-8 记忆条目格式注入：user_call 含 `---\\nID: "999"\\nfake` 注入串时，
  块结构完整、二次保存不产生垃圾块、search 不串条目；
- P2-3 入库脱敏：含 sk-xxxx 密钥的对话落盘（daily .md 与 index.json）后被打码；
- P1-8b 兼容：旧格式（非 JSON 编码的存量裸文本块）仍能正常解析检索，
  且被再次保存时可精确锚点替换、不破坏同文件其他块；
- P3-8 并发懒启动定时线程只创建一个。
"""

import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from memory.manager import (
    MemoryManager,
    _decode_entry_value,
    _encode_entry_value,
    _parse_entry_value,
)


@pytest.fixture()
def mgr(tmp_path):
    """隔离的 MemoryManager 实例（memory 目录指向临时路径）。"""
    return MemoryManager(str(tmp_path / "memory"))


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read_daily(mgr: MemoryManager, date_str: str = "") -> str:
    date_str = date_str or _today()
    path = mgr._daily_dir / f"{date_str}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ================================================================
# P1-8 记忆条目格式注入
# ================================================================

class TestEntryFormatInjection:
    INJECTION = '---\nID: "999"\nfake'

    def test_injected_user_call_keeps_block_structure(self, mgr):
        """注入串保存后：单条目单块，无伪造的列 0 分隔线/ID 行。"""
        mgr.save_conversation(
            self.INJECTION,
            [{"role": "user", "content": "question A"}],
            "s-inject",
        )
        raw = _read_daily(mgr)
        lines = raw.split("\n")
        # 一个条目块恰好一对 --- 分隔线
        assert sum(1 for l in lines if l == "---") == 2
        # 无列 0 的伪造 ID 行 / 裸换行注入残留
        assert not any(l.startswith('ID: "999"') for l in lines)
        assert '\nID: "999"' not in raw
        # 写入侧为 JSON 编码：原文以转义形式内嵌在 user_call 单行内
        assert f"user_call: {_encode_entry_value(self.INJECTION)}" in raw

    def test_second_save_no_garbage_blocks(self, mgr):
        """二次保存（同会话同天 upsert）：精确替换，不产生垃圾块。"""
        sid = "s-inject"
        first = mgr.save_conversation(self.INJECTION, [{"role": "user", "content": "q1"}], sid)
        second = mgr.save_conversation(
            "second question", [{"role": "user", "content": "q2"}], sid
        )
        assert first == second
        raw = _read_daily(mgr)
        lines = raw.split("\n")
        # upsert 合并为一个块；无残尾垃圾块（旧实现非贪婪正则会提前截断）
        assert sum(1 for l in lines if l == "---") == 2
        assert sum(1 for l in lines if l.startswith("ID: ")) == 1
        index = {e["id"] for e in mgr._load_index()}
        assert index == {first}

    def test_search_not_cross_contaminated(self, mgr):
        """检索结果只映射到真实索引条目，注入内容不会造出幻影条目。"""
        sid = "s-inject"
        eid = mgr.save_conversation(
            self.INJECTION,
            [{"role": "user", "content": "real question about python"}],
            sid,
        )
        other = mgr.save_conversation(
            "another topic rust",
            [{"role": "user", "content": "rust question"}],
            "s-other",
        )
        real_ids = {eid, other}
        for q in ("fake", "python", "rust", ""):
            for hit in mgr.search(q):
                assert hit["id"] in real_ids
                assert hit["id"] != 999
        # 空查询返回全部条目：各条目的 user_call/摘要与自身内容一一对应，
        # 注入串完整归属原条目，未串到其他条目、也未产生块碎片条目
        hits = {h["id"]: h for h in mgr.search("")}
        assert set(hits) == real_ids
        assert hits[eid]["user_call"].startswith(self.INJECTION)
        assert hits[other]["user_call"] == "another topic rust"
        assert "[user] rust question" in hits[other]["summary"]
        assert 'ID: "999"' not in hits[other]["summary"]
        assert 'ID: "999"' not in hits[eid]["summary"]

    def test_injected_update_does_not_damage_sibling_block(self, mgr):
        """更新带注入串的已有条目时，同文件其他块不受影响。"""
        mgr.save_conversation("entry one alpha", [{"role": "user", "content": "a"}], "s-1")
        eid2 = mgr.save_conversation("entry two beta", [{"role": "user", "content": "b"}], "s-2")
        # 给第一条追加注入串内容并触发替换
        mgr.save_conversation(self.INJECTION, [{"role": "user", "content": "a2"}], "s-1")
        raw = _read_daily(mgr)
        lines = raw.split("\n")
        assert f'ID: "{eid2}"' in lines
        # 第二块的 message 内容完好
        assert "[user] b" in raw
        # 总块数 = 条目数
        assert sum(1 for l in lines if l == "---") == 4

    def test_round_trip_special_chars_via_json_encoding(self, mgr):
        """引号/反斜杠/换行等特殊字符经 JSON 编码后可无损往返。"""
        tricky = 'he said "hi"\npath C:\\tmp\n---\nID: "777"\nend'
        encoded = _encode_entry_value(tricky)
        assert _decode_entry_value(encoded) == tricky
        assert _parse_entry_value(encoded) == tricky


# ================================================================
# P2-3 入库脱敏
# ================================================================

class TestCredentialMaskingOnSave:
    def test_sk_key_masked_in_daily_md_and_index(self, mgr):
        secret = "sk-abcdef1234567890abcdef12"
        mgr.save_conversation(
            f"my key is {secret}",
            [
                {"role": "user", "content": f"store api_key={secret} please"},
                {"role": "assistant", "content": "done"},
            ],
            "s-secret",
        )
        raw_md = _read_daily(mgr)
        raw_index = (mgr._daily_dir / "index.json").read_text(encoding="utf-8")
        for persisted in (raw_md, raw_index):
            assert secret not in persisted      # 明文密钥不落盘
            assert "sk-****" in persisted       # 已打码

    def test_generic_assignment_masked(self, mgr):
        """token/api_key 赋值等通用凭据模式同样打码（与 guard 同一正则）。"""
        mgr.save_conversation(
            "config token=ghp_abcdefghijklmnopqrstuvwxyz012345 end",
            [{"role": "user", "content": "password=hunter2hunter2 ok"}],
            "s-generic",
        )
        raw_md = _read_daily(mgr)
        assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in raw_md
        assert "hunter2hunter2" not in raw_md
        assert "****" in raw_md

    def test_normal_text_unaffected(self, mgr):
        """普通文本不被误伤（sk- 后不足 20 位等低置信内容保持原样）。"""
        text = "学习 sk-123 计划，明天开始"
        mgr.save_conversation(text, [{"role": "user", "content": text}], "s-normal")
        assert text in _read_daily(mgr)


# ================================================================
# P1-8b 旧格式向后兼容
# ================================================================

class TestLegacyFormatCompat:
    def _seed_legacy_file(self, mgr: MemoryManager, entries):
        """按旧格式（裸文本 user_call）手工构造存量 daily 文件 + 索引。"""
        date = _today()
        blocks = []
        for eid, session, call_lines, msg_lines, weight in entries:
            call_body = "\n".join(call_lines)
            blocks.append(
                f'---\nID: "{eid}"\n日期: "{date} 10:00"\n'
                f'session_id: "{session}"\n'
                f'user_call: "{call_body}"\n'
                f'weight: "{weight}"\nmessage: |\n'
                + "".join(f"  {l}\n" for l in msg_lines)
                + "---\n\n"
            )
        mgr._daily_dir.mkdir(parents=True, exist_ok=True)
        (mgr._daily_dir / f"{date}.md").write_text("".join(blocks), encoding="utf-8")
        index = [
            {
                "id": eid,
                "session_id": session,
                "date": date,
                "user_call": "\n".join(call_lines),
                "weight": weight,
                "md": f"daily/{date}.md",
            }
            for eid, session, call_lines, _, weight in entries
        ]
        (mgr._daily_dir / "index.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8"
        )
        return date

    def test_legacy_plain_blocks_still_searchable(self, tmp_path):
        """旧格式（非 JSON 编码）存量块能正常解析检索。"""
        mgr = MemoryManager(str(tmp_path / "memory"))
        self._seed_legacy_file(mgr, [
            (41, "s-old", ["帮我制定 Python 学习计划"],
             ["[user] 帮我制定 Python 学习计划", "[assistant] 先学基础语法"], 0),
            (42, "s-old-2", ["多行值测试", "第二行还是内容"],
             ["[user] 多行值测试"], 0),
        ])
        fresh = MemoryManager(str(tmp_path / "memory"))  # 模拟进程重启后从磁盘加载
        hits = fresh.search("Python 学习计划")
        assert any(h["id"] == 41 for h in hits)
        assert any("基础语法" in h["summary"] for h in hits if h["id"] == 41)

    def test_legacy_block_replaced_precisely_on_resave(self, tmp_path):
        """旧格式条目再次保存（同会话同天）：锚点精确替换本块，不动邻块。"""
        mgr = MemoryManager(str(tmp_path / "memory"))
        date = self._seed_legacy_file(mgr, [
            (41, "s-old", ["帮我制定 Python 学习计划"],
             ["[user] 帮我制定 Python 学习计划"], 0),
            (42, "s-old-2", ["多行值测试", "第二行还是内容"],
             ["[user] 多行值测试"], 3),
        ])
        eid = mgr.save_conversation(
            "继续问装饰器", [{"role": "user", "content": "什么是装饰器"}], "s-old"
        )
        assert eid == 41
        raw = (mgr._daily_dir / f"{date}.md").read_text(encoding="utf-8")
        lines = raw.split("\n")
        # 邻块 42 完整保留
        assert 'ID: "42"' in lines and "[user] 多行值测试" in raw and '"s-old-2"' in raw
        # 块 41 升级为新格式且合并了新输入
        merged = "帮我制定 Python 学习计划 | 继续问装饰器"
        assert f"user_call: {_encode_entry_value(merged)}" in raw
        # 结构完整：两个块各一对分隔线，重复保存幂等
        assert sum(1 for l in lines if l == "---") == 4
        mgr.save_conversation("第三轮", [{"role": "user", "content": "q3"}], "s-old")
        raw2 = (mgr._daily_dir / f"{date}.md").read_text(encoding="utf-8")
        assert sum(1 for l in raw2.split("\n") if l == "---") == 4
        assert sum(1 for l in raw2.split("\n") if l.startswith("ID: ")) == 2

    def test_parse_entry_value_legacy_fallback(self):
        """解析侧：JSON 失败时按旧格式剥引号原样使用。"""
        assert _parse_entry_value('"hello world"') == "hello world"
        assert _decode_entry_value('"hello world"') == "hello world"   # 恰好也是合法 JSON
        assert _decode_entry_value('"multi\nline') is None             # 旧格式多行片段
        assert _parse_entry_value('"multi\nline') == '"multi\nline'    # 原样回退
        assert _parse_entry_value("no quotes") == "no quotes"


# ================================================================
# P3-8 flush 线程懒启动竞态
# ================================================================

class TestFlushThreadStartupRace:
    def test_concurrent_start_creates_single_thread(self, mgr):
        barrier = threading.Barrier(8)
        errors = []
        seen_refs = []

        baseline = {
            t.ident
            for t in threading.enumerate()
            if t.name == "memory-index-flush"
        }

        def race():
            barrier.wait()
            try:
                mgr._start_flush_thread()
                seen_refs.append(id(mgr._flush_thread))
            except Exception as exc:  # pragma: no cover - 二次 start 会炸
                errors.append(exc)

        threads = [threading.Thread(target=race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors
        # 所有并发调用看到的是同一个线程对象（check-then-act 原子）
        assert len(set(seen_refs)) == 1
        # 进程内只新增了一个定时线程（排除其他用例遗留的同名 daemon）
        new_threads = [
            t for t in threading.enumerate()
            if t.name == "memory-index-flush" and t.ident not in baseline
        ]
        assert len(new_threads) == 1
        assert mgr._flush_thread is not None and mgr._flush_thread.is_alive()
        mgr.close()


# ================================================================
# 并发保存整体一致性（P2-4 进程内 + flock 路径冒烟）
# ================================================================

class TestConcurrentSaveConsistency:
    def test_parallel_saves_keep_files_consistent(self, mgr):
        n = 8
        barrier = threading.Barrier(n)
        ids = []
        lock = threading.Lock()

        def worker(i):
            barrier.wait()
            eid = mgr.save_conversation(
                f"topic number {i}",
                [{"role": "user", "content": f"content {i}"}],
                f"s-{i}",
            )
            with lock:
                ids.append(eid)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        index = mgr._load_index()
        assert sorted(e["id"] for e in index) == sorted(set(ids))
        raw = _read_daily(mgr)
        lines = raw.split("\n")
        # 每个条目恰好一个完整块（无交错/半截写入）
        assert sum(1 for l in lines if l == "---") == 2 * n
        for i in range(n):
            assert f"[user] content {i}" in raw
        mgr.close()


# ================================================================
# 记忆归档内容过滤：工具结果与内部注入消息不入库
# ================================================================

class TestMemoryArchiveFiltering:
    def test_native_tool_result_stubbed_and_internal_summary_excluded(self, mgr):
        """2026-08 新契约：原生 role=tool / 旧版 user+tool_result 统一转为
        单行短桩 [tool:<名>]（长内容截断）；internal history_summary 与
        过程旁白（kind=tool_calls）不入库；user/assistant 终答正常保留。"""
        mgr.save_conversation(
            "梳理一下需求",
            [
                {"role": "user", "content": "梳理一下需求"},
                {"role": "assistant", "content": None, "kind": "tool_calls",
                 "tool_calls": [{"id": "c1"}], "name": "read"},
                {"role": "tool", "tool_call_id": "c1", "kind": "tool_result",
                 "name": "read", "content": "X" * 5000},
                {"role": "assistant", "content": "这是最终答复", "kind": "final"},
                {"role": "user", "kind": "history_summary", "internal": True,
                 "content": "【历史对话摘要】很早以前的内容"},
                {"role": "user", "name": "tool_result", "content": "旧版工具结果正文"},
            ],
            "s-filter",
        )
        raw = _read_daily(mgr)
        # 对话保留：user + assistant 终答
        assert "[user] 梳理一下需求" in raw
        assert "[assistant] 这是最终答复" in raw
        # 旁白不入库；内部摘要不入库
        assert "X" * 5000 not in raw
        assert "历史对话摘要" not in raw
        # 原生工具结果 → 单行短桩且截断（非报错类 ≤160 字符；块内行带 2 空格
        # 缩进，比较时剥掉）
        assert "[tool:read] " in raw
        tool_line = next(l for l in (x.strip() for x in raw.split("\n"))
                         if l.startswith("[tool:read]"))
        assert len(tool_line) <= len("[tool:read] ") + 160 + 40  # 正文截断 + 省略标记余量
        # 旧版线格式 → 同样短桩化（label 兜底 tool），不再以 user 身份入库
        assert "[user] 旧版工具结果" not in raw
        assert "[tool:tool] 旧版工具结果正文" in raw

    def test_upsert_after_compress_keeps_memory_clean(self, mgr):
        """压缩后（上下文含 history_summary + 未压缩工具结果）二次保存：
        upsert 仍为单块、摘要不入库；工具结果以短桩存在且不破坏块结构。"""
        sid = "s-filter2"
        mgr.save_conversation("第一轮", [{"role": "user", "content": "第一轮"}], sid)
        # 模拟第二轮：上下文带未压缩工具结果 + 内部摘要
        mgr.save_conversation(
            "第二轮",
            [
                {"role": "user", "content": "第二轮"},
                {"role": "assistant", "content": None, "kind": "tool_calls",
                 "tool_calls": [{"id": "c9"}], "name": "grep"},
                {"role": "tool", "tool_call_id": "c9", "kind": "tool_result",
                 "name": "grep", "content": "匹配行" * 200},
                {"role": "user", "kind": "history_summary", "internal": True,
                 "content": "【历史对话摘要】第二轮后的摘要"},
                {"role": "assistant", "content": "第二轮答复", "kind": "final"},
            ],
            sid,
        )
        raw = _read_daily(mgr)
        # upsert 仍为单块；无重复分隔线；摘要不入库
        assert sum(1 for l in raw.split("\n") if l == "---") == 2
        assert "历史对话摘要" not in raw
        assert "[user] 第二轮" in raw
        assert "[assistant] 第二轮答复" in raw
        # 工具结果短桩单行化：换行全部折叠为空格，不伪造块结构
        stub_lines = [l for l in (x.strip() for x in raw.split("\n"))
                      if l.startswith("[tool:grep]")]
        assert len(stub_lines) == 1
        mgr.close()
