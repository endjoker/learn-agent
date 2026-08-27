# -*- coding: utf-8 -*-
"""PlanExecutor 外层 except 兜底 + 孤儿任务 watchdog 单测。

覆盖 JKagent 收官 P1：
- run() 外层异常先把当前任务 finish_task(success=False, runtime_unhealthy)
  再 publish runtime_error，避免计划悬在 ACTIVE 且任务停留 IN_PROGRESS；
- 30s watchdog 扫描 ACTIVE 计划的孤儿任务并重触发 executor。

P1 追加（用户审计 2026-08-27）：等待安全网与任务信封预算对齐——
安全网此前写死 300s < 信封 1200s，合法长任务被提前 blocked 且 watchdog
反复空转重触发；修复后装配处透传真实验算，wait 安全网跟随预算（+30s 余量）。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from core.plan import PlanExecutor, PlanManager
from core.runtime import ArtifactStore, RuntimeStore, TaskResult, TaskStatus


class _BrokenArtifactStore:
    """create_text 抛错，模拟 executor 资源写路径故障。"""

    def create_text(self, **kwargs):
        raise RuntimeError("disk full")


class PlanExecutorGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.tmp.name) / "p.db")
        self.manager = PlanManager(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _approved_plan(self, session_id="s1"):
        plan = self.manager.create_preview(session_id, {
            "steps": [{"description": "one"}, {"description": "two"}],
        }, source_prompt="do all")
        plan = self.manager.approve(plan.plan_id, actor="automatic")
        return self.manager.activate(plan.plan_id)

    async def test_outer_except_finishes_task_unhealthy_then_publishes_error(self):
        plan = self._approved_plan()
        submitted = []
        published = []

        async def submit_task(**kwargs):
            submitted.append(kwargs["plan_task"].plan_task_id)
            return kwargs["task_id"]

        async def wait_task(task_id):
            return TaskResult(task_id=task_id, status=TaskStatus.COMPLETED,
                              visible_text="done", summary="done")

        executor = PlanExecutor(
            self.manager, submit_task=submit_task, wait_task=wait_task,
            publish=lambda action, payload: published.append((action, payload)),
            artifact_store=_BrokenArtifactStore())
        await executor.run(plan.plan_id)
        current = self.manager.get(plan.plan_id)
        first = current.tasks[0]
        # 资源写路径抛错 → 外层 except 先把当前任务置 blocked(runtime_unhealthy)
        self.assertEqual(first.status.value, "blocked")
        self.assertEqual(first.blocked_reason["type"], "runtime_unhealthy")
        # 再广播 runtime_error
        self.assertTrue(any(action == "runtime_error" for action, _ in published))

    async def test_wait_safety_net_follows_budget_not_hardcoded_300s(self):
        """P1 回归：安全网超时用构造参数（对齐信封预算），不再写死 300s。

        wait_task 挂起超过安全网 → 任务置 blocked(wait_task_timeout)；安全网
        若仍是 300s 而信封 1200s，本用例以 0.2s 安全网 + 0.5s 慢任务验证
        "安全网到点即 blocked、且等待时长≈安全网而非 300s"。"""
        plan = self._approved_plan("s-budget")
        started = time.monotonic()

        async def submit_task(**kwargs):
            return kwargs["task_id"]

        async def slow_wait(task_id):
            await asyncio.sleep(2.5)  # 慢于安全网（__init__ 下限钳到 1.0s）
            return TaskResult(task_id=task_id, status=TaskStatus.COMPLETED)

        published = []
        executor = PlanExecutor(
            self.manager, submit_task=submit_task, wait_task=slow_wait,
            publish=lambda action, payload: published.append(action),
            artifact_store=ArtifactStore(self.store),
            wait_task_timeout_seconds=1.0,
        )
        await executor.run(plan.plan_id)
        elapsed = time.monotonic() - started
        current = self.manager.get(plan.plan_id)
        first = current.tasks[0]
        self.assertEqual(first.status.value, "blocked")
        self.assertEqual(first.blocked_reason["type"], "wait_task_timeout")
        # 安全网到点即返回（远小于慢任务 2.5s 与旧默认 300s 的组合）
        self.assertLess(elapsed, 5.0)
        self.assertTrue(any(action == "task_failed" for action in published))

    def test_budget_seconds_helper_reads_dispatcher_config(self):
        """PlanRuntime._task_budget_seconds 读 dispatcher 信封预算；缺失回落 1200。"""
        from gateway.plan_runtime import PlanRuntime

        class _FakeDispatcher:
            def __init__(self, cfg, hard):
                self._task_runtime_config = cfg
                self._hard_timeout = hard
            def runtime_task_budget_seconds(self):
                try:
                    return int(self._task_runtime_config.get(
                        "default_timeout_seconds", self._hard_timeout))
                except (TypeError, ValueError):
                    return int(self._hard_timeout)

        class _FakeStore:
            pass

        d1 = _FakeDispatcher({"default_timeout_seconds": 6000}, 1200)
        rt1 = PlanRuntime.__new__(PlanRuntime)
        rt1.dispatcher = d1
        self.assertEqual(rt1._task_budget_seconds(), 6000)

        d2 = _FakeDispatcher({}, 1200)
        rt2 = PlanRuntime.__new__(PlanRuntime)
        rt2.dispatcher = d2
        self.assertEqual(rt2._task_budget_seconds(), 1200)

    async def test_watchdog_retriggers_orphan_plan(self):
        plan = self._approved_plan("s2")
        submitted = []
        published = []

        async def submit_task(**kwargs):
            submitted.append(kwargs["plan_task"].plan_task_id)
            return kwargs["task_id"]

        async def wait_task(task_id):
            return TaskResult(task_id=task_id, status=TaskStatus.COMPLETED,
                              visible_text="done", summary="done")

        executor = PlanExecutor(
            self.manager, submit_task=submit_task, wait_task=wait_task,
            publish=lambda action, payload: published.append(action),
            artifact_store=ArtifactStore(self.store))
        try:
            # executor 未在运行（_running 为空）但有 READY 任务 → watchdog 重触发
            await executor._watchdog_scan()
            await asyncio.sleep(0.05)
            current = self.manager.get(plan.plan_id)
            self.assertEqual(submitted, ["step_1", "step_2"])
            self.assertEqual(current.status.value, "completed")
        finally:
            executor.stop_watchdog()


if __name__ == "__main__":
    unittest.main()
