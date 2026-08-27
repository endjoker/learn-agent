# -*- coding: utf-8 -*-
"""
Agent 工厂 SQLite 历史回放恢复 —— 单元测试。

覆盖（方案 1：统一会话链路写入侧/恢复侧同以 SQLite 为权威的闭环）：
- turn_nodes → agent.messages 的映射（user/assistant/tool/user_steering，
  reasoning/status 不回放；tool 摘要可解析还原原生结构、残缺降级纯文本）；
- JSON 缺失/为空 → SQLite 全量回放；
- JSON 陈旧（最新 Turn 晚于文件落盘）→ 锚点增量合并，不重复；
- 非工作区分支：sessions_map 元数据回填、无历史时新建会话；
- 回放序列经 AgentContext 校验无孤立 tool 消息。
"""

import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

from core.runtime import RuntimeStore
from core.agent_runtime.context import AgentContext
from gateway.webui.workspace_store import WorkspaceDatabase
from gateway.conversation import ConversationStore
from gateway.conversation.models import TurnNodeType  # noqa: F401


# ============================================================
# 测试桩：绕开真实 Agent 创建（工厂内部 from agent import create_agent
# 走 sys.modules，注入假模块即可）
# ============================================================

class FakeStore:
    def __init__(self, agent):
        self.session_id = "gen0001"
        self.model_id = ""
        self._messages = agent.messages

    def load_session_data(self, data):
        self.session_id = data.get("session_id", self.session_id)
        self.model_id = data.get("model_id", "") or ""
        self._messages.clear()
        for m in data.get("messages", []):
            msg = {"role": m["role"], "content": m.get("content", "")}
            for key in ("kind", "tool_calls", "tool_call_id", "is_error"):
                if key in m:
                    msg[key] = m[key]
            self._messages.append(msg)


class FakeAgent:
    def __init__(self):
        self.messages = []
        self.system_prompt = "SYSTEM-PROMPT"
        self.store = FakeStore(self)
        self.llm = types.SimpleNamespace(model="base-model")
        self.switch_calls = []
        self.permission = types.SimpleNamespace(default_mode="ask")
        self.auto_approve_plan = None
        self._gateway_permission_mode = None

    def switch_llm(self, **kwargs):
        self.switch_calls.append(kwargs)
        if kwargs.get("model"):
            self.llm.model = kwargs["model"]


class FactoryResumeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.runtime = RuntimeStore(self.tmp_path / "runtime.db")
        self.db = WorkspaceDatabase(runtime_store=self.runtime)
        self.store = ConversationStore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- 统一会话数据构造 ----

    def new_conversation(self, session_key, origin="webui", subtype="main",
                         workspace_id=None):
        conv, _created = self.store.create_conversation(
            session_key, origin=origin, subtype=subtype,
            workspace_id=workspace_id, execution_scope="gateway:default")
        return conv

    def add_turn(self, conv, index, nodes_spec):
        """nodes_spec: [(type, text, metadata), ...] → 建 Turn+Nodes 并回填
        确定性的 started_at（避免同毫秒排序不稳定）。"""
        with self.store.transaction() as conn:
            turn = self.store.create_turn(conn, conv.conversation_id,
                                          status="done")
            for pos, (ntype, text, meta) in enumerate(nodes_spec, start=1):
                self.store.create_node(
                    conn, conversation_id=conv.conversation_id,
                    turn_id=turn.turn_id, type=ntype, status="done",
                    text=text, position=pos, metadata=meta)
            conn.execute("UPDATE turns SET started_at=? WHERE turn_id=?",
                         (f"2026-01-01T00:00:{index:02d}.000+00:00",
                          turn.turn_id))
        return turn

    def write_session_json(self, session_id, messages, *, model_id="",
                           mtime=None):
        fp = self.tmp_path / f"{session_id}.json"
        data = {"schema_version": 3, "session_id": session_id,
                "model_id": model_id, "messages": messages}
        fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if mtime is not None:
            os.utime(fp, (mtime, mtime))
        return fp

    # ---- 工厂环境补丁（假 agent 模块 + 重定向 sessions 目录/映射表）----

    @contextmanager
    def patched_factory(self, fake_agent=None):
        import gateway.agent_factory as af
        import core.message_store as message_store_mod
        fake_module = types.ModuleType("agent")
        holder = {"agent": fake_agent or FakeAgent()}

        def fake_create_agent(**kwargs):
            agent = holder["agent"]
            agent.created_kwargs = kwargs
            return agent

        fake_module.create_agent = fake_create_agent
        saved = (sys.modules.get("agent"), af._MAP_FILE,
                 message_store_mod.DEFAULT_SESSION_DIR)
        sys.modules["agent"] = fake_module
        af._MAP_FILE = self.tmp_path / "sessions_map.json"
        message_store_mod.DEFAULT_SESSION_DIR = Path(self.tmp_path)
        try:
            yield af, holder
        finally:
            if saved[0] is not None:
                sys.modules["agent"] = saved[0]
            else:
                sys.modules.pop("agent", None)
            af._MAP_FILE = saved[1]
            message_store_mod.DEFAULT_SESSION_DIR = saved[2]

    def run_factory(self, session_key, *, conversation_store=None,
                    fake_agent=None, **kwargs):
        with self.patched_factory(fake_agent=fake_agent) as (af, holder):
            agent = af.create_gateway_agent(
                session_key=session_key,
                conversation_store=conversation_store, **kwargs)
        return agent, holder["agent"], af


# ============================================================
# 节点 → messages 映射
# ============================================================

class NodeMappingTests(FactoryResumeBase):
    def test_full_turn_mapping_skips_reasoning_and_status(self):
        conv = self.new_conversation("webui:mapping")
        self.add_turn(conv, 0, [
            ("user", "查一下状态", {}),
            ("reasoning", "thinking...", {}),
            ("status", "tool", {}),
            ("assistant", "我来查看。", {"intermediate": True}),
            ("tool", "", {"call_id": "call-1", "tool": "grep",
                          "params_summary": '{"pattern": "x"}',
                          "result_summary": "match line"}),
            ("assistant", "结论如下", {"final": True, "intermediate": False}),
        ])
        turn = self.store.list_turns(conv.conversation_id)[0]
        nodes = self.store.get_turn_nodes(turn.turn_id)
        import gateway.agent_factory as af
        messages = af._turn_nodes_to_messages(nodes)
        self.assertEqual([m["role"] for m in messages],
                         ["user", "assistant", "tool", "assistant"])
        carrier = messages[1]
        self.assertEqual(carrier["kind"], "tool_calls")
        self.assertEqual(carrier["content"], "我来查看。")
        self.assertEqual(len(carrier["tool_calls"]), 1)
        tc = carrier["tool_calls"][0]
        self.assertEqual(tc["id"], "call-1")
        self.assertEqual(tc["function"]["name"], "grep")
        self.assertEqual(json.loads(tc["function"]["arguments"]),
                         {"pattern": "x"})
        result = messages[2]
        self.assertEqual(result["tool_call_id"], "call-1")
        self.assertEqual(result["content"], "match line")
        self.assertFalse(result["is_error"])
        self.assertEqual(messages[3]["kind"], "final")

    def test_truncated_params_degrade_to_plain_note_without_orphans(self):
        import gateway.agent_factory as af
        run = [types.SimpleNamespace(
            node_id="n1", metadata={
                "call_id": "call-x", "tool": "bash",
                # 200 字符截断产生的残缺 JSON → 解析失败
                "params_summary": '{"command": "grep -rn \"api|9120\" src | hea',
                "result_summary": "output..."})]
        messages = af._tool_run_messages("先搜一下", run)
        self.assertEqual(len(messages), 1)
        msg = messages[0]
        self.assertEqual(msg["role"], "assistant")
        self.assertNotIn("tool_calls", msg)
        self.assertIn("bash", msg["content"])
        self.assertIn("先搜一下", msg["content"])
        kept = AgentContext(messages + [{"role": "user",
                                         "content": "next"}]).llm_messages()
        self.assertFalse(any(m.get("role") == "tool" for m in kept))

    def test_tool_error_flag_preserved(self):
        import gateway.agent_factory as af
        run = [types.SimpleNamespace(node_id="n2", metadata={
            "call_id": "c9", "tool": "read",
            "params_summary": "{}", "result_summary": "boom",
            "error_code": "TOOL_EXECUTION_ERROR"})]
        messages = af._tool_run_messages("", run)
        self.assertTrue(messages[1]["is_error"])

    def test_user_steering_replayed_as_user_message(self):
        import gateway.agent_factory as af
        nodes = [
            types.SimpleNamespace(type="user", text="开始吧", metadata={},
                                  node_id="u1"),
            types.SimpleNamespace(type="assistant", text="好的", metadata={},
                                  node_id="a1"),
            types.SimpleNamespace(type="user_steering", text="等等，改需求",
                                  metadata={}, node_id="s1"),
        ]
        messages = af._turn_nodes_to_messages(nodes)
        self.assertEqual([m["role"] for m in messages],
                         ["user", "assistant", "user"])


# ============================================================
# SQLite 历史收集与锚点合并
# ============================================================

class CollectHistoryTests(FactoryResumeBase):
    def test_missing_conversation_returns_none(self):
        import gateway.agent_factory as af
        self.assertIsNone(af._collect_sqlite_history(self.store, "webui:none"))

    def test_full_replay_across_turns_in_order(self):
        import gateway.agent_factory as af
        conv = self.new_conversation("webui:replay")
        self.add_turn(conv, 0, [("user", "第一问", {})])
        self.add_turn(conv, 1, [("assistant", "第一答", {"final": True})])
        self.add_turn(conv, 3, [
            ("user", "第二问", {}),
            ("user_steering", "补充一点", {}),
            ("assistant", "第二答", {"final": True}),
        ])
        hist = af._collect_sqlite_history(self.store, "webui:replay")
        self.assertTrue(hist["anchored"])
        roles = [m["role"] for m in hist["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user", "user",
                                 "assistant"])
        texts = [m["content"] for m in hist["messages"]]
        self.assertEqual(texts, ["第一问", "第一答", "第二问", "补充一点",
                                 "第二答"])
        self.assertTrue(hist["newest_started_at"].startswith(
            "2026-01-01T00:00:03"))

    def test_anchor_trims_to_tail_after_last_match(self):
        import gateway.agent_factory as af
        conv = self.new_conversation("webui:anchor")
        self.add_turn(conv, 0, [("user", "重复的问题", {})])
        self.add_turn(conv, 1, [("user", "重复的问题", {})])  # 同文本两次
        self.add_turn(conv, 2, [("assistant", "尾答", {"final": True})])
        hist = af._collect_sqlite_history(
            self.store, "webui:anchor", anchor_user_text="重复的问题")
        self.assertTrue(hist["anchored"])
        self.assertEqual([m["content"] for m in hist["messages"]], ["尾答"])

    def test_anchor_not_found_marks_unanchored(self):
        import gateway.agent_factory as af
        conv = self.new_conversation("webui:anchor-miss")
        self.add_turn(conv, 0, [("user", "sqlite 才有的问题", {})])
        hist = af._collect_sqlite_history(
            self.store, "webui:anchor-miss",
            anchor_user_text="json 才有的问题")
        self.assertFalse(hist["anchored"])
        self.assertEqual(hist["messages"], [])

    def test_empty_conversation_returns_none(self):
        import gateway.agent_factory as af
        self.new_conversation("webui:empty")
        self.assertIsNone(
            af._collect_sqlite_history(self.store, "webui:empty"))


# ============================================================
# 工厂分支集成
# ============================================================

class WorkspaceBranchTests(FactoryResumeBase):
    WS_KEY = "workspace:ws_x:wss_deadbeefcafe"

    def test_resumes_from_sqlite_when_json_missing(self):
        conv = self.new_conversation(
            self.WS_KEY, origin="webui", subtype="workspace",
            workspace_id="ws_x")
        self.add_turn(conv, 0, [("user", "你好", {})])
        self.add_turn(conv, 1, [("assistant", "你好呀", {"final": True})])
        agent, fake, af = self.run_factory(self.WS_KEY,
                                           conversation_store=self.store)
        self.assertTrue(fake.messages)
        self.assertEqual(fake.messages[0]["role"], "system")
        self.assertEqual(fake.messages[0]["content"], "SYSTEM-PROMPT")
        self.assertEqual([m["role"] for m in fake.messages[1:]],
                         ["user", "assistant"])
        self.assertEqual(fake.store.session_id, "wss_deadbeefcafe")
        self.assertEqual(agent._gateway_permission_mode, "allow")

    def test_fresh_when_no_history_anywhere(self):
        agent, fake, af = self.run_factory(self.WS_KEY,
                                           conversation_store=self.store)
        self.assertEqual(fake.messages, [])
        self.assertEqual(fake.store.session_id, "wss_deadbeefcafe")

    def test_sqlite_is_authoritative_regardless_of_json(self):
        """退役后契约：SQLite 统一会话是唯一权威，JSON 转录不再参与恢复。"""
        conv = self.new_conversation(
            self.WS_KEY, origin="webui", subtype="workspace",
            workspace_id="ws_x")
        self.add_turn(conv, 0, [
            ("user", "旧问题", {}),
            ("assistant", "SQLite 侧回答", {"final": True}),
        ])
        with self.store.transaction() as conn:
            conn.execute("UPDATE turns SET started_at=?",
                         ("2020-01-01T00:00:00.000+00:00",))
        # 即使存在更新的 JSON 转录文件，也不再读取
        self.write_session_json("wss_deadbeefcafe", [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "JSON 侧回答"},
        ], mtime=os.path.getmtime(__file__))
        _, fake, _af = self.run_factory(self.WS_KEY,
                                        conversation_store=self.store)
        contents = [m.get("content") for m in fake.messages]
        self.assertIn("SQLite 侧回答", contents)
        self.assertNotIn("JSON 侧回答", contents)

    def test_stale_json_gets_sqlite_tail_appended(self):
        conv = self.new_conversation(
            self.WS_KEY, origin="webui", subtype="workspace",
            workspace_id="ws_x")
        # T0 与 JSON 重叠；T1/T2 仅存在于 SQLite（停写后的新历史）
        self.add_turn(conv, 0, [
            ("user", "重叠问题", {}),
            ("assistant", "重叠回答", {"final": True}),
        ])
        self.add_turn(conv, 1, [
            ("user", "新问题一", {}),
            ("assistant", "新答一", {"final": True}),
        ])
        self.add_turn(conv, 2, [
            ("user", "新问题二", {}),
            ("assistant", "新答二", {"final": True}),
        ])
        self.write_session_json("wss_deadbeefcafe", [
            {"role": "user", "content": "重叠问题"},
            {"role": "assistant", "content": "重叠回答"},
        ], mtime=1000000000.0)  # 远早于 Turn 时间戳 → 陈旧转录
        _, fake, _af = self.run_factory(self.WS_KEY,
                                        conversation_store=self.store)
        contents = [m.get("content") for m in fake.messages
                    if m.get("role") != "system"]
        self.assertEqual(contents, ["重叠问题", "重叠回答", "新问题一",
                                    "新答一", "新问题二", "新答二"])

    def test_sqlite_replay_ignores_json_when_anchor_unmatched(self):
        """退役后契约：无锚点概念，SQLite 全量回放，JSON 完全不参与。"""
        conv = self.new_conversation(
            self.WS_KEY, origin="webui", subtype="workspace",
            workspace_id="ws_x")
        self.add_turn(conv, 0, [("user", "完全不同的问题", {})])
        self.write_session_json("wss_deadbeefcafe", [
            {"role": "user", "content": "锚点对不上的问题"},
            {"role": "assistant", "content": "答"},
        ], mtime=1000000000.0)
        _, fake, _af = self.run_factory(self.WS_KEY,
                                        conversation_store=self.store)
        contents = [m.get("content") for m in fake.messages]
        self.assertIn("完全不同的问题", contents)  # SQLite 权威回放
        self.assertNotIn("锚点对不上的问题", contents)  # JSON 不再读取


class MainSessionBranchTests(FactoryResumeBase):
    KEY = "webui:default"

    def _register_map(self, session_id, model="meta-model",
                      perm="unreviewed"):
        (self.tmp_path / "sessions_map.json").write_text(json.dumps({
            self.KEY: {"session_id": session_id, "model": model,
                       "permission_mode": perm}}), encoding="utf-8")

    def test_resumes_from_sqlite_with_meta_backfill(self):
        self._register_map("abc12345")
        conv = self.new_conversation(self.KEY)
        self.add_turn(conv, 0, [("user", "主会话问题", {})])
        self.add_turn(conv, 1, [("assistant", "主会话回答", {"final": True})])
        agent, fake, af = self.run_factory(self.KEY,
                                           conversation_store=self.store)
        self.assertEqual([m.get("content") for m in fake.messages[1:]],
                         ["主会话问题", "主会话回答"])
        self.assertEqual(fake.store.session_id, "abc12345")
        self.assertTrue(any(c.get("model") == "meta-model"
                            for c in fake.switch_calls))

    def test_fresh_creates_map_entry(self):
        agent, fake, af = self.run_factory(self.KEY,
                                           conversation_store=self.store)
        mapping = json.loads((self.tmp_path / "sessions_map.json")
                             .read_text(encoding="utf-8"))
        self.assertIn(self.KEY, mapping)
        self.assertEqual(mapping[self.KEY]["permission_mode"], "allow")


if __name__ == "__main__":
    unittest.main()
