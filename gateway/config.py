# -*- coding: utf-8 -*-
"""
Gateway 配置读取 —— 从 config.json["gateway"] 加载 + 环境变量覆盖
"""

import os
from typing import Any

from core.config_loader import load_config


def get_gateway_config(base: dict = None) -> dict:
    """读取 gateway 配置（含环境变量覆盖）。

    base: 可选基础配置段；缺省从 config.json 读取 gateway 段。
    环境变量覆盖始终显式应用（无论 base 来自文件还是调用方传入）。
    """
    cfg = dict(base) if base is not None else dict(load_config().get("gateway", {}))

    # 环境变量覆盖敏感信息
    channels = cfg.setdefault("channels", {})

    feishu = channels.setdefault("feishu", {})
    feishu["app_id"] = os.getenv("FEISHU_APP_ID", feishu.get("app_id", ""))
    feishu["app_secret"] = os.getenv("FEISHU_APP_SECRET", feishu.get("app_secret", ""))
    feishu["encrypt_key"] = os.getenv("FEISHU_ENCRYPT_KEY", feishu.get("encrypt_key", ""))
    feishu["verification_token"] = os.getenv("FEISHU_VERIFICATION_TOKEN", feishu.get("verification_token", ""))
    webui = cfg.setdefault("webui", {})
    webui["auth_token"] = os.getenv("WEBUI_AUTH_TOKEN", webui.get("auth_token", ""))

    return cfg
