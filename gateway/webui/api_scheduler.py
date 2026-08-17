# -*- coding: utf-8 -*-
"""
api_scheduler.py —— 定时任务查看/创建/管理端点（P-修 #6）

jobs 以 config.json gateway.scheduler.jobs 为唯一事实源；写盘后
scheduler.reload_config() 热加载。运行态（last_fire/状态/历史）取自
scheduler._state。deliver 支持 announce 到第三方通道（飞书/微信）。
"""

import json
import logging

from aiohttp import web

logger = logging.getLogger("jk_agent.gateway")


def register_routes(app: web.Application, module):
    app.router.add_get("/api/scheduler/jobs", _make_jobs(module))
    app.router.add_post("/api/scheduler/jobs", _make_write(module))
    app.router.add_put("/api/scheduler/jobs/{name}", _make_write(module))
    app.router.add_delete("/api/scheduler/jobs/{name}", _make_delete(module))
    app.router.add_post("/api/scheduler/jobs/{name}/run", _make_action(module, "run"))
    app.router.add_post("/api/scheduler/jobs/{name}/pause", _make_action(module, "pause"))
    app.router.add_post("/api/scheduler/jobs/{name}/resume", _make_action(module, "resume"))
    app.router.add_get("/api/scheduler/history", _make_history(module))
    app.router.add_get("/api/scheduler/channels", _make_channels(module))


def _err(text, status=400):
    return web.json_response({"error": text}, status=status)


async def _body(request):
    try:
        return await request.json()
    except (json.JSONDecodeError, Exception):
        return None


def _write_jobs(module, jobs):
    """整表写回 gateway.scheduler.jobs 并热加载"""
    from gateway.scheduler import _write_jobs_to_config
    _write_jobs_to_config(jobs)
    if module.scheduler:
        module.scheduler.reload_config()


def _make_jobs(module):
    async def handler(request):
        sched = module.scheduler
        jobs = sched.jobs if sched else []
        state = (sched._state.get("jobs", {}) if sched else {})
        running = (sched._running_jobs if sched else set())
        paused = (sched._paused if sched else set())
        out = []
        for j in jobs:
            name = j.get("name", "")
            st = state.get(name, {})
            out.append({
                **j,
                "running": name in running,
                "paused": name in paused,
                "last_fire": st.get("last_fire"),
                "last_status": st.get("last_status"),
                "runs": st.get("runs", 0),
                "failures": st.get("failures", 0),
            })
        return web.json_response({"jobs": out})
    return handler


def _validate_job(body):
    from croniter import croniter
    if not body.get("name"):
        return "缺少 name"
    if not body.get("schedule"):
        return "缺少 schedule"
    try:
        croniter(body["schedule"])
    except Exception:
        return f"cron 表达式无效: {body['schedule']}"
    if not body.get("prompt"):
        return "缺少 prompt"
    d = body.get("deliver") or {}
    if d.get("mode") == "announce" and not (d.get("channel") and d.get("target")):
        return "announce 投递必须指定 channel + target"
    return None


def _make_write(module):
    async def handler(request):
        body = await _body(request)
        if body is None:
            return _err("无效的 JSON")
        name = request.match_info.get("name")
        if name:
            body["name"] = name
        err = _validate_job(body)
        if err:
            return _err(err)
        sched = module.scheduler
        jobs = list(sched.jobs) if sched else []
        jobs = [j for j in jobs if j.get("name") != body["name"]]
        jobs.append(body)
        try:
            _write_jobs(module, jobs)
        except Exception as e:
            return _err(str(e), 500)
        module.bus.publish("cron.changed", {"action": "write", "job": body["name"]})
        return web.json_response({"ok": True, "name": body["name"]})
    return handler


def _make_delete(module):
    async def handler(request):
        name = request.match_info["name"]
        sched = module.scheduler
        jobs = list(sched.jobs) if sched else []
        new = [j for j in jobs if j.get("name") != name]
        if len(new) == len(jobs):
            return _err(f"未找到任务: {name}", 404)
        try:
            _write_jobs(module, new)
        except Exception as e:
            return _err(str(e), 500)
        module.bus.publish("cron.changed", {"action": "delete", "job": name})
        return web.json_response({"ok": True})
    return handler


def _make_action(module, action):
    async def handler(request):
        name = request.match_info["name"]
        sched = module.scheduler
        if not sched:
            return _err("scheduler 未启用", 503)
        if action == "run":
            return web.json_response({"ok": True,
                                      "reply": await sched.handle_command(f"run {name}")})
        if action == "pause":
            return web.json_response({"ok": True,
                                      "reply": await sched.handle_command(f"pause {name}")})
        return web.json_response({"ok": True,
                                  "reply": await sched.handle_command(f"resume {name}")})
    return handler


def _make_history(module):
    async def handler(request):
        name = request.query.get("name", "")
        sched = module.scheduler
        hist = list(sched._state.get("history", [])) if sched else []
        if name:
            hist = [h for h in hist if h.get("job") == name]
        return web.json_response({"history": hist[-50:]})
    return handler


def _make_channels(module):
    """可供 announce 投递的第三方通道（已启用者）"""
    async def handler(request):
        from core.config_loader import load_config
        channels = load_config().get("gateway", {}).get("channels", {})
        out = []
        for cname in ("feishu", "weixin"):
            cfg = channels.get(cname, {})
            if cfg.get("enabled", False):
                out.append({"channel": cname,
                            "hint": "飞书填 chat_id（oc_ 开头）；微信填用户 ID"
                            if cname == "feishu" else "微信填用户 ID"})
        # 已有 webhook 目标（#3：供前端下拉选择，目标改可选）
        webhooks = set()
        sched = module.scheduler
        if sched:
            for j in sched.jobs:
                d = j.get("deliver") or {}
                if d.get("mode") == "webhook" and d.get("target"):
                    webhooks.add(d["target"])
        # hooks 配置中的 webhook_notifier URL
        try:
            hooks = load_config().get("hooks", {}).get("hooks", {})
            for entries in hooks.values():
                if isinstance(entries, list):
                    for e in entries:
                        if isinstance(e, dict) and e.get("url"):
                            webhooks.add(e["url"])
        except Exception:
            pass
        # announce 可选目标（#4）：来自已有会话（feishu:/weixin: 前缀）+ 现有 announce 目标
        targets = {}
        def _add(chan, tgt):
            if tgt:
                targets.setdefault(chan, set()).add(tgt)
        # 已持久化会话映射
        try:
            from gateway.agent_factory import _load_map
            for key in _load_map():
                if key.startswith("feishu:"):
                    _add("feishu", key[len("feishu:"):])
                elif key.startswith("weixin:"):
                    _add("weixin", key[len("weixin:"):])
        except Exception:
            pass
        # 活跃会话
        if module.session_mgr:
            for e in module.session_mgr.list_entries():
                key = e.get("session_key", "")
                if key.startswith("feishu:"):
                    _add("feishu", key[len("feishu:"):])
                elif key.startswith("weixin:"):
                    _add("weixin", key[len("weixin:"):])
        # 现有 announce 目标
        sched = module.scheduler
        if sched:
            for j in sched.jobs:
                d = j.get("deliver") or {}
                if d.get("mode") == "announce" and d.get("channel") and d.get("target"):
                    _add(d["channel"], d["target"])
        targets = {k: sorted(v) for k, v in targets.items()}
        return web.json_response({"channels": out,
                                  "webhooks": sorted(webhooks),
                                  "targets": targets})
    return handler
