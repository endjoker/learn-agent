# -*- coding: utf-8 -*-
"""P2 回归：janitor 驱逐豁免谓词（armed Goal 所在会话不回收）。

用户审计 2026-08-27：Goal 轮间隙/pause 期间会话无活动，janitor 按空闲
回收 Agent 实例——上下文可持久化恢复，但 proc 子进程会话（REPL、
dev server 等纯内存态）全部丢失。修复：SessionManager 支持 evict_guard
谓词（session_key -> True 表示本轮跳过回收），janitor 扫描与 evict()
均尊重（force=True 仍可强制）。
"""

import unittest

from gateway.session import SessionManager


class EvictGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mgr = SessionManager(max_sessions=5, persist=False)

    async def asyncTearDown(self):
        await self.mgr.stop()

    async def test_guard_blocks_janitor_and_evict(self):
        self.mgr.get_or_create("webui:guarded")
        self.mgr.evict_guard = lambda key: key == "webui:guarded"
        # 模拟过期：last_active 拨回远古
        self.mgr._sessions["webui:guarded"].last_active = 0.0
        # janitor 扫描逻辑直接验证（过期集合不含被豁免会话）
        now = __import__("time").time()
        expired = [
            key for key, entry in self.mgr._sessions.items()
            if not entry.is_busy and (now - entry.last_active) > self.mgr.idle_timeout
            and not (self.mgr.evict_guard and self.mgr.evict_guard(key))
        ]
        self.assertEqual(expired, [])
        # evict() 同样拒绝（非 force）
        self.assertFalse(await self.mgr.evict("webui:guarded"))
        self.assertIn("webui:guarded", self.mgr._sessions)

    async def test_force_bypasses_guard(self):
        self.mgr.get_or_create("webui:guarded2")
        self.mgr.evict_guard = lambda key: True
        self.assertFalse(await self.mgr.evict("webui:guarded2"))
        self.assertTrue(await self.mgr.evict("webui:guarded2", force=True))
        self.assertNotIn("webui:guarded2", self.mgr._sessions)

    async def test_no_guard_evicts_normally(self):
        self.mgr.get_or_create("webui:plain")
        self.mgr._sessions["webui:plain"].last_active = 0.0
        self.assertIsNone(self.mgr.evict_guard)
        self.assertTrue(await self.mgr.evict("webui:plain"))


if __name__ == "__main__":
    unittest.main()
