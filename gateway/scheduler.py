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

logger = logging.getLogger("hello_agent.gateway")

_STATE_FILE = Path(__file__).parent / "scheduler_state.json"
_PREVIEW_CHARS = 120  # state/history 中回复预览截断长度
_POLL_CAP = 30.0      # tick 轮询上限（秒），保证 reload/pause 及时生效

# 模块级调度器引用（LLM 工具 / CronTool 通过此获取活动实例）
_scheduler_instance: Optional["Scheduler"] = None


def get_scheduler() -> Optional["Scheduler"]:
    return _scheduler_instance


def _write_jobs_to_config(jobs: list):
    """向 config.json gateway.scheduler.jobs 写入 jobs 并 force_reload。

    由 CronTool 和 /api/scheduler 共用。裸读→替换 jobs→备份→原子写→重载。
    """
    from core.config_writer import read_raw_config, write_config as cw_write, backup_file

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


def get_job(name: str) -> Optional[dict]:
    """查看单个任务"""
    src = get_scheduler()
    if src is None:
        return None
    return src.job_by_name(name)


def run_job(name: str) -> str:
    """手动触发定时任务"""
    src = get_scheduler()
    if src is None:
        return "❌ scheduler 未启动"
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return "❌ 无事件循环，无法触发"
    if loop.is_running():
        asyncio.create_task(src.handle_command(f"run {name}"))
        return f"▶ 已触发: {name}"
    else:
        return "❌ 事件循环未运行"


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
    """守护线程 HTTP POST（复用 webhook_notifier 范式）"""
    import requests
    payload = {
        "source": label,
        "at": _now_dt().isoformat(timespec="seconds"),
        "text": text,
    }

    def _post():
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error("%s webhook POST 失败: %s", label, e)

    threading.Thread(target=_post, daemon=True).start()


class Scheduler:
    """定时任务调度器（janitor 范式：start() 内 create_task，stop() 内取消）"""

    def __init__(self, config: dict, dispatcher, session_mgr):
        self._cfg = config or {}
        self._dispatcher = dispatcher
        self._session_mgr = session_mgr
        self._task: Optional[asyncio.Task] = None
        self._next_fire: dict[str, datetime] = {}
        self._running_jobs: set[str] = set()   # 正在运行的 job name（overlap/busy 判定）
        self._pending: dict[str, dict] = {}    # session_key -> {job, trigger, fired_at}
        self._paused: set[str] = set()
        self._state: dict = {"jobs": {}, "paused": [], "history": []}
        self._state_lock = threading.Lock()
        self.channel = SchedulerChannel(self)

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

    def job_by_name(self, name: str) -> Optional[dict]:
        for j in self.jobs:
            if j.get("name") == name:
                return j
        return None

    def reload_config(self) -> int:
        """热重载 scheduler 配置段（/cron reload）"""
        cfg = load_config(force_reload=True).get("gateway", {}).get("scheduler", {})
        self._cfg = cfg
        self._next_fire.clear()
        return len(self.jobs)

    def _job_active(self, job: dict) -> bool:
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
            name = job["name"]
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
                asyncio.create_task(self._try_fire(job, trigger="misfire_run"))

    # ---------- tick 循环 ----------

    async def _tick_loop(self):
        while True:
            try:
                delay = await self._tick_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("scheduler tick 异常: %s", e, exc_info=True)
                delay = _POLL_CAP
            await asyncio.sleep(delay)

    async def _tick_once(self) -> float:
        now = _now_dt()
        nearest: Optional[datetime] = None
        for job in self.jobs:
            name = job["name"]
            if not self._job_active(job):
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
        fired_at = time.time()
        if job.get("session", "isolated") == "persist":
            session_key = f"sched:{name}"
        else:
            session_key = f"sched:{name}:{time.strftime('%Y%m%d-%H%M%S', time.localtime(fired_at))}"
        self._pending[session_key] = {
            "job": job, "trigger": trigger, "fired_at": fired_at,
        }
        self._running_jobs.add(name)
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
            fired_at = time.time()
        self._pending[msg.session_key] = {
            "job": job, "trigger": trigger, "fired_at": fired_at,
        }
        self._running_jobs.add(job.get("name", job_name))
        logger.info("Restored scheduler context: %s", msg.session_key)

    # ---------- 回复处理（SchedulerChannel 调用） ----------

    async def on_reply(self, msg: InboundMessage, text: str):
        info = self._pending.pop(msg.session_key, None)
        if info is None:
            logger.debug("scheduler 收到未登记的会话回复: %s", msg.session_key)
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
        """isolated 投递完成后删除会话文件与 map 条目（防无限增长）"""
        await asyncio.sleep(2)  # 等 worker 收尾（is_busy 复位等）
        try:
            entry = self._session_mgr._sessions.get(session_key)
            sid = None
            if entry is not None and entry.agent is not None:
                sid = getattr(entry.agent.store, "session_id", None)
            await self._session_mgr.evict(session_key, save=False)
            from gateway.agent_factory import remove_map_entry
            remove_map_entry(session_key)
            if sid:
                from core.message_store import MessageStore
                MessageStore.delete_session_file(sid)
            logger.debug("isolated 定时会话已清理: %s", session_key)
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
