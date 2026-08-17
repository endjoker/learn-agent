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
from gateway.scheduler import deliver_reply, _now_dt

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
    def idle_skip_minutes(self) -> int:
        return int(self._cfg.get("idle_skip_minutes", 30))

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
                logger.info("heartbeat 会话忙碌，跳过本轮")
                return False
            if (self._scheduler is not None
                    and self._scheduler._running_jobs):
                self._skips += 1
                logger.info("有定时任务在跑，heartbeat 让位")
                return False
        text = self._build_prompt()
        if text is None:
            self._skips += 1
            logger.warning("heartbeat 清单 %s 不存在或为空，跳过",
                           self.prompt_file)
            return False

        self._beats += 1
        self._last_beat = now.isoformat(timespec="seconds")
        msg = InboundMessage(
            channel="heartbeat",
            session_key=self.session_key,
            user_id="heartbeat",
            user_name="heartbeat",
            text=text,
            # One durable key per heartbeat period enables RuntimeStore replay/idempotency.
            message_id=f"hb-{self.session_key}-{now.strftime('%Y%m%d%H%M')}",
        )
        logger.info("❤️ 心跳触发 (#%d, session=%s)",
                    self._beats, self.session_key)
        await self._dispatcher.on_inbound(msg)
        return True

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
        if _OK_MARKER in t:
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
