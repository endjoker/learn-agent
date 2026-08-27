# -*- coding: utf-8 -*-
"""stop_timeout 诊断 Fix-1 回归：stop_check 必须跨线程对池 worker 可见。

Bug 复盘（.jk/output/stop-timeout-diagnosis.md）：
  set_stop_check 写在调用方（loop）线程的 threading.local 里，而串行路径
  _execute_with_timeout 把 tool.execute 提交到工具池 worker 执行——
  run_killable 在 worker 线程轮询 get_stop_check() 恒为 None，bash 的
  「用户停止 → 0.2s 内杀进程组」快速路径完全失效，子进程只能自然跑完，
  10s 停止看门必然先到（turn_8eca86a3d1b3c738 的 stop_timeout）。

修复：_execute_with_timeout 的池提交处把 set/清除包进 worker 线程内执行。
这里钉死跨线程行为：loop 线程发起、worker 线程执行、外部置停止标志 →
秒级中断并标记 user_interrupted。
"""
import threading
import time
import unittest
from types import SimpleNamespace

from core.shell import run_killable
from core.runtime.tool_runtime import ToolRuntime


class _SlowBashTool:
    """模拟 BashTool：execute 在池 worker 线程内跑 run_killable。"""

    name = "slow_bash"
    capabilities = ("exec:shell",)

    def __init__(self):
        self.result = None

    def execute(self, **_kwargs):
        self.result = run_killable(["bash", "-c", "sleep 30"], timeout=60)
        return "done"


class StopCheckCrossThreadTests(unittest.TestCase):
    def _run_tool_then_stop(self):
        """loop 线程发起 _execute_with_timeout，稍后置停止标志。

        返回 (elapsed, tool)；修复前该用例必失败（sleep 30 跑满或被
        future.result(60s) 超时兜住，均远超断言阈值）。
        """
        rt = ToolRuntime(tool_timeout_seconds=60)
        agent = SimpleNamespace(_config={}, _stop_requested=False)
        tool = _SlowBashTool()
        holder: dict = {}

        def loop_thread():
            started = time.monotonic()
            observation, timeout_err = rt._execute_with_timeout(agent, tool, {})
            holder["elapsed"] = time.monotonic() - started
            holder["observation"] = observation
            holder["timeout_err"] = timeout_err

        worker = threading.Thread(target=loop_thread)
        worker.start()
        # 给池 worker 足够时间进入 run_killable 轮询（0.2s 粒度）
        time.sleep(0.8)
        agent._stop_requested = True  # 模拟用户点停止
        worker.join(timeout=15)
        return holder, tool

    def test_stop_requested_in_loop_thread_kills_pool_worker_subprocess(self):
        holder, tool = self._run_tool_then_stop()
        self.assertFalse(holder.get("timeout_err"),
                         f"不应走到工具超时兜底: {holder.get('timeout_err')}")
        self.assertLess(
            holder.get("elapsed", 999), 5,
            f"停止请求应在秒级中断池线程内的子进程，实际 {holder.get('elapsed', -1):.1f}s")
        self.assertTrue(getattr(tool.result, "user_interrupted", False),
                        "run_killable 应标记 user_interrupted")

    def test_worker_thread_observes_stop_check(self):
        """直接钉死边界：worker 线程内的 get_stop_check 与 loop 线程置入的一致。

        （诊断附录最小复现的反向断言——修复后 worker 不再读到 None。）
        """
        rt = ToolRuntime()
        agent = SimpleNamespace(_config={}, _stop_requested=False)
        seen: dict = {}
        gate = threading.Event()

        class _ProbeTool:
            name = "probe"
            capabilities = ()

            def execute(self, **_kwargs):
                from core.shell import get_stop_check
                seen["fn"] = get_stop_check()
                gate.set()
                return "ok"

        # 串行路径同款提交方式（_execute_with_timeout 内部逻辑）
        result, err = None, None
        def run():
            nonlocal result, err
            result, err = rt._execute_with_timeout(agent, _ProbeTool(), {})
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(gate.wait(timeout=5), "工具应在池线程内执行")
        thread.join(timeout=5)
        self.assertIsNone(err)
        self.assertIsNotNone(seen.get("fn"),
                             "worker 线程必须能读到 stop_check（修复点）")
        agent._stop_requested = True
        self.assertTrue(seen["fn"](), "回调应实时反映 agent 停止标志")

    def test_inflight_summary_tracks_and_clears(self):
        """Fix-3：在途工具摘要在执行期间可见、结束后清除（看门日志用）。"""
        rt = ToolRuntime(tool_timeout_seconds=60)
        agent = SimpleNamespace(_config={}, _stop_requested=False)
        started = threading.Event()
        release = threading.Event()

        class _HeldTool:
            name = "bash"
            capabilities = ("exec:shell",)

            def execute(self, **_kwargs):
                started.set()
                release.wait(timeout=10)
                return "ok"

        def loop_thread():
            rt._execute_with_timeout(agent, _HeldTool(), {"command": "sleep 45 && grep x"})

        worker = threading.Thread(target=loop_thread)
        worker.start()
        self.assertTrue(started.wait(timeout=5))
        summary = rt.current_execution_summary()
        self.assertIsNotNone(summary)
        self.assertIn("bash", summary)
        self.assertIn("sleep 45", summary)
        release.set()
        worker.join(timeout=10)
        self.assertIsNone(rt.current_execution_summary(),
                          "执行结束后摘要应清除，避免看门日志误报")


if __name__ == "__main__":
    unittest.main()


class ProcessGroupKillTests(unittest.TestCase):
    """stop_timeout Fix-2：request_stop 直通强杀在途进程组。

    run_killable 启动的进程组按 owner（agent id）登记；kill_owner_process_groups
    (owner) 从任意线程强杀该 owner 名下的存活组——不再依赖 0.2s 轮询先观察到
    停止标志，也不需要工具侧设置 stop_check。
    """

    def test_kill_owner_groups_terminates_inflight_subprocess(self):
        import time as _time
        from core import shell as shell_mod

        owner = 987654
        holder: dict = {}

        def worker():
            shell_mod.set_stop_owner(owner)
            started = _time.monotonic()
            holder["result"] = run_killable(["bash", "-c", "sleep 30"], timeout=60)
            holder["elapsed"] = _time.monotonic() - started

        worker_thread = threading.Thread(target=worker)
        worker_thread.start()
        _time.sleep(0.8)  # 让 Popen 完成并在册
        killed = shell_mod.kill_owner_process_groups(owner)
        worker_thread.join(timeout=10)
        self.assertFalse(worker_thread.is_alive())
        self.assertGreaterEqual(killed, 1, "应至少强杀一个在途进程组")
        self.assertLess(holder.get("elapsed", 999), 10,
                        f"强杀后 run_killable 应秒级返回，实际 {holder.get('elapsed', -1):.1f}s")
        self.assertEqual(holder["result"].returncode, -9)
        # 收尾注销：二次强杀应为 0（无残留登记）
        self.assertEqual(shell_mod.kill_owner_process_groups(owner), 0)
        shell_mod.set_stop_owner(None)

    def test_owner_isolation(self):
        """owner 隔离：A 的在途组不会被 kill_owner_process_groups(B) 误杀。"""
        import time as _time
        from core import shell as shell_mod

        owner_a, owner_b = 111111, 222222
        holder: dict = {}

        def worker():
            shell_mod.set_stop_owner(owner_a)
            holder["result"] = run_killable(["bash", "-c", "sleep 30"], timeout=60)

        worker_thread = threading.Thread(target=worker)
        worker_thread.start()
        _time.sleep(0.8)
        self.assertEqual(shell_mod.kill_owner_process_groups(owner_b), 0,
                         "B 的强杀不得触碰 A 的进程组")
        self.assertTrue(worker_thread.is_alive(), "A 的子进程应仍在运行")
        killed = shell_mod.kill_owner_process_groups(owner_a)
        self.assertGreaterEqual(killed, 1)
        worker_thread.join(timeout=10)
        self.assertFalse(worker_thread.is_alive())
        shell_mod.set_stop_owner(None)
