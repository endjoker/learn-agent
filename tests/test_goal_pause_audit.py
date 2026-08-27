# -*- coding: utf-8 -*-
"""Goal pause 联动取消 / dropped_rounds 审计 / 轮次等待超时兜底单测。

覆盖 JKagent 收官：
- pause 清除 current_task_id 并写 continuation.dropped_rounds 审计；
- pause_async 经注入的 cancel 钩子取消 current_task_id 对应的运行时任务；
- 被替代轮次（driver 侧）写 dropped_rounds（reason=superseded）；
- driver 的 wait 配置化超时（goal.round_wait_seconds）→ round_failed block。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core.goal import GoalRoundDriver, GoalRuntime
from core.runtime import RuntimeStore


class GoalPauseAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.tmp.name) / "g.db")
        self.runtime = GoalRuntime(self.store, publish=lambda *a, **k: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pause_records_dropped_round_and_clears_current_task(self):
        goal = self.runtime.create("s1", "objective")
        reserved = self.runtime.reserve_round(goal.goal_id, "task-1",
                                              expected_version=goal.version)
        paused = self.runtime.pause(goal.goal_id, expected_version=reserved.version)
        self.assertEqual(paused.status.value, "paused")
        self.assertIsNone(paused.current_task_id)
        dropped = paused.continuation.get("dropped_rounds") or []
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["task_id"], "task-1")
        self.assertEqual(dropped[0]["reason"], "paused")

    async def test_pause_async_cancels_current_task_via_hook(self):
        goal = self.runtime.create("s2", "objective")
        reserved = self.runtime.reserve_round(goal.goal_id, "task-2",
                                              expected_version=goal.version)
        cancelled = []

        async def cancel(task_id, reason):
            cancelled.append((task_id, reason))

        paused = await self.runtime.pause_async(
            goal.goal_id, expected_version=reserved.version, cancel=cancel)
        self.assertEqual(cancelled, [("task-2", "goal_paused")])
        self.assertEqual(paused.status.value, "paused")

    def test_record_dropped_round_appends_audit(self):
        goal = self.runtime.create("s3", "objective")
        updated = self.runtime.record_dropped_round(
            goal.goal_id, "task-3", round_no=2, reason="superseded",
            expected_version=goal.version)
        dropped = updated.continuation.get("dropped_rounds") or []
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["round"], 2)
        self.assertEqual(dropped[0]["reason"], "superseded")

    async def test_driver_blocks_on_round_wait_timeout(self):
        self.store.upsert_session("s4", "sess4")
        goal = self.runtime.create("s4", "objective", max_rounds=3)
        submitted = []

        async def submit(**kwargs):
            submitted.append(kwargs["task_id"])
            return kwargs["task_id"]

        async def wait(task_id):
            # 永不终态：驱动 round_wait 超时后应按 round_failed/timeout block
            await asyncio.Event().wait()

        driver = GoalRoundDriver(self.runtime, self.store, submit=submit, wait=wait,
                                 session_key=lambda _sid: "sess4", idle_delay=0.01)
        import core.goal.driver as driver_mod
        original = driver_mod._round_wait_seconds
        driver_mod._round_wait_seconds = lambda: 0.1
        try:
            driver.trigger(goal.goal_id)
            await asyncio.sleep(0.5)
            current = self.runtime.get(goal.goal_id)
            self.assertEqual(current.status.value, "blocked")
            self.assertEqual(current.blocked_reason["type"], "round_failed")
            self.assertEqual(current.blocked_reason["status"], "timeout")
        finally:
            driver_mod._round_wait_seconds = original
            await driver.stop()

    async def test_driver_audits_superseded_round(self):
        """wait 返回后 current_task_id 已被新一轮替代 → dropped_rounds(superseded)。"""
        self.store.upsert_session("s5", "sess5")
        goal = self.runtime.create("s5", "objective", max_rounds=3)
        submitted = []

        async def submit(**kwargs):
            submitted.append(kwargs["task_id"])
            return kwargs["task_id"]

        async def wait(task_id):
            # 模拟：本轮运行期间用户 pause→resume 已推进新一轮（current_task_id
            # 变为新任务）；这里直接返回成功结果，driver 应识别为被替代轮次。
            current = self.runtime.get(goal.goal_id)
            if current and current.current_task_id == task_id:
                self.runtime.pause(goal.goal_id, expected_version=current.version)
                resumed = self.runtime.resume(goal.goal_id)
                self.runtime.reserve_round(goal.goal_id, "next-round-task",
                                           expected_version=resumed.version)
            return type("R", (), {"status": type("S", (), {"value": "completed"})(),
                                   "summary": "done", "visible_text": "done"})()

        driver = GoalRoundDriver(self.runtime, self.store, submit=submit, wait=wait,
                                 session_key=lambda _sid: "sess5", idle_delay=0.01)
        driver.trigger(goal.goal_id)
        await asyncio.sleep(0.3)
        current = self.runtime.get(goal.goal_id)
        dropped = current.continuation.get("dropped_rounds") or []
        self.assertTrue(any(item.get("reason") == "superseded" for item in dropped))
        await driver.stop()


if __name__ == "__main__":
    unittest.main()
