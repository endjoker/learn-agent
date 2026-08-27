# -*- coding: utf-8 -*-
"""
统一会话执行器 —— 以 Conversation/Turn 为唯一权威（设计方案：后端状态机唯一权威）。

不复用旧 SessionManager FIFO worker / WebuiChannel future 回环：
- 每个 Conversation 独立维护自己的 Agent 实例（懒创建、复用）；
- execute 直接驱动 agent.run，事件 sink 只走统一模型（ConversationBridge →
  TurnNode / chat.done），不向旧 chat.* 事件总线广播；
- 回复经 chat.done 完成 Turn；异常 → Turn=error；
- Agent 实例不调用 message_store.save_session（统一模型以 SQLite 为权威，
  旧 sessions_map.json 不再产生写入）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from gateway.channels.base import InboundMessage
from gateway.conversation.store import STOP_TIMEOUT_SECONDS

logger = logging.getLogger("jk_agent.gateway")

# 会话互斥忙拒绝文案（与 dispatcher.SESSION_BUSY_REPLY 同源；本地定义避免
# 循环导入）。忙拒绝是控制面信号，不是对话内容：不得落成 assistant 答复节点。
BUSY_REPLY_MARKER = "⚠️ 会话正忙："


def _workspace_metadata(conversation) -> dict:
    """从会话提取工作区归属元数据（workspace_id / workspace_session_id）。

    工作区会话 session_key = workspace:{wid}:{sid}。审批/投递等事件需要这些
    字段才能按 workspace scope 被 SSE 正确转发（否则后端 matches_scope 因
    workspace_id 为空而丢弃，前端弹窗收不到 approval.requested）。"""
    parts = (getattr(conversation, "session_key", "") or "").split(":")
    if len(parts) == 3 and parts[0] == "workspace":
        return {"workspace_id": parts[1], "workspace_session_id": parts[2]}
    return {}


class _ConversationEntry:
    """轻量执行入口 —— 提供 _execute_agent 所需的 SessionEntry 兼容字段。
    Agent 由 _execute_agent 懒创建后缓存在此处（跨 Turn 复用，保持会话上下文）。"""

    def __init__(self, session_key: str, workspace_id: Optional[str] = None):
        self.session_key = session_key
        self.workspace_id = workspace_id
        self.agent = None
        self.is_busy = False
        # P1-2：跨路径执行互斥（与 SessionEntry.exec_lock 同语义），供
        # _execute_agent 入口非阻塞 try-acquire 使用。
        self.exec_lock = threading.Lock()
        self.created_at = time.time()
        self.last_active = time.time()
        # 工作区运行上下文（本轮主会话可用；工作区执行上下文为后续集成项）
        self.runtime_context = None
        self.runtime_model = ""
        self.runtime_max_steps = None
        self.runtime_permission_mode = ""
        self.runtime_mcp_servers = None
        self.runtime_profile_prompt = None
        self.runtime_allowed_tools = None
        self.runtime_allowed_skills = None
        self.runtime_reasoning_level = "inherit"
        self.runtime_snapshot_id = ""


class ConversationTurnRunner:
    """统一会话 Turn 执行器。``dispatcher`` 提供 _execute_agent /
    _conversation_bridge / channels / session_mgr（executor）等。"""

    def __init__(self, dispatcher):
        self._dispatcher = dispatcher
        self._entries: dict[str, _ConversationEntry] = {}
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        # 停止看门（设计方案 7.6）：conversation_id → 停止请求时间戳。
        # 请求停止后 10 秒内未收到确认（on_reply/on_stopped）→ Turn=error(stop_timeout)。
        self._stop_watchdog = float(STOP_TIMEOUT_SECONDS)
        self._stop_requested_at: dict[str, float] = {}
        # B10：事件驱动看门 —— 停止/Steering 变化即时 set Event 唤醒等待者
        self._stop_evts: dict[str, asyncio.Event] = {}
        self._steering_evts: dict[str, asyncio.Event] = {}
        self._steering_hook_installed = False
        # 工作区运行上下文 provider（由 WebUIModule 装配注入）：
        # workspace_id, session_id → (runtime_context, snapshot_id, model, permission_mode,
        # reasoning_level, max_steps, mcp_servers, profile_prompt, allowed_tools, allowed_skills)
        self._workspace_context_provider = None

    def set_workspace_context_provider(self, provider) -> None:
        """注入工作区上下文构建器（设计方案：工作区会话经统一链路执行需带
        快照冻结的模型/权限/MCP/工具集）。provider 签名：
        ``provider(workspace_id, session_id) -> dict | None``。"""
        self._workspace_context_provider = provider

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------

    def start(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cleanup_task = None
        await self._evict_all()
        self._stop_evts.clear()
        self._steering_evts.clear()

    async def _cleanup_loop(self) -> None:
        """空闲 Agent 清理：超过 30 分钟未活动的 Conversation Agent 驱逐
        （不调用 save_session —— 统一模型以 SQLite 为权威）。"""
        while True:
            await asyncio.sleep(300)
            try:
                now = time.time()
                with self._lock:
                    running = set(self._running)
                # 驱逐前检查活动 Turn（_running）：运行中的长 Turn 不驱逐，
                # 延迟到下一轮（5 分钟后再评估），避免打断执行（设计方案 16.5）
                stale = [cid for cid, e in list(self._entries.items())
                         if now - e.last_active > 1800 and cid not in running]
                for cid in stale:
                    await self.evict(cid)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("统一会话 Agent 清理异常")

    # ------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------

    async def run_turn(self, conversation_id: str) -> bool:
        """执行 Conversation 当前活动 Turn（status=queued）。"""
        # 新 Turn 执行前清除陈旧停止时间戳：停止请求只作用于其发起时的活动
        # Turn；上一 Turn 结束时若未消费（异常/迟到回复等），陈旧时间戳会在
        # 10 秒后误杀本 Turn（设计方案 7.6）。
        self._stop_requested_at.pop(conversation_id, None)
        bridge = self._dispatcher._conversation_bridge
        if bridge is None:
            logger.warning("统一会话桥未注入，跳过执行: %s", conversation_id)
            return False
        store = bridge.service.store
        try:
            turn = store.get_active_turn(conversation_id)
            if turn is None or turn.status != "queued":
                return False  # 无活动 Turn 或已被执行
            conversation = store.get_conversation(conversation_id)
            user_node = store.get_turn_user_node(turn.turn_id)
            if user_node is None or not (user_node.text or "").strip():
                logger.warning("Turn 缺少 User 节点文本: %s", turn.turn_id)
                return False
        except Exception:
            logger.exception("读取 Turn 失败: %s", conversation_id)
            return False
        with self._lock:
            if conversation_id in self._running:
                return False
            self._running.add(conversation_id)
        try:
            await self._execute(conversation, turn, user_node)
        finally:
            with self._lock:
                self._running.discard(conversation_id)
        return True

    async def _execute(self, conversation, turn, user_node) -> None:
        bridge = self._dispatcher._conversation_bridge
        # 渠道会话：按 route_metadata.channel 投递回复；webui 会话默认 webui channel
        route = dict(conversation.route_metadata or {})
        channel_name = str(route.get("channel") or "webui")
        channel = self._dispatcher._channels.get(channel_name) \
            or self._dispatcher._channels.get("webui")
        if channel is None:
            logger.error("webui channel 未注册，无法执行 Turn: %s", turn.turn_id)
            return
        entry = self._entry(conversation)
        # C1：共享 SessionManager entry 时置 busy，防止会话管理器 janitor
        # 在长 Turn 执行期间驱逐正在使用的共享 Agent 实例。
        entry.is_busy = True
        # 工作区会话：挂接快照冻结的运行上下文（模型/权限/MCP/工具集），
        # 与旧工作区漏斗语义一致（设计方案：工作区执行上下文集成）。
        # conversation.workspace_id 可能因旧数据为 None，故再从 session_key 判定。
        if conversation.workspace_id or (conversation.session_key or "").startswith("workspace:"):
            self._attach_workspace_context(entry, conversation)
        else:
            # 非工作区会话：应用会话偏好（模型/推理/权限，设计方案：管理操作统一化）
            self._attach_session_prefs(entry, conversation)
        cid = conversation.conversation_id
        is_channel = channel_name not in ("webui",)
        # 渠道消息回复投递的 message_id：取入站原始 message_id（debug 等
        # future 回环通道按 session_key:message_id 匹配 pending future）。
        # user_node.source_message_id 由渠道入队时记录。
        reply_message_id = (
            (user_node.source_message_id or "") if is_channel
            else turn.turn_id)
        # 图片重建（修正版方案 A）：本轮 User 节点 metadata.images 携带
        # artifacts 引用（send_next 落盘时写入），此处按 ref 读回 base64
        # 组装中性视觉块——执行段（agent.run images=）与白名单链路不变。
        message_images: list[dict] = []
        try:
            image_store = getattr(bridge.service, "image_store", None) \
                if bridge is not None else None
            for ref_meta in (getattr(user_node, "metadata", {}) or {}).get("images") or []:
                ref = str((ref_meta or {}).get("ref") or "")
                media_type = str((ref_meta or {}).get("media_type") or "image/png")
                if not (image_store and ref):
                    continue
                try:
                    message_images.append({
                        "type": "image", "source": "base64",
                        "media_type": media_type,
                        "data": image_store.load_b64(cid, ref),
                    })
                except Exception as exc:
                    logger.warning("图片读回失败（降级为占位）: %s: %s", ref, exc)
                    message_images.append({
                        "type": "text",
                        "text": f"[图片不可读取: {Path(ref).name}]"})
        except Exception:
            logger.debug("图片重建失败（按纯文本执行）: %s", cid, exc_info=True)
        msg = InboundMessage(
            channel=channel_name,
            session_key=conversation.session_key,
            user_id=str(route.get("user_id") or "webui"),
            user_name=str(route.get("user_name") or "WebUI"),
            text=user_node.text or "",
            message_id=reply_message_id or turn.turn_id,
            is_group=bool(route.get("is_group", False)),
            images=message_images or None,
            metadata={**_workspace_metadata(conversation),
                      "conversation_id": cid,
                      "turn_id": turn.turn_id,
                      "from_conversation_queue": True},
        )
        timed_out = False
        reply_task = None
        try:
            reply_task = asyncio.create_task(self._dispatcher._execute_agent(
                entry, msg, channel,
                runtime_metadata={"conversation_id": cid,
                                  "turn_id": turn.turn_id}))
            # 双看门：停止 10s / Steering 10s（设计方案 7.6/9.3）。
            # 两个截止取较近者作为 await 超时；到点后分别处理。
            reply, outcome, steering_qids = await self._await_with_watchdogs(
                cid, reply_task, bridge)
            if outcome == "stop_timeout":
                # 停止看门超时：Turn 终态化，Agent 迟到回复仅诊断
                timed_out = True
                # Fix-3（stop_timeout 诊断·可观测性）：日志带上看门到点时
                # 正在执行的工具与命令摘要——此前只能翻 SQLite 还原现场。
                inflight_tool = self._inflight_tool_summary(entry.agent)
                logger.warning(
                    "统一会话停止看门超时，Turn 置 error(stop_timeout): %s；在途工具: %s",
                    turn.turn_id, inflight_tool or "（无/已结束）")
                # agent 可能仍在旧 Turn 里运行：先登记事件退役（残余 delta/
                # 工具节点在 sink 处丢弃，防止 ensure_turn 复活链污染下一条
                # 排队消息的 Turn），再摘除 entry 并登记异步资源清理。
                if getattr(entry, "agent", None) is not None:
                    self._dispatcher.retire_agent(entry.agent)
                self._discard_agent_with_cleanup(entry)
                if bridge is not None:
                    # Steering 收口（与 steering_timeout/异常分支同语义）：
                    # 运行中插队走 prepare+request_stop，停止看门可能先于
                    # Steering 窗口到点——此处若不清理，waiting_for_steering
                    # 项与已过期的内存等待记录会泄漏到下一 Turn：前端按 §8.4
                    # 暂停自动分派后，手动分派的新 Turn 又会被陈旧记录在
                    # 看门首轮误判 steering_timeout（实测 5ms 内死亡），队列
                    # 表现为"手动发送也发不出去"。
                    try:
                        if steering_qids:
                            bridge.service.abort_steering(cid, steering_qids)
                        bridge.service.conclude_steering(cid)
                    except Exception:
                        logger.debug("停止看门 Steering 收口失败: %s",
                                     turn.turn_id, exc_info=True)
                    try:
                        bridge.service.complete_turn(
                            cid, turn.turn_id, "error",
                            error_code="stop_timeout")
                        # 消费停止标志并失效活动 Turn 缓存，防陈旧状态误伤下一 Turn
                        bridge.consume_stop_requested(cid)
                        bridge.discard_active_turn(cid)
                    except Exception:
                        logger.exception("停止看门终态失败: %s", turn.turn_id)
                self._stop_requested_at.pop(cid, None)
                try:
                    await asyncio.wait_for(reply_task, timeout=30)
                except (asyncio.TimeoutError, Exception):
                    pass
                reply = ""
            elif outcome == "steering_timeout":
                # Steering 看门超时：恢复队列项 + Turn=error
                timed_out = True
                logger.warning(
                    "统一会话 Steering 中断超时，Turn 置 error(steering_interrupt_timeout): %s",
                    turn.turn_id)
                # 同停止看门：先退役事件再摘 Agent（防迟到事件写穿新 Turn）
                if getattr(entry, "agent", None) is not None:
                    self._dispatcher.retire_agent(entry.agent)
                self._discard_agent_with_cleanup(entry)
                try:
                    bridge.service.abort_steering(cid, steering_qids or [])
                    bridge.service.conclude_steering(cid)
                    bridge.service.complete_turn(
                        cid, turn.turn_id, "error",
                        error_code="steering_interrupt_timeout")
                    bridge.discard_active_turn(cid)
                except Exception:
                    logger.exception("Steering 超时终态失败: %s", turn.turn_id)
                self._stop_requested_at.pop(cid, None)
                try:
                    await asyncio.wait_for(reply_task, timeout=30)
                except (asyncio.TimeoutError, Exception):
                    pass
                reply = ""
        except Exception as exc:
            logger.exception("统一会话 Turn 执行失败: %s", turn.turn_id)
            # 异常路径同样可能留下仍在 executor 线程里跑的 agent.run（如
            # bridge/store 中途抛错而 worker 未退出）：与看门分支同语义——
            # 先退役事件、再摘 Agent 登记资源清理，并给旧 worker 最多 30s
            # 收尾，避免迟到事件写进下一个 Turn。
            agent_now = getattr(entry, "agent", None)
            if agent_now is not None:
                try:
                    self._dispatcher.retire_agent(agent_now)
                except Exception:
                    logger.debug("retire_agent 失败（忽略）", exc_info=True)
                self._discard_agent_with_cleanup(entry)
            if reply_task is not None and not reply_task.done():
                try:
                    await asyncio.wait_for(reply_task, timeout=30)
                except (asyncio.TimeoutError, Exception):
                    pass
            # 异常路径统一清理停止时间戳/Steering 等待，避免泄漏到下一 Turn
            self._stop_requested_at.pop(cid, None)
            if bridge is not None:
                bridge.service.conclude_steering(cid)
                bridge.on_error(msg, str(exc))
            entry.last_active = time.time()
            return
        finally:
            entry.last_active = time.time()
            entry.is_busy = False
        if bridge is not None and not timed_out:
            # Steering 等待中：当前输出已被打断，注入 Steering 并续跑，
            # 本次执行不终态化 Turn（设计方案 9.1/9.2）。
            if bridge.service.pending_steering(cid):
                entry.last_active = time.time()
                await self._resume_after_steering(conversation, turn)
                return
            if isinstance(reply, str) and reply.startswith(BUSY_REPLY_MARKER):
                # 忙拒绝不是对话内容：Turn 以 error 终态收口，不落 assistant
                # 答复节点（此前会渲染成"系统告警"气泡）。队列项未被消费仍
                # waiting，当前轮释放后由倒计时/FIFO 正常推进。
                try:
                    bridge.service.complete_turn(
                        cid, turn.turn_id, "error", error_code="agent_session_busy")
                    bridge.discard_active_turn(cid)
                except Exception:
                    logger.exception("忙拒绝 Turn 终态失败: %s", turn.turn_id)
                logger.warning("统一会话忙拒绝入队消息（Turn=error 不落答复）: %s", cid)
                return
            bridge.on_reply(msg, reply or "")
            # 渠道会话：回复经渠道 channel 投递（统一 runner 执行，设计方案 11.2；
            # C1-②：与 TaskRuntime 投递合并为同一管线 —— 3 次退避重试 +
            # delivery 状态落账集中在 dispatcher._deliver_channel_reply；
            # TaskRuntime 的 DB delivery 行仍是重启恢复源）。
            if is_channel and reply:
                try:
                    await self._dispatcher._deliver_channel_reply(
                        channel, msg, reply, bridge=bridge)
                except Exception:
                    if bridge is not None:
                        bridge.record_channel_delivery(msg, "delivery_failed")
                    logger.exception("渠道回复投递失败: %s", conversation.session_key)
            # 渠道 FIFO 自动推进（设计方案 11.2）：终态后若仍有排队项且无活动
            # Turn，触发下一条执行（send_next 已建 queued Turn，run_turn 接管）
            if is_channel:
                try:
                    store = bridge.service.store
                    active = store.get_active_turn(cid)
                    # 无活动 Turn 时的 waiting_for_steering 属陈旧态（见
                    # send_next 自愈注释），同样允许 FIFO 推进
                    has_waiting = any(
                        q.status in ("waiting", "waiting_for_steering")
                        for q in store.list_active_queue(cid))
                    if active is None and has_waiting:
                        asyncio.get_running_loop().create_task(
                            self.run_turn(cid))
                except Exception:
                    logger.debug("渠道队列推进失败: %s", conversation.session_key)

    async def _await_with_watchdogs(
        self, cid: str, reply_task, bridge,
    ) -> tuple[str, str, list[str]]:
        """等待 Agent 回复并执行停止/Steering 双看门（设计方案 7.6/9.3）。

        C1-① 超时语义统一说明：本方法是控制面看门 —— 停止请求 10s /
        Steering 等待 10s（协作式停止/注入打断的响应时限），与执行时长
        超时（soft/hard）正交。执行时长超时与 zombie 隔离只存在于
        dispatcher._await_agent_future（runner 经 _execute_agent 复用，
        消除 wait_for 与 shield 两套并存）：soft 超时发进度提示、hard
        超时经 _quarantine_after_timeout 摘除 Agent 防并发复用。

        B10 事件驱动式：asyncio.wait({reply_task, stop_wait, steer_wait})，其中
        stop_wait/steer_wait 是 stop_evt/steering_evt 的 wait() 包装任务
        （asyncio.wait 只接受 Future/Task）。request_stop 与 service 的
        Steering 注册/结束钩子即时 set Event，取代旧的 0.5s 轮询；10s 超时
        语义保持不变（截止时间由事件触发时重新读取停止时间戳 / Steering 剩余
        时间计算）。

        返回 (reply, outcome, steering_qids)：
        - outcome ∈ {"ok", "stop_timeout", "steering_timeout"}；
        - steering_qids 为超时判定时仍等待中的队列项（steering_timeout 时恢复用）。
        成功（ok）路径统一消费停止时间戳并结束 Steering 等待，防止陈旧时间戳
        误杀下一个 Turn。"""
        stop_evt = asyncio.Event()
        steering_evt = asyncio.Event()
        self._stop_evts[cid] = stop_evt
        self._steering_evts[cid] = steering_evt
        # 首次调用时把 Steering 通知钩子挂到 service：register/conclude 即时唤醒
        if bridge is not None and not self._steering_hook_installed:
            try:
                bridge.service.set_steering_wait_hook(self._on_steering_wait)
                self._steering_hook_installed = True
            except Exception:
                logger.debug("Steering 看门钩子安装失败: %s", cid)
        try:
            first_probe = True
            while True:
                stop_requested_at = self._stop_requested_at.get(cid)
                steering_qids = None
                steering_remaining = None
                if bridge is not None:
                    steering_qids = bridge.service.pending_steering(cid)
                    steering_remaining = bridge.service.steering_remaining(cid)
                    # 首轮探测自愈：看门装配时即已过期的 Steering 等待必为
                    # 上一轮泄漏的陈旧记录（本 Turn 运行中新注册的插队只会
                    # 带来 ~10s 新窗口，且经 steering_evt 即时唤醒重算）。
                    # 不自愈会以 0 秒截止立刻误判 steering_timeout，新 Turn
                    # 尚未执行即死（队列表现为"发出去就报错"）。
                    if first_probe and steering_qids \
                            and (steering_remaining or 0.0) <= 0.0:
                        try:
                            bridge.service.abort_steering(cid, steering_qids)
                        except Exception:
                            logger.debug("陈旧 Steering 队列项恢复失败: %s",
                                         cid, exc_info=True)
                        bridge.service.conclude_steering(cid)
                        steering_qids = None
                        steering_remaining = None
                first_probe = False
                deadlines = []
                if stop_requested_at is not None:
                    deadlines.append(stop_requested_at + self._stop_watchdog)
                if steering_remaining is not None:
                    deadlines.append(time.time() + steering_remaining)
                if deadlines:
                    timeout = max(0.0, min(deadlines) - time.time())
                else:
                    timeout = None  # 事件驱动：无看门截止时仅等回复/事件
                stop_wait = asyncio.create_task(stop_evt.wait())
                steer_wait = asyncio.create_task(steering_evt.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {reply_task, stop_wait, steer_wait}, timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED)
                finally:
                    # 每轮重建事件等待任务；退出前取消本轮未完成者
                    if not stop_wait.done():
                        stop_wait.cancel()
                    if not steer_wait.done():
                        steer_wait.cancel()
                if reply_task in done:
                    reply = reply_task.result()
                    # 成功路径统一消费停止时间戳
                    self._stop_requested_at.pop(cid, None)
                    # Steering 语义（9.1/9.2）：本次停止若由 Steering 引起
                    # （仍有待注入项），必须**保留**等待记录，交由 _execute
                    # 的 pending_steering 检查走 commit→续跑（ conclude 由
                    # _resume_after_steering 负责）。此前这里无条件 conclude，
                    # 导致 resume 检查永远拿到 None——消息"已插入"却永远
                    # 卡在 waiting_for_steering（用户实测缺陷，2026-08）。
                    if bridge is None or bridge.service.pending_steering(cid) is None:
                        if bridge is not None:
                            bridge.service.conclude_steering(cid)
                    return (reply or ""), "ok", steering_qids
                # 事件唤醒（停止/Steering 注册）或看门到点：消费事件后重新计算
                if stop_evt.is_set():
                    stop_evt.clear()
                if steering_evt.is_set():
                    steering_evt.clear()
                if not deadlines:
                    continue  # 无看门截止：等待运行中到达的停止/Steering
                # 至少一个看门到点：分别判定
                stop_timed_out = (
                    stop_requested_at is not None
                    and time.time() >= stop_requested_at + self._stop_watchdog)
                steering_timed_out = (
                    steering_qids
                    and (bridge.service.steering_remaining(cid) or 0.0) <= 0.0)
                if stop_timed_out:
                    return "", "stop_timeout", steering_qids
                if steering_timed_out:
                    return "", "steering_timeout", steering_qids
                # 到点但尚未到任何截止（时钟边界）：继续循环重新计算
        finally:
            self._stop_evts.pop(cid, None)
            self._steering_evts.pop(cid, None)

    def _cleanup_agent_resources(self, agent) -> None:
        """清理 Agent 独占资源（进程池 / MCP / 并行工具池），可在线程池执行。

        【h 审计：共享 ToolRuntime 池清理后复用风险 —— 结论】
        - 池归属：agent._parallel_tool_executor 指向的池是"Agent 独占的
          ToolRuntime 单例池"（core/agent_runtime/tools.py::_runtime_pool：
          有 _tool_runtime 时返回 per-agent ToolRuntime.executor_pool()，
          无注入的测试/旧路径才退化为 Agent 级自建 ThreadPoolExecutor），
          不跨 Agent 共享 —— "共享"指同一 Agent 内串行/并行工具调用共用
          一池（L2#10），不是多 Agent 共享。
        - 复用防线：本清理只在两个"摘除"路径调用 —— evict() 与
          _discard_agent_with_cleanup()（停止/Steering 看门超时）。两者都在
          清理前把 entry.agent 置 None（_discard_agent_with_cleanup 先摘除
          再登记线程池异步清理），因此该 Agent 实例不会被下一轮执行复用；
          下次执行由 _execute_agent 懒创建全新 Agent（新 ToolRuntime 新池）。
          这是"清理后不复用"的不变量，代码注释与 evict 的 C1 说明一致。
        - 残余风险（已收敛）：看门超时路径下旧 agent.run() 可能仍在线程池
          运行，shutdown(wait=False) 后其后续工具提交会抛
          RuntimeError(cannot schedule new futures after shutdown)。该旧运行
          已被放弃（看门触发即不再消费其输出），调用方以 30s 上限等待并丢弃
          （_execute/_resume_after_steering 的 await wait_for(reply_task, 30)），
          不会泄漏给用户或下一 Turn —— 属 fail-fast 且被包裹，不构成可用性
          风险。
        - 注意事项：这里只关闭池并清别名，不关闭 agent._tool_runtime 本身
          （池的 owner 对象；线程为 daemon，随 Agent 实例 GC 自然回收），
          避免重复 shutdown 与同实例二次清理竞态。
        """
        try:
            if getattr(agent, "process_manager", None):
                agent.process_manager.cleanup_all()
            if getattr(agent, "mcp_manager", None):
                from core.mcp_client import run_in_mcp_loop
                run_in_mcp_loop(agent.mcp_manager.close_all(), timeout=10)
            # 并行工具执行池（Agent 独占 ToolRuntime 池，P1-3/L2#10）：
            # 统一走 ToolRuntime.shutdown_pool(wait=False)——关闭并标记，
            # 后续若该 Agent 被复用会惰性重建新池（不再抛 RuntimeError）。
            rt = getattr(agent, "_tool_runtime", None)
            if rt is not None and hasattr(rt, "shutdown_pool"):
                rt.shutdown_pool(wait=False)
            pool = getattr(agent, "_parallel_tool_executor", None)
            if pool is not None:
                pool.shutdown(wait=False)
                agent._parallel_tool_executor = None
        except Exception:
            logger.exception("统一会话 Agent 资源清理失败")

    def _discard_agent_with_cleanup(self, entry: "_ConversationEntry") -> None:
        """看门超时：摘除 entry 上的 Agent 并登记异步资源清理任务。

        停止/Steering 看门触发后 Agent 可能仍在旧 Turn 里运行；仅置
        entry.agent = None 会泄漏进程/MCP/并行池资源。这里把清理登记到
        线程池异步执行（设计方案 7.6/9.3）。"""
        agent = entry.agent
        entry.agent = None
        if agent is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(asyncio.to_thread(self._cleanup_agent_resources, agent))
        except RuntimeError:
            # 无运行中事件循环（如测试直接调用）：同步清理
            self._cleanup_agent_resources(agent)

    @staticmethod
    def _inflight_tool_summary(agent) -> str | None:
        """读取 Agent 当前在途工具摘要（Fix-3 可观测性；不可用则返回 None）。"""
        runtime = getattr(agent, "_tool_runtime", None)
        if runtime is None:
            return None
        try:
            return runtime.current_execution_summary()
        except Exception:
            return None

    async def _resume_after_steering(self, conversation, turn) -> None:
        """Steering 自动注入并续跑（设计方案 9.1/9.2）。

        prepare 已打断模型输出（agent.request_stop）；已运行工具自然结束后
        （_execute 返回、Turn 仍活动），这里把等待中的队列项注入为
        user_steering 节点并基于原上下文继续执行。续跑与普通 Turn 共用
        停止/Steering 双看门策略（设计方案 7.6/9.3），续跑期间新到的
        停止请求同样生效。"""
        bridge = self._dispatcher._conversation_bridge
        if bridge is None:
            return
        try:
            pending = bridge.service.pending_steering(conversation.conversation_id)
            if not pending:
                return
            # 注入节点（幂等：已 injected 的项跳过）
            nodes = bridge.service.commit_steering(
                conversation.conversation_id, pending)
            bridge.service.conclude_steering(conversation.conversation_id)
            if not nodes:
                return
            # 用最新注入的 Steering 文本续跑（复用同一活动 Turn）
            node = nodes[-1]
            self._stop_requested_at.pop(conversation.conversation_id, None)
            msg = InboundMessage(
                channel="webui",
                session_key=conversation.session_key,
                user_id="webui",
                user_name="WebUI",
                text=node.text or "",
                message_id=f"steer:{node.node_id}",
                metadata={"conversation_id": conversation.conversation_id,
                          "turn_id": turn.turn_id,
                          "from_conversation_queue": True,
                          "is_steering_resume": True},
            )
            entry = self._entry(conversation)
            channel = self._dispatcher._channels.get("webui")
            if channel is None:
                return
            entry.is_busy = True
            try:
                reply_task = asyncio.create_task(self._dispatcher._execute_agent(
                    entry, msg, channel,
                    runtime_metadata={"conversation_id": conversation.conversation_id,
                                      "turn_id": turn.turn_id}))
                reply, outcome, steering_qids = await self._await_with_watchdogs(
                    conversation.conversation_id, reply_task, bridge)
            except Exception as exc:
                logger.exception("Steering 续跑失败: %s", node.node_id)
                self._stop_requested_at.pop(conversation.conversation_id, None)
                if bridge is not None:
                    bridge.service.conclude_steering(conversation.conversation_id)
                    bridge.on_error(msg, str(exc))
                return
            finally:
                entry.last_active = time.time()
                entry.is_busy = False
            if outcome == "stop_timeout":
                logger.warning(
                    "Steering 续跑停止看门超时，Turn 置 error(stop_timeout): %s",
                    turn.turn_id)
                # 与主执行路径同语义：先退役事件再摘 Agent（防迟到事件写穿）
                if getattr(entry, "agent", None) is not None:
                    self._dispatcher.retire_agent(entry.agent)
                self._discard_agent_with_cleanup(entry)
                try:
                    bridge.service.complete_turn(
                        conversation.conversation_id, turn.turn_id, "error",
                        error_code="stop_timeout")
                    bridge.consume_stop_requested(conversation.conversation_id)
                    bridge.discard_active_turn(conversation.conversation_id)
                except Exception:
                    logger.exception("Steering 续跑停止看门终态失败: %s", turn.turn_id)
                return
            if outcome == "steering_timeout":
                logger.warning(
                    "Steering 续跑中断超时，Turn 置 error(steering_interrupt_timeout): %s",
                    turn.turn_id)
                # 同上：先退役事件再摘 Agent
                if getattr(entry, "agent", None) is not None:
                    self._dispatcher.retire_agent(entry.agent)
                self._discard_agent_with_cleanup(entry)
                try:
                    bridge.service.abort_steering(
                        conversation.conversation_id, steering_qids or [])
                    bridge.service.conclude_steering(conversation.conversation_id)
                    bridge.service.complete_turn(
                        conversation.conversation_id, turn.turn_id, "error",
                        error_code="steering_interrupt_timeout")
                    bridge.discard_active_turn(conversation.conversation_id)
                except Exception:
                    logger.exception("Steering 续跑超时终态失败: %s", turn.turn_id)
                return
            if bridge is not None:
                bridge.on_reply(msg, reply or "")
        except Exception:
            logger.exception("Steering 自动注入失败: %s",
                             conversation.conversation_id)

    # ------------------------------------------------------------
    # Agent 实例管理
    # ------------------------------------------------------------

    def _entry(self, conversation) -> object:
        """解析会话执行入口（C1：同一 session_key 全局唯一 Agent 实例）。

        优先复用 SessionManager 的 entry（存在即用；Agent 不存在时由
        _execute_agent 沿用现路径创建后回填 entry.agent），使统一 runner 与
        旧链路共享同一 Agent 实例；session_mgr 不可用或池满（get_or_create
        返回 None）时回退到私有入口，保持既有行为。运行时字段（工作区上下文/
        会话偏好）挂接在返回的 entry 上，_execute_agent 创建 Agent 时读取。"""
        session_key = conversation.session_key
        session_mgr = getattr(self._dispatcher, "session_mgr", None)
        shared = None
        if session_mgr is not None and hasattr(session_mgr, "get_or_create"):
            try:
                shared = session_mgr.get_or_create(session_key)
            except Exception:
                shared = None
        if shared is not None:
            with self._lock:
                previous = self._entries.get(conversation.conversation_id)
                self._entries[conversation.conversation_id] = shared
            # 池满期间曾回退私有入口并创建过 Agent：切回共享入口时清理，防泄漏
            if previous is not None and previous is not shared \
                    and isinstance(previous, _ConversationEntry):
                if getattr(previous, "agent", None) is not None:
                    self._discard_agent_with_cleanup(previous)
            return shared
        with self._lock:
            entry = self._entries.get(conversation.conversation_id)
            if entry is None:
                entry = _ConversationEntry(
                    session_key=session_key,
                    workspace_id=conversation.workspace_id)
                self._entries[conversation.conversation_id] = entry
            return entry

    def _on_steering_wait(self, conversation_id: str) -> None:
        """service Steering 钩子（B10）：注册/结束时唤醒对应会话的看门。

        由 ConversationService.set_steering_wait_hook 注册，在
        register_steering_wait / conclude_steering 时触发；看门事件不存在
        （无活动等待）时静默忽略。"""
        steering_evt = self._steering_evts.get(conversation_id)
        if steering_evt is not None:
            steering_evt.set()

    def agent_for_session(self, session_key: str):
        """返回会话（统一 runner 路径）持有的 Agent 实例。

        C1：runner 复用 SessionManager entry，因此这里返回的即 session_mgr
        entry.agent（同一 session_key 全局唯一实例），上下文统计（/context）
        能反映运行期切换过的模型/上下文长度。"""
        if not session_key:
            return None
        try:
            bridge = getattr(self._dispatcher, "_conversation_bridge", None)
            if bridge is None:
                return None
            conv = bridge.service.store.get_conversation_by_key(session_key)
            if conv is None:
                return None
            with self._lock:
                entry = self._entries.get(conv.conversation_id)
            return getattr(entry, "agent", None) if entry else None
        except Exception:
            return None

    def _attach_workspace_context(self, entry: "_ConversationEntry",
                                  conversation) -> None:
        """工作区会话挂接快照运行上下文（设计方案：工作区执行上下文集成）。

        从 session_key 解析 workspace_id/session_id，调用注入的 provider 构建
        冻结上下文；provider 返回 None（会话不存在等）则保持默认能力子集。"""
        provider = self._workspace_context_provider
        if provider is None:
            return
        parts = (conversation.session_key or "").split(":")
        if len(parts) < 3 or parts[0] != "workspace":
            return
        workspace_id, session_id = parts[1], parts[2]
        try:
            ctx = provider(workspace_id, session_id)
        except Exception:
            logger.exception("工作区上下文构建失败: %s", conversation.session_key)
            return
        if not ctx:
            return
        entry.runtime_context = ctx.get("runtime_context")
        entry.runtime_snapshot_id = ctx.get("snapshot_id") or ""
        entry.runtime_model = ctx.get("model") or ""
        entry.runtime_permission_mode = ctx.get("permission_mode") or ""
        entry.runtime_reasoning_level = ctx.get("reasoning_level") or "inherit"
        entry.runtime_max_steps = ctx.get("max_steps")
        entry.runtime_mcp_servers = ctx.get("mcp_servers")
        entry.runtime_profile_prompt = ctx.get("profile_prompt")
        entry.runtime_allowed_tools = ctx.get("allowed_tools")
        entry.runtime_allowed_skills = ctx.get("allowed_skills")

    def _attach_session_prefs(self, entry: "_ConversationEntry",
                              conversation) -> None:
        """非工作区会话：应用会话偏好（模型/推理/权限，设计方案：管理操作统一化）。

        prefs 存于 Conversation.route_metadata.prefs，由 update_prefs 写入；
        应用到 entry 的 runtime_* 字段，_execute_agent 创建 Agent 时读取。"""
        try:
            prefs = dict((conversation.route_metadata or {}).get("prefs") or {})
            if prefs.get("model"):
                entry.runtime_model = str(prefs["model"])
            if prefs.get("reasoning_level"):
                entry.runtime_reasoning_level = str(prefs["reasoning_level"])
            if prefs.get("permission_mode"):
                entry.runtime_permission_mode = str(prefs["permission_mode"])
        except Exception:
            logger.debug("会话偏好应用失败: %s", conversation.session_key)

    def apply_prefs(self, conversation_id: str, prefs: dict) -> bool:
        """运行中的 Agent 立即应用会话偏好（模型/推理/权限切换即时生效）。

        返回是否命中已加载的 Agent；未命中（未运行）则由下次执行懒应用。"""
        with self._lock:
            entry = self._entries.get(conversation_id)
        if entry is None:
            return False
        agent = getattr(entry, "agent", None)
        try:
            if agent is not None:
                import inspect
                # agent.switch_llm(model=..., reasoning_level=...)（线程安全切换）
                switch = getattr(agent, "switch_llm", None)
                model = prefs.get("model")
                reasoning = prefs.get("reasoning_level")
                # 切推理等级时也要真正应用到 live Agent（与 _cmd_reasoning 一致：
                # 带当前模型调 switch_llm），否则 agent.llm.reasoning_level 不更新，
                # _reasoning_payload.effective 与前端 toast 恒为旧值（陈旧提示）。
                if switch is not None and (model or reasoning):
                    switch_model = (str(model) if model else str(getattr(agent.llm, "model", "") or ""))
                    resolved_reasoning = reasoning or getattr(
                        agent, "_session_reasoning_override", None)
                    loop = asyncio.get_event_loop()
                    executor = getattr(self._dispatcher, "_executor", None) \
                        or getattr(self._dispatcher.session_mgr, "get_executor", lambda: None)()

                    def _log_switch_error(fut, _cid=conversation_id) -> None:
                        # P3-6：run_in_executor 的 future 不能丢弃——
                        # switch_llm 异常必须落日志，否则静默失败。
                        try:
                            exc = fut.exception()
                        except Exception:
                            return  # 已取消等边界情况
                        if exc is not None:
                            logger.warning("会话偏好即时切换模型失败: %s: %s",
                                           _cid, exc)

                    if executor is not None:
                        loop.run_in_executor(
                            executor, lambda: switch(
                                model=switch_model,
                                reasoning_level=resolved_reasoning)
                        ).add_done_callback(_log_switch_error)
                    else:
                        switch(model=switch_model,
                               reasoning_level=resolved_reasoning)
                    if reasoning:
                        agent._session_reasoning_override = (
                            reasoning if reasoning not in ("inherit", "") else None)
                # 权限档位即时生效
                if prefs.get("permission_mode"):
                    glue = getattr(self._dispatcher, "_webui_glue", None)
                    if glue is not None:
                        try:
                            glue.apply_permission_mode(agent, str(prefs["permission_mode"]))
                        except Exception:
                            logger.debug("权限档位即时应用失败", exc_info=True)
            # 更新 entry 字段（下次 Agent 重建也生效）
            entry.runtime_model = str(prefs.get("model") or entry.runtime_model or "")
            entry.runtime_reasoning_level = str(
                prefs.get("reasoning_level") or entry.runtime_reasoning_level or "inherit")
            entry.runtime_permission_mode = str(
                prefs.get("permission_mode") or entry.runtime_permission_mode or "")
            return agent is not None
        except Exception:
            logger.exception("会话偏好即时应用失败: %s", conversation_id)
            return False

    def request_stop(self, conversation_id: str) -> bool:
        """真正打断运行中的 Agent（协作式停止，设计方案 7.6）。

        与旧链路 ``api_chat._make_stop`` 的 ``entry.agent.request_stop()``
        对齐：仅置 stopping 状态/标志不会让 AgentLoop 退出，必须调用
        ``agent.request_stop()`` 让下一步检查点消费停止标志。同时记录
        停止请求时刻，供 _execute 的看门超时判定。"""
        with self._lock:
            entry = self._entries.get(conversation_id)
        self._stop_requested_at[conversation_id] = time.time()
        # B10：唤醒看门，立即按新的 10s 截止重新计算
        stop_evt = self._stop_evts.get(conversation_id)
        if stop_evt is not None:
            stop_evt.set()
        if entry is None:
            return False
        agent = getattr(entry, "agent", None)
        if agent is None:
            return False
        try:
            agent.request_stop()
            return True
        except Exception:
            logger.exception("统一会话 Agent 停止请求失败: %s", conversation_id)
            return False

    async def evict(self, conversation_id: str) -> None:
        """驱逐 Conversation 的 Agent 实例（不保存旧会话数据）。

        C1：入口可能是共享的 SessionManager entry —— 清理资源后必须把
        entry.agent 置空，避免 session_mgr 持有"资源已清理"的僵尸 Agent，
        下次任何链路执行时由 _execute_agent 重建。"""
        with self._lock:
            entry = self._entries.pop(conversation_id, None)
            self._running.discard(conversation_id)
        if entry is None:
            return
        agent = getattr(entry, "agent", None)
        try:
            entry.agent = None
        except Exception:
            logger.debug("驱逐时置空共享 entry.agent 失败: %s", conversation_id)
        if agent is None:
            return
        # P2-3：_cleanup_agent_resources 里 MCP close_all 最长 10s，移入线程
        # 执行，避免阻塞事件循环（与 _discard_agent_with_cleanup /
        # session.py:_cleanup_entry 同一做法）。先摘除再清理，保证"清理中
        # 不被复用"不变量。
        await asyncio.to_thread(self._cleanup_agent_resources, agent)

    async def _evict_all(self) -> None:
        for conversation_id in list(self._entries.keys()):
            await self.evict(conversation_id)
