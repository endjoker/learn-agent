# -*- coding: utf-8 -*-
"""P4 回归：GoalRoundDriver 仲裁锁的资源卫生（prune_session_gates）。

长生命周期网关中 _session_gates 随历史会话数缓慢累积；会话删除/终态回收
时装配方调用 prune_session_gates 移除空闲锁（持锁中的不移除，防竞态）。
"""

import asyncio
import unittest

from core.goal.driver import GoalRoundDriver


class _StubGoalRuntime:
    def get(self, goal_id):
        return None


class PruneSessionGatesTests(unittest.IsolatedAsyncioTestCase):
    def _driver(self):
        return GoalRoundDriver(
            _StubGoalRuntime(), store=None, submit=None, wait=None,
            session_key=lambda sid: f"webui:{sid}")

    async def test_prune_removes_idle_gate_only(self):
        driver = self._driver()
        gate_busy = driver._session_gates.setdefault("webui:a", asyncio.Lock())
        await gate_busy.acquire()  # 模拟持锁中的轮次
        driver._session_gates.setdefault("webui:b", asyncio.Lock())
        removed = driver.prune_session_gates(["webui:a", "webui:b"])
        self.assertEqual(removed, 1)
        self.assertIn("webui:a", driver._session_gates)  # 持锁中不移除
        self.assertNotIn("webui:b", driver._session_gates)
        gate_busy.release()

    async def test_prune_unknown_session_is_noop(self):
        driver = self._driver()
        self.assertEqual(driver.prune_session_gates(["webui:none"]), 0)


if __name__ == "__main__":
    unittest.main()
