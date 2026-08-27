# -*- coding: utf-8 -*-
"""硬超时后隔离 Agent，防止并发复用仍在运行的实例（P0-1）。

线程池里的 agent.run() 无法被 asyncio 取消；硬超时应把 entry.agent 摘除，
下一轮才能懒创建全新 Agent，而不是与仍在跑的旧实例并发执行。
"""
import asyncio
import unittest
from types import SimpleNamespace

from gateway.dispatcher import Dispatcher, SessionManager


class HardTimeoutQuarantineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sessions = SessionManager(max_sessions=5, worker_pool_size=1,
                                       persist=False)
        self.dispatcher = Dispatcher(self.sessions, agent_config={})

    def _entry(self, agent=None):
        return SimpleNamespace(agent=agent, is_busy=True, session_key="webui:x")

    async def test_hard_timeout_detaches_agent_from_entry(self):
        entry = self._entry(agent=object())
        fut = asyncio.get_running_loop().create_future()

        self.dispatcher._quarantine_after_timeout(entry, fut, entry.agent)

        # 下一轮不能再拿到仍在运行的旧 Agent —— entry 已与旧实例解耦。
        self.assertIsNone(entry.agent)
        self.assertFalse(entry.is_busy)
        self.assertIn(id(entry), self.dispatcher._timed_out_agents)
        self.assertEqual(self.dispatcher._timed_out_agents[id(entry)][0], fut)

    async def test_quarantine_released_when_old_worker_finishes(self):
        entry = self._entry(agent=object())
        fut = asyncio.get_running_loop().create_future()
        self.dispatcher._quarantine_after_timeout(entry, fut, entry.agent)
        self.assertIn(id(entry), self.dispatcher._timed_out_agents)

        # 模拟旧 worker 线程真正结束（future 完成）→ 隔离项出队
        fut.set_result("done")
        await asyncio.sleep(0)  # 让 done callback 执行
        self.assertNotIn(id(entry), self.dispatcher._timed_out_agents)

    async def test_quarantine_survives_future_exception(self):
        entry = self._entry(agent=object())
        fut = asyncio.get_running_loop().create_future()
        self.dispatcher._quarantine_after_timeout(entry, fut, entry.agent)
        fut.set_exception(RuntimeError("worker crashed"))
        # done callback 里 we swallow exception；不得从回调抛
        await asyncio.sleep(0)
        self.assertNotIn(id(entry), self.dispatcher._timed_out_agents)


if __name__ == "__main__":
    unittest.main()
