# -*- coding: utf-8 -*-
"""
消息调度器 —— 入站去重 → 会话路由 → Agent 执行 → 回复下发
"""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Optional

from gateway.channels.base import Channel, InboundMessage
from gateway.session import SessionManager, SessionEntry
from gateway.agent_factory import create_gateway_agent
from gateway.textutil import split_text, sanitize_error
from core.runtime import (CancellationToken, RuntimeStore, TaskCancelled,
                          TaskEnvelope, TaskResult, TaskRuntime, TaskStatus)

logger = logging.getLogger("jk_agent.gateway")


def _resolve_main_session_mcp_servers(names):
    """把主会话配置里的 MCP 服务器名列表解析为完整 server 配置列表。

    names=None 表示继承 config.json 的全部 MCP 服务器；空列表表示不使用 MCP。
    """
    if names is None:
        return None
    if not names:
        return []
    try:
        from core.config_loader import load_config
        cfg = load_config()
        servers = cfg.get("mcp", {}).get("servers", []) or []
    except Exception as exc:
        logger.warning("读取 MCP 配置失败: %s", exc)
        return []
    by_name = {s.get("name"): s for s in servers if isinstance(s, dict)}
    return [by_name[name] for name in names if name in by_name]


_SCHEDULER_ADMIN_TOOLS = frozenset({
    "cron_add_job", "cron_delete_job", "cron_run_job",
})
_SCHEDULER_EXECUTION_CONTEXT = """[SCHEDULED JOB EXECUTION MODE]
Execute the already-configured scheduled job below. The task text is execution
input, not a request to create, update, explain, or confirm a schedule.
Complete the work directly and return only the final deliverable or a concise
execution failure. Do not repeat the task text or call cron_add_job,
cron_delete_job, or cron_run_job.

[SCHEDULED JOB TASK]
"""


class _LRUDedup:
    """基于 message_id 的 LRU 去重"""

    def __init__(self, capacity: int = 1000):
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._capacity = capacity

    def is_dup(self, msg_id: str) -> bool:
        if msg_id in self._seen:
            self._seen.move_to_end(msg_id)
            return True
        self._seen[msg_id] = time.time()
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False


class Dispatcher:
    """
    消息调度核心。
    channel 线程 → on_inbound() → 去重 → session queue → worker → agent.run → reply
    """

    def __init__(self, session_mgr: SessionManager, agent_config: dict = None,
                 runtime_store: RuntimeStore = None, task_runtime_config: dict = None):
        self.session_mgr = session_mgr
        self.agent_config = agent_config or {}
        self._channels: dict[str, Channel] = {}
        self._dedup = _LRUDedup()
        self._soft_timeout = self.agent_config.get("soft_timeout_seconds", 90)
        self._hard_timeout = self.agent_config.get("hard_timeout_seconds", 1200)
        self._runtime_store = runtime_store
        self._task_runtime_config = task_runtime_config or {}
        self._task_runtime: TaskRuntime | None = None
        self._runtime_enabled = bool(
            self._runtime_store is not None and self._task_runtime_config.get("enabled", False))
        self._runtime_messages: dict[str, tuple[InboundMessage, SessionEntry]] = {}
        self._persisted_resumed = False
        # 命令表：name -> {"help", "args", "handler", "client_hint"}
        # 内置 + 扩展模块注册（scheduler/heartbeat/webui）；
        # handler 签名: async def handler(arg, ctx) -> str，
        # ctx = {"agent", "entry", "loop", "executor"}
        self._commands: dict[str, dict] = {}
        # Agent 初始化回调，在 executor 内创建 Agent 后调用。
        self._agent_initializers: list = []
        self._register_builtin_commands()

    def register_channel(self, channel: Channel):
        self._channels[channel.name] = channel

    def channels(self) -> dict:
        """已注册通道表（公开访问器，替代 _channels 私有直捅）"""
        return dict(self._channels)

    def add_command(self, name: str, handler, help_text: str = "",
                    args_hint: str = "", client_hint: str = ""):
        """注册命令。handler: async def handler(arg, ctx) -> str"""
        self._commands[name.lower()] = {
            "help": help_text, "args": args_hint,
            "handler": handler, "client_hint": client_hint,
        }

    def add_agent_initializer(self, cb):
        """注册 agent 初始化回调：agent 在 executor 内创建后立即调用 cb(agent, entry)。

        WebUI 等调用方可用它注入会话级初始化逻辑。
        回调在 executor 线程执行，勿做耗时操作。
        """
        self._agent_initializers.append(cb)

    @property
    def task_runtime_enabled(self) -> bool:
        return self._task_runtime is not None

    async def start(self) -> None:
        """Start optional persistent task execution before channels accept work."""
        if not self._runtime_enabled or self._task_runtime is not None:
            return
        # A Gateway restart cannot safely resume an in-flight user task until
        # full context/attachment recovery is implemented. Preserve the audit
        # trail and require an explicit retry instead of silently duplicating it.
        self._runtime_store.recover_interrupted(requeue=False)
        # Older gateway versions marked missing-context work as blocked. Only
        # scheduler/plan envelopes have a durable enough delivery context
        # to be replayed after restart. A normal inbound user message may be
        # backed by a one-shot WebUI future or a platform-native raw object, so
        # reopening it would duplicate work or deliver a reply nowhere.
        recovered = self._runtime_store.requeue_missing_context(
            sources={"scheduler", "plan"})
        if recovered:
            logger.info("Requeued %d tasks waiting for channel context", len(recovered))
        self._task_runtime = TaskRuntime(
            self._runtime_store, self._execute_runtime_task,
            max_global_concurrency=int(self._task_runtime_config.get("max_global_concurrency", 4)),
            worker_id="gateway",
            max_attempts=int(self._task_runtime_config.get("max_attempts", 2)),
            cancel_grace_seconds=float(self._task_runtime_config.get("cancel_grace_seconds", 10)),
            zombie_max_seconds=float(self._task_runtime_config.get("zombie_max_seconds", 300)),
        )
        # Existing tasks are enqueued only after Gateway has registered all
        # channels and the volatile delivery context has been reconstructed.
        await self._task_runtime.start(
            recover_interrupted=False, enqueue_existing=False)
        logger.info("TaskRuntime enabled: workers=%d", self._task_runtime.max_global_concurrency)

    async def resume_persisted_tasks(self) -> None:
        """Restore queued task delivery context after all channels are ready."""
        if self._task_runtime is None or self._runtime_store is None or self._persisted_resumed:
            return
        snapshots = self._runtime_store.list_tasks(
            statuses={TaskStatus.QUEUED, TaskStatus.RETRY_WAIT, TaskStatus.INTERRUPTED})
        for snapshot in snapshots:
            status = snapshot.record.status
            # In-flight user tasks are deliberately not replayed automatically.
            # Plan tasks are safe to hand back to PlanExecutor, which will
            # reconcile the interrupted step before continuing the Plan.
            if status is TaskStatus.INTERRUPTED:
                if not snapshot.envelope.plan_id:
                    continue
                self._runtime_store.transition_task(
                    snapshot.envelope.task_id, TaskStatus.QUEUED)
            if not self._restore_runtime_context(snapshot.envelope):
                # Do not let TaskRuntime execute a task whose channel cannot
                # be rebound; keep the durable failure explicit instead of
                # producing the same opaque replay error in a loop.
                current = self._runtime_store.get_task(snapshot.envelope.task_id)
                if current and current.record.status is TaskStatus.QUEUED:
                    self._runtime_store.transition_task(
                        snapshot.envelope.task_id, TaskStatus.BLOCKED,
                        error_code="RUNTIME_CONTEXT_MISSING",
                        error_message="cannot safely replay a task without its inbound channel context",
                    )
        await self._task_runtime.enqueue_persisted()
        self._persisted_resumed = True

    def _restore_runtime_context(self, envelope: TaskEnvelope) -> bool:
        """Rebuild the process-local message/SessionEntry pair from an envelope."""
        metadata = envelope.metadata or {}
        channel_name = str(metadata.get("channel") or "").strip()
        if not channel_name:
            channel_name = envelope.source if envelope.source in self._channels else "webui"
        entry = self.session_mgr.get_or_create(envelope.session_key)
        if entry is None:
            logger.warning("无法恢复任务上下文，当前会话池已满: %s", envelope.task_id)
            return False
        message = InboundMessage(
            channel=channel_name,
            session_key=envelope.session_key,
            user_id=str(metadata.get("user_id") or "system"),
            user_name=str(metadata.get("user_name") or "Runtime"),
            text=envelope.prompt,
            message_id=str(metadata.get("message_id") or envelope.task_id),
            is_group=bool(metadata.get("is_group", False)),
        )
        self._runtime_messages[envelope.task_id] = (message, entry)
        channel = self._channels.get(channel_name)
        restore = getattr(channel, "restore_runtime_context", None) if channel else None
        if callable(restore):
            try:
                restore(message, envelope)
            except Exception:
                self._runtime_messages.pop(envelope.task_id, None)
                logger.exception("Failed to restore channel context for task %s", envelope.task_id)
                return False
        return True

    async def stop(self) -> None:
        if self._task_runtime is not None:
            await self._task_runtime.stop()
            self._task_runtime = None
        self._persisted_resumed = False

    @staticmethod
    def _runtime_session_id(session_key: str) -> str:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
        return f"sess_{digest}"

    @staticmethod
    def _runtime_source(channel_name: str) -> str:
        return {
            "scheduler": "scheduler",
            "heartbeat": "heartbeat",
        }.get(channel_name, "user")

    def commands_table(self) -> list:
        """命令清单（GET /api/commands 数据源）"""
        return [
            {"name": name, "args": c.get("args", ""),
             "help": c.get("help", ""),
             "client_hint": c.get("client_hint", "")}
            for name, c in sorted(self._commands.items())
        ]

    # ---------- 内置命令 ----------

    def _register_builtin_commands(self):
        self._commands.update({
            "/compact": {"help": "/compact — 压缩上下文，释放 token 空间",
                          "args": "", "handler": self._cmd_compact},
            "/clear": {"help": "/clear — 清空会话历史",
                        "args": "", "handler": self._cmd_clear},
            "/stats": {"help": "/stats — 查看上下文占用",
                        "args": "", "handler": self._cmd_stats},
            "/model": {"help": "/model [名称] — 查看/切换模型",
                        "args": "[名称]", "handler": self._cmd_model},
            "/reasoning": {"help": "/reasoning [inherit|等级] — 查看/切换本会话推理等级",
                             "args": "[inherit|provider_default|none|minimal|low|medium|high|xhigh|max]",
                             "handler": self._cmd_reasoning},
            "/session": {"help": "/session — 查看会话信息",
                          "args": "", "handler": self._cmd_session},
            "/help": {"help": "/help — 显示此帮助",
                       "args": "", "handler": self._cmd_help},
        })

    async def _cmd_compact(self, arg, ctx):
        ok = await ctx["loop"].run_in_executor(
            ctx["executor"], ctx["agent"]._full_compress, False)
        return "✅ 上下文压缩完成" if ok else "ℹ️ 上下文较短，无需压缩"

    async def _cmd_clear(self, arg, ctx):
        await ctx["loop"].run_in_executor(
            ctx["executor"], ctx["agent"].clear_history)
        return "✅ 会话已清空"

    async def _cmd_stats(self, arg, ctx):
        agent = ctx["agent"]
        stats = agent.store.stats()
        ratio = stats.get("usage_ratio", 0) * 100
        return (
            f"📊 上下文统计\n"
            f"  模型: {agent.llm.model}\n"
            f"  消息数: {stats.get('total_messages', 0)}\n"
            f"  已用 token: {stats.get('total_tokens', 0)}\n"
            f"  上限: {stats.get('max_tokens', 0)}\n"
            f"  使用率: {ratio:.1f}%"
        )

    async def _cmd_model(self, arg, ctx):
        agent, entry = ctx["agent"], ctx["entry"]
        if not arg:
            return f"🤖 当前模型: {agent.llm.model}"
        try:
            reasoning_override = getattr(agent, "_session_reasoning_override", None)
            await ctx["loop"].run_in_executor(
                ctx["executor"], lambda m=arg, r=reasoning_override:
                agent.switch_llm(model=m, reasoning_level=r))
            # 记入 sessions_map v2，重启后可回填（修模型切换不持久缺口）
            if entry is not None:
                from gateway.agent_factory import update_map_meta
                update_map_meta(entry.session_key, model=arg)
            return f"✅ 已切换到模型: {arg}"
        except Exception as e:
            return f"❌ 切换到 {arg} 失败: {e}\n请检查 config.json 的 llm.models 中是否有该模型的配置（api_key / base_url）"

    async def _cmd_reasoning(self, arg, ctx):
        """Set a per-session override without changing config.json."""
        from core.reasoning import REASONING_LEVELS, normalize_reasoning_level

        agent, entry = ctx["agent"], ctx["entry"]
        requested = (arg or "").strip().lower()
        if not requested:
            override = getattr(agent, "_session_reasoning_override", None)
            source = "继承模型配置" if override is None else "会话覆盖"
            return (f"🧠 当前推理等级: {agent.llm.reasoning_level}\n"
                    f"  来源: {source}\n"
                    "  可选: inherit / " + " / ".join(REASONING_LEVELS))

        explicit_level = None
        if requested not in ("inherit", "default"):
            try:
                explicit_level = normalize_reasoning_level(
                    requested, source="推理等级")
            except ValueError as e:
                return f"❌ {e}"
        try:
            await ctx["loop"].run_in_executor(
                ctx["executor"], lambda: agent.switch_llm(
                    model=agent.llm.model, reasoning_level=explicit_level))
            agent._session_reasoning_override = explicit_level
            if entry is not None:
                from gateway.agent_factory import update_map_meta
                update_map_meta(entry.session_key, reasoning_level=explicit_level)
            source = "继承模型配置" if explicit_level is None else "会话覆盖"
            return f"✅ 推理等级已切换为: {agent.llm.reasoning_level}\n  来源: {source}"
        except Exception as e:
            return f"❌ 切换推理等级失败: {e}"

    async def _cmd_session(self, arg, ctx):
        agent = ctx["agent"]
        return f"💾 会话 ID: {agent.store.session_id}\n  消息数: {len(agent.messages)}"

    async def _cmd_help(self, arg, ctx):
        lines = ["📋 可用命令:"]
        for c in sorted(self._commands.values(),
                        key=lambda c: c.get("help", "")):
            if c.get("help"):
                lines.append(c["help"])
        return "\n".join(lines)

    async def on_inbound(self, msg: InboundMessage):
        """入站消息处理（从 channel 线程通过 run_coroutine_threadsafe 调用）"""
        # 去重
        if self._dedup.is_dup(msg.message_id):
            logger.debug("重复消息，跳过: %s", msg.message_id)
            return
        if self.task_runtime_enabled:
            await self._on_inbound_task_runtime(msg)
            return

        # 获取/创建会话
        entry = self.session_mgr.get_or_create(msg.session_key)
        if entry is None:
            # 会话池满
            channel = self._channels.get(msg.channel)
            if channel:
                await channel.send_reply(msg, "🈵 当前会话数已达上限，请稍后再试")
            return
        # 确保 worker 在运行
        if entry.worker_task is None or entry.worker_task.done():
            entry.worker_task = asyncio.create_task(
                self._session_worker(entry)
            )
        # 入队
        await entry.queue.put(msg)

    async def submit_plan_task(self, *, session_key: str, plan_id: str, plan_task_id: str,
                               prompt: str,
                               priority: int = 60, timeout_seconds: int | None = None,
                               metadata: dict | None = None, task_id: str | None = None) -> str:
        """Submit one approved PlanTask through the only persistent execution path."""
        if self._task_runtime is None or self._runtime_store is None:
            raise RuntimeError("TaskRuntime must be enabled before executing a Plan")
        entry = self.session_mgr.get_or_create(session_key)
        if entry is None:
            raise RuntimeError("session capacity has been reached")
        channel = self._channels.get("webui")
        if channel is None:
            raise RuntimeError("the WebUI delivery channel is unavailable")
        session_id = self._runtime_session_id(session_key)
        self._runtime_store.upsert_session(session_id, session_key, channel="webui", status="active")
        envelope = TaskEnvelope.create(
            session_id=session_id, session_key=session_key, source="plan", prompt=prompt, task_id=task_id,
            plan_id=plan_id, plan_task_id=plan_task_id, priority=priority,
            timeout_seconds=timeout_seconds or int(self._task_runtime_config.get(
                "default_timeout_seconds", self._hard_timeout)),
            max_steps=int(self.agent_config.get("max_steps", 100)),
            metadata={"channel": "webui", "deliver_reply": False, **(metadata or {})},
        )
        message = InboundMessage(
            channel="webui", session_key=session_key, user_id="system", user_name="PlanRuntime",
            text=prompt, message_id=envelope.task_id,
        )
        self._runtime_messages[envelope.task_id] = (message, entry)
        submitted_id = await self._task_runtime.submit(envelope)
        if submitted_id != envelope.task_id:
            self._runtime_messages.pop(envelope.task_id, None)
        return submitted_id

    async def wait_runtime_task(self, task_id: str, *, timeout: float | None = None) -> TaskResult:
        if self._task_runtime is None:
            raise RuntimeError("TaskRuntime is not enabled")
        result = await self._task_runtime.wait(task_id, timeout=timeout)
        # PlanRuntime is the only caller without the inbound terminal-delivery
        # watcher. Release its reconstructed/volatile context once its durable
        # result has been observed.
        if result.status.is_terminal:
            self._runtime_messages.pop(task_id, None)
        return result

    async def cancel_runtime_task(self, task_id: str, *, reason: str = "user_requested") -> None:
        """Cancel one durable runtime task without exposing TaskRuntime internals."""
        if self._task_runtime is None:
            raise RuntimeError("TaskRuntime is not enabled")
        await self._task_runtime.cancel(task_id, reason)
    async def _on_inbound_task_runtime(self, msg: InboundMessage) -> None:
        """Persist an inbound message before scheduling its Agent execution."""
        entry = self.session_mgr.get_or_create(msg.session_key)
        channel = self._channels.get(msg.channel)
        if entry is None:
            if channel:
                await channel.send_reply(msg, "🈵 当前会话数已达上限，请稍后再试")
            return
        if channel is None:
            logger.warning("TaskRuntime 收到未注册 channel: %s", msg.channel)
            return

        source = self._runtime_source(msg.channel)
        priority = {"user": 80, "scheduler": 20, "heartbeat": 10}.get(source, 40)
        runtime_session_id = self._runtime_session_id(msg.session_key)
        self._runtime_store.upsert_session(
            runtime_session_id, msg.session_key, channel=msg.channel,
            status="active", metadata={"user_id": msg.user_id, "is_group": msg.is_group},
        )
        envelope = TaskEnvelope.create(
            session_id=runtime_session_id,
            session_key=msg.session_key,
            source=source,
            prompt=msg.text or "[image message]",
            priority=priority,
            timeout_seconds=int(self._task_runtime_config.get("default_timeout_seconds", self._hard_timeout)),
            max_steps=int(self.agent_config.get("max_steps", 100)),
            idempotency_key=msg.message_id or None,
            metadata={"channel": msg.channel, "message_id": msg.message_id,
                      "has_images": bool(getattr(msg, "images", None)),
                      "user_id": msg.user_id, "user_name": msg.user_name,
                      "is_group": bool(msg.is_group),
                      # Only scheduler currently needs a durable raw context.
                      # Platform-native raw objects are intentionally not
                      # serialized into SQLite.
                      "channel_context": (msg.raw if msg.channel == "scheduler"
                                          and isinstance(msg.raw, dict) else {})},
        )
        # Register the volatile delivery context before queueing. A worker may
        # start as soon as submit() yields to PriorityQueue.put().
        self._runtime_messages[envelope.task_id] = (msg, entry)
        task_id = await self._task_runtime.submit(envelope)
        if task_id == envelope.task_id:
            asyncio.create_task(self._deliver_runtime_terminal(task_id, msg))
        else:
            self._runtime_messages.pop(envelope.task_id, None)
            logger.info("任务幂等命中: message=%s -> task=%s", msg.message_id, task_id)

    async def _deliver_runtime_terminal(self, task_id: str, msg: InboundMessage) -> None:
        """Deliver terminal failures/cancellation that have no Agent reply body."""
        try:
            result = await self._task_runtime.wait(task_id)
        except Exception:
            logger.exception("等待 TaskRuntime 终态失败: %s", task_id)
            return
        if result.status == TaskStatus.COMPLETED:
            self._runtime_messages.pop(task_id, None)
            return
        channel = self._channels.get(msg.channel)
        if channel is None:
            self._runtime_messages.pop(task_id, None)
            return
        labels = {
            TaskStatus.CANCELLED: "⏹️ 任务已停止",
            TaskStatus.TIMED_OUT: "⏰ 任务执行超时",
            TaskStatus.BLOCKED: "⚠️ 任务被阻塞",
            TaskStatus.FAILED: "❌ 任务执行失败",
        }
        text = labels.get(result.status, "⚠️ 任务未完成")
        detail = result.error_message or result.summary
        if detail:
            text += f"：{detail}"
        try:
            await channel.send_reply(msg, text)
        except Exception:
            logger.debug("任务终态投递失败: %s", task_id, exc_info=True)
        finally:
            # Keep the message/session pair through TaskRuntime retry attempts.
            # This watcher observes only a terminal result, so cleanup here
            # cannot make a retry lose its inbound delivery context.
            self._runtime_messages.pop(task_id, None)

    async def _execute_runtime_task(self, envelope: TaskEnvelope,
                                    token: CancellationToken) -> TaskResult:
        """TaskRuntime executor bridge: Agent execution plus channel delivery."""
        runtime_message = self._runtime_messages.get(envelope.task_id)
        if runtime_message is None:
            return TaskResult(
                task_id=envelope.task_id, status=TaskStatus.BLOCKED,
                summary="inbound delivery context is unavailable after restart",
                error_code="RUNTIME_CONTEXT_MISSING",
                error_message="cannot safely replay a task without its inbound channel context",
            )
        msg, entry = runtime_message
        channel = self._channels.get(msg.channel)
        if channel is None:
            return TaskResult(task_id=envelope.task_id, status=TaskStatus.FAILED,
                              summary="channel is unavailable", error_code="CHANNEL_UNAVAILABLE",
                              error_message=msg.channel)

        entry.is_busy = True
        started_at = time.time()
        entry.last_active = started_at
        try:
            def stop_agent(_reason: str) -> None:
                agent = entry.agent
                if agent is not None:
                    agent.request_stop()

            token.add_callback(stop_agent)
            runtime_metadata = {**envelope.metadata, "task_source": envelope.source}
            reply = await self._execute_agent(
                entry, msg, channel, runtime_managed=True, cancellation_token=token,
                runtime_metadata=runtime_metadata)
            token.checkpoint()
            elapsed = max(0.0, time.time() - started_at)
            if reply and envelope.metadata.get("deliver_reply", True):
                agent = entry.agent
                model = agent.llm.model if agent and getattr(agent, "llm", None) else ""
                msg._reply_meta = {"model": model, "elapsed": elapsed, "task_id": envelope.task_id}
                await self._send_chunked(channel, msg, reply)
            return TaskResult(task_id=envelope.task_id, status=TaskStatus.COMPLETED,
                              visible_text=reply or "", summary=(reply or "")[:1000])
        except TaskCancelled:
            raise
        except Exception:
            # TaskRuntime records the error and _deliver_runtime_terminal sends
            # one terminal reply. Do not duplicate channel output here.
            raise
        finally:
            entry.is_busy = False
            entry.last_active = time.time()
            # Do not discard this mapping here. TaskRuntime may turn an
            # exception/FAILED result into RETRY_WAIT and invoke this executor
            # again with the same envelope. Terminal cleanup is performed by
            # _deliver_runtime_terminal (or wait_runtime_task for plan tasks).

    async def _session_worker(self, entry: SessionEntry):
        """每会话的 FIFO worker 协程"""
        while True:
            try:
                msg: InboundMessage = await asyncio.wait_for(
                    entry.queue.get(), timeout=300
                )
            except asyncio.TimeoutError:
                # 5 分钟无消息，worker 自动退出（janitor 会清理 entry）
                break
            except asyncio.CancelledError:
                break

            channel = self._channels.get(msg.channel)
            if not channel:
                continue

            entry.is_busy = True
            entry.last_active = time.time()
            try:
                t0 = time.time()
                reply = await self._execute_agent(entry, msg, channel)
                elapsed = time.time() - t0
                if reply:
                    # 附加 meta 信息供 channel 格式化使用
                    a = entry.agent
                    model = a.llm.model if a and a.llm else ""
                    msg._reply_meta = {"model": model, "elapsed": elapsed}
                    await self._send_chunked(channel, msg, reply)
            except Exception as e:
                logger.error("处理消息异常 [%s]: %s", msg.session_key, e, exc_info=True)
                try:
                    err_text = f"❌ 处理出错: {sanitize_error(e)}"
                    await channel.send_reply(msg, err_text)
                except Exception:
                    pass
            finally:
                entry.is_busy = False
                entry.last_active = time.time()

    @staticmethod
    def _task_input_for_agent(msg: InboundMessage, runtime_metadata: dict | None) -> str:
        """Attach producer-specific execution constraints to synthetic work."""
        if (runtime_metadata or {}).get("task_source") == "scheduler":
            return _SCHEDULER_EXECUTION_CONTEXT + (msg.text or "")
        return msg.text

    async def _execute_agent(
        self, entry: SessionEntry, msg: InboundMessage, channel: Channel,
        *, runtime_managed: bool = False,
        cancellation_token: CancellationToken | None = None,
        runtime_metadata: dict | None = None,
    ) -> Optional[str]:
        """在线程池中执行 agent.run()，带 soft/hard 超时"""
        loop = asyncio.get_event_loop()
        executor = self.session_mgr.get_executor()

        # 延迟创建 Agent（创建后在 executor 内运行初始化回调）
        if entry.agent is None:
            cfg = self.agent_config
            initializers = list(self._agent_initializers)

            def _create():
                # Phase 4：工作区会话使用快照冻结的运行时上下文/模型/权限/MCP
                if getattr(entry, "runtime_context", None) is not None:
                    agent = create_gateway_agent(
                        session_key=entry.session_key,
                        model=getattr(entry, "runtime_model", "") or cfg.get("model", ""),
                        max_steps=getattr(entry, "runtime_max_steps", None)
                                 or cfg.get("max_steps", 100),
                        permission_mode=getattr(entry, "runtime_permission_mode", "")
                                        or cfg.get("permission_mode", "allow"),
                        quiet=cfg.get("quiet", True),
                        auto_approve_plan=cfg.get("auto_approve_plan", True),
                        runtime_context=entry.runtime_context,
                        mcp_servers=getattr(entry, "runtime_mcp_servers", None),
                        profile_prompt=getattr(entry, "runtime_profile_prompt", None),
                        allowed_tools=getattr(entry, "runtime_allowed_tools", None),
                        allowed_skills=getattr(entry, "runtime_allowed_skills", None),
                        reasoning_level=(None if getattr(entry, "runtime_reasoning_level", "inherit") == "inherit"
                                         else getattr(entry, "runtime_reasoning_level", None)),
                    )
                else:
                    # 所有非工作区会话（WebUI 主会话/新建 WebUI 会话/飞书等）
                    # 使用同一份默认能力子集；空值（缺省/null）继承全部。
                    main_caps = self.agent_config.get("main_session_caps") or {}
                    mcp_configs = _resolve_main_session_mcp_servers(
                        main_caps.get("mcp_servers")) if main_caps else None
                    agent = create_gateway_agent(
                        session_key=entry.session_key,
                        model=cfg.get("model", ""),
                        max_steps=cfg.get("max_steps", 100),
                        permission_mode=cfg.get("permission_mode", "allow"),
                        quiet=cfg.get("quiet", True),
                        auto_approve_plan=cfg.get("auto_approve_plan", True),
                        mcp_servers=mcp_configs,
                        allowed_tools=main_caps.get("tools") if main_caps else None,
                        allowed_skills=main_caps.get("skills") if main_caps else None,
                    )
                for cb in initializers:
                    try:
                        cb(agent, entry)
                    except Exception as e:
                        logger.warning("agent 初始化回调异常: %s", e)
                return agent

            entry.agent = await loop.run_in_executor(executor, _create)

        agent = entry.agent
        if cancellation_token is not None and cancellation_token.is_cancelled:
            agent.request_stop()

        # ---- / 命令拦截（不经过 LLM） ----
        cmd_reply = await self._handle_gateway_command(
            agent, msg.text, loop, executor, entry)
        if cmd_reply is not None:
            return cmd_reply

        # ---- agent.run() 含 soft/hard 超时 ----
        # 注意：必须保存 afuture 引用，软超时后继续等同一个 future
        # 而不能起第二次 agent.run()——线程池里的旧调用不会因 asyncio 取消而停止
        _images = getattr(msg, 'images', None) or None
        event_sink = None
        publisher = getattr(channel, "publish_agent_event", None)
        if callable(publisher):
            # The sink runs in the Agent executor thread.  WebuiChannel's
            # event bus is explicitly cross-thread safe.
            event_sink = lambda event: publisher(msg, event)
        task_source = (runtime_metadata or {}).get("task_source")
        prior_blocklist = getattr(agent, "_runtime_tool_blocklist", frozenset())
        if task_source == "scheduler":
            # Scheduled prompts can contain wording such as "every day at
            # 11:00". Scope a guard to stop the model from interpreting the
            # execution input as a request to reconfigure the scheduler.
            agent._runtime_tool_blocklist = (
                frozenset(prior_blocklist) | _SCHEDULER_ADMIN_TOOLS)
        task_input = self._task_input_for_agent(msg, runtime_metadata)
        afuture = loop.run_in_executor(
            executor,
            lambda: agent.run(task_input, False, images=_images,
                              event_sink=event_sink),
        )

        def _restore_task_tool_scope(_future=None):
            agent._runtime_tool_blocklist = prior_blocklist

        # A timed-out TaskRuntime coroutine can leave the executor worker alive
        # for quarantine accounting. Keep the scoped guard until that worker
        # actually finishes.
        afuture.add_done_callback(_restore_task_tool_scope)
        if runtime_managed:
            # TaskRuntime owns timeout/quarantine. Shield keeps a worker thread
            # alive for quarantine accounting if this coroutine is cancelled.
            return await asyncio.shield(afuture)

        try:
            result = await asyncio.wait_for(
                asyncio.shield(afuture), timeout=self._soft_timeout,
            )
            return result
        except asyncio.TimeoutError:
            try:
                # 进度提示走 send_progress，避免抢占 future 型通道的最终回复
                await channel.send_progress(msg, "⏳ 还在处理中，请稍候…")
            except Exception:
                pass
            try:
                result = await asyncio.wait_for(
                    afuture, timeout=self._hard_timeout - self._soft_timeout,
                )
                return result
            except asyncio.TimeoutError:
                return f"⏰ 处理超时（>{self._hard_timeout}s），请简化问题后重试"

    async def _handle_gateway_command(
        self, agent, text: str, loop, executor, entry=None
    ) -> Optional[str]:
        """拦截 / 前缀命令，直接处理不经过 LLM。返回回复文本或 None（非命令）。"""
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        command = self._commands.get(cmd)
        if command is None:
            # 非 gateway 命令，交给 agent 处理（可能是 /plan 等需要 LLM 参与的命令）
            return None
        ctx = {"agent": agent, "entry": entry, "loop": loop, "executor": executor}
        return await command["handler"](arg, ctx)

    async def _send_chunked(self, channel: Channel, msg: InboundMessage, text: str):
        """分片发送长文本。平台内部自行分片的 channel 跳过预切。"""
        if channel.handles_chunking:
            await channel.send_reply(msg, text)
        else:
            max_len = 1500
            chunks = split_text(text, max_len)
            for chunk in chunks:
                await channel.send_reply(msg, chunk)
