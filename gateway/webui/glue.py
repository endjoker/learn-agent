# -*- coding: utf-8 -*-
"""
WebUI 胶水层（P3b 完整版）

- send_and_wait：合成消息统一走 ConversationTurnRunner 执行并等待 Turn 终态
  取回复（C1-③：旧漏斗 fallback 已退役，失败显式报错，不再 future 回环）
- Glue.apply_permission_mode：权限四档（readonly / ask / allow / unreviewed）
- ApprovalBridge：ask 档审批桥（executor 线程等待，fail-closed 300s）
- QuestionBridge：Agent 结构化提问桥（候选项 + 自定义输入，与审批协议分离）
- plan 两阶段：/plan-preview（只读预览）+ 暂存 + /plan-apply（漏斗内执行）
- /perm /reload-prompt /mcp reload|reconnect 命令处理
"""

import asyncio
import json
import logging
import threading
import time
import uuid

from core.plan import PlanManager
from gateway.dispatcher import Dispatcher, plan_approval_required

logger = logging.getLogger("jk_agent.gateway")
from gateway.webui.question_bridge import (
    QuestionBridge,
    RESOLVE_CONTEXT_MISMATCH,
    RESOLVE_INVALID,
    RESOLVE_NOT_FOUND,
    RESOLVE_OK,
)

_PREVIEW_CHARS = 300          # 工具参数/结果预览截断
_APPROVAL_TIMEOUT = 300       # 审批等待上限（< 硬超时 600，fail-closed）

# L5-P0-3：send_and_wait 终态等待参数。
# 原实现 0.3s 固定轮询 get_active_turn，改为事件驱动（bus chat.done /
# approval.resolved 按 message_id / conversation_id 匹配），事件未达时回退
# 轮询，且轮询退化为 1s 起步指数退避（降低空转唤醒开销）。
_POLL_BASE_DELAY = 1.0      # 轮询兜底起始间隔（原 0.3s 固定 → 1s 起步）
_POLL_MAX_DELAY = 5.0       # 指数退避上限（1 → 2 → 4 → 5s）


async def _wait_turn_terminal(module, *, cid: str = "", message_id: str = "",
                              timeout: float, is_finished) -> bool:
    """事件驱动等待 Turn 终态（L5-P0-3）。

    订阅 bus 的 chat.done / approval.resolved（按 message_id 或
    conversation_id 匹配）：
    - chat.done（统一模型/旧漏斗广播）→ Turn 已终态，立即返回；
    - approval.resolved → 审批解除阻塞，立即复查 is_finished()；
    - 事件未达 → 回退原轮询逻辑（1s 起步指数退避，上限 5s）；
    - 总截止 = timeout，与旧轮询 deadline 语义一致。
    """
    deadline = time.time() + timeout
    # 先做一次即时检查：enqueue 未出队建 Turn 的常见路径立即返回
    # （等价于旧轮询首轮 0.3s 后见到 active=None，且更快）。
    try:
        if is_finished():
            return True
    except Exception:
        return False

    done_evt = asyncio.Event()
    sub_id = None
    consumer = None
    try:
        sub_id, q = module.bus.subscribe()

        def _match(ev: dict) -> bool:
            if ev.get("type") not in ("chat.done", "approval.resolved"):
                return False
            data = ev.get("data") or {}
            # 旧漏斗广播带 message_id；统一模型事件带 conversation_id
            return (data.get("message_id") == message_id
                    or data.get("conversation_id") == cid)

        async def _drain():
            while True:
                ev = await q.get()
                if ev is None:          # bus 停机哨兵
                    return
                if not _match(ev):
                    continue
                if ev.get("type") == "chat.done":
                    done_evt.set()
                    return
                # approval.resolved：审批已答复，立即复查是否已终态
                try:
                    if is_finished():
                        done_evt.set()
                        return
                except Exception:
                    pass

        consumer = asyncio.create_task(_drain())
    except Exception:
        # 订阅失败（bus 未绑定 loop / 测试桩无 bus）→ 仅轮询兜底
        logger.debug("send_and_wait 事件订阅失败，仅轮询兜底", exc_info=True)
    try:
        delay = _POLL_BASE_DELAY
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            if done_evt.is_set():
                return True
            try:
                await asyncio.wait_for(done_evt.wait(),
                                       timeout=min(delay, remaining))
                return True
            except asyncio.TimeoutError:
                pass
            try:
                if is_finished():
                    return True
            except Exception:
                return False
            delay = min(delay * 2, _POLL_MAX_DELAY)
    finally:
        if consumer is not None:
            consumer.cancel()
            try:
                await consumer
            except (asyncio.CancelledError, Exception):
                pass
        if sub_id is not None:
            try:
                module.bus.unsubscribe(sub_id)
            except Exception:
                pass


async def clear_cached_agent_context(module, session_key: str) -> None:
    """清空驻留 Agent 的内存消息（LLM 上下文），供各"清空会话"端点联动。

    统一会话模型（turns/nodes）与驻留 Agent 的 MessageStore 是两份存储：
    只清模型不清 Agent，下一条消息仍携带全部旧上下文（/clear 失效、上下文
    面板消息数不降）。Agent 缺失（未创建/已驱逐）时无需处理——重建路径
    走 SQLite 统一会话回放，清空后回放自然为空。
    """
    session_mgr = getattr(module, "session_mgr", None)
    get_or_create = getattr(session_mgr, "get_or_create", None)
    if not callable(get_or_create):
        return
    try:
        entry = get_or_create(session_key)
    except Exception:
        logger.debug("清空上下文：获取会话入口失败 %s", session_key, exc_info=True)
        return
    agent = getattr(entry, "agent", None)
    if agent is None:
        return
    executor = None
    get_executor = getattr(session_mgr, "get_executor", None)
    if callable(get_executor):
        try:
            executor = get_executor()
        except Exception:
            executor = None
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, agent.clear_history)
    except Exception:
        logger.warning("清空缓存 Agent 上下文失败: %s", session_key, exc_info=True)


async def send_and_wait(module, session_key: str, text: str,
                        timeout: float = 120.0,
                        user_id: str = "webui",
                        images: list = None,
                        metadata: dict = None,
                        message_id: str = None) -> dict:
    """合成消息经统一会话链路执行并等待 Turn 终态取回复（C1-③：旧漏斗退役）。

    images: 可选多模态图片块列表（{type:image,source:base64,...}）。
    metadata: Phase 4 工作区运行上下文（workspace_id/snapshot_id 等）。
    message_id: 可选显式消息 ID（工作区会话去重/审批归属用）。

    旧漏斗 fallback 已删除（C1-③）：会话未接入统一模型或统一链路失败时
    返回显式错误（{"ok": False, "error": ...}），不再经 WebuiChannel
    future 回环取回复 —— 失败必须显式暴露给调用方（api_chat/api_workspace
    据此回 504 或带错误码），避免管理命令静默丢失。
    """
    message_id = message_id or f"webui-{uuid.uuid4().hex[:12]}"
    svc = getattr(module, "conversation_service", None)
    if svc is None:
        return {"ok": False, "error": "统一会话服务未启用",
                "session_key": session_key, "message_id": message_id}
    conv = svc.store.get_conversation_by_key(session_key)
    if conv is None:
        # 旧漏斗退役：未接入统一模型的会话不再 future 回环，显式报错。
        return {"ok": False, "error": "会话未接入统一模型，无法执行该操作",
                "session_key": session_key, "message_id": message_id}
    try:
        cid = conv.conversation_id
        svc.enqueue(
            cid, text, message_id=message_id,
            sender_id=user_id, sender_name=user_id,
            create_queued_node=False)
        try:
            await module.dispatcher.execute_conversation_turn(cid)
        except Exception:
            # 执行协程异常不在此中断：Turn 可能已由 runner 看门/错误路径收敛
            # 终态，继续等待取最新回复。但必须留下日志——静默吞掉会让执行
            # 失败无从排查。
            logger.warning(
                "send_and_wait 执行 Turn 异常（继续等待终态）: session=%s",
                session_key, exc_info=True)
        # L5-P0-3：事件驱动等待 Turn 终态（bus chat.done / approval.resolved
        # 按 message_id / conversation_id 匹配），事件未达回退 1s 起步指数
        # 退避轮询（原 0.3s 固定轮询）。
        await _wait_turn_terminal(
            module, cid=cid, message_id=message_id, timeout=timeout,
            is_finished=lambda: svc.store.get_active_turn(cid) is None)
        # 取最新 Turn 的最终回复
        turns = svc.store.list_turns(cid, limit=1)
        reply = ""
        if turns:
            final = turns[0]
            if final.final_assistant_node_id:
                node = svc.store.get_node(final.final_assistant_node_id)
                reply = (node.text or "") if node else ""
            else:
                nodes = svc.store.get_turn_nodes(final.turn_id)
                texts = [n.text or "" for n in nodes
                         if n.type == "assistant" and (n.text or "").strip()]
                reply = texts[-1] if texts else ""
        return {"ok": True, "reply": reply,
                "session_key": session_key, "message_id": message_id}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        # 预期内的统一链路异常（会话/节点数据缺失、字段类型不符等）：
        # 记录 debug 后显式报错（不再回退旧漏斗）。
        logger.debug("统一链路 send_and_wait 失败: %s", exc)
        return {"ok": False, "error": f"统一链路执行失败: {exc}",
                "session_key": session_key, "message_id": message_id}
    except Exception:
        logger.exception("统一链路 send_and_wait 失败")
        return {"ok": False, "error": "统一链路执行失败",
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

    def _conversation_context(self, session_key: str, metadata: dict) -> tuple:
        """统一会话审批接线（设计方案 7.7）：返回 (conversation_id, turn_id)。

        仅当该会话已存在统一 Conversation 且存在活动 Turn 时启用统一模型记录，
        否则退化为纯旧审批事件（渠道等非统一场景保持原行为）。"""
        try:
            service = getattr(self.module, "conversation_service", None)
            if service is None:
                return None, None
            conv = service.store.get_conversation_by_key(session_key)
            if conv is None:
                return None, None
            turn = service.store.get_active_turn(conv.conversation_id)
            if turn is None:
                return None, None
            return conv.conversation_id, turn.turn_id
        except Exception:
            return None, None

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
        # 统一会话审批记录（设计方案 7.7）：Turn=approval + approvals 表 +
        # 统一 approval.requested 事件；旧 aid 关联统一 approval_id。
        conversation_id, turn_id = self._conversation_context(
            session_key, meta)
        unified_approval_id = ""
        if conversation_id and turn_id:
            try:
                service = getattr(self.module, "conversation_service", None)
                approval = service.request_approval(
                    conversation_id, turn_id, tool_name=tool_name,
                    params_summary=params_preview)
                unified_approval_id = approval.approval_id
                record["unified_approval_id"] = unified_approval_id
            except Exception:
                # 统一模型记录失败不阻断审批（保留旧路径可用性）
                unified_approval_id = ""
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
            "conversation_id": conversation_id or "",
            "turn_id": turn_id or "",
            "approval_id": unified_approval_id or "",
        })
        got = evt.wait(timeout=_APPROVAL_TIMEOUT)
        with self._lock:
            self._pending.pop(aid, None)
            # 竞态说明：resolve() 在持锁时写 answer 并 set() event；此处持锁
            # 读取保证可见性（Event 内部锁已提供 happens-before，双保险）。
            # 若 resolve 从未到达（超时 / WebUI 停止），answer 为空 → 兜底 "n"，
            # 保持 fail-closed：绝不替用户放行。
            resolved_answer = record["answer"]
        answer = resolved_answer if (got and resolved_answer) else "n"
        waited = round(time.time() - record["created_at"], 1)
        self.module.bus.publish("approval.resolved", {
            "id": aid, "session_key": session_key,
            "answer": answer, "waited_s": waited,
            "timeout": not got,
            "conversation_id": conversation_id or "",
            "approval_id": unified_approval_id or "",
        })
        return answer

    def resolve(self, aid: str, answer: str,
                context: dict = None) -> str:
        """答复审批。返回状态：ok / not_found / context_mismatch / invalid。

        context 可含 session_key/workspace_id/workspace_session_id/
        snapshot_id/message_id。fail-closed 单边匹配：桥记录携带的归属信息
        （session_key/工作区/消息 id）是权威，任一归属键记录有值而请求缺失或
        不同 → context_mismatch（防止跨会话/跨页面/跨消息答复）；仅当双方都
        完全不携带归属信息时才放行（向后兼容）。
        统一会话审批同步 resolve（设计方案 7.7）。"""
        if answer not in ("y", "n", "a", "s"):
            return RESOLVE_INVALID
        with self._lock:
            record = self._pending.get(aid)
            if record is None:
                return RESOLVE_NOT_FOUND
            if not self._context_matches(record, context):
                return RESOLVE_CONTEXT_MISMATCH
            record["answer"] = answer
            record["event"].set()
            unified_id = record.get("unified_approval_id", "")
        if unified_id:
            try:
                service = getattr(self.module, "conversation_service", None)
                if service is not None:
                    conv = service.store.get_conversation_by_key(
                        record.get("session_key", ""))
                    if conv is not None:
                        # y/a → approved；n/s → denied（设计方案 7.7）
                        decision = ("approved" if answer in ("y", "a")
                                    else "denied")
                        # resolved_by=旧桥 aid：保持审计可溯源（原 holder_id）
                        service.resolve_approval(
                            conv.conversation_id, unified_id, decision,
                            resolved_by=str(aid))
            except Exception:
                import logging
                logging.getLogger("jk_agent.gateway").exception(
                    "统一审批 resolve 失败: %s", aid)
        return RESOLVE_OK

    def _context_matches(self, record: dict, context: dict) -> bool:
        """fail-closed 单边归属匹配（与 QuestionBridge._context_matches 同规则）。"""
        for key in ("session_key", "workspace_id", "workspace_session_id",
                    "snapshot_id", "message_id"):
            have = str(record.get(key) or "").strip()
            want = str((context or {}).get(key) or "").strip()
            if have or want:
                if have != want:
                    return False
        return True

    def list_pending(self, session_key: str = "") -> list:
        """返回当前等待答复的审批；提供 session_key 时严格按会话过滤。

        载荷必须携带归属字段（workspace_id/workspace_session_id/snapshot_id/
        message_id）：前端兜底轮询会用该载荷**整队替换**待答列表，若缺失归属
        字段，用户答复时 POST 回传不了 message_id 等 → 桥单边匹配判
        context_mismatch → 403"审批归属不匹配"（子 Agent 审批无法答复的根因）。
        """
        now = time.time()
        with self._lock:
            return [
                {"id": aid,
                 "session_key": r.get("session_key", ""),
                 "tool": r.get("tool", ""),
                 "params_preview": r.get("params_preview", ""),
                 "waited_s": round(now - r["created_at"], 1),
                 "workspace_id": r.get("workspace_id", ""),
                 "workspace_session_id": r.get("workspace_session_id", ""),
                 "snapshot_id": r.get("snapshot_id", ""),
                 "message_id": r.get("message_id", "")}
                for aid, r in self._pending.items()
                if not session_key or r.get("session_key", "") == session_key
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
        self.question_bridge = QuestionBridge(module)
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

    # ---------- 权限四档 ----------

    def apply_permission_mode(self, agent, mode: str):
        """切换权限档位（运行期即时生效）。

        生效机制只有两个：PolicyEngine.set_permission_mode（四档裁决唯一
        权威）+ 沙箱 unreviewed 联动。不要在此用 set_rule/_init_default_rules
        做 allowlist——它们是注册元数据，裁决链不读取（历史死代码已清理）。
        readonly 的"只读白名单"语义完全由 PolicyEngine 承载：变更/执行类
        DENY、网络与系统路径 ASK、未分类非纯工具 DENY。
        """
        if mode not in ("readonly", "ask", "allow", "unreviewed"):
            raise ValueError("invalid permission mode; expected readonly/ask/allow/unreviewed")
        agent.permission.set_permission_mode(mode)
        sandbox = getattr(agent, "sandbox", None)
        if sandbox is not None:
            sandbox.set_unreviewed_mode(mode == "unreviewed")

        # 交互式审批桥：任何档位（除 unreviewed 外）只要 policy 返回 ASK
        # （系统路径 / 工作区外执行或副作用 / readonly 下的网络访问），都应把
        # 确认抛给 WebUI 用户，而不是 fail-closed 直接拒绝（设计方案 7.7）。
        # ApprovalBridge.ask 在 300s 超时/WebUI 停止时 fail-closed 返回 "n"，
        # 因此这里始终安装 ask_callback 是安全的。
        def _ask_callback(tool, params, _a=agent):
            return self.bridge.ask(
                self._session_key_of(_a), tool, params,
                metadata=getattr(_a, "_webui_metadata", None) or {})

        if mode == "unreviewed":
            # No interactive approval. Non-overridable SecurityGate/sandbox
            # checks remain active and can still hard-deny unsafe operations.
            agent.ask_callback = None
        else:
            if mode == "readonly":
                self._wrap_search_credential_sanitizer(agent)
            agent.ask_callback = _ask_callback

    # ---------- readonly 档外发参数打码 ----------

    @staticmethod
    def _wrap_search_credential_sanitizer(agent) -> int:
        """readonly 档 search query 凭据打码（DLP）。

        readonly 档允许 search 外发查询；若 LLM 把上下文中出现的 API Key /
        Token 等凭据拼进 query，会原样发给外部搜索引擎造成泄露。这里给
        search 工具的 execute 包一层 guard.sanitize_output（复用
        core/sandbox/guard.py 的 SECRET_PATTERNS 脱敏，不复制实现），字符串
        参数中的凭据替换为 **** 后再执行。幂等：已包装的工具跳过，模式来回
        切换不会叠加包装。
        """
        from core.sandbox.guard import sanitize_output

        wrapped = 0
        tool = agent.tool_registry.get_tool("search")
        if tool is None or getattr(tool, "_cred_sanitized", False):
            return wrapped
        original_execute = tool.execute

        def _sanitized_execute(*args, **kwargs):
            cleaned = {
                key: (sanitize_output(value) if isinstance(value, str) else value)
                for key, value in kwargs.items()
            }
            return original_execute(*args, **cleaned)

        tool.execute = _sanitized_execute
        tool._cred_sanitized = True
        wrapped += 1
        return wrapped

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
            # 统一模型持久化（设计方案：管理操作统一化），回退 sessions_map
            svc = getattr(self.module, "conversation_service", None)
            conv = None
            if svc is not None:
                conv = svc.store.get_conversation_by_key(entry.session_key)
            if conv is not None:
                svc.update_prefs(conv.conversation_id, permission_mode=mode)
            else:
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

    def create_plan(self, session_key: str, text: str, plan: dict, *, goal_id: str | None = None):
        """Persist a typed preview instead of retaining a process-local TTL object."""
        session_id = Dispatcher._runtime_session_id(session_key)
        self.module.runtime_store.upsert_session(session_id, session_key, channel="webui", status="active")
        return self.plan_manager.create_preview(
            session_id, plan, source_prompt=text, title=(text[:120] or "执行方案"), goal_id=goal_id,
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
    async def handle_goal_command(self, arg: str, ctx: dict) -> str:
        """Deterministically manage the current durable Goal."""
        text = arg.strip()
        session_key = ctx["entry"].session_key
        session_id = Dispatcher._runtime_session_id(session_key)
        self.module.runtime_store.upsert_session(session_id, session_key, channel="webui", status="active")
        goals = [item for item in self.module.goal_runtime.list(session_id) if not item.is_terminal]
        current = goals[0] if goals else None
        if not text:
            return (json.dumps(current.to_dict(), ensure_ascii=False) if current
                    else "当前没有活动 Goal。用法: /goal <长期目标>")
        parts = text.split(None, 1)
        action = parts[0].lower()
        try:
            if action == "pause":
                if not current: return "❌ 当前没有可暂停的 Goal"
                goal = await self.module.goal_runtime.pause_async(
                    current.goal_id, cancel=self.module.dispatcher.cancel_runtime_task)
            elif action == "resume":
                if not current: return "❌ 当前没有可恢复的 Goal"
                goal = self.module.goal_runtime.resume(current.goal_id)
                self.module.goal_driver.trigger(goal.goal_id)
            elif action == "edit":
                if not current or len(parts) < 2: return "用法: /goal edit <新目标>"
                goal = self.module.goal_runtime.edit(current.goal_id, parts[1])
            elif action in {"clear", "cancel"}:
                if not current: return "❌ 当前没有可清除的 Goal"
                goal = self.module.goal_runtime.cancel(current.goal_id)
            else:
                if current: return "❌ 当前已有未完成 Goal，请使用 /goal edit、pause 或 clear"
                goal = self.module.goal_runtime.create(session_id, text)
                self.module.goal_driver.trigger(goal.goal_id)
        except (ValueError, RuntimeError) as exc:
            return f"❌ {exc}"
        event_action = action if action in {"pause", "resume", "edit", "clear", "cancel"} else "created"
        self.module.bus.publish("goal.changed", self._scoped_event(
            {"action": event_action, "goal": goal.to_dict()}, session_key))
        return f"✅ Goal {goal.status.value}/{goal.activation.value}: {goal.goal_id}（{goal.rounds_started}/{goal.max_rounds} rounds）"

    async def handle_subagent_command(self, arg: str, ctx: dict) -> str:
        """/subagent starts a direct child and returns only a structured report."""
        prompt = arg.strip()
        if not prompt:
            return "用法: /subagent <任务>"
        if not self.module.dispatcher.task_runtime_enabled:
            return "❌ SessionRuntime 未启用，无法创建 Subagent"
        entry = ctx["entry"]
        session_id = Dispatcher._runtime_session_id(entry.session_key)
        report = await self.module.subagent_runtime.create(
            parent_session_id=session_id, parent_session_key=entry.session_key, prompt=prompt,
            mode="one-shot", parent_is_root=True)
        try:
            report = await asyncio.wait_for(
                self.module.subagent_runtime.wait_report(report.child_id), timeout=300)
        except asyncio.TimeoutError:
            return f"✅ Subagent 已启动并仍在运行: {report.child_id}"
        return (f"✅ Subagent {report.child_id} · {report.status}\n\n"
                f"{report.summary or '（无摘要）'}")

    async def handle_plan_command(self, arg: str, ctx: dict) -> str:
        """Generate, auto-approve and immediately execute a durable Plan."""
        text = arg.strip()
        if not text:
            return "用法: /plan <任务>"
        if not self.module.dispatcher.task_runtime_enabled:
            return "❌ SessionRuntime 未启用，无法执行 Plan"
        try:
            preview = await ctx["loop"].run_in_executor(
                ctx["executor"], self.plan_preview_sync, ctx["agent"], text)
            plan = self.create_plan(ctx["entry"].session_key, text, preview["plan"])
            tasks = preview.get("tasks") or []
            if plan_approval_required():
                # 两阶段审批：create_preview 后不自动 approve，进入
                # AWAITING_APPROVAL，等待既有 /api/plan/{id}/approve|reject。
                self.module.bus.publish("plan.changed", self._scoped_event(
                    {"action": "awaiting_approval", "plan": plan.to_dict()},
                    ctx["entry"].session_key))
                return (f"✅ 已创建 Plan 待确认：{plan.title or text[:120]}"
                        f"（共 {len(tasks)} 步），请审核后执行")
            plan = self.plan_manager.approve(plan.plan_id, actor="automatic")
            self.module.bus.publish("plan.changed", self._scoped_event(
                {"action": "approved", "plan": plan.to_dict()}, ctx["entry"].session_key))
            self.module.plan_runtime.start(plan.plan_id)
            # 返回人类可读的一行摘要，避免把原始 JSON 原文写进会话作为"运行进度"卡。
            # 结构化数据已通过 plan.changed 事件与 /api/plans 送达前端。
            return f"✅ 已创建并启动 Plan：{plan.title or text[:120]}（共 {len(tasks)} 步）"
        except Exception as exc:
            return f"❌ Plan 创建失败：{exc}"

    def _scoped_event(self, payload: dict, session_key: str) -> dict:
        event = {**payload, "session_key": session_key}
        if session_key.startswith("workspace:"):
            parts = session_key.split(":", 2)
            if len(parts) == 3:
                event["workspace_id"], event["workspace_session_id"] = parts[1], parts[2]
        return event

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
