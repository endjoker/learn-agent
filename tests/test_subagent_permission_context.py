# -*- coding: utf-8 -*-
"""子 Agent（subagent）权限随父工作区会话 + 审批归属字段回归测试。

修复两个问题：
1. 子 Agent 审批答复 403"审批归属不匹配"：GET /api/approvals（兜底轮询）
   载荷缺归属字段 → 前端整队替换后答复回传不了 message_id → 桥单边匹配判
   context_mismatch。修复：list_pending 载荷携带全部归属字段。
2. 子 Agent 权限不随父会话：子任务键 ``subagent:workspace:w1:s1:child_x``
   不以 workspace: 开头 → _ensure_workspace_context 早退 → 子 Agent 用全局
   默认档位（ask），父会话即使免审也弹审批。修复：从子任务键还原父工作区
   键并挂接同一套快照上下文（权限随父会话）；envelope 元数据注入父归属。
"""

import threading
import time
from types import SimpleNamespace

from gateway.dispatcher import Dispatcher
from gateway.webui.glue import ApprovalBridge


# ================================================================
# _workspace_key_of：从子任务键还原父工作区键
# ================================================================

class TestWorkspaceKeyOf:
    def test_workspace_key_passes_through(self):
        assert Dispatcher._workspace_key_of("workspace:w1:s1") == "workspace:w1:s1"

    def test_subagent_key_extracts_parent_workspace(self):
        assert Dispatcher._workspace_key_of(
            "subagent:workspace:w1:s1:child_abc123") == "workspace:w1:s1"

    def test_subagent_non_workspace_parent_returns_empty(self):
        assert Dispatcher._workspace_key_of("subagent:webui:default:child_1") == ""

    def test_continuation_key_returns_empty(self):
        # subagent:continued:{child_id} 不携带父会话信息
        assert Dispatcher._workspace_key_of("subagent:continued:child_1") == ""

    def test_non_workspace_key_returns_empty(self):
        assert Dispatcher._workspace_key_of("webui:default") == ""
        assert Dispatcher._workspace_key_of("") == ""
        assert Dispatcher._workspace_key_of(None) == ""


# ================================================================
# _ensure_workspace_context：子任务 entry 挂接父会话上下文
# ================================================================

class TestEnsureWorkspaceContextForSubagent:
    def _dispatcher_with_provider(self):
        d = object.__new__(Dispatcher)
        captured = {}

        def provider(wid, sid):
            captured["args"] = (wid, sid)
            return {
                "runtime_context": {"workspace_root": "/tmp/ws"},
                "snapshot_id": "snap-1",
                "model": "test-model",
                "permission_mode": "unreviewed",
                "reasoning_level": "high",
            }

        d._workspace_context_provider = provider
        return d, captured

    def test_subagent_entry_gets_parent_session_context(self):
        d, captured = self._dispatcher_with_provider()
        entry = SimpleNamespace(
            session_key="subagent:workspace:w1:s1:child_abc",
            runtime_context=None)
        d._ensure_workspace_context(entry)
        assert captured["args"] == ("w1", "s1")
        assert entry.runtime_permission_mode == "unreviewed"  # 随父会话（免审）
        assert entry.runtime_snapshot_id == "snap-1"
        assert entry.runtime_context == {"workspace_root": "/tmp/ws"}

    def test_plan_entry_still_works(self):
        d, captured = self._dispatcher_with_provider()
        entry = SimpleNamespace(session_key="workspace:w1:s1", runtime_context=None)
        d._ensure_workspace_context(entry)
        assert captured["args"] == ("w1", "s1")
        assert entry.runtime_permission_mode == "unreviewed"

    def test_non_workspace_entry_untouched(self):
        d, captured = self._dispatcher_with_provider()
        entry = SimpleNamespace(session_key="webui:default", runtime_context=None)
        d._ensure_workspace_context(entry)
        assert "args" not in captured
        assert entry.runtime_context is None

    def test_existing_context_not_overwritten(self):
        d, captured = self._dispatcher_with_provider()
        entry = SimpleNamespace(
            session_key="subagent:workspace:w1:s1:child_abc",
            runtime_context={"existing": True})
        d._ensure_workspace_context(entry)
        assert "args" not in captured  # 已有上下文不重建
        assert entry.runtime_context == {"existing": True}


# ================================================================
# 子任务 envelope 元数据注入父归属
# ================================================================

class TestSubagentEnvelopeMetadata:
    def test_workspace_parent_injects_ownership(self):
        merged = Dispatcher._subagent_envelope_metadata(
            "subagent:workspace:w1:s1:child_abc",
            {"parent_session_id": "s-1", "child_session_id": "child_abc"})
        assert merged["workspace_id"] == "w1"
        assert merged["workspace_session_id"] == "s1"
        assert merged["channel"] == "webui"
        assert merged["deliver_reply"] is False
        assert merged["parent_session_id"] == "s-1"

    def test_non_workspace_parent_no_ownership(self):
        merged = Dispatcher._subagent_envelope_metadata(
            "subagent:webui:default:child_1", None)
        assert "workspace_id" not in merged
        assert "workspace_session_id" not in merged

    def test_caller_fields_take_precedence(self):
        merged = Dispatcher._subagent_envelope_metadata(
            "subagent:workspace:w1:s1:child_abc",
            {"workspace_id": "explicit"})
        assert merged["workspace_id"] == "explicit"


# ================================================================
# 审批待答载荷携带归属字段（修复轮询替换后答复 403）
# ================================================================

class _RecordingBus:
    def __init__(self):
        self.events = []
        self._loop = object()

    def publish(self, event_type, payload=None):
        self.events.append((event_type, payload or {}))


class TestApprovalListPendingOwnership:
    def _ask_background(self, bridge, metadata):
        holder = {}

        def _run():
            holder["value"] = bridge.ask(
                "subagent:workspace:w1:s1:child_abc", "bash",
                {"command": "ls"}, metadata=metadata)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and not bridge.list_pending():
            time.sleep(0.01)
        return holder, t

    def test_list_pending_carries_ownership_fields(self):
        module = SimpleNamespace(bus=_RecordingBus())
        bridge = ApprovalBridge(module)
        metadata = {"workspace_id": "w1", "workspace_session_id": "s1",
                    "snapshot_id": "snap-1", "message_id": "task-9"}
        holder, thread = self._ask_background(bridge, metadata)
        pending = bridge.list_pending()
        assert len(pending) == 1
        item = pending[0]
        # 归属字段必须与记录一致（前端轮询整队替换后仍能原样回传）
        assert item["session_key"] == "subagent:workspace:w1:s1:child_abc"
        assert item["workspace_id"] == "w1"
        assert item["workspace_session_id"] == "s1"
        assert item["snapshot_id"] == "snap-1"
        assert item["message_id"] == "task-9"
        # 完整归属回放 → 答复成功
        assert bridge.resolve(item["id"], "y", context={
            "session_key": item["session_key"],
            "workspace_id": "w1", "workspace_session_id": "s1",
            "snapshot_id": "snap-1", "message_id": "task-9"}) == "ok"
        thread.join(timeout=5)
        assert holder["value"] == "y"

    def test_answer_without_record_message_id_still_mismatch(self):
        """fail-closed 语义保持：记录带 message_id 而答复缺失 → 仍判不匹配。"""
        module = SimpleNamespace(bus=_RecordingBus())
        bridge = ApprovalBridge(module)
        holder, thread = self._ask_background(
            bridge, {"message_id": "task-9"})
        aid = bridge.list_pending()[0]["id"]
        # 缺 message_id（修复前的轮询载荷答复形态）→ 403 语义
        assert bridge.resolve(aid, "y", context={
            "session_key": "subagent:workspace:w1:s1:child_abc"}) == "context_mismatch"
        # 补齐后可答复
        assert bridge.resolve(aid, "y", context={
            "session_key": "subagent:workspace:w1:s1:child_abc",
            "message_id": "task-9"}) == "ok"
        thread.join(timeout=5)


# ================================================================
# 计划/目标任务 envelope 归属注入（与子进程对齐）
# ================================================================

class TestPlanGoalEnvelopeOwnership:
    def test_plan_metadata_injects_workspace_ownership(self):
        meta = Dispatcher._with_workspace_ownership(
            "workspace:w1:s1",
            {"channel": "webui", "deliver_reply": False,
             "task_source": "plan", "plan_id": "p1", "plan_task_id": "pt1"})
        assert meta["workspace_id"] == "w1"
        assert meta["workspace_session_id"] == "s1"
        assert meta["task_source"] == "plan"

    def test_goal_metadata_injects_workspace_ownership(self):
        meta = Dispatcher._with_workspace_ownership(
            "workspace:w1:s1",
            {"channel": "webui", "deliver_reply": False,
             "task_source": "goal", "goal_id": "g1"})
        assert meta["workspace_id"] == "w1"
        assert meta["workspace_session_id"] == "s1"
        assert meta["goal_id"] == "g1"

    def test_non_workspace_goal_keeps_metadata_unchanged(self):
        meta = Dispatcher._with_workspace_ownership(
            "webui:default",
            {"channel": "webui", "deliver_reply": False, "task_source": "goal"})
        assert "workspace_id" not in meta
        assert "workspace_session_id" not in meta

    def test_present_caller_fields_take_precedence(self):
        meta = Dispatcher._with_workspace_ownership(
            "workspace:w1:s1", {"workspace_id": "explicit"})
        assert meta["workspace_id"] == "explicit"

    def test_subagent_metadata_reuses_workspace_ownership(self):
        # 重构后子进程 envelope 仍注入父工作区归属
        meta = Dispatcher._subagent_envelope_metadata(
            "subagent:workspace:w1:s1:child_abc", None)
        assert meta["workspace_id"] == "w1"
        assert meta["workspace_session_id"] == "s1"
        assert meta["channel"] == "webui"


# ================================================================
# 渠道（debug）会话审批归属：message_id 稳定即可答复，不 403
# ================================================================

class _FakeModule:
    def __init__(self):
        self.bus = _RecordingBus()


class TestChannelApprovalOwnership:
    """渠道会话：审批记录归属取自 agent._webui_metadata（msg.metadata +
    session_key + message_id）；前端回传同值即可答复，不判 403。"""

    def _ask_like_channel_message(self, bridge, message_id):
        """模拟渠道消息：msg 走 _webui_metadata 汇入归属字段。"""
        meta = {"channel": "debug", "session_key": "debug:t1",
                "message_id": message_id}
        holder = {}

        def _run():
            holder["value"] = bridge.ask(
                "debug:t1", "write", {"path": "/tmp/x"},
                metadata=meta)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and not bridge.list_pending():
            time.sleep(0.01)
        return holder, t

    def test_channel_approval_payload_and_resolve_match(self):
        module = _FakeModule()
        bridge = ApprovalBridge(module)
        mid = "0812abcd"  # debug channel 用 uuid[:8]，非空且稳定
        holder, thread = self._ask_like_channel_message(bridge, mid)
        pending = bridge.list_pending()
        assert len(pending) == 1
        item = pending[0]
        assert item["session_key"] == "debug:t1"
        assert item["message_id"] == mid
        # 前端回传同值（session_key + message_id）→ 无"归属不匹配"
        assert bridge.resolve(item["id"], "y", context={
            "session_key": "debug:t1", "message_id": mid}) == "ok"
        thread.join(timeout=5)
        assert holder["value"] == "y"

    def test_channel_approval_without_message_id_is_consistent(self):
        """渠道若没给 message_id：record 与请求都为空 → 双方空放行，不再 403。"""
        module = _FakeModule()
        bridge = ApprovalBridge(module)
        holder, thread = self._ask_like_channel_message(bridge, "")
        item = bridge.list_pending()[0]
        assert item["message_id"] == ""
        # 前端不传 message_id（空）→ record 也为空 → 放行
        assert bridge.resolve(item["id"], "y", context={
            "session_key": "debug:t1"}) == "ok"
        thread.join(timeout=5)
