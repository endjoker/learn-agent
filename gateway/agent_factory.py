# -*- coding: utf-8 -*-
"""
Agent 工厂 —— 为 gateway 会话创建非交互 Agent 实例
支持 session_key → session_id 持久化映射，重启后恢复会话
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hello_agent.gateway")

# session_key → session_id 的持久化映射文件
_MAP_FILE = Path(__file__).parent / "sessions_map.json"
_map_lock = threading.Lock()


def _load_map() -> dict:
    """读取 session 映射表"""
    if _MAP_FILE.exists():
        try:
            with open(_MAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_map(mapping: dict):
    """保存 session 映射表"""
    try:
        with open(_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存 sessions_map.json 失败: %s", e)


def create_gateway_agent(
    session_key: str,
    model: str = "",
    max_steps: int = 30,
    permission_mode: str = "allow",
    quiet: bool = True,
    auto_approve_plan: bool = True,
) -> "Agent":
    """
    创建 gateway 专用的非交互 Agent。
    如果 session_key 有历史会话，自动恢复上下文。
    """
    from agent import create_agent

    agent = create_agent(
        name="helloworld agent",
        model=model or None,
        max_steps=max_steps,
        permission=True,
        memory=True,
        skills=False,
        sandbox=True,
        hooks=True,
        non_interactive=True,
        quiet=quiet,
    )
    agent.auto_approve_plan = auto_approve_plan

    # 权限档位
    if permission_mode == "allow":
        agent.permission.default_mode = "allow"
    elif permission_mode == "readonly":
        from core.permission import DENY
        for tool in ("bash", "write", "edit", "python", "http", "file_mgr"):
            agent.permission.set_rule(tool, DENY)

    # ---- 会话恢复：检查是否有该 session_key 的历史 session_id ----
    with _map_lock:
        mapping = _load_map()
        old_session_id = mapping.get(session_key)
        resumed = False

        if old_session_id:
            from core.message_store import DEFAULT_SESSION_DIR
            session_file = Path(DEFAULT_SESSION_DIR) / f"{old_session_id}.json"
            if session_file.exists():
                agent.store.session_id = old_session_id
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        session_data = json.load(f)
                    agent.store.load_session_data(session_data)
                    agent.messages.insert(0, {"role": "system", "content": agent.system_prompt})
                    resumed = True
                    logger.info("恢复会话: %s → session=%s (%d 条消息)",
                               session_key, old_session_id, len(agent.messages))
                except Exception as e:
                    logger.warning("恢复会话失败 %s: %s，将创建新会话", session_key, e)

        if not resumed:
            mapping[session_key] = agent.store.session_id
            _save_map(mapping)
            logger.info("创建 gateway agent: %s → session=%s, model=%s",
                       session_key, agent.store.session_id, agent.llm.model)

    return agent
