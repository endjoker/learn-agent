# -*- coding: utf-8 -*-
"""
CronTool —— 让 LLM 创建/管理/触发定时任务

注册到 Agent 的 ToolRegistry 后，LLM 可在对话中调用以下操作：
  - cron_add_job：创建或更新定时任务
  - cron_delete_job：删除定时任务
  - cron_list_jobs：列出所有任务
  - cron_run_job：手动触发

每项返回文本结果直接写入 LLM 上下文，不经过漏斗（工具本身即生效）。
"""

from tools import BaseTool
from gateway.scheduler import add_job, delete_job, get_scheduler, run_job

_CHANNEL_HINT = (
    "deliver_channel 可用值：feishu（需同时传 deliver_target=chat_id，"
    "形如 oc_xxxxxxxx）；deliver_mode=webhook 时 deliver_target 填 URL。"
)


class CronAddJobTool(BaseTool):
    name = "cron_add_job"
    description = (
        "创建或更新定时任务（heal-agent 的 cron）。"
        "当用户需要定期执行某个操作时（日报/巡检/数据同步等），使用此工具。"
        "传入 name/schedule/prompt；可指定投递方式（announce 飞书/webhook/仅日志）。"
        f" {_CHANNEL_HINT}"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "任务唯一名称，如 'daily-report'、'health-check'",
            },
            "schedule": {
                "type": "string",
                "description": "标准 5 段 cron 表达式（服务器本地时区），如 '0 9 * * 1-5'（工作日早9点）、'*/30 * * * *'（每30分钟）",
            },
            "prompt": {
                "type": "string",
                "description": "任务提示词，与直接对话的 prompt 一样——描述要做什么、产什么结果",
            },
            "session": {
                "type": "string",
                "enum": ["isolated", "persist"],
                "description": "isolated=每次全新上下文（推荐）；persist=固定会话跨次累积",
            },
            "deliver_mode": {
                "type": "string",
                "enum": ["none", "announce", "webhook"],
                "description": "none=仅日志；announce=推送到第三方通道（飞书/微信）；webhook=HTTP POST",
            },
            "deliver_channel": {
                "type": "string",
                "description": "announce 时的目标通道，如 'feishu'",
            },
            "deliver_target": {
                "type": "string",
                "description": "announce 的 chat_id（oc_ 开头）或 webhook 的 URL",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数（默认 600）",
            },
        },
        "required": ["name", "schedule", "prompt"],
    }

    def execute(self, **kwargs) -> str:
        # timeout 参数 try/int 包裹，非法值回退默认 600 秒并钳制到 [1, 3600]
        try:
            timeout = max(1, min(int(kwargs.get("timeout") or 600), 3600))
        except (TypeError, ValueError):
            timeout = 600

        deliver_mode = kwargs.get("deliver_mode", "none") or "none"
        deliver_target = kwargs.get("deliver_target", "") or ""

        # webhook 投递目标 URL 经 safe_http 校验（SSRF 防护）
        if deliver_mode == "webhook" and deliver_target:
            from core.safe_http import validate_url
            try:
                validate_url(deliver_target)
            except Exception as e:
                return f"❌ webhook 目标 URL 无效，已拒绝创建任务: {e}"

        return add_job(
            name=kwargs.get("name", ""),
            schedule=kwargs.get("schedule", ""),
            prompt=kwargs.get("prompt", ""),
            session=kwargs.get("session", "isolated"),
            deliver_mode=deliver_mode,
            deliver_channel=kwargs.get("deliver_channel", ""),
            deliver_target=deliver_target,
            timeout=timeout,
            enabled=True,
        )


class CronDeleteJobTool(BaseTool):
    name = "cron_delete_job"
    description = "删除一个定时任务（heal-agent 的 cron）。"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "要删除的任务名称"},
        },
        "required": ["name"],
    }

    def execute(self, **kwargs) -> str:
        return delete_job(kwargs.get("name", ""))


class CronListJobsTool(BaseTool):
    name = "cron_list_jobs"
    description = "列出所有定时任务及最近执行状态。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs) -> str:
        src = get_scheduler()
        if src is None:
            return "⚠️ 调度器未启动"
        jobs = src.jobs
        if not jobs:
            return "当前无定时任务。用 cron_add_job 创建一个。"
        # 经调度器公开只读快照读取（不触碰私有 _state/_paused）
        state = src.job_stats()
        paused = src.paused_jobs()
        lines = ["⏰ 定时任务列表:"]
        for j in jobs:
            name = j.get("name", "?")
            st = state.get(name, {})
            lines.append(
                f"  - {name}  [{j.get('schedule','')}]  "
                f"deliver={j.get('deliver',{}).get('mode','none')}  "
                f"{'暂停' if name in paused else '运行中'}  "
                f"最近: {st.get('last_status','-')}  "
                f"({st.get('runs',0)}次/{st.get('failures',0)}失败)"
            )
        return "\n".join(lines)


class CronRunJobTool(BaseTool):
    name = "cron_run_job"
    description = "立即手动触发一个定时任务（不等下次 cron 排程）。"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "要触发的任务名称"},
        },
        "required": ["name"],
    }

    def execute(self, **kwargs) -> str:
        return run_job(kwargs.get("name", ""))
