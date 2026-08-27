# -*- coding: utf-8 -*-
"""运行时后台任务（plan/goal）会话忙退避重试回归测试。

场景：LLM 在 agent.run 内调用 create_plan / goal 续跑工具派生子任务时，
父 run 仍持有 entry.exec_lock（生成收尾回复需要数秒~数十秒）。修复前
plan 首步立即被互斥拒绝 → "⚠️ 会话正忙：上一条消息仍在处理中" → 任务
按 AGENT_SESSION_BUSY 落 FAILED，用户看到"LLM 创建计划提示会话正忙"。

修复：_execute_runtime_task 对忙信号做有界退避重试，等父 run 释放锁后再
执行；超窗未拿到锁才按忙拒绝落终态（走既有失败分类）。
"""

import asyncio
from types import SimpleNamespace

import gateway.dispatcher as dispatcher_module
from core.runtime import CancellationToken, TaskStatus
from gateway.dispatcher import (
    SESSION_BUSY_REPLY,
    Dispatcher,
    _RUNTIME_BUSY_RETRY_MAX_DELAY_S,
    _RUNTIME_BUSY_RETRY_MAX_S,
)


async def _instant_sleep(_delay):
    return None


class _FakeClock:
    """每次 monotonic() 前进 1s：让重试窗口判定确定化、零真实等待。"""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        self.t += 1.0
        return self.t


def _make_dispatcher(replies, calls):
    """最小化 Dispatcher 桩：只装配 _execute_runtime_task 的依赖。"""
    d = object.__new__(Dispatcher)
    entry = SimpleNamespace(session_key="webui:t", is_busy=False,
                            last_active=0.0, agent=None)
    msg = SimpleNamespace(channel="webui", session_key="webui:t", message_id="m-1")
    envelope = SimpleNamespace(
        task_id="t-1", source="plan", plan_id="p-1", plan_task_id="pt-1",
        session_id="s-1",
        metadata={"deliver_reply": False, "final_response": False})
    d._runtime_messages = {"t-1": (msg, entry)}
    d._channels = {"webui": SimpleNamespace()}
    d._runtime_store = SimpleNamespace(save_channel_delivery=lambda **kw: None)
    d._conversation_bridge = None

    async def fake_execute_agent(entry, msg, channel, **kwargs):
        reply = replies[min(calls["n"], len(replies) - 1)]
        calls["n"] += 1
        return reply

    d._execute_agent = fake_execute_agent
    return d, envelope


def test_runtime_task_retries_while_session_busy(monkeypatch):
    """前两次忙 → 退避重试 → 第三次拿到锁执行成功：任务 COMPLETED。"""
    monkeypatch.setattr(dispatcher_module, "_RUNTIME_BUSY_RETRY_MAX_S", 30.0)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    clock = _FakeClock()
    monkeypatch.setattr(dispatcher_module.time, "monotonic", clock.monotonic)
    replies = [SESSION_BUSY_REPLY, SESSION_BUSY_REPLY, "✅ step_1 完成"]
    calls = {"n": 0}
    d, envelope = _make_dispatcher(replies, calls)

    result = asyncio.run(d._execute_runtime_task(envelope, CancellationToken()))

    assert calls["n"] == 3
    assert result.status == TaskStatus.COMPLETED
    assert result.visible_text == "✅ step_1 完成"
    assert result.error_code is None


def test_runtime_task_busy_retry_gives_up_after_window(monkeypatch):
    """持续忙直至超窗 → 保留忙提示，按 AGENT_SESSION_BUSY 落 FAILED。"""
    monkeypatch.setattr(dispatcher_module, "_RUNTIME_BUSY_RETRY_MAX_S", 2.5)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    clock = _FakeClock()
    monkeypatch.setattr(dispatcher_module.time, "monotonic", clock.monotonic)
    calls = {"n": 0}
    d, envelope = _make_dispatcher([SESSION_BUSY_REPLY], calls)

    result = asyncio.run(d._execute_runtime_task(envelope, CancellationToken()))

    # t=1,2 忙（< 2.5 重试）；t=3 超窗放弃
    assert calls["n"] == 3
    assert result.status == TaskStatus.FAILED
    assert result.error_code == "AGENT_SESSION_BUSY"
    assert result.visible_text == SESSION_BUSY_REPLY


def test_non_busy_reply_never_retries():
    """正常回复不触发重试（单次调用直接返回）。"""
    calls = {"n": 0}
    d, envelope = _make_dispatcher(["正常完成"], calls)
    result = asyncio.run(d._execute_runtime_task(envelope, CancellationToken()))
    assert calls["n"] == 1
    assert result.status == TaskStatus.COMPLETED


def test_retry_delay_is_backoff_capped():
    """退避序列 1→2→4→8→10（封顶 _RUNTIME_BUSY_RETRY_MAX_DELAY_S）。"""
    delays = []
    delay = 1.0
    for _ in range(6):
        delays.append(delay)
        delay = min(delay * 2.0, _RUNTIME_BUSY_RETRY_MAX_DELAY_S)
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    assert _RUNTIME_BUSY_RETRY_MAX_DELAY_S <= _RUNTIME_BUSY_RETRY_MAX_S
