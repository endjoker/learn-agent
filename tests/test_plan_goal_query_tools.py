# -*- coding: utf-8 -*-
"""get_goal / get_plan / list_goals / list_plans 查询工具测试。

验证模型可主动读取当前会话的 Plan/Goal 执行状态（dsh goal 工具的查询侧）。
用真实 RuntimeStore + GoalRuntime + PlanManager 装配，避免 mock 掩盖契约断裂。
"""

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
from gateway.webui.runtime_tools import (
    GetGoalTool, ListGoalsTool, GetPlanTool, ListPlansTool,
    GetRuntimeStatusTool,
)
from gateway.webui.workspace_store import WorkspaceDatabase


class _Entry(SimpleNamespace):
    session_key = "webui:default"
    runtime_snapshot_id = ""


class PlanGoalQueryToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.tmp.name) / "q.db")
        self.goal_runtime = GoalRuntime(self.store, publish=lambda *a, **k: None)
        self.conversation_service = ConversationService(
            ConversationStore(WorkspaceDatabase(runtime_store=self.store)),
            lambda event_type, payload: None, max_global_turns=100)
        self.conversation_bridge = ConversationBridge(self.conversation_service)
        self.module = SimpleNamespace(
            dispatcher=Dispatcher,
            goal_runtime=self.goal_runtime,
            plan_runtime=SimpleNamespace(manager=PlanManager(self.store)),
            bus=SimpleNamespace(publish=lambda *a, **k: None, _loop=object()),
            goal_driver=SimpleNamespace(trigger=lambda *a, **k: None),
            runtime_store=self.store,
            conversation_service=self.conversation_service,
            conversation_bridge=self.conversation_bridge,
        )
        self.entry = _Entry()
        self.session_id = Dispatcher._runtime_session_id("webui:default")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_goal(self, objective="归档 2026 年度审计材料"):
        return self.goal_runtime.create(self.session_id, objective, max_rounds=8)

    def _make_plan(self, title="执行方案"):
        mgr = self.module.plan_runtime.manager
        plan = mgr.create_preview(
            self.session_id, {"steps": [
                {"id": "t1", "description": "收集原始数据"},
                {"id": "t2", "description": "生成审计报告", "depends_on": ["t1"]},
            ]}, source_prompt="请执行", title=title)
        plan = mgr.approve(plan.plan_id, actor="agent")
        return mgr.activate(plan.plan_id)

    def test_get_goal_empty_returns_null(self):
        out = GetGoalTool(self.module, self.entry, None).execute()
        assert json.loads(out) == {"goal": None}

    def test_get_goal_returns_active_goal(self):
        goal = self._make_goal()
        data = json.loads(GetGoalTool(self.module, self.entry, None).execute())
        assert data["goal"]["id"] == goal.goal_id
        assert data["goal"]["phase"] == "active"
        assert data["goal"]["activation"] == "armed"
        assert data["goal"]["roundsStarted"] == 0
        assert data["goal"]["maxGoalRounds"] == 8
        assert data["goal"]["progress"] == 0.0

    def test_get_goal_by_id(self):
        goal = self._make_goal()
        data = json.loads(GetGoalTool(self.module, self.entry, goal.goal_id).execute())
        assert data["goal"]["id"] == goal.goal_id

    def test_list_goals(self):
        self._make_goal("目标一")
        self._make_goal("目标二")
        data = json.loads(ListGoalsTool(self.module, self.entry, None).execute())
        assert len(data["goals"]) == 2

    def test_get_plan_returns_steps(self):
        plan = self._make_plan()
        data = json.loads(GetPlanTool(self.module, self.entry, None).execute())
        p = data["plan"]
        assert p["id"] == plan.plan_id
        assert p["status"] == "active"
        assert 0.0 <= p["progress"] <= 1.0
        assert len(p["tasks"]) == 2
        assert p["tasks"][1]["dependsOn"] == ["t1"]

    def test_get_plan_by_id(self):
        plan = self._make_plan()
        data = json.loads(GetPlanTool(self.module, self.entry, plan.plan_id).execute())
        assert data["plan"]["id"] == plan.plan_id

    def test_list_plans(self):
        self._make_plan("方案 A")
        self._make_plan("方案 B")
        data = json.loads(ListPlansTool(self.module, self.entry, None).execute())
        assert len(data["plans"]) == 2


class RuntimeStatusToolTests(unittest.TestCase):
    """get_runtime_status 实时状态工具：持久进度 + 正在执行子任务的实时活动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.tmp.name) / "rt.db")
        self.goal_runtime = GoalRuntime(self.store, publish=lambda *a, **k: None)
        self.conversation_service = ConversationService(
            ConversationStore(WorkspaceDatabase(runtime_store=self.store)),
            lambda event_type, payload: None, max_global_turns=100)
        self.conversation_bridge = ConversationBridge(self.conversation_service)
        self.module = SimpleNamespace(
            dispatcher=Dispatcher,
            goal_runtime=self.goal_runtime,
            plan_runtime=SimpleNamespace(manager=PlanManager(self.store)),
            bus=SimpleNamespace(publish=lambda *a, **k: None, _loop=object()),
            goal_driver=SimpleNamespace(trigger=lambda *a, **k: None),
            runtime_store=self.store,
            conversation_service=self.conversation_service,
            conversation_bridge=self.conversation_bridge,
        )
        self.entry = _Entry()
        self.session_id = Dispatcher._runtime_session_id("webui:default")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_plan(self, title="执行方案"):
        mgr = self.module.plan_runtime.manager
        plan = mgr.create_preview(
            self.session_id, {"steps": [{"id": "t1", "description": "步骤一"}]},
            source_prompt="请执行", title=title)
        plan = mgr.approve(plan.plan_id, actor="agent")
        return mgr.activate(plan.plan_id)

    def _write_runtime_tool_node(self, plan_id: str, *, status="running") -> None:
        conv = self.conversation_bridge.resolve("webui:default")
        turn = self.conversation_service.start_turn(conv.conversation_id)
        with self.conversation_service.store.transaction() as conn:
            self.conversation_service.store.create_node(
                conn, conversation_id=conv.conversation_id, turn_id=turn.turn_id,
                type="tool", status=status, text="",
                metadata={"runtime_type": "plan", "runtime_id": plan_id,
                          "tool": "bash", "call_id": "b1",
                          "params_summary": "step1.sh", "result_summary": ""})
        self.conversation_service.complete_turn(conv.conversation_id, turn.turn_id,
                                                "done", full_text="")

    def _enqueue_running_plan_task(self, plan_id: str) -> str:
        envelope = TaskEnvelope.create(
            session_id=self.session_id, session_key="webui:default",
            source="plan", prompt="执行步骤", plan_id=plan_id, plan_task_id="t1")
        self.store.create_task(envelope)
        # 合法迁移链：created → queued → leased → running
        self.store.transition_task(envelope.task_id, TaskStatus.QUEUED)
        self.store.transition_task(envelope.task_id, TaskStatus.LEASED)
        self.store.transition_task(envelope.task_id, TaskStatus.RUNNING)
        return envelope.task_id

    def test_empty_state_returns_idle(self):
        data = json.loads(GetRuntimeStatusTool(self.module, self.entry, None).execute())
        assert data["plan"] is None
        assert data["goal"] is None
        assert data["live"]["stage"] == "idle"
        assert data["live"]["recent_activity"] == []

    def test_running_task_and_activity_visible(self):
        plan = self._make_plan()
        self._write_runtime_tool_node(plan.plan_id)
        task_id = self._enqueue_running_plan_task(plan.plan_id)
        data = json.loads(GetRuntimeStatusTool(self.module, self.entry, None).execute())
        assert data["plan"]["id"] == plan.plan_id
        live = data["live"]
        assert live["running_task_id"] == task_id
        assert live["current_task_status"] == "running"
        assert live["stage"] == "tool"
        recent = live["recent_activity"]
        assert recent and recent[-1]["tool"] == "bash"
        assert recent[-1]["status"] == "running"
        assert live["elapsed_seconds"] is not None

    def test_activity_limit_truncates(self):
        plan = self._make_plan()
        for i in range(4):
            self._write_runtime_tool_node(plan.plan_id, status="done")
        data = json.loads(GetRuntimeStatusTool(
            self.module, self.entry, None).execute(activity_limit=1))
        assert len(data["live"]["recent_activity"]) == 1

    def test_cross_session_isolation(self):
        plan = self._make_plan()
        self._write_runtime_tool_node(plan.plan_id)
        entry = _Entry()
        entry.session_key = "webui:other"
        data = json.loads(GetRuntimeStatusTool(self.module, entry, None).execute())
        assert data["plan"] is None
        assert data["live"]["stage"] == "idle"

    def test_plan_task_id_and_current_task_linked(self):
        # P1：plan task 暴露 taskId；live.current_task_id 关联到对应 plan step。
        plan = self._make_plan()
        self._enqueue_running_plan_task(plan.plan_id)
        data = json.loads(GetRuntimeStatusTool(self.module, self.entry, None).execute())
        tasks = data["plan"]["tasks"]
        assert all(t.get("taskId") is None for t in tasks)  # 未提交的 step 无运行时 id
        live = data["live"]
        assert live["running_task_id"]
        assert live["current_task_id"] == "t1"

    def test_stall_fields_present_when_running(self):
        # P1：运行中带 last_activity_at / seconds_since_activity（stall 判定输入）。
        plan = self._make_plan()
        self._write_runtime_tool_node(plan.plan_id)
        self._enqueue_running_plan_task(plan.plan_id)
        data = json.loads(GetRuntimeStatusTool(self.module, self.entry, None).execute())
        live = data["live"]
        assert "last_activity_at" in live
        assert live["seconds_since_activity"] is not None
        assert live["stage"] in {"tool", "stalled"}


if __name__ == "__main__":
    unittest.main()
