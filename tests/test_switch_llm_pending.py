# -*- coding: utf-8 -*-
"""P2-9：运行中 switch_llm 不并发改共享状态——挂起为 pending，下一轮原子应用。"""
import unittest

from agent import Agent


class _Recorder:
    def __init__(self):
        self.applied = []
    def __call__(self, **kwargs):
        self.applied.append(dict(kwargs))


class SwitchLlmPendingTests(unittest.TestCase):
    def _agent(self, running=False):
        obj = object.__new__(Agent)
        obj._is_running = running
        obj._pending_llm_config = None
        obj._apply_llm_config = _Recorder()
        return obj

    def test_running_switch_is_deferred(self):
        agent = self._agent(running=True)
        agent.switch_llm(model="gpt-5")
        # 未立即应用，而是挂起。
        self.assertEqual(agent._apply_llm_config.applied, [])
        self.assertEqual(agent._pending_llm_config, {"model": "gpt-5"})

    def test_idle_switch_applies_immediately(self):
        agent = self._agent(running=False)
        agent.switch_llm(model="gpt-5")
        self.assertEqual(len(agent._apply_llm_config.applied), 1)
        self.assertEqual(agent._apply_llm_config.applied[0], {"model": "gpt-5"})
        self.assertIsNone(agent._pending_llm_config)

    def test_pending_consumed_at_next_run_start(self):
        agent = self._agent(running=True)
        agent.switch_llm(model="gpt-5", reasoning_level="high")
        # 本轮结束后，下一轮开始时原子应用并清空 pending。
        agent._consume_pending_llm()
        self.assertEqual(agent._apply_llm_config.applied, [
            {"model": "gpt-5", "reasoning_level": "high"}])
        self.assertIsNone(agent._pending_llm_config)

    def test_consume_pending_with_none_is_noop(self):
        agent = self._agent(running=False)
        agent._consume_pending_llm()  # nothing pending; no crash
        self.assertEqual(agent._apply_llm_config.applied, [])


if __name__ == "__main__":
    unittest.main()
