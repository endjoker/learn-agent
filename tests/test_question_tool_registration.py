# -*- coding: utf-8 -*-
"""ask_question 工具注册 + Dispatcher 归属元数据传播测试。

覆盖：
- register_structured_capability_tools 注册 ask_question（幂等、系统保留、
  不出现在用户 Catalog、出现在 provider tools）
- Dispatcher._execute_agent 在 agent.run 前写入 _webui_metadata（message_id /
  workspace 上下文），运行后还原/清除 —— Question/Approval 上下文校验的数据源
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from gateway.channels.base import InboundMessage
from gateway.dispatcher import Dispatcher
from gateway.session import SessionEntry
from gateway.webui import runtime_tools
from gateway.webui.question_bridge import QuestionBridge
from tools.registry import SYSTEM_RESERVED_TOOLS, ToolRegistry

_ALL_CAPABILITY_TOOLS = {
    "create_plan", "get_plan", "list_plans", "update_plan",
    "create_goal", "get_goal", "list_goals", "update_goal",
    "get_runtime_status",
    "pause_goal", "resume_goal", "complete_goal", "cancel_goal",
    "create_subagent", "ask_question",
}


class _FakeAgent:
    def __init__(self):
        self.tool_registry = ToolRegistry()
        self.rebuilt = False

    def _rebuild_system_prompt(self):
        self.rebuilt = True


def _fake_module() -> SimpleNamespace:
    return SimpleNamespace(
        glue=SimpleNamespace(question_bridge=QuestionBridge(SimpleNamespace(
            bus=SimpleNamespace(publish=lambda *a, **k: None)))),
        _started=True,
        bus=SimpleNamespace(_loop=object()),
        dispatcher=SimpleNamespace(task_runtime_enabled=True),
    )


def _fake_entry() -> SimpleNamespace:
    return SimpleNamespace(session_key="webui:default", runtime_snapshot_id="")


# ============================================================
# 工具注册
# ============================================================

def test_registers_ask_question_with_structured_capabilities():
    agent = _FakeAgent()
    runtime_tools.register_structured_capability_tools(
        agent, _fake_module(), _fake_entry())
    assert _ALL_CAPABILITY_TOOLS.issubset(set(agent.tool_registry.list_tool_names()))
    tool = agent.tool_registry.get_tool("ask_question")
    assert tool is not None
    assert tool.parameters["required"] == ["question"]
    assert agent.rebuilt is True


def test_registration_is_idempotent():
    agent = _FakeAgent()
    module = _fake_module()
    entry = _fake_entry()
    runtime_tools.register_structured_capability_tools(agent, module, entry)
    runtime_tools.register_structured_capability_tools(agent, module, entry)  # 不抛重复注册
    assert len(agent.tool_registry.list_tool_names()) == len(_ALL_CAPABILITY_TOOLS)


def test_ask_question_is_system_reserved_and_hidden_from_catalog():
    assert "ask_question" in SYSTEM_RESERVED_TOOLS
    agent = _FakeAgent()
    runtime_tools.register_structured_capability_tools(
        agent, _fake_module(), _fake_entry())
    catalog = agent.tool_registry.get_catalog()
    assert all(item["name"] != "ask_question" for item in catalog)
    # 但仍对模型可见（provider tools 包含全部已注册能力）
    provider_tools, mapping = agent.tool_registry.get_provider_tools()
    assert "ask_question" in mapping.values()
    assert any(tool["function"]["name"] in mapping
               and mapping[tool["function"]["name"]] == "ask_question"
               for tool in provider_tools)


def test_ask_question_skipped_for_subagent_sessions():
    """WebUIModule._init_agent 只为 root 会话注册结构化能力。"""
    agent = _FakeAgent()
    module = _fake_module()
    entry = SimpleNamespace(session_key="subagent:child-1", runtime_snapshot_id="")
    # 模拟 WebUIModule._init_agent 的判断逻辑
    if not str(entry.session_key).startswith("subagent:"):
        runtime_tools.register_structured_capability_tools(agent, module, entry)
    assert "ask_question" not in agent.tool_registry.list_tool_names()
    assert "create_plan" not in agent.tool_registry.list_tool_names()


def test_structured_tools_register_for_workspace_sessions():
    """workspace: 会话与主会话同样获得查询/控制工具（_init_agent 仅排除 subagent:）。"""
    agent = _FakeAgent()
    module = _fake_module()
    entry = SimpleNamespace(session_key="workspace:w1:s1", runtime_snapshot_id="")
    if not str(entry.session_key).startswith("subagent:"):
        runtime_tools.register_structured_capability_tools(agent, module, entry)
    names = set(agent.tool_registry.list_tool_names())
    assert {"get_goal", "update_goal", "get_plan", "update_plan",
            "list_goals", "list_plans"}.issubset(names)


# ============================================================
# Dispatcher 归属元数据传播
# ============================================================

class _StubAgent:
    def __init__(self):
        self._webui_session_key = "ws:w1:s1"
        self._runtime_tool_blocklist = frozenset()
        self.captured_metadata = None

    def run(self, text, verbose, images=None, event_sink=None):
        # 模拟工具/审批桥在执行线程内读取 _webui_metadata
        self.captured_metadata = dict(getattr(self, "_webui_metadata", None) or {})
        return "reply-ok"

    def request_stop(self):
        pass


class _StubSessionManager:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=2)

    def get_executor(self):
        return self._executor


class _StubChannel:
    publish_agent_event = None


def _dispatcher(sm) -> Dispatcher:
    return Dispatcher(sm, agent_config={"permission_mode": "allow"})


def _run_via_execute_agent(dispatcher, agent, msg):
    async def scenario():
        entry = SessionEntry(session_key=msg.session_key)
        entry.agent = agent
        return await dispatcher._execute_agent(entry, msg, _StubChannel())
    return asyncio.run(scenario())


def test_dispatcher_propagates_metadata_for_bare_message():
    """msg.metadata 缺省时，_webui_metadata 仍写入 message_id/session_key，
    运行结束后清除（不泄漏）。"""
    sm = _StubSessionManager()
    try:
        dispatcher = _dispatcher(sm)
        agent = _StubAgent()
        msg = InboundMessage(
            channel="webui", session_key="s1", user_id="u", user_name="U",
            text="hi", message_id="m-9")  # 无 metadata
        _run_via_execute_agent(dispatcher, agent, msg)
        assert agent.captured_metadata == {"session_key": "s1", "message_id": "m-9"}
        assert not hasattr(agent, "_webui_metadata")
    finally:
        sm._executor.shutdown(wait=True)


def test_apply_permission_mode_ask_forwards_current_turn_metadata():
    """ApprovalBridge 读取当前 turn metadata（真实装配链）：

    Glue.apply_permission_mode(agent, "ask") 安装 ask_callback → callback 把
    agent._webui_metadata（dispatcher 每次 agent.run 前写入）透传给
    ApprovalBridge.ask → 审批记录携带 message/workspace 归属 →
    跨消息审批（错误 message_id）被拒绝，归属一致则通过。
    """
    import threading
    import time

    import core.config_loader
    from gateway.webui.glue import Glue

    class _Bus:
        def __init__(self):
            self.events = []
        def publish(self, event_type, payload=None):
            self.events.append((event_type, payload or {}))

    class _StubPermission:
        def set_permission_mode(self, mode):
            self.mode = mode
        def _init_default_rules(self, cfg_perm):
            self.cfg_perm = cfg_perm
        def set_rule(self, name, level):
            pass

    class _AskCallbackAgent:
        def __init__(self):
            self.permission = _StubPermission()
            self._webui_session_key = "ws:w1:s1"
            self._webui_metadata = {}

    module = SimpleNamespace(bus=_Bus(), runtime_store=None)
    glue = Glue(module)

    original_load_config = core.config_loader.load_config
    core.config_loader.load_config = lambda: {"permission": {}}
    try:
        agent = _AskCallbackAgent()
        glue.apply_permission_mode(agent, "ask")
        assert agent.ask_callback is not None
        # dispatcher 在 agent.run 前写入的当前 turn 归属元数据
        agent._webui_metadata = {
            "workspace_id": "w1", "workspace_session_id": "s1",
            "snapshot_id": "snap1", "message_id": "msg-42",
            "session_key": "ws:w1:s1",
        }
        answer = {}

        def _ask():
            answer["value"] = agent.ask_callback("bash", {"command": "echo hi"})

        thread = threading.Thread(target=_ask, daemon=True)
        thread.start()
        deadline = time.time() + 3
        while len(module.bus.events) < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert module.bus.events[0][0] == "approval.requested"
        requested = module.bus.events[0][1]
        assert requested["message_id"] == "msg-42"
        assert requested["workspace_id"] == "w1"
        assert requested["workspace_session_id"] == "s1"
        aid = requested["id"]
        # 跨消息审批（错误 message_id）→ 拒绝（fail-closed 返回状态串）
        assert glue.bridge.resolve(
            aid, "y", context={"message_id": "other"}) == "context_mismatch"
        # 归属一致 → 通过（fail-closed：session_key/workspace/snapshot 均须回传）
        assert glue.bridge.resolve(
            aid, "y", context={"session_key": "ws:w1:s1",
                               "workspace_id": "w1",
                               "workspace_session_id": "s1",
                               "snapshot_id": "snap1",
                               "message_id": "msg-42"}) == "ok"
        thread.join(timeout=3)
        assert answer["value"] == "y"
    finally:
        core.config_loader.load_config = original_load_config


def test_dispatcher_propagates_webui_metadata_into_run():
    sm = _StubSessionManager()
    try:
        dispatcher = _dispatcher(sm)
        agent = _StubAgent()
        msg = InboundMessage(
            channel="webui", session_key="ws:w1:s1", user_id="u", user_name="U",
            text="hello", message_id="msg-42",
            metadata={"workspace_id": "w1", "workspace_session_id": "s1",
                      "snapshot_id": "snap-1", "message_id": "msg-42",
                      "session_key": "ws:w1:s1"})
        reply = _run_via_execute_agent(dispatcher, agent, msg)
        assert reply == "reply-ok"
        # 运行期间（agent.run 内）可读到完整归属上下文
        assert agent.captured_metadata["message_id"] == "msg-42"
        assert agent.captured_metadata["session_key"] == "ws:w1:s1"
        assert agent.captured_metadata["workspace_id"] == "w1"
        assert agent.captured_metadata["workspace_session_id"] == "s1"
        assert agent.captured_metadata["snapshot_id"] == "snap-1"
        # 运行结束后：此前无值 → 属性被删除，不泄漏到下一轮
        assert not hasattr(agent, "_webui_metadata")
    finally:
        sm._executor.shutdown(wait=True)


def test_dispatcher_restores_previous_metadata_after_run():
    sm = _StubSessionManager()
    try:
        dispatcher = _dispatcher(sm)
        agent = _StubAgent()
        agent._webui_metadata = {"message_id": "previous-turn"}  # 上一轮残留
        msg = InboundMessage(
            channel="webui", session_key="s1", user_id="u", user_name="U",
            text="hi", message_id="current-message")
        _run_via_execute_agent(dispatcher, agent, msg)
        assert agent.captured_metadata["message_id"] == "current-message"
        # 运行后还原为上一轮值
        assert agent._webui_metadata == {"message_id": "previous-turn"}
    finally:
        sm._executor.shutdown(wait=True)


def test_dispatcher_metadata_feeds_approval_bridge_context():
    """ApprovalBridge.ask 的 metadata 参数直接读取 agent._webui_metadata：
    传播修复后 message_id/workspace 归属进入审批记录，跨消息审批被拒绝。"""
    import gateway.webui.glue as glue_module
    from gateway.webui.glue import ApprovalBridge

    class _Bus:
        def __init__(self):
            self.events = []
        def publish(self, event_type, payload=None):
            self.events.append((event_type, payload or {}))

    module = SimpleNamespace(bus=_Bus())
    bridge = ApprovalBridge(module)
    agent = _StubAgent()
    agent._webui_metadata = {"workspace_id": "w1", "workspace_session_id": "s1",
                             "snapshot_id": "snap-1", "message_id": "msg-42",
                             "session_key": "ws:w1:s1"}
    agent._webui_session_key = "ws:w1:s1"
    original_timeout = glue_module._APPROVAL_TIMEOUT
    glue_module._APPROVAL_TIMEOUT = 0.1
    try:
        # 模拟 apply_permission_mode 中安装的 ask_callback（运行期读取元数据）
        answer = bridge.ask(
            agent._webui_session_key, "bash",
            {"command": "echo hi"},
            metadata=getattr(agent, "_webui_metadata", None) or {})
    finally:
        glue_module._APPROVAL_TIMEOUT = original_timeout
    # 无答复超时 → fail-closed "n"
    assert answer == "n"
    requested = module.bus.events[0][1]
    assert requested["message_id"] == "msg-42"
    assert requested["workspace_id"] == "w1"
    assert requested["workspace_session_id"] == "s1"
    resolved = module.bus.events[1][1]
    assert resolved["timeout"] is True
