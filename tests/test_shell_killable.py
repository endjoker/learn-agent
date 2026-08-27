# -*- coding: utf-8 -*-
"""P0-2：可终止的 subprocess/进程组（run_killable）。

subprocess.run(timeout=N) 只 kill 直接子进程，会留下孙进程。run_killable 以
start_new_session 启动子进程使其成为进程组组长，超时后 SIGKILL 整个进程组。
这里验证基本行为与超时触发（不依赖脆弱的进程探测）。
"""
import subprocess
import unittest

from core.shell import run_killable


class RunKillableTests(unittest.TestCase):
    def test_happy_path_returns_stdout(self):
        result = run_killable(["bash", "-c", "echo hi"], timeout=5)
        self.assertEqual(result.stdout.strip(), "hi")
        self.assertEqual(result.returncode, 0)

    def test_timeout_raises_subprocess_timeoutexpired(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_killable(["bash", "-c", "sleep 3"], timeout=1)

    def test_stop_check_interrupts_long_running_command(self):
        """停止直通：stop_check 命中 → 立即杀进程组返回 user_interrupted，
        不等满 timeout（用户停止不再被 1200s subprocess_timeout 拖住）。"""
        import time as _time
        from core.shell import set_stop_check

        calls = {"n": 0}

        def stop_after_two_polls():
            calls["n"] += 1
            return calls["n"] > 2

        set_stop_check(stop_after_two_polls)
        try:
            started = _time.monotonic()
            result = run_killable(["bash", "-c", "sleep 30"], timeout=60)
            elapsed = _time.monotonic() - started
        finally:
            set_stop_check(None)
        self.assertTrue(getattr(result, "user_interrupted", False),
                        "应标记 user_interrupted")
        self.assertLess(elapsed, 10, f"应秒级中断，实际 {elapsed:.1f}s")
        # 进程组确实被杀：sleep 30 不会留下存活进程（无法直接断言 pid，
        # 以快速返回 + 标记为证）。

    def test_stop_check_none_keeps_normal_behavior(self):
        """未设置 stop_check（None）时行为与原先完全一致。"""
        result = run_killable(["bash", "-c", "echo ok"], timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(getattr(result, "user_interrupted", False))


if __name__ == "__main__":
    unittest.main()
