# -*- coding: utf-8 -*-
"""
WebUI 胶水层（P3b 完整版）

- send_and_wait：合成消息走漏斗 + future 取回（一切运行期 agent 操作的统一范式）
- Glue.apply_permission_mode：权限三档（ask / allow / unreviewed）
- ApprovalBridge：ask 档审批桥（executor 线程等待，fail-closed 300s）
- plan 两阶段：/plan-preview（只读预览）+ 暂存 + /plan-apply（漏斗内执行）
- /perm /reload-prompt /mcp reload|reconnect 命令处理
"""

import asyncio
import json
import threading
import time
import uuid

from core.plan import PlanManager
from gateway.dispatcher import Dispatcher
from gateway.channels.base import InboundMessage

_PREVIEW_CHARS = 300          # 工具参数/结果预览截断
_APPROVAL_TIMEOUT = 300       # 审批等待上限（< 硬超时 600，fail-closed）
_WRITE_TOOLS = ("bash", "write", "edit", "python", "http", "file_mgr")


async def send_and_wait(module, session_key: str, text: str,
                        timeout: float = 120.0,
                        user_id: str = "webui",
                        images: list = None,
                        metadata: dict = None,
                        message_id: str = None) -> dict:
    """合成 InboundMessage 走漏斗，等 WebuiChannel future 取回回复。

    images: 可选多模态图片块列表（{type:image,source:base64,...}），透传给 agent.run。
    metadata: Phase 4 工作区运行上下文（workspace_id/snapshot_id 等），透传给 Dispatcher。
    message_id: 可选显式消息 ID（工作区会话去重/审批归属用）。
    """
    message_id = message_id or f"webui-{uuid.uuid4().hex[:12]}"
    fut = module.channel.register_future(session_key, message_id)
    msg = InboundMessage(
        channel="webui",
        session_key=session_key,
        user_id=user_id,
        user_name="WebUI",
        text=text,
        message_id=message_id,
        images=images or [],
        metadata=dict(metadata or {}),
    )
    await module.dispatcher.on_inbound(msg)
    try:
        reply = await asyncio.wait_for(fut, timeout=timeout)
        return {"ok": True, "reply": reply,
                "session_key": session_key, "message_id": message_id}
    except asyncio.TimeoutError:
        module.channel.discard_future(session_key, message_id)
        return {"ok": False, "error": f"timeout({timeout}s)",
                "session_key": session_key, "message_id": message_id}


# ============================================================
# ApprovalBridge —— ask 档审批桥
# ============================================================

class ApprovalBridge:
    """agent.ask_callback 的实现：executor 线程阻塞等待 WebUI 审批。

    fail-closed：300s 超时自动 n；WebUIModule.stop() 时全部置 n。
    """

    def __init__(self, module):
        self.module = module
        self._pending: dict = {}   # id -> dict(answer/event/...)
        self._lock = threading.Lock()

    def ask(self, session_key: str, tool_name: str, params: dict,
            metadata: dict = None) -> str:
        aid = uuid.uuid4().hex[:8]
        evt = threading.Event()
        params_preview = str(params)[:_PREVIEW_CHARS]
        meta = dict(metadata or {})
        record = {"answer": "", "event": evt, "created_at": time.time(),
                  "tool": tool_name, "session_key": session_key,
                  "params_preview": params_preview,
                  "workspace_id": meta.get("workspace_id", ""),
                  "workspace_session_id": meta.get("workspace_session_id", ""),
                  "snapshot_id": meta.get("snapshot_id", ""),
                  "message_id": meta.get("message_id", "")}
        with self._lock:
            self._pending[aid] = record
        self.module.bus.publish("approval.requested", {
            "id": aid,
            "session_key": session_key,
            "tool": tool_name,
            "params_preview": params_preview,
            "workspace_id": record["workspace_id"],
            "workspace_session_id": record["workspace_session_id"],
            "snapshot_id": record["snapshot_id"],
            "message_id": record["message_id"],
        })
        got = evt.wait(timeout=_APPROVAL_TIMEOUT)
        with self._lock:
            self._pending.pop(aid, None)
        answer = record["answer"] if (got and record["answer"]) else "n"
        waited = round(time.time() - record["created_at"], 1)
        self.module.bus.publish("approval.resolved", {
            "id": aid, "session_key": session_key,
            "answer": answer, "waited_s": waited,
            "timeout": not got,
        })
        return answer

    def resolve(self, aid: str, answer: str,
                context: dict = None) -> bool:
        """答复审批。context 可含 workspace_id/session_id/message_id/snapshot_id，
        与 pending 记录不匹配时拒绝（防止跨消息/跨页面答复）。
        """
        if answer not in ("y", "n", "a", "s"):
            return False
        with self._lock:
            record = self._pending.get(aid)
            if record is None:
                return False
            if context:
                for key in ("workspace_id", "workspace_session_id",
                            "snapshot_id", "message_id"):
                    want = context.get(key)
                    if want and record.get(key) and record.get(key) != want:
                        return False
            record["answer"] = answer
            record["event"].set()
        return True

    def list_pending(self) -> list:
        now = time.time()
        with self._lock:
            return [
                {"id": aid,
                 "session_key": r.get("session_key", ""),
                 "tool": r.get("tool", ""),
                 "params_preview": r.get("params_preview", ""),
                 "waited_s": round(now - r["created_at"], 1)}
                for aid, r in self._pending.items()
            ]

    def fail_close_all(self):
        with self._lock:
            for r in self._pending.values():
                if not r["answer"]:
                    r["answer"] = "n"
                r["event"].set()


# ============================================================
# Glue —— 胶水汇总
# ============================================================

class Glue:
    def __init__(self, module):
        self.module = module
        self.bridge = ApprovalBridge(module)
        self.plan_manager = PlanManager(module.runtime_store)
    def init_agent(self, agent, entry):
        """Attach WebUI approval/session state to a lazily-created Agent.

        Dispatcher invokes initializers inside the same executor thread that
        creates the Agent. Installing the callback here avoids a race where
        the first tool call is evaluated before the WebUI approval bridge is
        available. Runtime-native tool events remain the sole event path;
        this method intentionally does not install the removed BridgeHook.
        """
        session_key = getattr(entry, "session_key", "")
        agent._webui_session_key = session_key
        configured = self.module.dispatcher.agent_config.get("permission_mode", "allow")
        mode = getattr(agent, "_gateway_permission_mode", None) or configured
        try:
            self.apply_permission_mode(agent, mode)
        except ValueError:
            import logging
            logging.getLogger("jk_agent.gateway").warning(
                "invalid WebUI permission mode %r; falling back to ask", mode)
            self.apply_permission_mode(agent, "ask")

    # ---------- 权限三档 ----------

    def apply_permission_mode(self, agent, mode: str):
        """切换权限档位（运行期即时生效，set_rule 无缓存）"""
        from core.config_loader import load_config
        from core.permission import ALLOW, DENY

        cfg_perm = load_config().get("permission", {})
        if mode == "ask":
            agent.permission._init_default_rules(cfg_perm)
            agent.ask_callback = (
                lambda tool, params, _a=agent: self.bridge.ask(
                    self._session_key_of(_a), tool, params,
                    metadata=getattr(_a, "_webui_metadata", None) or {}))
        elif mode == "allow":
            agent.permission._init_default_rules(cfg_perm)
            for t in _WRITE_TOOLS:
                agent.permission.set_rule(t, ALLOW)
            agent.ask_callback = None
        elif mode == "unreviewed":
            for t in agent.tool_registry.list_tool_names():
                agent.permission.set_rule(t, ALLOW)
            agent.ask_callback = None
        elif mode == "readonly":
            # Read-only is intentionally a strict allowlist: inspecting local
            # files and using approved search tools are allowed; every write,
            # execution, scheduling, process, or administration tool is denied.
            for t in agent.tool_registry.list_tool_names():
                agent.permission.set_rule(t, DENY)
            for t in ("read", "grep", "glob", "search", "web_fetch"):
                if agent.tool_registry.get_tool(t) is not None:
                    agent.permission.set_rule(t, ALLOW)
            agent.ask_callback = None
        else:
            raise ValueError("invalid permission mode; expected readonly/ask/allow/unreviewed")

    @staticmethod
    def _session_key_of(agent) -> str:
        return getattr(agent, "_webui_session_key", "") or ""

    # ---------- /perm 命令 ----------

    async def handle_perm_command(self, arg: str, ctx: dict) -> str:
        agent = ctx["agent"]
        entry = ctx["entry"]
        mode = arg.strip().lower()
        if not mode:
            return "用法: /perm readonly|ask|allow|unreviewed"
        try:
            self.apply_permission_mode(agent, mode)
        except ValueError as e:
            return f"❌ {e}"
        agent._webui_session_key = entry.session_key
        try:
            from gateway.agent_factory import update_map_meta
            update_map_meta(entry.session_key, permission_mode=mode)
        except Exception:
            pass
        warn = ""
        if mode == "unreviewed":
            sb = getattr(agent, "sandbox", None)
            if sb is None or not getattr(sb, "enabled", True):
                warn = "\n⚠️ 沙箱未启用：unreviewed 已退化为完全放开"
        return f"✅ 权限档位已切换: {mode}{warn}"

    # ---------- Plan preview / persistence ----------

    def plan_preview_sync(self, agent, text: str) -> dict:
        """Generate typed Plan data without mutating conversation or runtime state."""
        plan = agent.generate_plan(text)
        steps = plan.get("steps") if isinstance(plan, dict) else None
        if not isinstance(steps, list) or not steps:
            raise ValueError("方案生成失败：未识别出有效任务")
        return {
            "plan": plan,
            "tasks": [{"id": item.get("id", index), "description": item.get("description", "")}
                      for index, item in enumerate(steps, start=1) if isinstance(item, dict)],
        }

    def create_plan(self, session_key: str, text: str, plan: dict):
        """Persist a typed preview instead of retaining a process-local TTL object."""
        session_id = Dispatcher._runtime_session_id(session_key)
        self.module.runtime_store.upsert_session(session_id, session_key, channel="webui", status="active")
        return self.plan_manager.create_preview(
            session_id, plan, source_prompt=text, title=(text[:120] or "执行方案"),
        )

    async def handle_plan_preview_command(self, arg: str, ctx: dict) -> str:
        """/plan-preview {text}: generate a typed Plan in the session executor."""
        text = arg.strip()
        if not text:
            return json.dumps({"ok": False, "error": "缺少任务描述"}, ensure_ascii=False)
        try:
            preview = await ctx["loop"].run_in_executor(
                ctx["executor"], self.plan_preview_sync, ctx["agent"], text)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, **preview}, ensure_ascii=False)
    # ---------- /reload-prompt 与 /mcp ----------

    async def handle_reload_prompt_command(self, arg: str, ctx: dict) -> str:
        agent = ctx["agent"]
        await ctx["loop"].run_in_executor(
            ctx["executor"], agent._rebuild_system_prompt)
        return "✅ 提示词已重载（重读磁盘四文件）"

    async def handle_mcp_command(self, arg: str, ctx: dict) -> str:
        agent = ctx["agent"]
        parts = arg.split(None, 1)
        sub = parts[0].lower() if parts else ""
        if sub == "reload":
            from core.config_loader import load_config

            def _reload():
                cfg = load_config(force_reload=True)
                agent.reload_mcp(cfg.get("mcp", {}).get("servers", []))

            await ctx["loop"].run_in_executor(ctx["executor"], _reload)
            self.module.bus.publish("mcp.changed",
                                    {"action": "reload",
                                     "session_key": ctx["entry"].session_key})
            return "✅ MCP 配置已重载（按 config 全量 diff）"
        if sub == "reconnect":
            name = parts[1].strip() if len(parts) > 1 else ""
            if not name:
                return "用法: /mcp reconnect <name>"
            await ctx["loop"].run_in_executor(
                ctx["executor"], agent.reconnect_mcp, name)
            self.module.bus.publish("mcp.changed",
                                    {"action": "reconnect", "server": name,
                                     "session_key": ctx["entry"].session_key})
            return f"✅ MCP 已重连: {name}"
        return "用法: /mcp reload | /mcp reconnect <name>"
