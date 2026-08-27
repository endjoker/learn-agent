# -*- coding: utf-8 -*-
"""JKagent 收官：/metrics 指标注册表与 trace 覆盖针对性单测。

覆盖：
- gateway.metrics_registry：Counter/Gauge/Histogram 渲染、桶数学、
  标签转义、幂等注册、并发 observe 无竞态压测；
- dispatcher._GatewayMetrics：事件 data 嵌套读取修复（首 delta/guard）、
  guard rule_id 派生、LLM cache hit/miss 计数器、snapshot() JSON 不回归、
  Dispatcher.metrics_prometheus() 冒烟；
- core/debug.py formatter 复核：set_run_context 注入 [run_id/turn_id]，
  asyncio.to_thread 自动继承 contextvars（异步边界验证），
  原生态线程不继承（解释 event_sink 边界重新应用的必要性）。
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from gateway.metrics_registry import (
    Counter, DEFAULT_BUCKETS, Gauge, Histogram, MetricsRegistry,
)
from gateway.dispatcher import (
    Dispatcher, _GatewayMetrics, _derive_guard_rule,
    _is_guard_denial_result,
)
from core.debug import _RunContextFormatter, clear_run_context, set_run_context


class RegistryRenderingTests(unittest.TestCase):
    """渲染结构：# HELP / # TYPE / 样本行；标签与转义。"""

    def test_counter_render(self):
        reg = MetricsRegistry()
        c = reg.counter("jkagent_turns_total", "累计 Turn 数")
        c.inc()
        c.inc(3)
        text = reg.render_prometheus()
        self.assertIn("# HELP jkagent_turns_total 累计 Turn 数", text)
        self.assertIn("# TYPE jkagent_turns_total counter", text)
        self.assertIn("jkagent_turns_total 4", text)

    def test_labeled_counter_render_and_escape(self):
        reg = MetricsRegistry()
        c = reg.counter("jkagent_guard_interceptions_total", "guard 拦截",
                        labelnames=("rule_id",))
        c.inc(labels={"rule_id": "hard.high_risk_command"})
        c.inc(labels={"rule_id": 'a"b\\c'})
        text = reg.render_prometheus()
        self.assertIn('rule_id="hard.high_risk_command"} 1', text)
        # 标签值转义：双引号 -> \"，反斜杠 -> \\
        self.assertIn('rule_id="a\\"b\\\\c"} 1', text)

    def test_gauge_set_inc_dec(self):
        reg = MetricsRegistry()
        g = reg.gauge("jkagent_uptime_seconds", "运行秒数")
        g.set(10.5)
        g.inc(2)
        g.dec(0.5)
        self.assertIn("jkagent_uptime_seconds 12", reg.render_prometheus())

    def test_histogram_render_structure(self):
        reg = MetricsRegistry()
        h = reg.histogram("jkagent_turn_duration_seconds", "Turn 耗时")
        h.observe(0.1)
        h.observe(99.0)
        text = reg.render_prometheus()
        self.assertIn("# TYPE jkagent_turn_duration_seconds histogram", text)
        self.assertIn('le="0.05"', text)
        self.assertIn('le="+Inf"', text)
        self.assertIn("jkagent_turn_duration_seconds_sum 99.1", text)
        self.assertIn("jkagent_turn_duration_seconds_count 2", text)

    def test_render_empty_registry(self):
        self.assertEqual(MetricsRegistry().render_prometheus(), "")

    def test_deterministic_family_order(self):
        reg = MetricsRegistry()
        reg.counter("z_last", "").inc()
        reg.counter("a_first", "").inc()
        text = reg.render_prometheus()
        self.assertLess(text.index("a_first"), text.index("z_last"))

    def test_invalid_names(self):
        reg = MetricsRegistry()
        with self.assertRaises(ValueError):
            reg.counter("非法-名称", "")
        with self.assertRaises(ValueError):
            reg.counter("ok", "", labelnames=("bad-label",))

    def test_counter_negative_rejected(self):
        c = Counter("c", "")
        with self.assertRaises(ValueError):
            c.inc(-1)

    def test_registry_idempotent_and_conflict(self):
        reg = MetricsRegistry()
        c1 = reg.counter("x", "help1")
        c2 = reg.counter("x", "help2")
        self.assertIs(c1, c2)  # 幂等复用
        with self.assertRaises(ValueError):
            reg.gauge("x", "")  # 类型冲突
        with self.assertRaises(ValueError):
            reg.counter("x", "", labelnames=("a",))  # 标签不一致


class HistogramMathTests(unittest.TestCase):
    """桶数学：累积计数、sum/count、+Inf、自定义桶。"""

    def test_bucket_placement_cumulative(self):
        h = Histogram("h", "", buckets=(0.05, 0.1, 1.0))
        h.observe(0.03)   # le=0.05
        h.observe(0.1)    # le=0.1
        h.observe(0.5)    # le=1.0
        h.observe(2.0)    # +Inf
        cell = h._cells[()]
        self.assertEqual(cell["cum"], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(cell["count"], 4.0)
        self.assertAlmostEqual(cell["sum"], 0.03 + 0.1 + 0.5 + 2.0)

    def test_default_buckets(self):
        self.assertEqual(DEFAULT_BUCKETS,
                         (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))

    def test_histogram_with_labels(self):
        reg = MetricsRegistry()
        h = reg.histogram("h", "", buckets=(0.1, 1.0), labelnames=("k",))
        h.observe(0.05, labels={"k": "a"})
        text = reg.render_prometheus()
        self.assertIn('h_bucket{le="0.1",k="a"} 1', text)
        self.assertIn('h_count{k="a"} 1', text)

    def test_bad_buckets(self):
        # 0.0 是合法桶（Prometheus 允许非负边界）；负桶边界非法
        reg = MetricsRegistry()
        h = reg.histogram("h", "", buckets=(0.0, 1.0))
        h.observe(0.0)
        self.assertIn('h_bucket{le="0.0"} 1', reg.render_prometheus())
        with self.assertRaises(ValueError):
            Histogram("h", "", buckets=(-1.0, 1.0))


class ConcurrencyStressTests(unittest.TestCase):
    """并发 observe/inc 压测：精确总量无竞态（agent 主线程/并行工具线程/
    事件循环线程并发写入口的等价场景）。"""

    def test_parallel_observe_no_race(self):
        reg = MetricsRegistry()
        counter = reg.counter("c_total", "")
        hist = reg.histogram("h_seconds", "", buckets=(0.1, 0.5, 1.0))
        n_threads, n_obs = 8, 2000
        barrier = threading.Barrier(n_threads)

        def worker(seed):
            barrier.wait()  # 尽量同时起跑
            for i in range(n_obs):
                value = (i % 3) * 0.4 + seed * 0.01
                counter.inc()
                hist.observe(value)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected_total = n_threads * n_obs
        text = reg.render_prometheus()
        self.assertIn(f"c_total {expected_total}", text)
        self.assertIn(f"h_seconds_count {expected_total}", text)
        self.assertIn(f'h_seconds_bucket{{le="+Inf"}} {expected_total}', text)
        # 桶单调不减，+Inf 桶等于总量
        lines = {ln.split()[0]: float(ln.split()[1])
                 for ln in text.splitlines()
                 if ln.startswith("h_seconds_bucket")}
        bucket_vals = [lines[f'h_seconds_bucket{{le="{b}"}}']
                       for b in (0.1, 0.5, 1.0, "+Inf")]
        self.assertEqual(bucket_vals, sorted(bucket_vals))
        self.assertEqual(bucket_vals[-1], float(expected_total))
        # sum 精确核对
        expected_sum = sum(
            ((i % 3) * 0.4 + t * 0.01)
            for t in range(n_threads) for i in range(n_obs))
        sum_line = [ln for ln in text.splitlines()
                    if ln.startswith("h_seconds_sum ")][0]
        self.assertAlmostEqual(float(sum_line.split()[1]), expected_sum,
                               places=6)

    def test_parallel_mixed_guard_labels(self):
        reg = MetricsRegistry()
        c = reg.counter("g_total", "", labelnames=("rule_id",))
        rules = ["a", "b", "c"]
        barrier = threading.Barrier(3)

        def worker(rule):
            barrier.wait()
            for _ in range(1000):
                c.inc(labels={"rule_id": rule})

        threads = [threading.Thread(target=worker, args=(r,)) for r in rules]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        text = reg.render_prometheus()
        for rule in rules:
            self.assertIn(f'g_total{{rule_id="{rule}"}} 1000', text)


class GatewayMetricsIntegrationTests(unittest.TestCase):
    """_GatewayMetrics：事件 data 嵌套读取（修复前 event.get 读到顶层恒空）、
    guard rule_id、cache hit/miss、JSON snapshot 不回归。"""

    def _metrics_with_one_turn(self):
        m = _GatewayMetrics()
        m.note_inbound()
        m.note_turn_start()
        turn_state = {
            "turn_started": time.monotonic() - 2.3,
            "first_delta": None,
            "delta_events": 0,
            "inbound_ts": time.monotonic() - 3.1,
        }
        return m, turn_state

    def test_delta_and_guard_from_event_data(self):
        # 事件结构 {"type": ..., "data": {...}}——修复前读取顶层字段恒为空
        m, ts = self._metrics_with_one_turn()
        m.observe_agent_event(ts, {"type": "text_delta", "data": {"text": "你好"}})
        m.observe_agent_event(ts, {"type": "reasoning_delta",
                                   "data": {"text": "思考中"}})
        m.observe_agent_event(ts, {"type": "tool_execution_end", "data": {
            "result": "⛔ 已拒绝: tool is disabled for this task execution mode",
            "is_error": True}})
        m.observe_agent_event(ts, {"type": "tool_execution_end", "data": {
            "result": "⛔ 已拒绝: 检测到高危命令，已拒绝: rm -rf /",
            "is_error": True}})
        text = m.render_prometheus()
        # 首 delta 2.3s -> le=2.5 桶
        self.assertIn('jkagent_first_delta_seconds_bucket{le="2.5"} 1', text)
        self.assertIn(
            'jkagent_inbound_to_first_delta_seconds_bucket{le="5.0"} 1', text)
        self.assertIn("jkagent_delta_events_total 2", text)
        self.assertIn('rule_id="task_mode_disabled"} 1', text)
        self.assertIn('rule_id="hard.high_risk_command"} 1', text)
        snap = m.snapshot()
        self.assertEqual(snap["guard_interceptions"], 2)
        self.assertEqual(snap["delta"]["events"], 2)
        self.assertEqual(snap["first_delta_ms"]["count"], 1)

    def test_guard_denial_detection_and_rule(self):
        self.assertTrue(_is_guard_denial_result("⛔ 已拒绝: bash 需要审批"))
        self.assertFalse(_is_guard_denial_result("⛔ hook 拦截: xxx"))
        self.assertFalse(_is_guard_denial_result("❌ 工具出错: boom"))
        self.assertEqual(_derive_guard_rule(
            "⛔ 已拒绝: tool is disabled for this task execution mode"),
            "task_mode_disabled")
        self.assertEqual(_derive_guard_rule(
            "⛔ 已拒绝: 检测到高危命令，已拒绝: rm"),
            "hard.high_risk_command")
        self.assertEqual(_derive_guard_rule(
            "⛔ SecurityGate 拒绝最终调用: 只读模式禁止执行命令、代码或管理进程"),
            "mode.readonly.execution")
        self.assertEqual(_derive_guard_rule("⛔ 已拒绝: 未知原因"),
                         "permission.other")

    def test_llm_cache_hit_miss_counters(self):
        m, ts = self._metrics_with_one_turn()
        m.note_turn_end(duration_s=1.0, failed=False, usage={
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20,
        }, flush_lag_s=None)
        text = m.render_prometheus()
        self.assertIn("jkagent_llm_cache_hit_total 80", text)
        self.assertIn("jkagent_llm_cache_miss_total 20", text)
        self.assertIn("jkagent_turn_duration_seconds_count 1", text)
        snap = m.snapshot()
        self.assertEqual(snap["llm"]["usage"]["prompt_cache_hit_tokens"], 80)
        self.assertEqual(snap["llm"]["usage"]["prompt_cache_miss_tokens"], 20)

    def test_failed_turn_counter(self):
        m, ts = self._metrics_with_one_turn()
        m.note_turn_end(duration_s=0.5, failed=True, usage=None,
                        flush_lag_s=None)
        self.assertIn("jkagent_turns_failed_total 1", m.render_prometheus())
        self.assertEqual(m.snapshot()["turns"]["failed"], 1)

    def test_dispatcher_metrics_prometheus_smoke(self):
        d = Dispatcher(session_mgr=None)
        try:
            text = d.metrics_prometheus()
            self.assertIn("# TYPE jkagent_turns_total counter", text)
            self.assertIn("# TYPE jkagent_uptime_seconds gauge", text)
            self.assertIn("# TYPE jkagent_turn_duration_seconds histogram", text)
            # JSON 快照仍可用（/health 不回归）
            snap = d.metrics()
            self.assertIn("turns", snap)
            self.assertIn("guard_interceptions", snap)
        finally:
            if d._long_task_executor is not None:
                d._long_task_executor.shutdown(wait=False, cancel_futures=True)


class RunContextFormatterTests(unittest.TestCase):
    """core/debug.py formatter 复核（只读验证，不触碰 setup_logging 全局态）：
    - _RunContextFormatter 在 set_run_context 后追加 [run_id/turn_id]；
    - asyncio.to_thread 自动继承 contextvars（异步边界 ✓ 无需额外处理）；
    - 原生态线程不继承 contextvars（证明 dispatcher event_sink 边界
      重新应用 run/turn 上下文的必要性）。"""

    def setUp(self):
        self._buf = io.StringIO()
        self._logger = logging.getLogger("jk_agent.test_runctx")
        self._logger.handlers = []
        self._logger.propagate = False
        self._logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(self._buf)
        handler.setFormatter(_RunContextFormatter("%(levelname)s %(message)s"))
        self._logger.addHandler(handler)

    def tearDown(self):
        clear_run_context()
        self._logger.handlers = []

    def test_formatter_appends_run_id_when_context_set(self):
        self._logger.info("无上下文")
        self.assertIn("无上下文", self._buf.getvalue())
        self.assertNotIn("run_abc", self._buf.getvalue())
        set_run_context("run_abc", "turn_1")
        self._logger.info("带上下文")
        self.assertIn("带上下文 [run_abc/turn_1]", self._buf.getvalue())
        clear_run_context()
        self._logger.info("已清空")
        self.assertIn("已清空", self._buf.getvalue())
        self.assertNotIn("run_abc", self._buf.getvalue().split("已清空")[1])

    def test_override_and_turn_only(self):
        set_run_context("run_1", "turn_a")
        set_run_context("run_2")  # 覆盖 run_id，turn 保持
        self._logger.info("覆盖")
        self.assertIn("覆盖 [run_2/turn_a]", self._buf.getvalue())

    def test_asyncio_to_thread_propagates_context(self):
        set_run_context("run_thread", "turn_9")

        def log_in_thread():
            self._logger.info("to_thread 内日志")
            return True

        async def main():
            return await asyncio.to_thread(log_in_thread)

        self.assertTrue(asyncio.run(main()))
        self.assertIn("to_thread 内日志 [run_thread/turn_9]",
                      self._buf.getvalue())

    def test_raw_thread_does_not_propagate(self):
        # 原生态线程不继承 contextvars：这正是 dispatcher event_sink 在
        # 并行工具线程边界重新应用 set_run_context 的原因。
        set_run_context("run_raw", "turn_2")
        result = {}

        def log_in_raw_thread():
            self._logger.info("原生态线程日志")
            result["done"] = True

        t = threading.Thread(target=log_in_raw_thread)
        t.start()
        t.join()
        self.assertTrue(result.get("done"))
        self.assertIn("原生态线程日志", self._buf.getvalue())
        self.assertNotIn("[run_raw/turn_2]", self._buf.getvalue())
        # event_sink 边界重新应用后（等价代码路径）：线程内先 set 再 log
        def log_with_reapply():
            set_run_context("run_raw", "turn_2")
            self._logger.info("重新应用后日志")
        t2 = threading.Thread(target=log_with_reapply)
        t2.start()
        t2.join()
        self.assertIn("重新应用后日志 [run_raw/turn_2]", self._buf.getvalue())

    def test_tpool_worker_does_not_propagate(self):
        # ThreadPoolExecutor 线程同样不继承（并行工具段所在线程池）
        set_run_context("run_pool", "turn_3")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(
                lambda: self._logger.info("池线程日志")).result()
        self.assertIn("池线程日志", self._buf.getvalue())
        self.assertNotIn("[run_pool/turn_3]", self._buf.getvalue())


if __name__ == "__main__":
    unittest.main()

