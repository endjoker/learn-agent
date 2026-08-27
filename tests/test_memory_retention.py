# -*- coding: utf-8 -*-
"""记忆保留期清理 + 内容形态改造回归（2026-08）。

清理规则（与用户对齐）：
- ≥15 天且 weight<0  → 删除（index 条目 + daily 块双删，空文件回收）
- ≥30 天且 weight==0 → 删除（从未被检索命中过的沉睡条目）
- ≥30 天且 weight>0  → 永久保留
- 日期解析失败        → fail-closed 保留
- save_conversation 尾部每日至多自动触发一次（config.last_cleanup_date 记账）

内容形态：只入 user / assistant 终答（≤2000）/ 工具短桩；assistant 过程
旁白与 internal 注入不入库；条目块 ≤8KB。
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from memory.manager import (
    _BLOCK_BUDGET_BYTES,
    MemoryManager,
)


@pytest.fixture()
def mgr(tmp_path):
    return MemoryManager(str(tmp_path / "memory"))


def _age_index_entry(mgr: MemoryManager, entry_id: int, days: float,
                     weight: int | None = None) -> None:
    """把条目完整地"变老"N 天：index 的 date/md 指向与 daily 块所在文件
    一同迁往过去日期（模拟真实的原位老化——写入时索引与块永远同日）。
    可选改权重。mtime 变化令管理器缓存失效，下次操作自动重建。"""
    entries = json.loads(mgr._index_path.read_text(encoding="utf-8"))
    entry = next(e for e in entries if e.get("id") == entry_id)
    stamp = time.time() - days * 86400
    new_date = time.strftime("%Y-%m-%d", time.localtime(stamp))
    if weight is not None:
        entry["weight"] = weight
    old_md = mgr._daily_dir / f"{entry.get('date', '')}.md"
    new_md = mgr._daily_dir / f"{new_date}.md"
    if old_md.name != new_md.name and old_md.exists():
        lines = old_md.read_text(encoding="utf-8").split("\n")
        span = MemoryManager._find_block_line_span(lines, entry_id)
        if span is not None:
            block_lines = lines[span[0]:span[1]]
            del lines[span[0]:span[1]]
            old_rest = "\n".join(lines).strip("\n").strip()
            if old_rest:
                old_md.write_text(old_rest + "\n\n", encoding="utf-8")
            else:
                old_md.unlink()
            merged = (new_md.read_text(encoding="utf-8").rstrip("\n") + "\n\n"
                      if new_md.exists() else "")
            new_md.write_text(merged + "\n".join(block_lines) + "\n\n",
                              encoding="utf-8")
    entry["date"] = new_date
    entry["md"] = f"daily/{new_date}.md"
    mgr._index_path.write_text(json.dumps(entries, ensure_ascii=False),
                               encoding="utf-8")


def _all_daily_text(mgr: MemoryManager) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in mgr._daily_dir.glob("*.md"))


def _ids(mgr: MemoryManager) -> set[int]:
    return {e.get("id") for e in json.loads(
        mgr._index_path.read_text(encoding="utf-8"))}


# ================================================================
# 清理矩阵
# ================================================================

class TestRetentionMatrix:
    def test_old_negative_removed_with_block(self, mgr):
        eid = mgr.save_conversation("旧烦恼", [{"role": "user", "content": "旧烦恼"}], "s1")
        _age_index_entry(mgr, eid, days=16, weight=-3)
        stats = mgr.cleanup()
        assert stats["removed_negative"] == 1
        assert eid not in _ids(mgr)
        # 双删：所有 daily .md 里都不再有该条目内容
        assert "旧烦恼" not in _all_daily_text(mgr)

    def test_young_negative_kept(self, mgr):
        eid = mgr.save_conversation("近期的负分", [{"role": "user", "content": "x"}], "s2")
        _age_index_entry(mgr, eid, days=5, weight=-9)
        mgr.cleanup()
        assert eid in _ids(mgr)

    def test_old_zero_removed_but_recent_kept(self, mgr):
        old = mgr.save_conversation("沉睡30天", [{"role": "user", "content": "a"}], "s3")
        recent = mgr.save_conversation("沉睡29天", [{"role": "user", "content": "b"}], "s4")
        _age_index_entry(mgr, old, days=31, weight=0)
        _age_index_entry(mgr, recent, days=29, weight=0)
        mgr.cleanup()
        assert old not in _ids(mgr)
        assert recent in _ids(mgr)

    def test_positive_weight_never_expires(self, mgr):
        eid = mgr.save_conversation("黄金记忆", [{"role": "user", "content": "c"}], "s5")
        _age_index_entry(mgr, eid, days=100, weight=7)
        mgr.cleanup()
        assert eid in _ids(mgr)

    def test_boundary_ages_exact_thresholds(self, mgr):
        neg = mgr.save_conversation("恰15天负分", [{"role": "user", "content": "n"}], "s6")
        zero = mgr.save_conversation("恰30天零分", [{"role": "user", "content": "z"}], "s7")
        _age_index_entry(mgr, neg, days=15, weight=-1)
        _age_index_entry(mgr, zero, days=30, weight=0)
        # 精确等于阈值天数时年龄含当日剩余秒数必然 > 阈值 → 视为到期删除
        stats = mgr.cleanup()
        assert stats["removed_negative"] == 1
        assert stats["removed_zero"] == 1


class TestCleanupSafety:
    def test_corrupt_date_fail_closed(self, mgr):
        eid = mgr.save_conversation("坏日期", [{"role": "user", "content": "d"}], "s8")
        entries = json.loads(mgr._index_path.read_text(encoding="utf-8"))
        for e in entries:
            if e.get("id") == eid:
                e["date"] = "not-a-date"
                e["weight"] = -10
        mgr._index_path.write_text(json.dumps(entries), encoding="utf-8")
        mgr.cleanup()
        assert eid in _ids(mgr)  # 宁可漏删不可误删

    def test_empty_daily_file_removed(self, tmp_path):
        m = MemoryManager(str(tmp_path / "m2"))
        # 同日两条负分老记忆 → 清理后该日 .md 应被回收
        for i, sid in enumerate(("a", "b")):
            eid = m.save_conversation(f"gone{i}", [{"role": "user", "content": f"g{i}"}], sid)
            _age_index_entry(m, eid, days=20, weight=-2)
        md = next(m._daily_dir.glob("*.md"))
        stats = m.cleanup()
        assert stats["removed_files"] == 1
        assert not md.exists()

    def test_missing_md_block_does_not_crash(self, mgr):
        eid = mgr.save_conversation("无块", [{"role": "user", "content": "e"}], "s9")
        _age_index_entry(mgr, eid, days=40, weight=-1)
        # 手工删掉 daily 文件模拟历史缺失
        for p in mgr._daily_dir.glob("*.md"):
            p.unlink()
        stats = mgr.cleanup()  # 不应抛异常
        assert stats["removed_negative"] == 1

    def test_explicit_cleanup_removable_outside_gate(self, mgr):
        """显式 cleanup() 不受每日一次闸门约束（运维/测试可反复调用）。"""
        eid = mgr.save_conversation("x", [{"role": "user", "content": "x"}], "sA")
        _age_index_entry(mgr, eid, days=16, weight=-5)
        assert mgr.cleanup()["removed_negative"] == 1
        eid2 = mgr.save_conversation("y", [{"role": "user", "content": "y"}], "sB")
        _age_index_entry(mgr, eid2, days=16, weight=-5)
        assert mgr.cleanup()["removed_negative"] == 1


class TestDailyAutoGate:
    def test_auto_cleanup_runs_once_per_day_on_save(self, mgr, monkeypatch):
        # 第一笔保存触发首次自动清理：清掉一笔预先埋入的老负分
        mgr2_first = True
        eid = mgr.save_conversation("首轮正常", [{"role": "user", "content": "ok"}], "sC")
        _age_index_entry(mgr, eid, days=20, weight=-4)
        # 再埋一笔 removable 并保存 → 首次闸门已在本实例生命周期关闭?
        # 注意：闸门以 config.last_cleanup_date 判断；首笔保存时 config 尚无
        # 记录 → 已触发过一次。第二笔 removable 应在下一笔保存时才可能清理。
        victim = mgr.save_conversation("victim", [{"role": "user", "content": "v"}], "sD")
        _age_index_entry(mgr, victim, days=20, weight=-4)
        saved = mgr.save_conversation("次日推进", [{"role": "user", "content": "n"}], "sE")
        # 闸门今天已记账 → victim 未被清
        assert victim in _ids(mgr)
        # 把记账日期改为昨天 → 再保存一笔即触发清理
        cfg = json.loads(mgr._config_path.read_text(encoding="utf-8"))
        cfg["last_cleanup_date"] = "2000-01-01"
        mgr._config_path.write_text(json.dumps(cfg), encoding="utf-8")
        mgr.save_conversation("再推一轮", [{"role": "user", "content": "m"}], "sF")
        assert victim not in _ids(mgr)
        cfg2 = json.loads(mgr._config_path.read_text(encoding="utf-8"))
        assert cfg2["last_cleanup_date"] != "2000-01-01"

    def test_cleanup_failure_never_breaks_save(self, mgr, monkeypatch):
        monkeypatch.setattr(mgr, "cleanup",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        eid = mgr.save_conversation("崩溃中", [{"role": "user", "content": "z"}], "sG")
        assert eid in _ids(mgr)  # 归档不受清理异常影响


# ================================================================
# 内容形态
# ================================================================

class TestContentShape:
    def test_final_only_assistant_and_internal_skip(self, mgr):
        msgs = [
            {"role": "user", "content": "问个问题"},
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [{"id": "c"}], "name": "read"},
            {"role": "assistant", "content": "已完成 Plan 的 step_1 旁白",
             "kind": "tool_calls"},
            {"role": "assistant", "content": "最终结论 A", "kind": "final"},
            {"role": "user", "internal": True, "content": "[系统] 你正在执行 step_2"},
        ]
        out = mgr._serialize_messages(msgs)
        assert "[user] 问个问题" in out
        assert "[assistant] 最终结论 A" in out
        assert "旁白" not in out and "step_2" not in out

    def test_long_answer_truncated_with_tail_marker(self, mgr):
        long_final = "结论" * 3000  # 6000 字符 > 2000
        out = mgr._serialize_messages([
            {"role": "assistant", "content": long_final, "kind": "final"},
        ])
        line = next(l for l in out.split("\n") if l.startswith("[assistant]"))
        body = line.removeprefix("[assistant] ")
        assert len(body) < 2100
        assert "截断" in body and len(long_final) > len(body)

    def test_tool_stub_error_gets_wider_budget(self, mgr):
        err = "❌ Traceback (most recent call last): " + "e" * 500
        ok = "y" * 500
        out = mgr._serialize_messages([
            {"role": "tool", "name": "bash", "content": ok},
            {"role": "tool", "name": "bash", "content": err},
        ])
        lines = [l for l in out.split("\n") if l.startswith("[tool:bash]")]
        assert all("\n" not in l for l in lines)
        lens = [len(l.removeprefix("[tool:bash] ")) for l in lines]
        assert max(lens[:1]) <= 160 + 60      # 常规 ≤160（+省略标记余量）
        assert any("Traceback" in l for l in lines)   # 报错头保留
        assert min(lens[1:]) <= 400 + 60      # 报错放宽档

    def test_block_budget_enforced(self, mgr):
        msgs = [
            {"role": "user", "content": "u" * 900},
            {"role": "assistant", "content": "a" * 2500, "kind": "final"},
            {"role": "tool", "name": "bash", "content": "t" * 100},
        ] * 10
        out = mgr._serialize_messages(msgs)
        assert len(out.encode("utf-8")) <= _BLOCK_BUDGET_BYTES + 256  # 标记余量
        assert "超出记忆块预算" in out

    def test_runtime_task_source_skips_archive(self, tmp_path):
        """B2：plan/goal/scheduler/subagent 轮整轮不入记忆。"""
        from agent import Agent  # 仅复用未绑定方法？——改为构造最小桩验证语义
        # 直接测守卫语义（不拉起完整 Agent）：模拟 agent 属性即可
        class _Stub:
            memory = object()
            _runtime_task_source = "plan"
            messages = []
            store = type("S", (), {"session_id": "x"})()

        stub = _Stub()
        # 调用未绑定方法（避免 create_agent 全家桶）
        Agent._save_memory(stub, "执行 step_1")
        assert stub.messages == []  # 早退，不产生任何写入副作用
