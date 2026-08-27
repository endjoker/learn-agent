# -*- coding: utf-8 -*-
"""
Scheduler —— 定时任务模块（P1）

设计参考：code/docs/DESIGN-automation-webui.md §4
- 配置：gateway.scheduler（enabled / max_concurrent / misfire_policy / history_limit / jobs[]）
- 触发 = 合成 InboundMessage(channel="scheduler") 走 dispatcher 漏斗
  （复用：去重 / 会话 FIFO / agent 线程池 / soft·hard 超时）
- 回复经 SchedulerChannel.send_reply → 按 job.deliver 投递（announce / webhook / none）
  + 写 state/history；isolated 会话投递完成后即删（文件 + map 条目）防无限增长
- 边界：上轮未跑完 → skipped_overlap；超 max_concurrent → skipped_busy（不排队）；
  停机错过 → misfire_policy（skip | run_once）；失败不重试
- 注：job.timeout 字段当前为参考值，执行时限实际受 gateway.sessions.hard_timeout_seconds 约束
"""

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from croniter import croniter

from core.config_loader import load_config, is_enabled
from gateway.channels.base import Channel, InboundMessage

logger = logging.getLogger("jk_agent.gateway")

_STATE_FILE = Path(__file__).parent / "scheduler_state.json"
_PREVIEW_CHARS = 120  # state/history 中回复预览截断长度
_POLL_CAP = 30.0      # tick 轮询上限（秒），保证 reload/pause 及时生效
# Agent 硬超时兜底默认值：与 server.py sessions 段缺省一致。
_DEFAULT_HARD_TIMEOUT_SECONDS = 1200.0
# 多实例选主（leader_lease，见 docs/multi-instance.md）：
_LEASE_TTL_SECONDS = 60.0   # scheduler 租约 TTL：须大于最大 tick 间隔（30s）
_LEASE_RETRY_SECONDS = 5.0  # 非 leader 时轮询抢占的间隔

# 模块级调度器引用（LLM 工具 / CronTool 通过此获取活动实例）
_scheduler_instance: Optional["Scheduler"] = None
# jobs 配置读改写互斥（CronTool / /api/scheduler 可能并发写 config.json）
_jobs_lock = threading.RLock()


def get_scheduler() -> Optional["Scheduler"]:
    return _scheduler_instance


def acquire_leader_lease(dispatcher, name: str, ttl_seconds: float) -> bool:
    """经 runtime.db 的 leader_lease 表抢占/续租单点执行权（多实例防双跑）。

    - 无共享 DB（runtime_store 为 None）或缺实例身份 → 恒真（保持单实例行为）；
    - 租约异常只记日志并放行（fail-open：不因租约故障阻断 cron/heartbeat）。
    """
    store = getattr(dispatcher, "_runtime_store", None)
    holder = getattr(dispatcher, "instance_id", None)
    if store is None or not holder:
        return True
    try:
        return bool(store.try_acquire_lease(name, holder, ttl_seconds))
    except Exception as exc:
        logger.warning("leader_lease(%s) 获取失败，本轮按持有者处理: %s", name, exc)
        return True


def _write_jobs_to_config(jobs: list):
    """向 config.json gateway.scheduler.jobs 写入 jobs 并 force_reload。

    由 CronTool 和 /api/scheduler 共用。裸读→替换 jobs→备份→原子写→重载。
    """
    from core.config_writer import read_raw_config, write_config as cw_write, backup_file

    with _jobs_lock:
        data, status = read_raw_config()
        if status == "corrupt":
            raise RuntimeError("config.json 损坏，请人工修复")
        data.setdefault("gateway", {}).setdefault("scheduler", {})["jobs"] = jobs
        backup_file()
        cw_write(None, data)
        load_config(force_reload=True)


def add_job(name: str, schedule: str, prompt: str,
            session: str = "isolated",
            deliver_mode: str = "none",
            deliver_channel: str = "",
            deliver_target: str = "",
            timeout: int = 600,
            enabled: bool = True) -> str:
    """添加或更新定时任务（LLM 工具直接调用，同步写入 config + 热加载）。

    返回 ok 或错误信息。
    """
    from croniter import croniter as _cr

    try:
        _cr(schedule)
    except Exception:
        return f"❌ cron 表达式无效: {schedule}"

    if not name or not prompt:
        return "❌ name 和 prompt 不能为空"

    src = get_scheduler()
    with _jobs_lock:
        jobs = list(src.jobs) if src else []

        deliver = {"mode": deliver_mode}
        if deliver_mode == "announce" and deliver_channel and deliver_target:
            deliver["channel"] = deliver_channel
            deliver["target"] = deliver_target
        elif deliver_mode == "webhook" and deliver_target:
            deliver["target"] = deliver_target

        job = {
            "name": name, "schedule": schedule, "prompt": prompt,
            "session": session, "deliver": deliver,
            "timeout": timeout, "enabled": enabled,
        }

        jobs = [j for j in jobs if j.get("name") != name]
        jobs.append(job)

        try:
            _write_jobs_to_config(jobs)
        except Exception as e:
            return f"❌ 写入 config 失败: {e}"

    if src:
        src.reload_config()
    return f"✅ 定时任务已保存: {name}（{schedule}），deliver={deliver_mode}"


def delete_job(name: str) -> str:
    """删除定时任务（LLM 工具直接调用）"""
    src = get_scheduler()
    with _jobs_lock:
        jobs = list(src.jobs) if src else []
        new = [j for j in jobs if j.get("name") != name]
        if len(new) == len(jobs):
            return f"❌ 未找到任务: {name}"

        try:
            _write_jobs_to_config(new)
        except Exception as e:
            return f"❌ 写入 config 失败: {e}"

    if src:
        src.reload_config()
    return f"✅ 已删除定时任务: {name}"


def _log_trigger_future(future) -> None:
    """run_coroutine_threadsafe 返回 future：统一记录未观察到的异常。"""
    try:
        future.result()
    except Exception as e:
        logger.error("手动触发定时任务失败: %s", e, exc_info=True)


def run_job(name: str) -> str:
    """手动触发定时任务（经主事件循环执行，不依赖调用线程的事件循环）。

    server 启动时把主循环引用注入 Scheduler（set_event_loop），
    这里用 run_coroutine_threadsafe 投递，避免在工作线程里
    asyncio.get_event_loop() 拿到错误/不存在的循环。
    """
    src = get_scheduler()
    if src is None:
        return "❌ scheduler 未启动"
    loop = getattr(src, "_loop", None)
    if loop is None or loop.is_closed():
        return "❌ 事件循环不可用（scheduler 未启动或已停止）"
    if not loop.is_running():
        return "❌ 事件循环未运行"
    future = asyncio.run_coroutine_threadsafe(
        src.handle_command(f"run {name}"), loop)
    future.add_done_callback(_log_trigger_future)
    return f"▶ 已触发: {name}"


def _now_dt() -> datetime:
    # 服务器本地时区（设计约定）
    return datetime.now()


async def deliver_reply(dispatcher, deliver: dict, label: str, text: str):
    """按 deliver 配置投递回复文本（scheduler / heartbeat 共用）。

    deliver: {"mode": none|webhook|announce, "channel"?, "target"?}
    label:   日志标识（如 "定时任务 daily-report" / "心跳"）
    """
    deliver = deliver or {}
    mode = deliver.get("mode", "none")

    if mode == "none":
        logger.info("%s deliver=none，回复仅记录", label)
        return

    if mode == "webhook":
        url = deliver.get("target") or deliver.get("url")
        if not url:
            logger.error("%s webhook 投递缺少 target", label)
            return
        _post_webhook(label, url, text)
        return

    if mode == "announce":
        ch_name = deliver.get("channel")
        target = deliver.get("target")
        if not ch_name or not target:
            logger.error("%s announce 投递必须显式指定 channel+target", label)
            return
        channel = dispatcher.channels().get(ch_name)
        if channel is None:
            logger.error("%s 投递通道 %s 未注册", label, ch_name)
            return
        sender = getattr(channel, "send_to_chat", None)
        if sender is None:
            logger.error("通道 %s 不支持主动推送（无 send_to_chat），无法投递 %s",
                         ch_name, label)
            return
        try:
            result = sender(target, text)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error("%s 投递失败: %s", label, e)
        return

    logger.error("%s 未知投递模式: %s", label, mode)


def _post_webhook(label: str, url: str, text: str):
    """守护线程 HTTP POST（复用 webhook_notifier 范式）。

    SSRF 防护：接入 core.safe_http 的 URL 校验——禁私网/链路本地/元数据
    地址，且禁止重定向；非法目标记日志并跳过投递。
    """
    from core.safe_http import UnsafeUrl, request as safe_request, validate_url
    payload = {
        "source": label,
        "at": _now_dt().isoformat(timespec="seconds"),
        "text": text,
    }

    def _post():
        try:
            validate_url(url)
        except UnsafeUrl as e:
            logger.error("%s webhook URL 非法，跳过投递: %s", label, e)
            return
        try:
            # max_redirects=0：重定向响应直接按非法处理，防止跳到内网地址
            safe_request("POST", url, max_redirects=0,
                         json=payload, timeout=10)
        except Exception as e:
            logger.error("%s webhook POST 失败: %s", label, e)

    threading.Thread(target=_post, daemon=True).start()


class Scheduler:
    """定时任务调度器（janitor 范式：start() 内 create_task，stop() 内取消）"""

    def __init__(self, config: dict, dispatcher, session_mgr,
                 *, hard_timeout_seconds: Optional[float] = None):
        self._cfg = config or {}
        self._dispatcher = dispatcher
        self._session_mgr = session_mgr
        # Agent 硬超时（秒）：与 dispatcher 同源——server.py 把 sessions 段的
        # hard_timeout_seconds 注入 agent_config 传给 Dispatcher，这里优先读
        # 同一来源，构造参数仅作显式覆盖（避免再造配置双源）。
        if hard_timeout_seconds is None:
            agent_cfg = getattr(dispatcher, "agent_config", None) or {}
            hard_timeout_seconds = agent_cfg.get("hard_timeout_seconds")
        try:
            self.hard_timeout_seconds = max(
                0.0, float(hard_timeout_seconds)
                if hard_timeout_seconds is not None
                else _DEFAULT_HARD_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            self.hard_timeout_seconds = _DEFAULT_HARD_TIMEOUT_SECONDS
        self._task: Optional[asyncio.Task] = None
        self._next_fire: dict[str, datetime] = {}
        self._running_jobs: set[str] = set()   # 正在运行的 job name（overlap/busy 判定）
        self._pending: dict[str, dict] = {}    # session_key -> {job, trigger, fired_at}
        self._paused: set[str] = set()
        self._state: dict = {"jobs": {}, "paused": [], "history": []}
        self._state_lock = threading.Lock()
        self.channel = SchedulerChannel(self)
        # 无回复 job 看门：session_key -> {job, trigger, fired_at, deadline}
        self._watchdogs: dict[str, dict] = {}
        # 看门已触发（按超时收尾）的 session_key -> {job}：迟到回复到达时
        # 尽力补投递，不再静默丢弃（见 on_reply）。
        self._expired: dict[str, dict] = {}
        # misfire 补跑任务追踪（stop() 统一取消）
        self._misfire_tasks: set[asyncio.Task] = set()
        # 主事件循环引用（run_job 经 run_coroutine_threadsafe 投递）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 多实例选主：上一轮租约持有状态（None=未知），用于领导权变更日志
        self._lease_held: Optional[bool] = None

    def set_event_loop(self, loop) -> None:
        """注入主事件循环（server 启动时保存），供 run_job 跨线程投递。"""
        self._loop = loop

    # ---------- 配置 ----------

    @property
    def jobs(self) -> list:
        return self._cfg.get("jobs") or []

    @property
    def max_concurrent(self) -> int:
        return int(self._cfg.get("max_concurrent", 2))

    @property
    def misfire_policy(self) -> str:
        return self._cfg.get("misfire_policy", "skip")

    @property
    def lease_ttl_seconds(self) -> float:
        """scheduler 选主租约 TTL（gateway.scheduler.lease_ttl_seconds，默认 60s）。

        必须大于最大 tick 间隔（_POLL_CAP=30s），否则存活 leader 在两次续租
        之间被误抢，造成 cron 双跑。"""
        try:
            return max(1.0, float(self._cfg.get("lease_ttl_seconds", _LEASE_TTL_SECONDS)))
        except (TypeError, ValueError):
            return _LEASE_TTL_SECONDS

    def job_by_name(self, name: str) -> Optional[dict]:
        for j in self.jobs:
            if j.get("name") == name:
                return j
        return None

    # ---------- 公开只读快照（供 LLM 工具 / 展示层使用） ----------

    def job_stats(self) -> dict:
        """各任务最近执行统计的只读快照（name -> {runs, failures, ...}）。

        供 cron_list 等工具读取，避免外部直接访问私有 ``_state``。
        """
        with self._state_lock:
            jobs = self._state.get("jobs") or {}
            return {name: dict(stats) if isinstance(stats, dict) else {}
                    for name, stats in jobs.items()}

    def paused_jobs(self) -> set:
        """当前暂停中的任务名集合（只读副本）。"""
        return set(self._paused)

    def _watchdog_deadline(self, job: dict, fired_at: float) -> float:
        """看门时限：max(job.timeout + 60, 硬超时 + 30)。

        - job.timeout + 60：job 自报时限的宽限；
        - hard_timeout + 30：Agent 真实执行上限（sessions.hard_timeout_seconds）
          再加投递余量——job.timeout 配得比硬超时短时，看门不得早于硬超时触发，
          否则会把仍在正常执行的 job 误判为"无回复"而提前收尾。"""
        job_timeout = int(job.get("timeout") or 600)
        return fired_at + max(job_timeout + 60.0,
                              self.hard_timeout_seconds + 30.0)

    def reload_config(self) -> int:
        """热重载 scheduler 配置段（/cron reload）"""
        cfg = load_config(force_reload=True).get("gateway", {}).get("scheduler", {})
        self._cfg = cfg
        self._next_fire.clear()
        return len(self.jobs)

    def _job_active(self, job: dict) -> bool:
        if not isinstance(job, dict):
            return False
        if not is_enabled(job.get("enabled"), True):
            return False
        if not job.get("name") or not job.get("prompt"):
            return False
        if job["name"] in self._paused:
            return False
        return self._valid_schedule(job.get("schedule", ""))

    @staticmethod
    def _valid_schedule(expr: str) -> bool:
        try:
            croniter(expr)
            return True
        except Exception:
            return False

    # ---------- 状态持久化 ----------

    def _load_state(self):
        if _STATE_FILE.exists():
            try:
                with open(_STATE_FILE, "r", encoding="utf-8") as f:
                    self._state = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("scheduler_state.json 读取失败，重置: %s", e)
                self._state = {"jobs": {}, "paused": [], "history": []}

    def _save_state_locked(self):
        """调用方需持有 _state_lock。tmp + os.replace 原子写。"""
        self._state["paused"] = sorted(self._paused)
        try:
            tmp = _STATE_FILE.with_name(_STATE_FILE.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _STATE_FILE)
        except OSError as e:
            logger.error("scheduler_state.json 写入失败: %s", e)

    def _record(self, name: str, trigger: str, status: str,
                duration_s: float, text: str):
        preview = (text or "").strip().replace("\n", " ")[:_PREVIEW_CHARS]
        at = _now_dt().isoformat(timespec="seconds")
        with self._state_lock:
            js = self._state.setdefault("jobs", {}).setdefault(
                name, {"runs": 0, "failures": 0})
            js["last_fire"] = at
            js["last_status"] = status
            js["last_duration_s"] = duration_s
            js["last_reply_preview"] = preview
            if status not in ("skipped_overlap", "skipped_busy"):
                js["runs"] = js.get("runs", 0) + 1
                if status in ("error", "timeout"):
                    js["failures"] = js.get("failures", 0) + 1
            hist = self._state.setdefault("history", [])
            hist.append({"job": name, "at": at, "trigger": trigger,
                         "status": status, "duration_s": duration_s,
                         "preview": preview})
            limit = int(self._cfg.get("history_limit", 50))
            if len(hist) > limit:
                del hist[: len(hist) - limit]
            self._save_state_locked()
        logger.info("⏰ 定时任务 %s 结束: status=%s duration=%ss trigger=%s",
                    name, status, duration_s, trigger)

    # ---------- 生命周期 ----------

    async def start(self):
        global _scheduler_instance
        _scheduler_instance = self
        self._load_state()
        self._paused = set(self._state.get("paused", []))
        for job in self.jobs:
            if not self._valid_schedule(job.get("schedule", "")):
                logger.warning("定时任务 %s cron 表达式无效，已跳过: %r",
                               job.get("name", "?"), job.get("schedule"))
        self._check_misfires()
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("⏰ Scheduler 已启动: %d 个 job (max_concurrent=%d, misfire=%s)",
                    len(self.jobs), self.max_concurrent, self.misfire_policy)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for t in list(self._misfire_tasks):
            t.cancel()
        self._misfire_tasks.clear()
        self._watchdogs.clear()
        self._expired.clear()
        with self._state_lock:
            self._save_state_locked()
        logger.info("⏰ Scheduler 已停止")

    # ---------- misfire ----------

    def _check_misfires(self):
        if self.misfire_policy != "run_once":
            return
        now = _now_dt()
        jobs_state = self._state.get("jobs", {})
        for job in self.jobs:
            if not self._job_active(job):
                continue
            name = job.get("name", "")
            if not name:
                continue  # 畸形 job：无 name，跳过
            last_s = jobs_state.get(name, {}).get("last_fire")
            if not last_s:
                continue  # 从未运行过不算错过
            try:
                last_dt = datetime.fromisoformat(last_s)
            except ValueError:
                continue
            prev = croniter(job["schedule"], now).get_prev(datetime)
            if prev > last_dt:
                logger.info("⏰ 定时任务 %s 停机期间错过触发（计划 %s），"
                            "misfire_policy=run_once → 补跑一次",
                            name, prev.strftime("%m-%d %H:%M"))
                task = asyncio.create_task(
                    self._try_fire(job, trigger="misfire_run"))
                self._misfire_tasks.add(task)
                task.add_done_callback(self._misfire_tasks.discard)

    # ---------- tick 循环 ----------

    async def _tick_loop(self):
        while True:
            try:
                # 多实例选主：每轮先抢占/续租 leader_lease（name='scheduler'），
                # 只有租约持有者执行 tick；租约过期后其他实例可抢占（故障转移）。
                if not acquire_leader_lease(self._dispatcher, "scheduler",
                                            self.lease_ttl_seconds):
                    if self._lease_held is not False:
                        self._lease_held = False
                        logger.info("⏰ scheduler 非 leader（租约被其他实例持有），本轮跳过")
                    await asyncio.sleep(_LEASE_RETRY_SECONDS)
                    continue
                if self._lease_held is not True:
                    self._lease_held = True
                    logger.info("⏰ scheduler 成为 leader（instance_id=%s）",
                                getattr(self._dispatcher, "instance_id", "?"))
                delay = await self._tick_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("scheduler tick 异常: %s", e, exc_info=True)
                delay = _POLL_CAP
            await asyncio.sleep(delay)

    async def _tick_once(self) -> float:
        self._sweep_watchdogs()
        now = _now_dt()
        nearest: Optional[datetime] = None
        for job in self.jobs:
            name = job.get("name", "")
            if not name or not self._job_active(job):
                self._next_fire.pop(name, None)
                continue
            nf = self._next_fire.get(name)
            if nf is None:
                nf = croniter(job["schedule"], now).get_next(datetime)
                self._next_fire[name] = nf
            if now >= nf:
                await self._try_fire(job, trigger="cron")
                nf = croniter(job["schedule"], _now_dt()).get_next(datetime)
                self._next_fire[name] = nf
            nearest = nf if nearest is None else min(nearest, nf)
        if nearest is None:
            return _POLL_CAP
        return max(1.0, min(_POLL_CAP, (nearest - _now_dt()).total_seconds()))

    def _sweep_watchdogs(self) -> None:
        """看门狗：清理超时未收到回复的 job。

        看门时限 = max(job.timeout + 60, 硬超时 + 30)（见 _watchdog_deadline）。
        Agent 硬超时/取消/异常吞掉回复时避免 _running_jobs/_pending 永久泄漏；
        按 error 收尾并清空登记，同时把 job 记入 _expired：此后到达的迟到回复
        仍尽力补投递（见 on_reply），不再静默丢弃。
        """
        now = time.time()
        for key, wd in list(self._watchdogs.items()):
            if now < wd["deadline"]:
                continue
            self._watchdogs.pop(key, None)
            info = self._pending.pop(key, None)
            if info is None:
                continue  # 已正常收尾
            job = info["job"]
            name = job.get("name", "?")
            self._running_jobs.discard(name)
            self._expired[key] = {"job": job}
            duration = round(now - info["fired_at"], 1)
            self._record(name, info["trigger"], "error", duration,
                         "看门狗：job 超时未收到回复")
            if job.get("session", "isolated") == "isolated":
                asyncio.create_task(self._cleanup_isolated(key))

    # ---------- 触发 ----------

    async def _try_fire(self, job: dict, trigger: str) -> bool:
        name = job.get("name", "?")
        if name in self._running_jobs:
            self._record(name, trigger, "skipped_overlap", 0, "上轮未跑完")
            return False
        if len(self._running_jobs) >= self.max_concurrent:
            self._record(name, trigger, "skipped_busy", 0,
                         f"并发已达上限 {self.max_concurrent}")
            return False
        await self._fire(job, trigger)
        return True

    async def _fire(self, job: dict, trigger: str):
        name = job.get("name", "?")
        capability = str(job.get("capability") or "plan").lower()
        if capability not in {"plan", "subagent", "goal"}:
            self._record(name, trigger, "skipped_invalid_capability", 0, capability)
            return
        if capability == "goal" and not bool(job.get("goal_enabled", False)):
            self._record(name, trigger, "skipped_goal_disabled", 0, "goal_enabled=false")
            return
        fired_at = time.time()
        if job.get("session", "isolated") == "persist":
            session_key = f"sched:{name}"
        else:
            session_key = f"sched:{name}:{time.strftime('%Y%m%d-%H%M%S', time.localtime(fired_at))}"
        self._pending[session_key] = {
            "job": job, "trigger": trigger, "fired_at": fired_at,
        }
        self._running_jobs.add(name)
        # 看门狗：job 无回复时按 error 收尾
        # （时限 = max(job.timeout + 60, 硬超时 + 30)，见 _watchdog_deadline）
        self._watchdogs[session_key] = {
            "job": job, "trigger": trigger, "fired_at": fired_at,
            "deadline": self._watchdog_deadline(job, fired_at),
        }
        msg = InboundMessage(
            channel="scheduler",
            session_key=session_key,
            user_id="scheduler",
            user_name="scheduler",
            text=job.get("prompt", ""),
            # Persisted runtime tasks use this scheduled instant as their idempotency key.
            message_id=f"sched-{name}-{trigger}-{time.strftime('%Y%m%d%H%M%S', time.localtime(fired_at))}",
            raw={"job": job, "trigger": trigger, "fired_at": fired_at},
        )
        logger.info("⏰ 定时任务触发: %s (trigger=%s, session=%s)",
                    name, trigger, session_key)
        await self._dispatcher.on_inbound(msg)

    def restore_runtime_context(self, msg: InboundMessage, envelope=None) -> None:
        """Restore the in-memory completion/delivery state after a restart."""
        context = {}
        if envelope is not None:
            context = (getattr(envelope, "metadata", {}) or {}).get("channel_context") or {}
        job_data = context.get("job") if isinstance(context, dict) else None
        job_name = job_data.get("name") if isinstance(job_data, dict) else None
        if not job_name:
            parts = msg.session_key.split(":", 2)
            job_name = parts[1] if len(parts) >= 2 else ""
        job = self.job_by_name(job_name) if job_name else None
        if job is None and isinstance(job_data, dict):
            job = job_data
        if job is None:
            raise RuntimeError(f"scheduled job is unavailable: {job_name or msg.session_key}")

        trigger = context.get("trigger") if isinstance(context, dict) else None
        if not trigger:
            trigger = "recovered"
        fired_at = context.get("fired_at") if isinstance(context, dict) else None
        try:
            fired_at = float(fired_at)
        except (TypeError, ValueError):
            fired_at = 0.0
        # 健全性校验：恢复数据里的 fired_at 必须落在合理区间。实测曾出现
        # last_duration_s≈5.6e9（约 56.7 年）的脏数据——异常时间戳会让看门
        # deadline/时长统计完全失真，这里直接归零按"未知触发时刻"处理。
        now = time.time()
        if not (0.0 < fired_at <= now):
            logger.warning(
                "调度恢复上下文的 fired_at 非法（%r），按当前时刻处理: %s",
                context.get("fired_at") if isinstance(context, dict) else None,
                msg.session_key)
            fired_at = now
        self._pending[msg.session_key] = {
            "job": job, "trigger": trigger, "fired_at": fired_at,
        }
        self._running_jobs.add(job.get("name", job_name))
        self._watchdogs[msg.session_key] = {
            "job": job, "trigger": trigger, "fired_at": fired_at,
            "deadline": self._watchdog_deadline(job, fired_at),
        }
        logger.info("Restored scheduler context: %s", msg.session_key)

    # ---------- 回复处理（SchedulerChannel 调用） ----------

    async def on_reply(self, msg: InboundMessage, text: str):
        key = msg.session_key
        info = self._pending.pop(key, None)
        self._watchdogs.pop(key, None)
        if info is None:
            expired = self._expired.pop(key, None)
            if expired is not None:
                # 迟到回复：看门已按超时收尾，但回复最终到达。记 warning 并
                # 尽力按 job.deliver 补投递（announce/webhook），不再静默丢弃。
                # 投递失败只记日志——登记已清、看门已收尾，无需重试路径。
                job = expired.get("job") or {}
                logger.warning(
                    "⏰ 定时任务 %s 的回复在看门收尾后到达（迟到），仍尽力投递",
                    job.get("name", "?"))
                try:
                    await self._deliver(job, text)
                except Exception as e:
                    logger.warning("定时任务迟到回复补投递失败: %s", e,
                                   exc_info=True)
            else:
                logger.debug("scheduler 收到未登记的会话回复: %s", key)
            return
        job = info["job"]
        name = job.get("name", "?")
        self._running_jobs.discard(name)
        duration = round(time.time() - info["fired_at"], 1)
        status = self._classify(text)
        self._record(name, info["trigger"], status, duration, text)
        await self._deliver(job, text)
        if job.get("session", "isolated") == "isolated":
            asyncio.create_task(self._cleanup_isolated(msg.session_key))

    @staticmethod
    def _classify(text: str) -> str:
        t = (text or "").strip()
        if t.startswith("❌"):
            return "error"
        if t.startswith("⏰ 处理超时"):
            return "timeout"
        return "ok"

    # ---------- 投递 ----------

    async def _deliver(self, job: dict, text: str):
        deliver = job.get("deliver") or {}
        name = job.get("name", "?")
        body = text
        if deliver.get("mode") == "announce":
            body = f"⏰ [定时任务 {name}]\n{text}"
        await deliver_reply(self._dispatcher, deliver,
                            f"定时任务 {name}", body)

    # ---------- isolated 会话清理 ----------

    async def _cleanup_isolated(self, session_key: str):
        """isolated 会话收尾：仅驱逐内存实例。

        注意归属：持久化侧的 conversation 数据由 WebUI retention loop
        （service.cleanup → delete_stale_system_conversations，覆盖
        sched:/heartbeat:/system: 键）按 7 天保留期清理；runtime.db 任务域
        sessions 表由 RuntimeStore.delete_stale_sessions（RetentionManager）
        清理。本函数不负责任何持久化删除；sessions_map 条目自一次性会话键
        停写后不再产生（agent_factory._is_ephemeral_session_key）。"""
        await asyncio.sleep(2)  # 等 worker 收尾（is_busy 复位等）
        try:
            await self._session_mgr.evict(session_key, save=True)
            logger.debug("isolated 定时会话 archived_pending: %s", session_key)
        except Exception as e:
            logger.warning("isolated 定时会话清理失败 %s: %s", session_key, e)

    # ---------- /cron 命令 ----------

    async def handle_command(self, arg: str, ctx: dict = None) -> str:
        parts = arg.split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub in ("", "list"):
            return self._cmd_list()
        if sub == "run":
            return await self._cmd_run(rest)
        if sub == "pause":
            return self._cmd_pause(rest, pause=True)
        if sub == "resume":
            return self._cmd_pause(rest, pause=False)
        if sub == "history":
            return self._cmd_history(rest)
        if sub == "reload":
            return self._cmd_reload()
        return ("用法: /cron list | run <name> | pause <name> | "
                "resume <name> | history [name] | reload")

    def _cmd_list(self) -> str:
        if not self.jobs:
            return "⏰ 当前无定时任务（config gateway.scheduler.jobs 为空）"
        lines = ["⏰ 定时任务列表:"]
        for job in self.jobs:
            if not isinstance(job, dict):
                continue
            name = job.get("name", "?")
            if self._job_active(job):
                mark = "✅"
            elif name in self._paused:
                mark = "⏸"
            else:
                mark = "⬜"
            stats = self._state.get("jobs", {}).get(name, {})
            last = stats.get("last_status", "-")
            nf = self._next_fire.get(name)
            nf_s = nf.strftime("%m-%d %H:%M") if (nf and mark == "✅") else "-"
            running = " 🏃" if name in self._running_jobs else ""
            lines.append(f"  {mark} {name}  [{job.get('schedule', '?')}]  "
                         f"last:{last}  next:{nf_s}{running}")
        return "\n".join(lines)

    async def _cmd_run(self, name: str) -> str:
        job = self.job_by_name(name)
        if not job:
            return f"❌ 未找到定时任务: {name}"
        if name in self._running_jobs:
            return f"⚠️ {name} 正在运行中，跳过"
        if len(self._running_jobs) >= self.max_concurrent:
            return f"⚠️ 并发已达上限 {self.max_concurrent}，请稍后再试"
        await self._fire(job, trigger="manual")
        return f"▶ 已手动触发 {name}，结果将按 deliver 配置投递"

    def _cmd_pause(self, name: str, pause: bool) -> str:
        job = self.job_by_name(name)
        if not job:
            return f"❌ 未找到定时任务: {name}"
        if pause:
            self._paused.add(name)
        else:
            self._paused.discard(name)
        with self._state_lock:
            self._save_state_locked()
        self._next_fire.pop(name, None)
        return f"{'⏸ 已暂停' if pause else '▶ 已恢复'}定时任务: {name}"

    def _cmd_history(self, name: str) -> str:
        with self._state_lock:
            hist = list(self._state.get("history", []))
        if name:
            hist = [h for h in hist if h.get("job") == name]
        hist = hist[-10:]
        if not hist:
            suffix = f"（{name}）" if name else ""
            return f"⏰ 暂无执行历史{suffix}"
        lines = ["⏰ 最近执行历史（最新 10 条）:"]
        for h in hist:
            lines.append(f"  [{h.get('at', '?')}] {h.get('job', '?')}  "
                         f"{h.get('status', '?')}  {h.get('duration_s', '-')}s  "
                         f"({h.get('trigger', '?')})")
        return "\n".join(lines)

    def _cmd_reload(self) -> str:
        n = self.reload_config()
        self._paused &= {j.get("name") for j in self.jobs}
        return f"🔄 已重载 scheduler 配置，共 {n} 个 job"


class SchedulerChannel(Channel):
    """定时任务的回复通道：投递 + 状态/历史 + isolated 清理"""

    name = "scheduler"
    handles_chunking = True  # 回复整段处理，不分片

    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_reply(self, msg: InboundMessage, text: str) -> None:
        await self.scheduler.on_reply(msg, text)

    def restore_runtime_context(self, msg: InboundMessage, envelope=None) -> None:
        self.scheduler.restore_runtime_context(msg, envelope)

    async def send_progress(self, msg: InboundMessage, text: str) -> None:
        # 软超时等进度提示不是最终回复，仅记日志
        logger.debug("定时任务进行中 [%s]: %s", msg.session_key, text)

    def status(self) -> dict:
        return {
            "name": self.name,
            "status": "running",
            "jobs": len(self.scheduler.jobs),
            "running": sorted(self.scheduler._running_jobs),
        }
