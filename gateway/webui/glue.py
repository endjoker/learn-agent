# -*- coding: utf-8 -*-
"""
WebUI 胶水层（P3b 完整版）

- send_and_wait：合成消息走漏斗 + future 取回（一切运行期 agent 操作的统一范式）
- Glue.init_agent：agent 初始化器（BridgeHook 工具事件桥）
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

from core.hook.events import HookEvent, Decision, HookResult
from core.hook.hooks import BaseHook
from gateway.channels.base import InboundMessage

_PREVIEW_CHARS = 300          # 工具参数/结果预览截断
_APPROVAL_TIMEOUT = 300       # 审批等待上限（< 硬超时 600，fail-closed）
_PLAN_TTL = 600               # plan 暂存 TTL（秒）
_PLAN_MAX = 32                # plan 暂存上限
_WRITE_TOOLS = ("bash", "write", "edit", "python", "http", "file_mgr")


async def send_and_wait(module, session_key: str, text: str,
                        timeout: float = 120.0,
                        user_id: str = "webui",
                        images: list = None) -> dict:
    """合成 InboundMessage 走漏斗，等 WebuiChannel future 取回回复。

    images: 可选多模态图片块列表（{type:image,source:base64,...}），透传给 agent.run。
    """
    message_id = f"webui-{uuid.uuid4().hex[:12]}"
    fut = module.channel.register_future(session_key, message_id)
    msg = InboundMessage(
        channel="webui",
        session_key=session_key,
        user_id=user_id,
        user_name="WebUI",
        text=text,
        message_id=message_id,
        images=images or [],
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
# BridgeHook —— hook 事件 → SSE（executor 线程 → 主循环，publish 线程安全）
# ============================================================

class BridgeHook(BaseHook):
    """把 PRE_TOOL / POST_TOOL / DENIED 转发为 SSE chat.tool.* 事件"""

    name = "webui-bridge"

    def __init__(self, bus, session_key: str):
        self.bus = bus
        self.session_key = session_key

    def run(self, ctx):
        p = ctx.payload or {}
        if ctx.event == HookEvent.PRE_TOOL:
            self.bus.publish("chat.tool.start", {
                "session_key": self.session_key,
                "tool": p.get("tool_name", ""),
                "params_preview": str(p.get("params", ""))[:_PREVIEW_CHARS],
            })
        elif ctx.event == HookEvent.POST_TOOL:
            self.bus.publish("chat.tool.done", {
                "session_key": self.session_key,
                "tool": p.get("tool_name", ""),
                "ok": not p.get("is_error", False),
                "preview": str(p.get("result", ""))[:_PREVIEW_CHARS],
            })
        elif ctx.event == HookEvent.DENIED:
            self.bus.publish("chat.tool.denied", {
                "session_key": self.session_key,
                "tool": p.get("tool_name", ""),
                "reason": p.get("reason", ""),
            })
        return HookResult(Decision.CONTINUE)


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

    def ask(self, session_key: str, tool_name: str, params: dict) -> str:
        aid = uuid.uuid4().hex[:8]
        evt = threading.Event()
        params_preview = str(params)[:_PREVIEW_CHARS]
        record = {"answer": "", "event": evt, "created_at": time.time(),
                  "tool": tool_name, "session_key": session_key,
                  "params_preview": params_preview}
        with self._lock:
            self._pending[aid] = record
        self.module.bus.publish("approval.requested", {
            "id": aid,
            "session_key": session_key,
            "tool": tool_name,
            "params_preview": params_preview,
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

    def resolve(self, aid: str, answer: str) -> bool:
        if answer not in ("y", "n", "a", "s"):
            return False
        with self._lock:
            record = self._pending.get(aid)
            if record is None:
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
        self._plans: dict = {}     # plan_id -> {session_key,text,plan_text,created_at}
        self._plans_lock = threading.Lock()

    # ---------- agent 初始化器 ----------

    def init_agent(self, agent, entry):
        """agent 创建后（executor 线程）：挂 BridgeHook 工具事件桥"""
        # Runtime tool events carry call IDs and are forwarded by Dispatcher.
        # Do not duplicate them through a hook side channel.
        return None

    # ---------- 权限三档 ----------

    def apply_permission_mode(self, agent, mode: str):
        """切换权限档位（运行期即时生效，set_rule 无缓存）"""
        from core.config_loader import load_config
        from core.permission import ALLOW

        cfg_perm = load_config().get("permission", {})
        if mode == "ask":
            agent.permission._init_default_rules(cfg_perm)
            agent.ask_callback = (
                lambda tool, params, _sk=agent: self.bridge.ask(
                    self._session_key_of(agent), tool, params))
        elif mode == "allow":
            agent.permission._init_default_rules(cfg_perm)
            for t in _WRITE_TOOLS:
                agent.permission.set_rule(t, ALLOW)
            agent.ask_callback = None
        elif mode == "unreviewed":
            for t in agent.tool_registry.list_tool_names():
                agent.permission.set_rule(t, ALLOW)
            agent.ask_callback = None
        else:
            raise ValueError(f"未知权限档位: {mode}（ask/allow/unreviewed）")

    @staticmethod
    def _session_key_of(agent) -> str:
        return getattr(agent, "_webui_session_key", "") or ""

    # ---------- /perm 命令 ----------

    async def handle_perm_command(self, arg: str, ctx: dict) -> str:
        agent = ctx["agent"]
        entry = ctx["entry"]
        mode = arg.strip().lower()
        if not mode:
            return "用法: /perm ask|allow|unreviewed"
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

    # ---------- plan 两阶段 ----------

    def plan_preview_sync(self, agent, text: str) -> dict:
        """阶段一只读预览：镜像 CLI /plan 守卫与规划 prompt，不改 agent.messages"""
        from agent import PLAN_PROMPT_TEXT, extract_plan_text
        from core.task_list import TaskList

        msgs = list(agent.messages)
        if not msgs or msgs[0].get("role") != "system":
            msgs.insert(0, {"role": "system", "content": agent.system_prompt})
        msgs = msgs + [
            {"role": "user", "content": text},
            {"role": "user", "content": PLAN_PROMPT_TEXT},
        ]
        resp = agent.llm.think(msgs, temperature=0.3, stream=False, silent=True)
        plan_text = extract_plan_text(resp) if resp else None
        if not plan_text:
            raise ValueError("未能识别出方案内容（LLM 未遵循 PLAN 格式）")
        tasks = TaskList.from_plan_text(plan_text).tasks
        if not tasks:
            raise ValueError("方案解析失败：未识别出有效任务")
        return {
            "plan_text": plan_text,
            "tasks": [{"id": t.id, "description": t.description} for t in tasks],
        }

    def create_plan(self, session_key: str, text: str, plan_text: str) -> str:
        plan_id = uuid.uuid4().hex[:12]
        with self._plans_lock:
            # TTL 清理 + 上限
            now = time.time()
            self._plans = {
                k: v for k, v in self._plans.items()
                if now - v["created_at"] < _PLAN_TTL
            }
            while len(self._plans) >= _PLAN_MAX:
                oldest = min(self._plans, key=lambda k: self._plans[k]["created_at"])
                del self._plans[oldest]
            self._plans[plan_id] = {
                "session_key": session_key,
                "text": text,
                "plan_text": plan_text,
                "created_at": now,
            }
        return plan_id

    def take_plan(self, plan_id: str):
        """批准时取出（一次性）；过期返回 None"""
        with self._plans_lock:
            p = self._plans.pop(plan_id, None)
        if p is None:
            return None
        if time.time() - p["created_at"] > _PLAN_TTL:
            return None
        return p

    def reject_plan(self, plan_id: str) -> bool:
        with self._plans_lock:
            return self._plans.pop(plan_id, None) is not None

    async def handle_plan_preview_command(self, arg: str, ctx: dict) -> str:
        """/plan-preview {text} —— executor 内只读预览，返回 JSON"""
        agent = ctx["agent"]
        loop = ctx["loop"]
        executor = ctx["executor"]
        text = arg.strip()
        if not text:
            return json.dumps({"ok": False, "error": "缺少任务描述"},
                              ensure_ascii=False)
        try:
            preview = await loop.run_in_executor(
                executor, self.plan_preview_sync, agent, text)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        return json.dumps({"ok": True, **preview}, ensure_ascii=False)

    async def handle_plan_apply_command(self, arg: str, ctx: dict) -> str:
        """/plan-apply —— adopt_plan + _run_task_list（独立超时保护）"""
        agent = ctx["agent"]
        entry = ctx["entry"]
        loop = ctx["loop"]
        executor = ctx["executor"]

        plan_info = getattr(entry, "pending_plan", None)
        if not plan_info:
            return "❌ 无待执行方案（请先 POST /api/plan 并批准）"

        from core.config_loader import load_config
        plan_timeout = load_config().get("gateway", {}).get(
            "sessions", {}).get("plan_timeout_seconds", 3600)

        def _run():
            steps = agent.adopt_plan(plan_info["plan_text"],
                                     plan_info.get("text", ""))
            return agent._run_task_list(
                user_input=plan_info.get("text") or "plan execution",
                max_steps=agent.max_steps,
                verbose=False,
            ), steps

        try:
            result, steps = await asyncio.wait_for(
                loop.run_in_executor(executor, _run), timeout=plan_timeout)
            return f"✅ 方案执行完成（{steps} 步）\n{result}"
        except asyncio.TimeoutError:
            return f"⏰ 方案执行超时（>{plan_timeout}s），已放弃等待"
        except ValueError as e:
            return f"❌ {e}"

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
