# -*- coding: utf-8 -*-
"""Zombie 迟到事件退役机制回归。

背景：硬超时隔离（_quarantine_after_timeout）与统一 runner 的停止/Steering
看门超时只把 Agent 从 entry 摘除，但旧 executor 线程里的 agent.run 可能仍在
产出事件；放行会让 bridge.ensure_turn 因"无活动 Turn"出队下一条排队消息建
新 Turn（或 start_turn 凭空建 Turn），僵尸的残余 delta/工具节点写进无辜的
新 Turn。修复 = Dispatcher.retire_agent 登记 run_id + event_sink 按归属丢弃。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from gateway.dispatcher import Dispatcher


def _bare_dispatcher(**attrs) -> Dispatcher:
    """绕过 __init__（依赖 session_mgr/agent_config 全家桶）的最小实例。"""
    d = Dispatcher.__new__(Dispatcher)
    d._retired_run_ids = set()
    d._retired_run_ids_cap = 1024
    d._timed_out_agents = {}
    d._zombie_watchers = set()
    for name, value in attrs.items():
        setattr(d, name, value)
    return d


class RetireAgentTests(unittest.TestCase):
    def test_retire_registers_run_and_sink_guard_drops(self):
        d = _bare_dispatcher()
        agent = SimpleNamespace(_run_id="run-a")
        other = SimpleNamespace(_run_id="run-b")

        self.assertFalse(d._run_is_retired(agent))
        retired_id = d.retire_agent(agent)
        self.assertEqual(retired_id, "run-a")
        self.assertTrue(d._run_is_retired(agent))
        # 其他 run 不受牵连
        self.assertFalse(d._run_is_retired(other))

    def test_retire_agent_without_run_id_is_noop(self):
        d = _bare_dispatcher()
        self.assertEqual(d.retire_agent(SimpleNamespace()), "")
        self.assertEqual(d.retire_agent(SimpleNamespace(_run_id="")), "")
        self.assertEqual(d._retired_run_ids, set())

    def test_cap_overflow_clears_instead_of_growing(self):
        d = _bare_dispatcher(_retired_run_ids_cap=4)
        for i in range(6):
            d.retire_agent(SimpleNamespace(_run_id=f"run-{i}"))
        self.assertLessEqual(len(d._retired_run_ids), 4)


class QuarantineRetiresTests(unittest.TestCase):
    """硬超时隔离路径必须先登记事件退役，再摘 entry。"""

    def test_quarantine_marks_run_retired(self):
        d = _bare_dispatcher()
        agent = SimpleNamespace(_run_id="run-zombie", session_key="webui:x")
        entry = SimpleNamespace(session_key="webui:x", agent=agent,
                                is_busy=True)
        loop = asyncio.new_event_loop()
        try:
            async def scenario():
                future = loop.create_future()
                future.set_result("late-reply")
                d._quarantine_after_timeout(entry, future, agent)
                # watcher 是后台任务：让出一轮事件循环让它跑完登记
                await asyncio.sleep(0)
            loop.run_until_complete(scenario())
        finally:
            loop.close()

        self.assertTrue(d._run_is_retired(agent))
        self.assertIsNone(entry.agent)
        self.assertFalse(entry.is_busy)


class SinkGuardSemanticsTests(unittest.TestCase):
    """event_sink 的守卫语义（与 _execute_agent_locked 内联实现同规则）。"""

    def test_events_from_retired_run_are_filtered(self):
        d = _bare_dispatcher()
        agent = SimpleNamespace(_run_id="run-live")
        seen: list[dict] = []

        def sink(event):
            if d._run_is_retired(agent):
                return
            seen.append(event)

        sink({"type": "text_delta"})
        d.retire_agent(agent)  # 看门超时/硬超时隔离触发
        sink({"type": "text_delta"})   # 僵尸残余事件 → 必须被丢
        sink({"type": "tool_execution_end"})

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["type"], "text_delta")


if __name__ == "__main__":
    unittest.main()
