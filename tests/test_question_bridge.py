# -*- coding: utf-8 -*-
"""QuestionBridge / /api/questions / ask_question 工具 —— P1-4 后端桥测试。

覆盖：结构化提问桥语义（阻塞等待/答复）、context 校验（跨会话/跨工作区拒绝）、
pending 恢复（GET）、one-answer 语义（重复答复 409）、超时与停机 fail-closed
（不自动选择推荐项）、SSE 事件（question.requested/resolved）与 agent 侧
ask_question 工具（含 WebUI 不可用时返回明确状态）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.tool_schema import validate_arguments
from gateway.webui import api_chat
from gateway.webui.events import EventBus
from gateway.webui.question_bridge import (
    RESOLVE_ALREADY_ANSWERED,
    RESOLVE_CONTEXT_MISMATCH,
    RESOLVE_INVALID,
    RESOLVE_NOT_FOUND,
    RESOLVE_OK,
    QuestionBridge,
)
from gateway.webui.runtime_tools import AskQuestionTool


class _RecordingBus:
    """捕获 publish 的假总线（Shape 断言用）；_loop 非空以便工具可用性判断。"""

    def __init__(self):
        self.events = []
        self._loop = object()

    def publish(self, event_type, payload=None):
        self.events.append((event_type, payload or {}))


class _FakeModule:
    def __init__(self, bus=None):
        self.bus = bus or _RecordingBus()
        self._started = True
        self.glue = SimpleNamespace(question_bridge=None)


def _wait_pending(bridge, expected=1, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = bridge.list_pending()
        if len(pending) >= expected:
            return pending
        time.sleep(0.01)
    raise AssertionError(f"pending questions not visible: {bridge.list_pending()}")


def _ask_thread(bridge, **kwargs):
    """在后台线程发起 ask()，返回 (holder, thread)。holder['value'] 为结果。"""
    holder = {}

    def _run():
        holder["value"] = bridge.ask(**kwargs)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return holder, thread


# ============================================================
# 桥语义：答复 / 自定义输入 / 必答
# ============================================================

def test_ask_and_resolve_happy_path():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="请选择首屏加载数量",
        options=[{"id": "100", "label": "100 条"},
                 {"id": "200", "label": "200 条", "recommended": True}],
        allow_custom=True, custom_placeholder="输入其他数量", timeout_s=5)
    pending = _wait_pending(bridge)
    qid = pending[0]["id"]
    assert pending[0]["question"] == "请选择首屏加载数量"
    assert pending[0]["allow_custom"] is True
    assert pending[0]["options"][1]["recommended"] is True

    assert bridge.resolve(
        qid, {"selected_option_ids": ["200"], "custom_text": ""},
        context={"session_key": "s1"}) == RESOLVE_OK
    thread.join(timeout=5)
    result = holder["value"]
    assert result["status"] == "answered"
    assert result["selected_option_ids"] == ["200"]
    # wire id 契约：id 主字段 + question_id 别名，两者恒相等
    assert result["id"] == qid
    assert result["question_id"] == qid
    assert bridge.list_pending() == []


def test_custom_only_answer_satisfies_required():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="请补充数量",
        options=[{"id": "100", "label": "100 条"}],
        allow_custom=True, required=True, timeout_s=5)
    qid = _wait_pending(bridge)[0]["id"]
    assert bridge.resolve(
        qid, {"selected_option_ids": [], "custom_text": "250 条"},
        context={"session_key": "s1"}) == RESOLVE_OK
    thread.join(timeout=5)
    assert holder["value"]["status"] == "answered"
    assert holder["value"]["selected_option_ids"] == []
    assert holder["value"]["custom_text"] == "250 条"


# ============================================================
# fail-closed：超时 / WebUI 停止，绝不自动选择推荐项
# ============================================================

def test_timeout_fail_closed_never_auto_selects():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    result = bridge.ask(
        "s1", "选？", [{"id": "a", "label": "A", "recommended": True},
                      {"id": "b", "label": "B"}],
        allow_custom=True, timeout_s=0.1)
    assert result["status"] == "timeout"
    assert result.get("selected_option_ids", []) == []
    assert result.get("custom_text", "") == ""
    # wire id 契约：超时结果同样携带 id + question_id 别名
    assert result["id"] == result["question_id"]
    assert result["id"].startswith("q-")
    assert bridge.list_pending() == []
    # SSE：requested → resolved(timeout)
    types = [event_type for event_type, _ in module.bus.events]
    assert types == ["question.requested", "question.resolved"]
    resolved = module.bus.events[1][1]
    assert resolved["status"] == "timeout"
    assert resolved["timeout"] is True
    assert resolved["selected_option_ids"] == []
    assert resolved["id"] == resolved["question_id"] == result["id"]


def test_fail_close_all_on_stop():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="选？",
        options=[{"id": "a", "label": "A", "recommended": True}],
        timeout_s=30)
    _wait_pending(bridge)
    bridge.fail_close_all()
    thread.join(timeout=5)
    result = holder["value"]
    assert result["status"] == "fail_closed"
    assert result.get("selected_option_ids", []) == []
    assert result["id"] == result["question_id"]
    assert bridge.list_pending() == []


# ============================================================
# context 校验：跨会话 / 跨工作区答复被拒绝
# ============================================================

def test_resolve_rejects_wrong_session_key():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="选？",
        options=[{"id": "a", "label": "A"}], timeout_s=5)
    qid = _wait_pending(bridge)[0]["id"]
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"session_key": "s2"}) == RESOLVE_CONTEXT_MISMATCH
    # P0-5 fail-closed：记录携带 session_key 时，缺失归属同样拒绝
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]}, context={}) == RESOLVE_CONTEXT_MISMATCH
    # 归属一致 → 通过
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"session_key": "s1"}) == RESOLVE_OK
    thread.join(timeout=5)
    assert holder["value"]["status"] == "answered"


def test_resolve_rejects_workspace_context_mismatch():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="workspace:w1:s1", question="选？",
        options=[{"id": "a", "label": "A"}], timeout_s=5,
        context={"workspace_id": "w1", "workspace_session_id": "s1",
                 "snapshot_id": "snap1"})
    qid = _wait_pending(bridge)[0]["id"]
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"workspace_id": "w2"}) == RESOLVE_CONTEXT_MISMATCH
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"workspace_id": "w1", "workspace_session_id": "other"}
    ) == RESOLVE_CONTEXT_MISMATCH
    # 匹配上下文通过（fail-closed：记录携带的 session_key 也必须回传）
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"session_key": "workspace:w1:s1", "workspace_id": "w1",
                 "workspace_session_id": "s1",
                 "snapshot_id": "snap1"}) == RESOLVE_OK
    thread.join(timeout=5)
    assert holder["value"]["status"] == "answered"


def test_resolve_rejects_message_id_mismatch():
    """message context：记录携带 message_id 后，跨消息答复必须被拒绝。"""
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="选？",
        options=[{"id": "a", "label": "A"}], timeout_s=5,
        context={"message_id": "msg-1"})
    qid = _wait_pending(bridge)[0]["id"]
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"message_id": "msg-2"}) == RESOLVE_CONTEXT_MISMATCH
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"session_key": "s1", "message_id": "msg-1"}) == RESOLVE_OK
    thread.join(timeout=5)
    assert holder["value"]["status"] == "answered"


# ============================================================
# one-answer 语义
# ============================================================

def test_one_answer_semantics():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="选？",
        options=[{"id": "a", "label": "A"}], timeout_s=5)
    qid = _wait_pending(bridge)[0]["id"]
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"session_key": "s1"}) == RESOLVE_OK
    # 重复答复：问题已从 pending 移入 answered 缓存 → 409
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"session_key": "s1"}) == RESOLVE_ALREADY_ANSWERED
    # 从未存在 / 已过期 → 404
    assert bridge.resolve(
        "q-nonexistent", {"selected_option_ids": ["a"]},
        context={"session_key": "s1"}) == RESOLVE_NOT_FOUND
    thread.join(timeout=5)
    assert holder["value"]["status"] == "answered"


# ============================================================
# 答案约束校验
# ============================================================

def test_invalid_answers_rejected():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="选？",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        allow_custom=False, multiple=False, required=True, timeout_s=5)
    qid = _wait_pending(bridge)[0]["id"]
    ctx = {"session_key": "s1"}
    assert bridge.resolve(qid, {"selected_option_ids": ["x"]},
                         context=ctx) == RESOLVE_INVALID          # 非法选项
    assert bridge.resolve(qid, {"selected_option_ids": ["a", "b"]},
                         context=ctx) == RESOLVE_INVALID          # 单选越界
    assert bridge.resolve(qid, {"selected_option_ids": [], "custom_text": ""},
                         context=ctx) == RESOLVE_INVALID          # 必答未答
    assert bridge.resolve(qid, {"selected_option_ids": [], "custom_text": "自由"},
                         context=ctx) == RESOLVE_INVALID          # 未允许自定义
    assert bridge.resolve(qid, {"selected_option_ids": ["a"]},
                         context=ctx) == RESOLVE_OK
    thread.join(timeout=5)
    assert holder["value"]["status"] == "answered"


# ============================================================
# pending 恢复与 scope 过滤
# ============================================================

def test_list_pending_filters_by_scope():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder1, t1 = _ask_thread(
        bridge, session_key="s1", question="q1",
        options=[{"id": "a", "label": "A"}], timeout_s=5)
    holder2, t2 = _ask_thread(
        bridge, session_key="workspace:w1:s1", question="q2",
        options=[{"id": "a", "label": "A"}], timeout_s=5,
        context={"workspace_id": "w1", "workspace_session_id": "s1"})
    _wait_pending(bridge, expected=2)

    by_session = bridge.list_pending(session_key="s1")
    assert [q["question"] for q in by_session] == ["q1"]
    by_workspace = bridge.list_pending(workspace_id="w1")
    assert [q["question"] for q in by_workspace] == ["q2"]
    assert bridge.list_pending(workspace_id="w2") == []
    assert len(bridge.list_pending()) == 2
    assert bridge.count_pending() == 2

    # pending 公共字段不含内部 event/answer
    for q in bridge.list_pending():
        assert "event" not in q
        assert "answer" not in q
        # wire id 契约：pending 同时返回 id 主字段与 question_id 别名
        assert q["id"] == q["question_id"]
        assert q["id"].startswith("q-")

    # 清理：全部答复后 pending 清空（fail-closed：回传记录携带的全部归属键）
    for q in bridge.list_pending():
        ctx = {"session_key": q["session_key"], **(q.get("context") or {})}
        assert bridge.resolve(q["id"], {"selected_option_ids": []},
                              context=ctx) == RESOLVE_OK
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert bridge.list_pending() == []


# ============================================================
# 输入规范化 / 校验
# ============================================================

def test_options_normalization_and_recommended_never_auto_selected():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    bridge.ask(
        "s1", "q", [
            {"id": "a", "label": "A"},
            {"id": "a", "label": "重复 id 被跳过"},
            {"id": "", "label": "空 id 被跳过"},
            {"id": "b", "label": "B", "description": "补充", "recommended": True},
            "not-a-dict",
        ],
        allow_custom=True, timeout_s=0.1)
    requested = module.bus.events[0][1]
    assert requested["options"] == [
        {"id": "a", "label": "A", "description": None, "recommended": False},
        {"id": "b", "label": "B", "description": "补充", "recommended": True},
    ]
    # 超时结果不自动选择 recommended
    assert module.bus.events[1][1]["selected_option_ids"] == []


def test_ask_rejects_invalid_input():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    with pytest.raises(ValueError):
        bridge.ask("s1", "", [{"id": "a", "label": "A"}])
    with pytest.raises(ValueError):
        bridge.ask("s1", "q", [], allow_custom=False)   # 无候选项且禁止自定义


# ============================================================
# SSE 事件（真实 EventBus + 订阅队列）
# ============================================================

def test_sse_events_through_real_bus():
    async def scenario():
        bus = EventBus()
        bus.bind_loop(asyncio.get_event_loop())
        sub_id, queue = bus.subscribe()
        module = _FakeModule(bus=bus)
        bridge = QuestionBridge(module)
        module.glue.question_bridge = bridge

        holder, thread = _ask_thread(
            bridge, session_key="workspace:w1:s1", question="选？",
            options=[{"id": "a", "label": "A", "recommended": True}],
            allow_custom=True, timeout_s=5,
            context={"workspace_id": "w1", "workspace_session_id": "s1"})

        requested_event = await asyncio.wait_for(queue.get(), timeout=3)
        assert requested_event["type"] == "question.requested"
        requested = requested_event["data"]
        assert requested["session_key"] == "workspace:w1:s1"
        assert requested["workspace_id"] == "w1"
        assert requested["workspace_session_id"] == "s1"
        assert requested["options"][0]["recommended"] is True
        qid = requested["id"]
        # wire id 契约：SSE requested 同时返回 id 主字段与 question_id 别名
        assert requested["question_id"] == qid
        assert qid.startswith("q-")

        assert bridge.resolve(
            qid, {"selected_option_ids": ["a"]},
            context={"session_key": "workspace:w1:s1", "workspace_id": "w1",
                     "workspace_session_id": "s1"}) == RESOLVE_OK

        resolved_event = await asyncio.wait_for(queue.get(), timeout=3)
        assert resolved_event["type"] == "question.resolved"
        resolved = resolved_event["data"]
        assert resolved["status"] == "answered"
        assert resolved["selected_option_ids"] == ["a"]
        assert resolved["session_key"] == "workspace:w1:s1"
        # wire id 契约：SSE resolved 同时返回 id 主字段与 question_id 别名
        assert resolved["id"] == resolved["question_id"] == qid

        thread.join(timeout=5)
        assert holder["value"]["status"] == "answered"
        bus.unsubscribe(sub_id)

    asyncio.run(scenario())


# ============================================================
# REST API（aiohttp TestClient）
# ============================================================

def _api_app(module) -> web.Application:
    app = web.Application()
    api_chat.register_routes(app, module)
    return app


def test_api_get_questions_empty():
    async def scenario():
        module = _FakeModule()
        module.glue.question_bridge = QuestionBridge(module)
        server = TestServer(_api_app(module))
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/api/questions")
            assert resp.status == 200
            assert await resp.json() == {"questions": []}
        finally:
            await client.close()

    asyncio.run(scenario())


def test_api_question_lifecycle():
    """pending 恢复 → 答复 200 → 重复 409 → 未知 404 → 跨会话 403 → 非法 400。"""
    async def scenario():
        module = _FakeModule()
        bridge = QuestionBridge(module)
        module.glue.question_bridge = bridge
        server = TestServer(_api_app(module))
        client = TestClient(server)
        await client.start_server()
        try:
            holder, thread = _ask_thread(
                bridge, session_key="s1", question="请选择首屏加载数量",
                options=[{"id": "100", "label": "100 条"},
                         {"id": "200", "label": "200 条", "recommended": True}],
                allow_custom=True, timeout_s=5)
            # pending 恢复（页面刷新后重取）
            for _ in range(200):
                if bridge.list_pending():
                    break
                await asyncio.sleep(0.02)
            resp = await client.get("/api/questions?session_key=s1")
            assert resp.status == 200
            questions = (await resp.json())["questions"]
            assert len(questions) == 1
            qid = questions[0]["id"]
            # wire id 契约：GET pending 同时返回 id 主字段与 question_id 别名
            assert questions[0]["question_id"] == qid
            assert questions[0]["options"][1]["recommended"] is True

            # 跨会话答复 → 403
            resp = await client.post(
                f"/api/questions/{qid}",
                json={"session_key": "other", "selected_option_ids": ["200"]})
            assert resp.status == 403

            # 非法选项 → 400
            resp = await client.post(
                f"/api/questions/{qid}",
                json={"session_key": "s1", "selected_option_ids": ["nope"]})
            assert resp.status == 400

            # 正常答复（候选项 + 自定义组合）→ 200
            resp = await client.post(
                f"/api/questions/{qid}",
                json={"session_key": "s1", "selected_option_ids": ["200"],
                      "custom_text": "补充说明"})
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["selected_option_ids"] == ["200"]
            # wire id 契约：POST 成功响应同样返回 id 主字段与 question_id 别名
            assert body["id"] == body["question_id"] == qid

            thread.join(timeout=5)
            assert holder["value"]["status"] == "answered"
            assert holder["value"]["custom_text"] == "补充说明"

            # pending 已清空
            resp = await client.get("/api/questions")
            assert (await resp.json()) == {"questions": []}

            # 重复答复 → 409
            resp = await client.post(
                f"/api/questions/{qid}",
                json={"session_key": "s1", "selected_option_ids": ["200"]})
            assert resp.status == 409

            # 未知问题 → 404
            resp = await client.post(
                "/api/questions/q-nonexistent",
                json={"session_key": "s1", "selected_option_ids": ["200"]})
            assert resp.status == 404
        finally:
            await client.close()

    asyncio.run(scenario())


def test_api_rejects_wrong_message_id_and_malformed_body():
    """message 上下文校验经 API 生效；畸形请求体 400 且不消费问题。"""
    async def scenario():
        module = _FakeModule()
        bridge = QuestionBridge(module)
        module.glue.question_bridge = bridge
        server = TestServer(_api_app(module))
        client = TestClient(server)
        await client.start_server()
        try:
            holder, thread = _ask_thread(
                bridge, session_key="s1", question="选？",
                options=[{"id": "a", "label": "A"}], timeout_s=5,
                context={"message_id": "msg-1"})
            for _ in range(200):
                if bridge.list_pending():
                    break
                await asyncio.sleep(0.02)
            qid = bridge.list_pending()[0]["id"]

            # 跨消息答复（错误 message_id）→ 403
            resp = await client.post(
                f"/api/questions/{qid}",
                json={"session_key": "s1", "message_id": "other",
                      "selected_option_ids": ["a"]})
            assert resp.status == 403
            assert bridge.list_pending()  # 问题仍在等待

            # 匹配 message_id → 200
            resp = await client.post(
                f"/api/questions/{qid}",
                json={"session_key": "s1", "message_id": "msg-1",
                      "selected_option_ids": ["a"]})
            assert resp.status == 200
            thread.join(timeout=5)
            assert holder["value"]["status"] == "answered"

            # 畸形请求体（null / 非 JSON）→ 400 且问题保留
            holder2, thread2 = _ask_thread(
                bridge, session_key="s1", question="再选？",
                options=[{"id": "b", "label": "B"}], timeout_s=5)
            for _ in range(200):
                if bridge.list_pending():
                    break
                await asyncio.sleep(0.02)
            qid2 = bridge.list_pending()[0]["id"]
            resp = await client.post(
                f"/api/questions/{qid2}", data="null",
                headers={"Content-Type": "application/json"})
            assert resp.status == 400
            resp = await client.post(
                f"/api/questions/{qid2}", data="not-json",
                headers={"Content-Type": "application/json"})
            assert resp.status == 400
            assert [q["id"] for q in bridge.list_pending()] == [qid2]
            # selected_option_ids 非数组 → 400
            resp = await client.post(
                f"/api/questions/{qid2}",
                json={"session_key": "s1", "selected_option_ids": "not-a-list"})
            assert resp.status == 400
            assert bridge.list_pending()

            bridge.fail_close_all()
            thread2.join(timeout=5)
            assert holder2["value"]["status"] == "fail_closed"
        finally:
            await client.close()

    asyncio.run(scenario())


# ============================================================
# agent 侧 ask_question 工具
# ============================================================

def test_ask_question_tool_schema():
    tool = AskQuestionTool(_FakeModule(), SimpleNamespace(
        session_key="webui:default", runtime_snapshot_id=""), None)
    example = {
        "question": "请选择首屏加载数量",
        "options": [
            {"id": "100", "label": "100 条"},
            {"id": "200", "label": "200 条", "recommended": True},
        ],
        "allow_custom": True,
        "custom_placeholder": "输入其他数量",
    }
    assert validate_arguments(tool.parameters, example) == []
    # 必填 question 缺失 → 校验失败
    assert validate_arguments(tool.parameters, {"options": []})


def test_ask_question_tool_returns_unavailable_when_webui_down():
    module = _FakeModule()
    module._started = False
    tool = AskQuestionTool(module, SimpleNamespace(
        session_key="webui:default", runtime_snapshot_id=""), None)
    out = json.loads(tool.execute(
        question="选？", options=[{"id": "a", "label": "A"}]))
    assert out["status"] == "unavailable"
    assert "需要用户输入" in out["reason"]
    assert "selected_option_ids" not in out


def test_ask_question_tool_end_to_end():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    module.glue.question_bridge = bridge
    # Dispatcher 在 agent.run 前写入的当前轮次归属元数据
    agent_meta = {"workspace_id": "w1", "workspace_session_id": "s1",
                  "snapshot_id": "snap1", "message_id": "msg-42"}
    tool = AskQuestionTool(module, SimpleNamespace(
        session_key="workspace:w1:s1", runtime_snapshot_id="snap1"),
        SimpleNamespace(_webui_metadata=agent_meta))

    holder = {}

    def _run():
        holder["value"] = json.loads(tool.execute(
            question="请选择首屏加载数量",
            options=[{"id": "100", "label": "100 条"},
                     {"id": "200", "label": "200 条", "recommended": True}],
            allow_custom=True, custom_placeholder="输入其他数量"))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    pending = _wait_pending(bridge)
    qid = pending[0]["id"]
    # wire id 契约：pending 同时返回 id 主字段与 question_id 别名
    assert pending[0]["question_id"] == qid
    # 工具推导出的归属上下文已写入记录（含 message_id，message 校验真实生效）
    assert pending[0]["context"]["workspace_id"] == "w1"
    assert pending[0]["context"]["workspace_session_id"] == "s1"
    assert pending[0]["context"]["snapshot_id"] == "snap1"
    assert pending[0]["context"]["message_id"] == "msg-42"

    # 跨消息答复（错误 message_id）→ 拒绝
    assert bridge.resolve(
        qid, {"selected_option_ids": ["200"]},
        context={"session_key": "workspace:w1:s1", "workspace_id": "w1",
                 "workspace_session_id": "s1",
                 "message_id": "other-message"}) == RESOLVE_CONTEXT_MISMATCH
    # 归属一致 → 通过（fail-closed：snapshot_id 也在记录归属内，必须回传）
    assert bridge.resolve(
        qid, {"selected_option_ids": ["200"]},
        context={"session_key": "workspace:w1:s1", "workspace_id": "w1",
                 "workspace_session_id": "s1", "snapshot_id": "snap1",
                 "message_id": "msg-42"}) == RESOLVE_OK
    thread.join(timeout=5)
    result = holder["value"]
    assert result["status"] == "answered"
    assert result["selected_option_ids"] == ["200"]
    # wire id 契约：ask_question 工具结果同样返回 id + question_id
    assert result["id"] == result["question_id"] == qid


def test_wire_id_contract_across_all_channels():
    """统一 wire id 契约：GET pending / SSE requested / SSE resolved /
    ask 结果四处均同时返回 id 主字段与 question_id 别名，且恒相等。"""
    module = _FakeModule()
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="选？",
        options=[{"id": "a", "label": "A"}], timeout_s=5)
    pending = _wait_pending(bridge)
    qid = pending[0]["id"]
    assert pending[0]["question_id"] == qid

    # SSE requested 与 resolved 均携带双字段
    requested = module.bus.events[0][1]
    assert requested["id"] == requested["question_id"] == qid
    assert bridge.resolve(
        qid, {"selected_option_ids": ["a"]},
        context={"session_key": "s1"}) == RESOLVE_OK
    thread.join(timeout=5)
    resolved = module.bus.events[1][1]
    assert resolved["id"] == resolved["question_id"] == qid

    # ask 结果双字段
    result = holder["value"]
    assert result["id"] == result["question_id"] == qid


def test_ask_question_tool_without_metadata_falls_back_to_entry():
    """无 _webui_metadata（例如老代码路径）时回退到 SessionEntry 推导。"""
    module = _FakeModule()
    module.glue.question_bridge = QuestionBridge(module)
    tool = AskQuestionTool(module, SimpleNamespace(
        session_key="workspace:w1:s1", runtime_snapshot_id="snap1"), None)
    ctx = tool._question_context()
    assert ctx["workspace_id"] == "w1"
    assert ctx["workspace_session_id"] == "s1"
    assert ctx["snapshot_id"] == "snap1"
    assert ctx["message_id"] == ""


def test_ask_question_tool_prefers_turn_metadata_over_entry():
    """AskQuestionTool 以当前 turn 的 agent._webui_metadata 为准
    （dispatcher 每次 agent.run 前写入）：message_id/workspace/snapshot 均取自
    该 turn 元数据，而非 SessionEntry 上的旧值。"""
    module = _FakeModule()
    module.glue.question_bridge = QuestionBridge(module)
    tool = AskQuestionTool(module, SimpleNamespace(
        session_key="workspace:w1:s1", runtime_snapshot_id="entry-snap"),
        SimpleNamespace(_webui_metadata={
            "workspace_id": "w1", "workspace_session_id": "s1",
            "snapshot_id": "turn-snap", "message_id": "msg-7",
            "session_key": "workspace:w1:s1"}))
    ctx = tool._question_context()
    assert ctx["message_id"] == "msg-7"          # 当前 turn 的 message_id
    assert ctx["snapshot_id"] == "turn-snap"     # 元数据优先于 entry
    assert ctx["workspace_id"] == "w1"
    assert ctx["workspace_session_id"] == "s1"


def test_ask_question_tool_rejects_bad_input():
    module = _FakeModule()
    bridge = QuestionBridge(module)
    module.glue.question_bridge = bridge
    tool = AskQuestionTool(module, SimpleNamespace(
        session_key="webui:default", runtime_snapshot_id=""), None)
    out = json.loads(tool.execute(
        question="", options=[{"id": "a", "label": "A"}]))
    assert out["status"] == "invalid"
    assert bridge.list_pending() == []


# ============================================================
# 取消语义（修复"取消后 LLM 不知道 → 重复弹窗"）+ GET 恢复归属字段
# ============================================================

def test_cancel_wakes_ask_with_cancelled_status():
    """用户取消：ask() 以 status=cancelled 返回（LLM 明确知道用户取消），
    pending 清空，SSE 发 question.resolved(cancelled)。"""
    module = _FakeModule()
    bus = module.bus
    bridge = QuestionBridge(module)
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="选一个方案",
        options=[{"id": "a", "label": "A"}], timeout_s=10)
    pending = _wait_pending(bridge)
    qid = pending[0]["id"]

    assert bridge.cancel(qid, context={"session_key": "s1"}) == RESOLVE_OK
    thread.join(timeout=5)
    result = holder["value"]
    assert result["status"] == "cancelled"
    assert result["question_id"] == qid
    assert "selected_option_ids" not in result  # fail-closed：不携带任何选择
    assert bridge.list_pending() == []
    resolved = [e for e in bus.events if e[0] == "question.resolved"]
    assert resolved and resolved[-1][1]["status"] == "cancelled"


def test_cancel_unknown_or_answered():
    bridge = QuestionBridge(_FakeModule())
    assert bridge.cancel("q-missing") == RESOLVE_NOT_FOUND
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="q",
        options=[{"id": "a", "label": "A"}], timeout_s=10)
    qid = _wait_pending(bridge)[0]["id"]
    assert bridge.resolve(qid, {"selected_option_ids": ["a"]},
                          context={"session_key": "s1"}) == RESOLVE_OK
    thread.join(timeout=5)
    assert bridge.cancel(qid) == RESOLVE_ALREADY_ANSWERED


def test_cancel_context_mismatch():
    bridge = QuestionBridge(_FakeModule())
    holder, thread = _ask_thread(
        bridge, session_key="s1", question="q",
        options=[{"id": "a", "label": "A"}],
        context={"message_id": "m-1"}, timeout_s=10)
    qid = _wait_pending(bridge)[0]["id"]
    assert bridge.cancel(qid, context={"session_key": "s1", "message_id": "other"}) == RESOLVE_CONTEXT_MISMATCH
    # 取消被拒后问题仍在等待；正确归属可取消
    assert bridge.cancel(qid, context={"session_key": "s1", "message_id": "m-1"}) == RESOLVE_OK
    thread.join(timeout=5)


def test_list_pending_payload_carries_top_level_ownership():
    """GET 恢复载荷必须带顶层 workspace/message 归属字段（与 SSE 一致）：
    前端 normalizeQuestion 只读顶层字段，缺失会导致答复 POST 归属校验 403
    （"刷新/轮询后无法答复"）。"""
    bridge = QuestionBridge(_FakeModule())
    holder, thread = _ask_thread(
        bridge, session_key="workspace:w1:s1", question="q",
        options=[{"id": "a", "label": "A"}],
        context={"workspace_id": "w1", "workspace_session_id": "s1",
                 "snapshot_id": "snap-1", "message_id": "m-9"},
        timeout_s=10)
    pending = _wait_pending(bridge)
    item = pending[0]
    assert item["message_id"] == "m-9"
    assert item["workspace_id"] == "w1"
    assert item["workspace_session_id"] == "s1"
    assert item["snapshot_id"] == "snap-1"
    assert item["context"]["message_id"] == "m-9"  # 嵌套 context 保留（兼容）
    # 归属必须完整回传（与前端 answer/dismiss 一致）：缺任一记录已有字段即 mismatch
    assert bridge.cancel(item["id"], context={
        "session_key": "workspace:w1:s1", "workspace_id": "w1",
        "workspace_session_id": "s1", "snapshot_id": "snap-1",
        "message_id": "m-9"}) == RESOLVE_OK
    thread.join(timeout=5)


def test_api_question_cancel_route():
    """POST /api/questions/{qid}/cancel：200 取消成功；404 未知；409 已答复。"""
    module = _FakeModule()
    bridge = QuestionBridge(module)
    module.glue.question_bridge = bridge

    async def _run():
        app = _api_app(module)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            holder, thread = _ask_thread(
                bridge, session_key="s1", question="q",
                options=[{"id": "a", "label": "A"}], timeout_s=10)
            qid = _wait_pending(bridge)[0]["id"]
            # 未知问题 → 404
            resp = await client.post("/api/questions/q-missing/cancel", data=json.dumps({}))
            assert resp.status == 404
            # 正常取消 → 200 + cancelled
            resp = await client.post(
                f"/api/questions/{qid}/cancel",
                data=json.dumps({"session_key": "s1"}),
                headers={"Content-Type": "application/json"})
            assert resp.status == 200
            payload = await resp.json()
            assert payload["ok"] is True and payload["status"] == "cancelled"
            thread.join(timeout=5)
            assert holder["value"]["status"] == "cancelled"
            # 已终结问题再取消 → 409
            resp = await client.post(
                f"/api/questions/{qid}/cancel",
                data=json.dumps({"session_key": "s1"}),
                headers={"Content-Type": "application/json"})
            assert resp.status == 409
        finally:
            await client.close()

    asyncio.run(_run())
