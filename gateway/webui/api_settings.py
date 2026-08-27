# -*- coding: utf-8 -*-
"""
api_settings.py —— 设置页端点（P3d）

保守白名单（用户决策）：llm / gateway.sessions / prompt / workspace；
mcp.servers 走 §6.3 专用端点。permission / sandbox / hooks / gateway 主段
不开放 UI 编辑（规避 guard 锁存假象与安全层误配）。

密钥脱敏流水线：GET 全脱敏；写入时占位符（空/含 …/****）= 保留原值。
"""

import json
import logging

from aiohttp import web

from core.config_writer import mask_dict
from gateway.webui.config_service import ConfigService, ConfigConflictError
from core.reasoning import normalize_reasoning_level

logger = logging.getLogger("jk_agent.gateway")


def register_routes(app: web.Application, module):
    app.router.add_get("/api/config", _make_config_get(module))
    app.router.add_patch("/api/config/{section}", _make_config_patch(module))
    app.router.add_get("/api/config/providers", _make_providers(module))
    app.router.add_get("/api/config/models", _make_models_list(module))
    app.router.add_post("/api/config/models", _make_model_write(module))
    app.router.add_put("/api/config/models/{name}", _make_model_write(module))
    app.router.add_delete("/api/config/models/{name}", _make_model_delete(module))
    app.router.add_put("/api/config/llm", _make_llm_default(module))


def _err(text, status=400):
    return web.json_response({"error": text}, status=status)


async def _body(request):
    try:
        return await request.json()
    except (json.JSONDecodeError, Exception):
        return None


def _parse_int(value, default=None):
    """安全解析整数参数；缺失/空返回 default，非法值抛 ValueError（调用方转 400）。"""
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError("参数必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise ValueError(f"参数必须是整数，收到 {value!r}")


def _make_config_get(module):
    async def handler(request):
        data, rev, status = module.config_service.read_masked()
        if status == "corrupt":
            return _err("config.json 损坏，请人工修复", 500)
        return web.json_response({"config": data, "rev": rev})
    return handler


def _make_config_patch(module):
    async def handler(request):
        section = request.match_info["section"]
        body = await _body(request)
        if body is None:
            return _err("无效的 JSON")
        patch = body.get("patch")
        base_rev = body.get("base_rev")
        if not isinstance(patch, dict):
            return _err("patch 必须是对象")
        try:
            rev = await module.config_service.patch_section(
                section, patch, base_rev)
        except ConfigConflictError as e:
            return web.json_response({
                "error": str(e),
                "rev": getattr(e, "current_rev", 0),
            }, status=409)
        except PermissionError as e:
            return _err(str(e), 403)
        except ValueError as e:
            return _err(str(e), 500)
        module.bus.publish("config.updated", {"section": section, "rev": rev})
        return web.json_response({"ok": True, "rev": rev})
    return handler


# ---------- 厂商化添加 LLM ----------

def _make_providers(module):
    async def handler(request):
        from core.init_wizard import (
            _PROVIDER_DEFAULTS, _CLOUD_PROTOCOLS, _ENV_KEY_HINTS)
        return web.json_response({
            "local": _PROVIDER_DEFAULTS,
            "cloud": [
                {"protocol": p, "label": lbl, "default_url": url}
                for (p, lbl, url) in _CLOUD_PROTOCOLS
            ],
            "env_hints": _ENV_KEY_HINTS,
        })
    return handler


def _make_models_list(module):
    """会话页模型下拉 + 设置页模型列表共用（脱敏）"""
    async def handler(request):
        data, rev, status = module.config_service.read_masked()
        if status == "corrupt":
            return _err("config.json 损坏", 500)
        models = data.get("llm", {}).get("models", {})
        out = []
        for name, cfg in models.items():
            out.append({"name": name, **(cfg or {})})
        return web.json_response({
            "models": out,
            "default_model": data.get("llm", {}).get("model_id", ""),
            "rev": rev,
        })
    return handler


def _make_model_write(module):
    async def handler(request):
        name = request.match_info.get("name")
        body = await _body(request)
        if body is None:
            return _err("无效的 JSON")
        model_name = name or body.get("name")
        if not model_name:
            return _err("缺少模型名称")

        # 字段组装（镜像 _collect_one_model）
        from core.config_writer import read_raw_config
        data, status = read_raw_config()
        if status == "corrupt":
            return _err("config.json 损坏", 500)
        models = data.setdefault("llm", {}).setdefault("models", {})
        existing = models.get(model_name, {})

        cfg = {}
        mtype = body.get("type")
        try:
            local_ctx = _parse_int(body.get("context_length"), default=131072)
            cloud_ctx = _parse_int(body.get("context_length"), default=128000)
        except ValueError:
            return _err("context_length 必须是整数")
        if mtype == "local":
            provider = body.get("provider", "")
            cfg["provider"] = provider
            cfg["base_url"] = body.get("base_url", "")
            cfg["api_key"] = body.get("api_key", "")
            cfg["context_length"] = local_ctx
        elif mtype == "cloud":
            protocol = body.get("protocol", "")
            if protocol:
                cfg["protocol"] = protocol
            cfg["base_url"] = body.get("base_url", "")
            cfg["api_key"] = body.get("api_key", "")
            cfg["context_length"] = cloud_ctx
        else:
            # 直接透传字段（PUT 更新场景）
            for k in ("provider", "protocol", "base_url", "api_key",
                      "context_length"):
                if k in body:
                    cfg[k] = body[k]

        if "reasoning" in body:
            reasoning = body["reasoning"]
            if not isinstance(reasoning, dict):
                return _err("reasoning 必须是对象")
            try:
                level = normalize_reasoning_level(
                    reasoning.get("level"), source="reasoning.level")
            except ValueError as e:
                return _err(str(e))
            cfg["reasoning"] = {"level": level}

            # The common level vocabulary maps to Chat Completions only.  Do
            # not persist a setting that the selected native protocol cannot
            # honour; callers get an actionable error before restart.
            effective_protocol = (cfg.get("protocol")
                                  or existing.get("protocol")
                                  or "openai").lower()
            if level != "provider_default" and effective_protocol != "openai":
                return _err(
                    "推理等级当前仅支持 OpenAI / OpenAI-compatible 协议；"
                    "Anthropic 和 Gemini 请使用 provider_default")

        # 密钥保留规则：api_key 为空/占位符 → 保留原值
        new_key = cfg.get("api_key", "")
        from core.config_writer import is_masked_placeholder
        if not new_key or is_masked_placeholder(new_key):
            if "api_key" in existing:
                cfg["api_key"] = existing["api_key"]
            else:
                cfg.pop("api_key", None)

        merged = dict(existing)
        merged.update(cfg)
        models[model_name] = merged

        await backup_and_write(module, data)
        module.bus.publish("config.updated", {"section": "llm"})
        return web.json_response({"ok": True, "name": model_name})
    return handler


def _make_model_delete(module):
    async def handler(request):
        name = request.match_info["name"]
        from core.config_writer import read_raw_config
        data, status = read_raw_config()
        if status == "corrupt":
            return _err("config.json 损坏", 500)
        llm = data.get("llm", {})
        models = llm.get("models", {})
        if name not in models:
            return _err(f"未找到模型: {name}", 404)
        if llm.get("model_id") == name:
            return _err("不能删除默认模型（请先切换默认模型）", 409)
        del models[name]
        await backup_and_write(module, data)
        module.bus.publish("config.updated", {"section": "llm"})
        return web.json_response({"ok": True, "name": name})
    return handler


def _make_llm_default(module):
    async def handler(request):
        body = await _body(request)
        if body is None:
            return _err("无效的 JSON")
        from core.config_writer import read_raw_config
        data, status = read_raw_config()
        if status == "corrupt":
            return _err("config.json 损坏", 500)
        llm = data.setdefault("llm", {})
        if "model_id" in body:
            mid = body["model_id"]
            if mid and mid not in llm.get("models", {}):
                return _err(f"模型不存在: {mid}", 404)
            llm["model_id"] = mid
        if "timeout" in body:
            try:
                llm["timeout"] = _parse_int(body["timeout"])
            except ValueError:
                return _err("timeout 必须是整数")
        if "reasoning" in body:
            reasoning = body["reasoning"]
            if not isinstance(reasoning, dict):
                return _err("reasoning 必须是对象")
            try:
                llm["reasoning"] = {"level": normalize_reasoning_level(
                    reasoning.get("level"), source="reasoning.level")}
            except ValueError as e:
                return _err(str(e))
        await backup_and_write(module, data)
        module.bus.publish("config.updated", {"section": "llm"})
        return web.json_response({"ok": True})
    return handler


async def backup_and_write(module, data: dict) -> int:
    """配置写盘统一走 ConfigService 持锁管线（backup + write + force_reload）。"""
    return await module.config_service.write_full(data)
