# -*- coding: utf-8 -*-
"""
Heartbeat —— 心跳检查模块（P2）

设计参考：code/docs/DESIGN-automation-webui.md §5
- 每 `every`（默认 30m）在固定会话（session_key，默认 heartbeat:main）跑一轮：
  读 HEARTBEAT.md 清单注入为 prompt，agent 自主决定是否行动
- 静默抑制：回复含 HEARTBEAT_OK → 只记日志不投递
- 防堆积：心跳会话 busy / 超出 active_hours / 有 cron 在跑 → 跳过而非排队
"""

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from gateway.channels.base import Channel, InboundMessage
from gateway.scheduler import acquire_leader_lease, deliver_reply, _now_dt

logger = logging.getLogger("jk_agent.gateway")

_OK_MARKER = "HEARTBEAT_OK"
_PREVIEW_CHARS = 120
# 清单之外的固定指令（即使模板被编辑也保证静默抑制语义）
_SUFFIX = ("\n\n【系统指令】评估以上清单：若无需行动，仅回复 HEARTBEAT_OK"
           "（不要包含任何其他内容）；否则执行必要的行动并简要汇报结果。")


def _parse_duration(value, default_seconds: int = 1800) -> int:
    """解析 '30m' / '2h' / '90s' / 纯数字（秒）"""
    s = str(value or "").strip().lower()
    if not s:
        return default_seconds
    try:
        if s.endswith("ms"):
            return max(1, int(float(s[:-2]) / 1000))
        if s.endswith("s"):
            return max(1, int(float(s[:-1])))
        if s.endswith("m"):
            return max(1, int(float(s[:-1]) * 60))
        if s.endswith("h"):
            return max(1, int(float(s[:-1]) * 3600))
        return max(1, int(float(s)))
    except ValueError:
        logger.warning("heartbeat every 无法解析 %r，使用默认 %ds",
                       value, default_seconds)
        return default_seconds


def _parse_active_hours(value):
    """'08:00-22:00' → ((8,0),(22,0))；空/无效/always → None（不限制）"""
    s = str(value or "").strip()
    if not s or s.lower() in ("always", "all"):
        return None
    try:
        a, b = s.split("-", 1)
        ah, am = a.strip().split(":", 1)
        bh, bm = b.strip().split(":", 1)
        return ((int(ah), int(am)), (int(bh), int(bm)))
    except ValueError:
        logger.warning("heartbeat active_hours 无法解析 %r，视为不限制", value)
        return None


def _in_active_hours(hours, now: datetime) -> bool:
    if hours is None:
        return True
    (ah, am), (bh, bm) = hours
    cur = now.hour * 60 + now.minute
    start, end = ah * 60 + am, bh * 60 + bm
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # 跨夜时段


def _resolve_prompt_file(path_str: str) -> Path:
    """相对路径锚定项目根目录解析。

    create_agent() 会 os.chdir(workspace)（agent.py:1569），agent 创建后
    进程 CWD 不可靠——与 skills/manager.py、memory/manager.py 同款处理。
    """
    p = Path(path_str)
    if p.is_absolute():
        return p
    from core.config_loader import _find_project_root
    return _find_project_root() / p


class Heartbeat:
    """Agent 心跳 + 系统健康探针（janitor 范式：start 创建 / stop 取消）"""

    def __init__(self, config: dict, dispatcher, session_mgr, scheduler=None):
        self._cfg = config or {}
        self._dispatcher = dispatcher
        self._session_mgr = session_mgr
        self._scheduler = scheduler
        self._task: Optional[asyncio.Task] = None
        self._paused = False
        self._beats = 0
        self._skips = 0
        # 连续 busy skip 计数（无声卡死自愈用）：正常心跳一轮或观测到会话
        # 不再忙碌即归零；达到 stuck_skip_limit 触发 error 告警并尝试驱逐。
        self._busy_skips = 0
        self._last_beat: Optional[str] = None
        self._last_result: Optional[str] = None  # ok | acted | error
        self.channel = HeartbeatChannel(self)

    # ---------- 配置 ----------

    @property
    def every_seconds(self) -> int:
        return _parse_duration(self._cfg.get("every", "30m"))

    @property
    def session_key(self) -> str:
        return self._cfg.get("session_key", "heartbeat:main")

    @property
    def prompt_file(self) -> str:
        return self._cfg.get("prompt_file", "workspace/HEARTBEAT.md")

    @property
    def defer_when_busy(self) -> bool:
        return bool(self._cfg.get("defer_when_busy", True))

    @property
    def lease_ttl_seconds(self) -> float:
        """heartbeat 选主租约 TTL（gateway.heartbeat.lease_ttl_seconds）。

        默认取 2×心跳间隔（覆盖 leader 两轮心跳之间的睡眠期，防存活 leader
        被误抢造成重复心跳）；显式配置时优先。"""
        explicit = self._cfg.get("lease_ttl_seconds")
        if explicit:
            try:
                return max(1.0, float(explicit))
            except (TypeError, ValueError):
                pass
        return max(120.0, self.every_seconds * 2.0)

    @property
    def lease_poll_seconds(self) -> float:
        """非 leader 时轮询抢占间隔（默认 30s，心跳间隔过小时收紧）。"""
        return max(5.0, min(30.0, self.every_seconds / 2.0))

    @property
    def idle_skip_minutes(self) -> int:
        return int(self._cfg.get("idle_skip_minutes", 30))

    @property
    def stuck_skip_limit(self) -> int:
        """连续 busy skip 的自愈阈值（gateway.heartbeat.stuck_skip_limit）。

        解析失败回退默认 3；下限 1。"""
        try:
            return max(1, int(self._cfg.get("stuck_skip_limit", 3)))
        except (TypeError, ValueError):
            return 3

    def _idle_skip(self) -> bool:
        """最近 idle_skip_minutes 内无用户会话活动 → True（不心跳）"""
        import time as _t
        cutoff = _t.time() - self.idle_skip_minutes * 60
        last = 0.0
        for e in self._session_mgr.list_entries():
            key = e.get("session_key", "")
            if key.startswith("heartbeat:") or key.startswith("sched:"):
                continue
            last = max(last, e.get("last_active", 0.0))
        return last < cutoff

    # ---------- 生命周期 ----------

    async def start(self):
        self._task = asyncio.create_task(self._beat_loop())
        logger.info("❤️ Heartbeat 已启动: every=%s session=%s active_hours=%s",
                    self._cfg.get("every", "30m"), self.session_key,
                    self._cfg.get("active_hours", "always"))

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("❤️ Heartbeat 已停止")

    # ---------- 心跳循环 ----------

    async def _beat_loop(self):
        while True:
            try:
                # 多实例选主：先抢占/续租 leader_lease（name='heartbeat'），
                # 只有租约持有者按 every 节奏心跳；租约过期后其他实例可抢占。
                if not acquire_leader_lease(self._dispatcher, "heartbeat",
                                            self.lease_ttl_seconds):
                    await asyncio.sleep(self.lease_poll_seconds)
                    continue
                await asyncio.sleep(self.every_seconds)
                await self._maybe_beat()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("heartbeat 循环异常: %s", e, exc_info=True)

    async def _maybe_beat(self, ignore_idle: bool = False) -> bool:
        if self._paused:
            self._skips += 1
            return False
        now = _now_dt()
        if not _in_active_hours(
                _parse_active_hours(self._cfg.get("active_hours")), now):
            self._skips += 1
            logger.debug("heartbeat 超出 active_hours，跳过")
            return False
        # 无用户活跃会话则不心跳（#6）：最近 N 分钟内无人发起会话即跳过
        if not ignore_idle and self._idle_skip():
            self._skips += 1
            logger.debug("heartbeat 近 %s 分钟无用户会话，跳过",
                         self.idle_skip_minutes)
            return False
        if self.defer_when_busy:
            if self._session_mgr.is_busy(self.session_key):
                self._skips += 1
                self._busy_skips += 1
                if self._busy_skips >= self.stuck_skip_limit:
                    # 无声死亡防护：连续多轮 busy 说明会话可能永久卡在
                    # busy（zombie 复位全靠 dispatcher 超时兜底，存在漏网），
                    # 升级告警并尝试驱逐该会话 entry 强制恢复。
                    logger.error(
                        "heartbeat 会话 %s 连续 %d 轮忙碌跳过（阈值 %d），"
                        "疑似卡死，尝试强制恢复", self.session_key,
                        self._busy_skips, self.stuck_skip_limit)
                    await self._recover_stuck_session()
                else:
                    logger.info("heartbeat 会话忙碌，跳过本轮")
                return False
            # 观测到会话不再忙碌 → 连续忙碌计数归零
            self._busy_skips = 0
            if (self._scheduler is not None
                    and self._scheduler._running_jobs):
                self._skips += 1
                logger.info("有定时任务在跑，heartbeat 让位")
                return False
            # Durable Goal/Subagent tasks may run in child sessions, outside
            # the heartbeat SessionEntry. Inspect TaskRuntime state centrally.
            runtime_store = getattr(self._dispatcher, "_runtime_store", None)
            if runtime_store is not None:
                from core.runtime import TaskStatus
                active = runtime_store.list_tasks(statuses={
                    TaskStatus.QUEUED, TaskStatus.LEASED, TaskStatus.RUNNING,
                    TaskStatus.WAITING_APPROVAL, TaskStatus.WAITING_DEPENDENCY,
                })
                if any(item.envelope.source in {"goal", "subagent"} for item in active):
                    self._skips += 1
                    logger.info("有 Goal/Subagent 在跑，heartbeat 让位")
                    return False
        text = self._build_prompt()
        if text is None:
            self._skips += 1
            logger.warning("heartbeat 清单 %s 不存在或为空，跳过",
                           self.prompt_file)
            return False

        self._beats += 1
        # 心跳正常运行 → 连续忙碌计数归零（正常一轮即视为脱离卡死状态）
        self._busy_skips = 0
        self._last_beat = now.isoformat(timespec="seconds")
        msg = InboundMessage(
            channel="heartbeat",
            session_key=self.session_key,
            user_id="heartbeat",
            user_name="heartbeat",
            text=text,
            # One durable key per heartbeat period enables RuntimeStore
            # replay/idempotency. 秒级时间戳避免同分钟两次心跳（如手动触发）
            # 因 message_id 相同被去重吞掉。
            message_id=f"hb-{self.session_key}-{now.strftime('%Y%m%d%H%M%S')}",
        )
        logger.info("❤️ 心跳触发 (#%d, session=%s)",
                    self._beats, self.session_key)
        await self._dispatcher.on_inbound(msg)
        return True

    async def _recover_stuck_session(self) -> bool:
        """强制恢复疑似卡死的心跳会话：驱逐内存 entry（保存后重建）。

        句柄来自 session_mgr.evict（SessionManager 标准入口，与
        scheduler isolated 会话清理同款，save=True 保留历史）。驱逐成功后
        复位连续忙碌计数；无句柄/驱逐失败时保持计数——下一轮达到阈值会
        继续升级告警，不静默吞掉。
        注意 force=True：目标 entry 正处于 is_busy（这正是触发本恢复的
        条件），普通 evict 会被 busy 防护拒绝。
        """
        evict = getattr(self._session_mgr, "evict", None)
        if not callable(evict):
            logger.error(
                "heartbeat 会话 %s 无法强制恢复：session_mgr 无 evict 句柄"
                "（%s），仅升级告警", self.session_key,
                type(self._session_mgr).__name__)
            return False
        try:
            try:
                evicted = bool(await evict(self.session_key, save=True,
                                           force=True))
            except TypeError:
                # 兼容无 force 参数的旧签名/测试桩
                evicted = bool(await evict(self.session_key, save=True))
        except Exception as e:
            logger.error("heartbeat 强制恢复会话 %s 失败: %s",
                         self.session_key, e)
            return False
        if evicted:
            self._busy_skips = 0
            logger.warning(
                "heartbeat 已强制驱逐卡死会话 %s（entry 将在下次投递时重建）",
                self.session_key)
        else:
            logger.warning(
                "heartbeat 强制恢复：会话 %s 无活跃 entry 可驱逐",
                self.session_key)
        return evicted

    def _build_prompt(self) -> Optional[str]:
        try:
            content = _resolve_prompt_file(self.prompt_file).read_text(
                encoding="utf-8").strip()
        except OSError:
            return None
        if not content:
            return None
        return content + _SUFFIX

    async def force_beat(self) -> bool:
        """/heartbeat run 手动触发（跳过空闲限制，仍尊重 busy/active_hours）"""
        return await self._maybe_beat(ignore_idle=True)

    # ---------- 回复处理（HeartbeatChannel 调用） ----------

    async def on_reply(self, msg: InboundMessage, text: str):
        t = (text or "").strip()
        # 精确尾部匹配：仅当回复以 HEARTBEAT_OK 结尾（允许尾部空白）才静默
        # 抑制；"HEARTBEAT_OK 但有问题" 之类内容不再被误吞。
        if t.rstrip().endswith(_OK_MARKER):
            self._last_result = "ok"
            logger.info("❤️ 心跳 OK（无需行动），不投递")
            return
        if t.startswith("❌") or t.startswith("⏰ 处理超时"):
            self._last_result = "error"
            logger.warning("❤️ 心跳执行异常: %s", t[:_PREVIEW_CHARS])
            return
        self._last_result = "acted"
        logger.info("❤️ 心跳行动: %s", t[:_PREVIEW_CHARS])
        deliver = self._cfg.get("deliver") or {}
        if deliver.get("mode", "none") != "none":
            await deliver_reply(self._dispatcher, deliver, "心跳",
                                f"❤️ [心跳]\n{t}")

    # ---------- /heartbeat 命令 ----------

    async def handle_command(self, arg: str, ctx: dict = None) -> str:
        sub = (arg or "").strip().lower()
        if sub in ("", "status"):
            return self._cmd_status()
        if sub == "pause":
            self._paused = True
            return "⏸ 心跳已暂停"
        if sub == "resume":
            self._paused = False
            return "▶ 心跳已恢复"
        if sub == "run":
            ok = await self.force_beat()
            return ("❤️ 已触发一轮心跳" if ok
                    else "⚠️ 心跳被跳过（暂停 / 忙碌 / 超出时段 / 清单缺失）")
        return "用法: /heartbeat status | pause | resume | run"

    def _cmd_status(self) -> str:
        st = "⏸ 已暂停" if self._paused else "▶ 运行中"
        pf = _resolve_prompt_file(self.prompt_file)
        return (
            f"❤️ 心跳状态: {st}\n"
            f"  间隔: {self._cfg.get('every', '30m')}"
            f"（{self.every_seconds}s）\n"
            f"  会话: {self.session_key}\n"
            f"  活跃时段: {self._cfg.get('active_hours', 'always')}\n"
            f"  清单文件: {pf}"
            f"{' ✅' if pf.exists() else ' ❌ 不存在'}\n"
            f"  已触发: {self._beats} 轮 / 跳过: {self._skips} 次\n"
            f"  上次心跳: {self._last_beat or '-'}"
            f"  结果: {self._last_result or '-'}"
        )


class HeartbeatChannel(Channel):
    """心跳回复通道：静默抑制 + 按需投递"""

    name = "heartbeat"
    handles_chunking = True

    def __init__(self, heartbeat: Heartbeat):
        self.heartbeat = heartbeat

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_reply(self, msg: InboundMessage, text: str) -> None:
        await self.heartbeat.on_reply(msg, text)

    async def send_progress(self, msg: InboundMessage, text: str) -> None:
        logger.debug("心跳进行中 [%s]: %s", msg.session_key, text)

    def status(self) -> dict:
        hb = self.heartbeat
        return {
            "name": self.name,
            "status": "running",
            "paused": hb._paused,
            "every": hb._cfg.get("every", "30m"),
            "session_key": hb.session_key,
            "beats": hb._beats,
            "skips": hb._skips,
            "last_beat": hb._last_beat,
            "last_result": hb._last_result,
        }
