# -*- coding: utf-8 -*-
"""核心运行时修复回归测试（P2-5 / P3-2 / P3-6 / P3-7 / P2-2 / P2-3 / P3-4）。

覆盖：
- P2-5：转录"声明 N 个 call_id 却只有 M 个结果"时按缺失 id 合成占位结果；
  串行工具分支单工具异常不逃逸；交互式 ASK 在 EOF/Ctrl+C 下按拒绝收口；
  ToolRuntime.execute_native_call 兜底。
- P3-7：finish("blocked") 映射为 RunStatus.BLOCKED，不再误记 ERROR。
- P3-6：并行段逐调用经 rt.tool_timeout_for 计算超时（与串行同一口径）。
- P2-2：MCP stdio 子进程 spawn/close 全路径登记孤儿日志 record(pid, True/False)。
- P2-3：call_tool 超时透传到 _send_request；服务器配置 tool_call_timeout 生效；
  MCPTool 内外层超时同口径、文案动态化。
- P3-2：token 锚点只记 input_tokens，不再把刚生成的 output 计入基准。
- P3-4：think() 最终失败抛出最后异常（与 complete() 一致），不再返回 None。
"""
import asyncio
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from core.agent_runtime.context import AgentContext
from core.agent_runtime.loop import AgentLoop
from core.agent_runtime.models import RunStatus
from core.agent_runtime.tools import PreparedToolCall, ToolBatchExecutor
from core.message_store import MessageStore


# ============================================================
# P2-5a：context.py 按声明集合完整校验 + 合成占位结果
# ============================================================

class MissingToolResultContextTests(unittest.TestCase):

    def test_two_declared_one_result_gets_placeholder(self):
        """2 声明 1 结果：为缺失 call_id 合成占位，配对完整、组不丢弃。"""
        source = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [
                 {"id": "call_a", "type": "function",
                  "function": {"name": "t", "arguments": "{}"}},
                 {"id": "call_b", "type": "function",
                  "function": {"name": "t", "arguments": "{}"}},
             ]},
            {"role": "tool", "tool_call_id": "call_a", "content": "ok"},
        ]
        view = AgentContext(source).llm_messages()
        # assistant 声明保留（旧实现只看"后一条是否 tool"会放过本例 → provider 400）
        self.assertEqual(len(view), 4)
        self.assertEqual(view[1]["role"], "assistant")
        self.assertEqual(view[2]["tool_call_id"], "call_a")
        placeholder = view[3]
        self.assertEqual(placeholder["role"], "tool")
        self.assertEqual(placeholder["tool_call_id"], "call_b")
        self.assertTrue(placeholder.get("is_error"))
        # 每个 call_id 都有配对结果
        declared = {c["id"] for c in view[1]["tool_calls"]}
        answered = [m["tool_call_id"] for m in view if m.get("role") == "tool"]
        self.assertEqual(set(answered), declared)
        self.assertEqual(len(answered), len(declared))

    def test_all_results_missing_keeps_assistant_and_synthesizes(self):
        """0 结果：assistant 组不再整组丢弃，全部合成占位。"""
        source = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [
                 {"id": "x1", "type": "function",
                  "function": {"name": "t", "arguments": "{}"}},
             ]},
            {"role": "user", "content": "next"},
        ]
        view = AgentContext(source).llm_messages()
        self.assertEqual([m["role"] for m in view],
                         ["user", "assistant", "tool", "user"])
        self.assertEqual(view[2]["tool_call_id"], "x1")
        self.assertTrue(view[2].get("is_error"))
        self.assertIn("无结果", view[2]["content"])

    def test_complete_pairing_untouched(self):
        """完整配对的正常转录不被改动、不产生占位。"""
        source = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": None, "kind": "tool_calls",
             "tool_calls": [
                 {"id": "a", "type": "function",
                  "function": {"name": "t", "arguments": "{}"}},
                 {"id": "b", "type": "function",
                  "function": {"name": "t", "arguments": "{}"}},
             ]},
            {"role": "tool", "tool_call_id": "a", "content": "ra"},
            {"role": "tool", "tool_call_id": "b", "content": "rb"},
            {"role": "assistant", "content": "done", "kind": "final"},
        ]
        view = AgentContext(source).llm_messages()
        self.assertEqual(len(view), len(source))
        self.assertNotIn("is_error", view[3])
        self.assertNotIn("is_error", view[4])

    def test_sequential_groups_each_paired(self):
        """多组连续声明各自独立校验补齐。"""
        source = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "g1", "type": "function",
                             "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "g1", "content": "r1"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "g2", "type": "function",
                             "function": {"name": "t", "arguments": "{}"}}]},
        ]
        view = AgentContext(source).llm_messages()
        ids = [m.get("tool_call_id") for m in view if m.get("role") == "tool"]
        self.assertEqual(ids, ["g1", "g2"])
        self.assertTrue(view[-1].get("is_error"))


# ============================================================
# P2-5b：串行分支异常兜底 + P2-5 兜底层 execute_native_call
# ============================================================

class SerialBranchExceptionTests(unittest.TestCase):

    def test_serial_exception_becomes_error_result_and_batch_continues(self):
        class Registry:
            def get_tool(self, name): return object()  # 非 parallel_safe

        class Agent:
            tool_registry = Registry()
            _config = {}

            def _execute_native_tool_call(self, call_id, provider, tool, arguments, raw):
                if call_id == "boom":
                    raise RuntimeError("审批桥崩溃")
                return ("ok", False)

        batch = ToolBatchExecutor(Agent())
        calls = [
            PreparedToolCall("boom", "p1", "t1", {}, "{}", 0),
            PreparedToolCall("fine", "p2", "t2", {}, "{}", 1),
        ]
        results = batch.execute(calls)
        self.assertEqual(len(results), 2)          # 本批未被跳过
        self.assertEqual(results[0][0].call_id, "boom")
        self.assertIn("工具执行异常", results[0][1])
        self.assertIn("RuntimeError", results[0][1])
        self.assertTrue(results[0][2])              # is_error
        self.assertEqual(results[1][1], "ok")       # 后续调用继续执行

    def test_execute_native_call_bottom_line_guard(self):
        """execute_native_call 判定链异常转为 typed observation，end 事件仍发一次。"""
        from core.runtime.tool_runtime import ToolRuntime

        class Agent:
            def __init__(self):
                self.events = []

            def _emit_event(self, event_type, **payload):
                self.events.append(event_type)

            def _gate_check(self, name, args):
                raise RuntimeError("gate exploded")

        agent = Agent()
        observation, is_error = ToolRuntime().execute_native_call(
            agent, "cid", "prov", "tool", {})
        self.assertTrue(is_error)
        self.assertIn("❌ 工具执行异常", observation)
        self.assertEqual(agent.events.count("tool_execution_start"), 1)
        self.assertEqual(agent.events.count("tool_execution_end"), 1)


# ============================================================
# P2-5c：交互式 ASK EOF / Ctrl+C 视为拒绝
# ============================================================

class AskUserInterruptedTests(unittest.TestCase):

    def _agent(self):
        from agent import Agent
        agent = object.__new__(Agent)
        agent.non_interactive = False
        agent.ask_callback = None
        return agent

    def test_eof_is_treated_as_denial(self):
        agent = self._agent()
        with patch("builtins.input", side_effect=EOFError):
            answer = agent._ask_user("write_file", {"path": "x"})
        self.assertEqual(answer, "n")

    def test_keyboard_interrupt_is_treated_as_denial(self):
        agent = self._agent()
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            answer = agent._ask_user("write_file", {"path": "x"})
        self.assertEqual(answer, "n")


# ============================================================
# P3-7：blocked 状态映射
# ============================================================

class _FakeStore:
    def __init__(self): self.save_calls = 0
    def save_session(self): self.save_calls += 1


class _BlockedAgent:
    """finish("blocked") 收口的轻量 fake Agent。"""

    def __init__(self):
        self._config = {}
        self._run_id = "run_blocked"
        self._last_run_reason = "completed"
        self._events = []
        self._event_seq = 0
        self.store = _FakeStore()
        self._run_token = None
        self._active_loop = None

    def _consume_pending_llm(self):
        pass

    def _emit_event(self, event_type, **payload):
        self._event_seq += 1
        self._events.append((event_type, payload))

    def _runtime_failure_text(self, exc):
        return str(exc)

    def _run_native_loop(self, prompt, *, images=None, event_sink=None):
        # hook 拦截路径：先 finish("blocked") 再返回拦截文案。
        self._active_loop.finish("blocked")
        return "⛔ 输入被 hook 拦截"


class BlockedStatusTests(unittest.TestCase):

    def test_blocked_reason_maps_to_blocked_status(self):
        agent = _BlockedAgent()
        result = AgentLoop(agent).run("hi")
        self.assertIs(RunStatus.BLOCKED, getattr(RunStatus, "BLOCKED"))
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertNotEqual(result.status, RunStatus.ERROR)
        ends = [p for t, p in agent._events if t == "agent_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].get("reason"), "blocked")


# ============================================================
# P3-6：并行段逐调用 tool_timeout_for 口径统一
# ============================================================

def _make_tool(name, parallel_safe=True, capabilities=()):
    return type(f"Tool_{name}", (), {
        "name": name, "parallel_safe": parallel_safe,
        "capabilities": capabilities,
    })()


class ParallelTimeoutConsistencyTests(unittest.TestCase):

    def test_tool_timeout_for_widens_subprocess_like_serial(self):
        """真实 ToolRuntime：exec 类工具按 subprocess 超时放宽，普通工具用基础值。"""
        from core.runtime.tool_runtime import ToolRuntime
        rt = ToolRuntime(tool_timeout_seconds=1)
        agent = type("A", (), {"_config": {"agent_runtime": {
            "subprocess_timeout_seconds": 6}}})()
        shell = _make_tool("bash", True, capabilities=("exec:shell",))
        plain = _make_tool("web", True)
        self.assertEqual(rt.tool_timeout_for(agent, shell, {}), 6)
        self.assertEqual(rt.tool_timeout_for(agent, plain, {}), 1)
        # arguments['timeout'] 显式放宽同样生效
        self.assertEqual(rt.tool_timeout_for(agent, plain, {"timeout": 9}), 9)

    def test_per_call_timeout_uses_tool_timeout_for(self):
        """并行段逐 future 经 rt.tool_timeout_for 取超时；超时值直接约束等待。"""
        seen = []

        class FakeRt:
            def __init__(self, value):
                self.value = value
                self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="t")

            def ensure_executor_capacity(self, workers): pass

            def executor_pool(self): return self._pool

            def submit_native_call(self, agent, call_id, provider_name,
                                   tool_name, arguments, raw_arguments=None):
                return self._pool.submit(agent._execute_native_tool_call,
                                         call_id, provider_name, tool_name,
                                         arguments, raw_arguments)

            def register_inflight_timeout(self, future): pass

            def tool_timeout_for(self, agent, tool, arguments):
                item = (getattr(tool, "name", None),
                        dict(arguments) if isinstance(arguments, dict) else arguments)
                seen.append(item)
                return self.value

        class Registry:
            def get_tool(self, name): return _make_tool(name, True)

        class Agent:
            pass

        agent = Agent()
        agent.tool_registry = Registry()

        def native(call_id, provider, tool, arguments, raw):
            time.sleep(0.8)
            return "ok", False

        agent._execute_native_tool_call = native
        runtime = FakeRt(5)                       # 放宽后的窗口 > 工具耗时
        agent._tool_runtime = runtime
        batch = ToolBatchExecutor(agent)
        results = batch.execute([
            PreparedToolCall("c1", "t1", "t1", {"timeout": 4}, "{}", 0),
            PreparedToolCall("c2", "t2", "t2", {}, "{}", 1),
        ])
        runtime._pool.shutdown(wait=False)
        agent._parallel_tool_executor.shutdown(wait=False)
        self.assertEqual([r[1] for r in results], ["ok", "ok"])
        # 每条调用各查询一次，且携带自己的 arguments
        self.assertEqual(seen, [("t1", {"timeout": 4}), ("t2", {})])

    def test_narrow_per_call_timeout_times_out(self):
        """tool_timeout_for 返回的窄窗口会真实约束并行 future 等待。"""
        class FakeRt:
            def __init__(self):
                self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="t")

            def ensure_executor_capacity(self, workers): pass

            def executor_pool(self): return self._pool

            def submit_native_call(self, agent, call_id, provider_name,
                                   tool_name, arguments, raw_arguments=None):
                return self._pool.submit(agent._execute_native_tool_call,
                                         call_id, provider_name, tool_name,
                                         arguments, raw_arguments)

            def register_inflight_timeout(self, future): pass

            def tool_timeout_for(self, agent, tool, arguments):
                return 1                          # 秒级窄窗口

        class Registry:
            def get_tool(self, name): return _make_tool(name, True)

        class Agent:
            pass

        agent = Agent()
        agent.tool_registry = Registry()

        def hanging(call_id, provider, tool, arguments, raw):
            time.sleep(3)
            return "late", False

        agent._execute_native_tool_call = hanging
        agent._tool_runtime = FakeRt()
        batch = ToolBatchExecutor(agent)
        start = time.monotonic()
        results = batch.execute(
            [PreparedToolCall("h1", "hang", "hang", {}, "{}", 0)])
        elapsed = time.monotonic() - start
        agent._parallel_tool_executor.shutdown(wait=False)
        self.assertLess(elapsed, 2.5)
        self.assertIn("超时", results[0][1])
        self.assertTrue(results[0][2])


# ============================================================
# P2-2：MCP stdio 孤儿进程登记
# ============================================================

class StdioOrphanRecordTests(unittest.TestCase):

    def _transport(self, command=None, args=None):
        from core.mcp_client import StdioTransport
        return StdioTransport(command or sys.executable,
                              args=args if args is not None else ["-c", "import time; time.sleep(60)"])

    def test_connect_records_true_and_close_records_false(self):
        import core.mcp_client as mcp_client
        transport = self._transport()
        recorder = MagicMock()

        async def scenario():
            await transport.connect()      # connect 与 close 必须在同一事件循环
            pid = transport._process.pid
            await transport.close()
            return pid

        with patch.object(mcp_client, "record_orphan_process", recorder):
            pid = asyncio.run(scenario())
        recorded = [(call.args[0], call.args[1]) for call in recorder.call_args_list]
        self.assertIn((pid, True), recorded)
        self.assertIn((pid, False), recorded)
        # close 的收口是最后一次记录
        self.assertEqual(recorded[-1], (pid, False))
        self.assertIsNotNone(transport._process.returncode)  # 进程确已退出

    def test_reconnect_records_old_pid_false(self):
        import core.mcp_client as mcp_client
        transport = self._transport()
        recorder = MagicMock()

        async def scenario():
            await transport.connect()
            old_pid = transport._process.pid
            await transport.connect()       # 重连替换旧子进程（同一事件循环）
            new_pid = transport._process.pid
            await transport.close()
            return old_pid, new_pid

        with patch.object(mcp_client, "record_orphan_process", recorder):
            old_pid, new_pid = asyncio.run(scenario())
        recorded = [(call.args[0], call.args[1]) for call in recorder.call_args_list]
        self.assertIn((old_pid, True), recorded)
        self.assertIn((old_pid, False), recorded)   # 旧进程被终止即收口
        self.assertIn((new_pid, True), recorded)
        self.assertIn((new_pid, False), recorded)

    def test_failed_spawn_records_nothing(self):
        import core.mcp_client as mcp_client
        transport = self._transport(command="definitely-not-a-real-cmd-xyz", args=[])
        recorder = MagicMock()
        with patch.object(mcp_client, "record_orphan_process", recorder):
            with self.assertRaises(ConnectionError):
                asyncio.run(transport.connect())
        recorder.assert_not_called()


# ============================================================
# P2-3：call_tool 超时透传 + 服务器配置 + MCPTool 文案
# ============================================================

class McpCallTimeoutTests(unittest.TestCase):

    def test_connection_default_call_timeout_resolution(self):
        from core.mcp_client import MCPConnection, MCPTransport

        class _T(MCPTransport):
            async def connect(self): pass
            async def send(self, message): pass
            async def receive(self): return None
            async def close(self): pass
            @property
            def is_connected(self): return True

        conn = MCPConnection("s", _T())
        self.assertEqual(conn.default_call_timeout, 30.0)
        conn2 = MCPConnection("s", _T(), call_timeout=120)
        self.assertEqual(conn2.default_call_timeout, 120.0)
        conn3 = MCPConnection("s", _T(), call_timeout="bad")
        self.assertEqual(conn3.default_call_timeout, 30.0)

    def test_server_config_timeout_precedence(self):
        from core.mcp_client import _resolve_server_call_timeout
        self.assertEqual(_resolve_server_call_timeout({}), 30.0)
        self.assertEqual(_resolve_server_call_timeout({"timeout": 45}), 45.0)
        self.assertEqual(_resolve_server_call_timeout(
            {"tool_call_timeout": 120, "timeout": 45}), 120.0)
        self.assertEqual(_resolve_server_call_timeout({"timeout": "abc"}), 30.0)
        self.assertEqual(_resolve_server_call_timeout({"timeout": -1}), 30.0)

    def test_call_tool_passes_timeout_to_send_request(self):
        from core.mcp_client import MCPConnection, MCPTransport

        captured = {}

        class _T(MCPTransport):
            async def connect(self): pass
            async def send(self, message): pass
            async def receive(self): return None
            async def close(self): pass
            @property
            def is_connected(self): return True

        conn = MCPConnection("s", _T(), call_timeout=77)
        conn._initialized = True

        async def fake_send_request(method, params, timeout=None):
            captured["method"] = method
            captured["timeout"] = timeout
            return {"result": {"content": [], "isError": False}}

        conn._send_request = fake_send_request
        asyncio.run(conn.call_tool("tool", {}))
        self.assertEqual(captured["method"], "tools/call")
        self.assertEqual(captured["timeout"], 77.0)      # 连接级默认注入
        asyncio.run(conn.call_tool("tool", {}, timeout=7))
        self.assertEqual(captured["timeout"], 7.0)       # 显式传入优先

    def test_mcp_tool_wires_same_timeout_to_inner_and_outer(self):
        import core.mcp_client as mcp_client
        from tools.mcp_tools import MCPTool

        seen = {}

        class FakeConn:
            name = "srv"
            default_call_timeout = 42.0

            async def call_tool(self, name, arguments, timeout=None):
                seen["inner_timeout"] = timeout
                seen["args"] = arguments
                return {"content": [{"type": "text", "text": "done"}],
                        "isError": False}

        def fake_run(coro, timeout=None):
            seen["outer_timeout"] = timeout
            return asyncio.run(coro)

        tool = MCPTool(connection=FakeConn(), tool_desc={
            "name": "srv/search", "description": "d",
            "inputSchema": {"type": "object", "properties": {}}})
        with patch.object(mcp_client, "run_in_mcp_loop", fake_run):
            result = tool.execute(query="q", timeout=999)  # 参数含同名键也不冲突
        self.assertEqual(result, "done")
        self.assertEqual(seen["inner_timeout"], 42.0)
        self.assertEqual(seen["outer_timeout"], 42.0 + MCPTool.OUTER_TIMEOUT_GRACE_SECONDS)
        self.assertEqual(seen["args"], {"query": "q", "timeout": 999})

    def test_timeout_message_reports_dynamic_value(self):
        import core.mcp_client as mcp_client
        from tools.mcp_tools import MCPTool

        class FakeConn:
            name = "srv"
            default_call_timeout = 1.0   # 小值便于快速测试；语义同服务器配置

            async def call_tool(self, name, arguments, timeout=None):
                # 模拟协议层 _send_request：按传入 timeout 触发 TimeoutError
                await asyncio.wait_for(asyncio.sleep(60), timeout=timeout)

        tool = MCPTool(connection=FakeConn(), tool_desc={
            "name": "srv/search", "description": "d",
            "inputSchema": {"type": "object", "properties": {}}})

        def fake_run(coro, timeout=None):
            return asyncio.run(asyncio.wait_for(coro, timeout=timeout))

        with patch.object(mcp_client, "run_in_mcp_loop", fake_run):
            start = time.monotonic()
            result = tool.execute()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 10)         # 内层超时先到期，不等满外层
        self.assertIn("1", result)           # 动态真实值，不再是写死 60
        self.assertIn("tool_call_timeout", result)


# ============================================================
# P3-2：token 锚点只记 input_tokens
# ============================================================

class AnchorInputOnlyTests(unittest.TestCase):

    def test_anchor_excludes_output_tokens(self):
        store = MessageStore(max_tokens=100000)
        store.append({"role": "user", "content": "hello"})
        store.set_anchor({"input_tokens": 500, "output_tokens": 7000})
        self.assertEqual(store._anchor_total, 500)
        self.assertEqual(store.stats()["anchored_tokens"], 500)

    def test_live_tokens_builds_on_input_baseline_only(self):
        store = MessageStore(max_tokens=100000)
        store.append({"role": "user", "content": "hello"})
        store.set_anchor({"input_tokens": 500, "output_tokens": 7000})
        baseline = store.live_tokens()
        self.assertEqual(baseline, 500)     # 无新增消息时等于输入基准
        store.append({"role": "assistant", "content": "w" * 400})  # ~100 tokens
        self.assertGreater(store.live_tokens(), baseline)

    def test_empty_usage_keeps_previous_anchor(self):
        store = MessageStore(max_tokens=100000)
        store.set_anchor({"input_tokens": 300, "output_tokens": 10})
        store.set_anchor(None)
        store.set_anchor({})
        self.assertEqual(store._anchor_total, 300)

    def test_reset_anchor_via_public_method(self):
        store = MessageStore(max_tokens=100000)
        store.set_anchor({"input_tokens": 300, "output_tokens": 10})
        store.reset_anchor()
        self.assertEqual(store._anchor_total, 0)
        self.assertFalse(store.stats()["anchored"])


# ============================================================
# P3-4：think() 最终失败抛出最后异常
# ============================================================

class _FakeAdapter:
    def __init__(self, exc):
        self._exc = exc
        self.last_usage = None

    def generate(self, model, messages, temperature, timeout):
        raise self._exc

    def generate_stream(self, model, messages, temperature, timeout):
        raise self._exc
        yield  # pragma: no cover


def _client(exc):
    from core.llm_client import JKAgentLLM as LLMClient
    client = object.__new__(LLMClient)
    client.llm_type = "cloud"
    client.model = "test-model"
    client.base_url = "http://localhost"
    client.last_usage = None
    client._config_timeout = 5
    client._adapter = _FakeAdapter(exc)
    client._retry_delay = lambda attempt, exception: 0
    return client


class ThinkRaisesTests(unittest.TestCase):

    def test_non_retryable_error_raises_immediately(self):
        client = _client(ValueError("bad request"))
        with self.assertRaises(ValueError):
            client.think([{"role": "user", "content": "hi"}],
                         stream=False, silent=True)

    def test_retryable_error_raises_after_retries(self):
        exc = OSError("connection reset")
        exc.status_code = 503
        client = _client(exc)
        with self.assertRaises(OSError):
            client.think([{"role": "user", "content": "hi"}],
                         stream=False, silent=True)

    def test_success_still_returns_text(self):
        from core.llm_client import JKAgentLLM as LLMClient
        client = _client(ValueError("unused"))

        class _OkAdapter:
            last_usage = {"input_tokens": 1}

            def generate(self, model, messages, temperature, timeout):
                class _Resp:
                    text = "fine"
                    usage = {"input_tokens": 1, "output_tokens": 2}
                return _Resp()

        client._adapter = _OkAdapter()
        self.assertEqual(
            client.think([{"role": "user", "content": "hi"}],
                         stream=False, silent=True),
            "fine")


if __name__ == "__main__":
    unittest.main()
