# -*- coding: utf-8 -*-
"""
Gateway 配置读取 —— 从 config.json["gateway"] 加载 + 环境变量覆盖
"""

import os
from typing import Any

from core.config_loader import load_config


def get_gateway_config() -> dict:
    """读取 gateway 配置（含环境变量覆盖）"""
    cfg = load_config().get("gateway", {})

    # 环境变量覆盖敏感信息
    channels = cfg.get("channels", {})

    feishu = channels.get("feishu", {})
    feishu["app_id"] = os.getenv("FEISHU_APP_ID", feishu.get("app_id", ""))
    feishu["app_secret"] = os.getenv("FEISHU_APP_SECRET", feishu.get("app_secret", ""))

    return cfg
