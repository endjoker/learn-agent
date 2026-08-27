# -*- coding: utf-8 -*-
"""P2：Plan/Goal 子任务实时活动环形缓冲 + runtime.progress 心跳 + 状态注入测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.goal import GoalRuntime
from core.plan import PlanManager
from core.runtime import RuntimeStore, TaskEnvelope, TaskStatus
from gateway.conversation.bridge import ConversationBridge
from gateway.conversation.service import ConversationService
from gateway.conversation.store import ConversationStore
from gateway.dispatcher import Dispatcher
from gateway.webui.runtime_tools import GetRuntimeStatusTool
from gateway.webui.workspace_store import WorkspaceDatabase


def _msg(session_key="webui:default", plan_id="", goal_id=""):
    meta = {}
    if plan_id:
        meta["plan_id"] = plan_id
    if goal_id:
        meta["goal_id"] = goal_id
    return SimpleNamespace(session_key=session_key, metadata=meta,
                           message_id="m1", channel="webui")


class RuntimeBufferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.tmp.name) / "b.db")
        self.goal_runtime = GoalRuntime(self.store, publish=lambda *a, **k: None)
        self.conversation_service = ConversationService(
            ConversationStore(WorkspaceDatabase(runtime_store=self.store)),
            lambda event_type, payload: None, max_global_turns=100)
        self.bridge = ConversationBridge(self.conversation_service)
        self.published = []
        self.bridge.runtime_progress_publish = self.published.append

    def tearDown(self):
        self.tmp.cleanup()

    def _emit_tool(self, plan_id: str, *, event: str, call_id: str,
                   status: str | None = None) -> None:
        msg = _msg(plan_id=plan_id)
        data = {"tool_call_id": call_id, "tool": "bash"}
        if event == "tool_call_end":
            data["arguments"] = {"cmd": "ls"}
        if status is not None:
            data["status"] = status
        self.bridge._write_runtime_event_to_parent(
            msg, "plan", plan_id, event, data)

    def test_tool_start_end_updates_same_call_id(self):
        plan_id = "plan-1"
        self._emit_tool(plan_id, event="tool_call_start", call_id="c1")
        self._emit_tool(plan_id, event="tool_call_end", call_id="c1")
        recent = self.bridge.recent_runtime_activity(plan_id, limit=10)
        # start+end 同 call_id 原位更新 → 只有一条 done 记录
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["call_id"], "c1")
        self.assertEqual(recent[0]["status"], "done")
        self.assertEqual(recent[0]["tool"], "bash")

    def test_buffer_scoped_by_runtime_id(self):
        self.bridge._buffer_runtime_activity("plan-a", {
            "type": "tool", "call_id": "c1", "tool": "bash", "status": "done"})
        self.assertEqual(self.bridge.recent_runtime_activity("plan-b", limit=10), [])
        self.assertEqual(len(self.bridge.recent_runtime_activity("plan-a", limit=10)), 1)

    def test_progress_heartbeat_throttled(self):
        plan_id = "plan-2"
        for i in range(5):
            self._emit_tool(plan_id, event="tool_call_start", call_id=f"c{i}")
        # 5 次 start 事件在节流窗口内只应发布 1 次心跳
        plan_events = [p for p in self.published if p.get("runtime_id") == plan_id]
        self.assertEqual(len(plan_events), 1)
        self.assertEqual(plan_events[0]["session_key"], "webui:default")

    def test_buffer_capacity_drops_oldest(self):
        plan_id = "plan-3"
        for i in range(60):
            self.bridge._buffer_runtime_activity(plan_id, {
                "type": "tool", "call_id": f"c{i}", "tool": "bash", "status": "done"})
        recent = self.bridge.recent_runtime_activity(plan_id, limit=50)
        self.assertLessEqual(len(recent), 50)
        # 返回按新→旧排序：最新 c59 在首，最旧保留的 c10 在尾（c0..c9 已丢弃）
        self.assertEqual(recent[0]["call_id"], "c59")
        self.assertEqual(recent[-1]["call_id"], "c10")


class RuntimeStatusBufferFastPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.tmp.name) / "f.db")
        self.goal_runtime = GoalRuntime(self.store, publish=lambda *a, **k: None)
        self.conversation_service = ConversationService(
            ConversationStore(WorkspaceDatabase(runtime_store=self.store)),
            lambda event_type, payload: None, max_global_turns=100)
        self.bridge = ConversationBridge(self.conversation_service)
        self.module = SimpleNamespace(
            dispatcher=Dispatcher,
            goal_runtime=self.goal_runtime,
            plan_runtime=SimpleNamespace(manager=PlanManager(self.store)),
            bus=SimpleNamespace(publish=lambda *a, **k: None, _loop=object()),
            goal_driver=SimpleNamespace(trigger=lambda *a, **k: None),
            runtime_store=self.store,
            conversation_service=self.conversation_service,
            conversation_bridge=self.bridge,
        )
        self.entry = SimpleNamespace(session_key="webui:default")

    def tearDown(self):
        self.tmp.cleanup()

    def test_buffer_fast_path_feeds_recent_activity(self):
        plan_id = "plan-fast"
        self.bridge._buffer_runtime_activity(plan_id, {
            "type": "tool", "call_id": "c9", "tool": "write",
            "status": "running", "params_summary": "out.md",
        }, session_key="webui:default")
        data = json.loads(GetRuntimeStatusTool(self.module, self.entry, None).execute())
        # plan 不存在时 live 为 idle；这里补一个持久 plan 让 runtime_ids 命中缓冲
        if data["plan"] is None:
            mgr = PlanManager(self.store)
            plan = mgr.create_preview(
                self.session_id_default(), {"steps": [{"id": "t1", "description": "s"}]},
                source_prompt="p", title="t")
            plan = mgr.approve(plan.plan_id, actor="agent")
            mgr.activate(plan.plan_id)
            self.bridge._buffer_runtime_activity(plan.plan_id, {
                "type": "tool", "call_id": "c9", "tool": "write",
                "status": "running", "params_summary": "out.md",
            }, session_key="webui:default")
            data = json.loads(GetRuntimeStatusTool(self.module, self.entry, None).execute())
        recent = data["live"]["recent_activity"]
        self.assertTrue(recent)
        self.assertEqual(recent[-1]["tool"], "write")
        self.assertEqual(data["live"]["stage"], "tool")

    def session_id_default(self):
        return Dispatcher._runtime_session_id("webui:default")


class RuntimeStatusNoteTests(unittest.TestCase):
    def test_note_empty_when_no_runtime(self):
        self.assertEqual(Dispatcher._runtime_status_note(None, None), "")

    def test_note_contains_plan_progress(self):
        plan = SimpleNamespace(
            title="调试", plan_id="p1",
            tasks=[SimpleNamespace(status=SimpleNamespace(value="completed")),
                   SimpleNamespace(status=SimpleNamespace(value="running"))])
        note = Dispatcher._runtime_status_note(plan, None)
        self.assertIn("Plan「调试」进行中：1/2 步", note)
        self.assertIn("get_runtime_status", note)

    def test_note_contains_goal_rounds(self):
        goal = SimpleNamespace(
            objective="目标", goal_id="g1", status=SimpleNamespace(value="active"),
            rounds_started=2, max_rounds=8)
        note = Dispatcher._runtime_status_note(None, goal)
        self.assertIn("Goal「目标」active，第 2/8 轮", note)


if __name__ == "__main__":
    unittest.main()
