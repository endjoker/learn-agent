# -*- coding: utf-8 -*-
"""
Scheduler 看门狗与迟到回复单元测试。

覆盖：
- 看门 deadline = max(job.timeout + 60, hard_timeout + 30)；
- hard_timeout_seconds 同源读取（dispatcher.agent_config → 构造参数覆盖）；
- 看门触发清 _pending/_running_jobs 登记并按 error 收尾；
- 迟到回复（看门收尾后到达）不再静默丢弃：log warning + 尽力补投递；
- 公开只读快照 job_stats()/paused_jobs()（cron_tools 不再读私有字段）。
"""

import asyncio
import unittest

from gateway.channels.base import InboundMessage
from gateway.scheduler import Scheduler


def _make_scheduler(config=None, dispatcher=None, **kwargs) -> Scheduler:
    return Scheduler(config or {"jobs": []}, dispatcher, object(), **kwargs)


class _FakeDispatcher:
    """最小 dispatcher 桩：记录 on_inbound 与 announce 投递。"""

    def __init__(self, agent_config=None):
        self.agent_config = dict(agent_config or {})
        self.inbound = []
        self.sent = []
        self._channels = {}

    def register_channel(self, channel):
        self._channels[channel.name] = channel

    def channels(self):
        return self._channels

    async def on_inbound(self, msg):
        self.inbound.append(msg)


class WatchdogDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def test_deadline_is_max_of_job_timeout_and_hard_timeout(self):
        sched = _make_scheduler(dispatcher=_FakeDispatcher(
            agent_config={"hard_timeout_seconds": 1200}))
        # job.timeout + 60 更大 → 取 job 侧
        dl = sched._watchdog_deadline({"timeout": 3600}, fired_at=100.0)
        self.assertEqual(dl, 100.0 + 3600 + 60)
        # hard_timeout + 30 更大 → 取硬超时侧
        dl = sched._watchdog_deadline({"timeout": 10}, fired_at=100.0)
        self.assertEqual(dl, 100.0 + 1200.0 + 30)

    def test_hard_timeout_read_from_dispatcher_agent_config(self):
        # 同源：server.py 把 sessions.hard_timeout_seconds 注入 agent_config
        sched = _make_scheduler(dispatcher=_FakeDispatcher(
            agent_config={"hard_timeout_seconds": 900}))
        self.assertEqual(sched.hard_timeout_seconds, 900.0)

    def test_hard_timeout_constructor_override_wins(self):
        sched = _make_scheduler(
            dispatcher=_FakeDispatcher(agent_config={"hard_timeout_seconds": 900}),
            hard_timeout_seconds=300)
        self.assertEqual(sched.hard_timeout_seconds, 300.0)

    def test_hard_timeout_defaults_when_unavailable(self):
        class Bare:
            pass
        sched = _make_scheduler(dispatcher=Bare())
        self.assertEqual(sched.hard_timeout_seconds, 1200.0)
        # 非法值回退默认
        sched2 = _make_scheduler(
            dispatcher=_FakeDispatcher(agent_config={"hard_timeout_seconds": "x"}))
        self.assertEqual(sched2.hard_timeout_seconds, 1200.0)

    async def test_fire_registers_watchdog_with_computed_deadline(self):
        dispatcher = _FakeDispatcher()
        sched = _make_scheduler(dispatcher=dispatcher)
        await sched._fire({"name": "j1", "prompt": "p", "timeout": 5},
                          trigger="manual")
        (key, wd), = sched._watchdogs.items()
        self.assertAlmostEqual(
            wd["deadline"],
            wd["fired_at"] + max(5 + 60, sched.hard_timeout_seconds + 30),
            places=3)


class WatchdogSweepTests(unittest.IsolatedAsyncioTestCase):
    async def test_sweep_expires_and_clears_registration(self):
        dispatcher = _FakeDispatcher()
        sched = _make_scheduler(dispatcher=dispatcher)
        job = {"name": "j1", "prompt": "p", "session": "persist",
               "deliver": {"mode": "none"}}
        sched._pending["sched:j1"] = {"job": job, "trigger": "manual",
                                      "fired_at": 0.0}
        sched._running_jobs.add("j1")
        sched._watchdogs["sched:j1"] = {
            "job": job, "trigger": "manual", "fired_at": 0.0,
            "deadline": 0.0,  # 已到期
        }
        sched._sweep_watchdogs()
        # 登记（_pending/_running_jobs/_watchdogs）全部清理，防泄漏
        self.assertNotIn("sched:j1", sched._pending)
        self.assertNotIn("j1", sched._running_jobs)
        self.assertNotIn("sched:j1", sched._watchdogs)
        # 记入 _expired：迟到回复仍可识别
        self.assertIn("sched:j1", sched._expired)
        stats = sched.job_stats()
        self.assertEqual(stats["j1"]["last_status"], "error")


class LateReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_reply_is_delivered_not_dropped(self):
        dispatcher = _FakeDispatcher()

        class AnnounceChannel:
            name = "announce"

            async def send_to_chat(self, target, text):
                dispatcher.sent.append((target, text))

        dispatcher._channels["announce"] = AnnounceChannel()
        sched = _make_scheduler(dispatcher=dispatcher)
        job = {"name": "j1", "prompt": "p", "session": "persist",
               "deliver": {"mode": "announce", "channel": "announce",
                           "target": "oc_x"}}
        # 模拟看门已触发收尾：登记已清、_expired 有记录
        sched._expired["sched:j1"] = {"job": job}
        msg = InboundMessage(channel="scheduler", session_key="sched:j1",
                             user_id="scheduler", user_name="scheduler",
                             text="", message_id="m-late")
        with self.assertLogs("jk_agent.gateway", level="WARNING") as logs:
            await sched.on_reply(msg, "迟到的结果")
        # warning 而非静默丢弃
        self.assertTrue(any("迟到" in line for line in logs.output))
        # 尽力投递：announce 收到带前缀的回复
        self.assertEqual(len(dispatcher.sent), 1)
        target, text = dispatcher.sent[0]
        self.assertEqual(target, "oc_x")
        self.assertIn("迟到的结果", text)
        # 标记消费，重复回复不再投递
        await sched.on_reply(msg, "again")
        self.assertEqual(len(dispatcher.sent), 1)

    async def test_unregistered_reply_stays_debug_quiet(self):
        dispatcher = _FakeDispatcher()
        sched = _make_scheduler(dispatcher=dispatcher)
        msg = InboundMessage(channel="scheduler", session_key="sched:none",
                             user_id="scheduler", user_name="scheduler",
                             text="", message_id="m-x")
        with self.assertLogs("jk_agent.gateway", level="DEBUG") as logs:
            await sched.on_reply(msg, "hi")
        self.assertTrue(any("未登记" in line for line in logs.output))


class PublicReadonlySnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_stats_returns_snapshot_copy(self):
        sched = _make_scheduler(dispatcher=_FakeDispatcher())
        with sched._state_lock:
            sched._state.setdefault("jobs", {})["j1"] = {
                "runs": 2, "failures": 1}
        snap = sched.job_stats()
        self.assertEqual(snap["j1"]["runs"], 2)
        snap["j1"]["runs"] = 99          # 改副本不影响内部状态
        self.assertEqual(sched.job_stats()["j1"]["runs"], 2)

    def test_paused_jobs_returns_copy(self):
        sched = _make_scheduler(dispatcher=_FakeDispatcher())
        sched._paused.add("j1")
        paused = sched.paused_jobs()
        self.assertEqual(paused, {"j1"})
        paused.add("j2")                 # 改副本不影响内部状态
        self.assertEqual(sched.paused_jobs(), {"j1"})


class CronToolPublicApiTests(unittest.TestCase):
    def test_cron_list_tool_works_without_private_fields(self):
        """cron_list_jobs 经公开快照工作——假调度器不提供 _state/_paused。"""
        from tools.cron_tools import CronListJobsTool

        class PublicOnlyScheduler:
            jobs = [{"name": "j1", "schedule": "* * * * *",
                     "deliver": {"mode": "webhook"}}]

            def job_stats(self):
                return {"j1": {"last_status": "ok", "runs": 3, "failures": 0}}

            def paused_jobs(self):
                return set()

        tool = CronListJobsTool()
        # execute 经 get_scheduler() 取实例；注入模块级引用后验证输出
        import gateway.scheduler as sched_mod
        original = sched_mod.get_scheduler()
        sched_mod._scheduler_instance = PublicOnlyScheduler()
        try:
            out = tool.execute()
        finally:
            sched_mod._scheduler_instance = original
        self.assertIn("j1", out)
        self.assertIn("运行中", out)
        self.assertIn("ok", out)


if __name__ == "__main__":
    unittest.main()
