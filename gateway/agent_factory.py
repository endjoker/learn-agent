# -*- coding: utf-8 -*-
"""
Agent 工厂 —— 为 gateway 会话创建非交互 Agent 实例
支持 session_key → 会话元数据 持久化映射，重启后恢复会话

sessions_map v2 结构（兼容旧版扁平字符串值）：
    { session_key: { "session_id": "...", "model": "...",
                     "permission_mode": "allow" } }
"""

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from core.atomic_io import atomic_write_json

logger = logging.getLogger("hello_agent.gateway")

# session_key → 会话元数据 的持久化映射文件
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
        atomic_write_json(_MAP_FILE, mapping, prefix=".sessions_map.")
    except OSError as e:
        logger.error("保存 sessions_map.json 失败: %s", e)


def remove_map_entry(session_key: str):
    """删除 session_key 的持久化映射条目（isolated 定时会话清理用）"""
    with _map_lock:
        mapping = _load_map()
        if session_key in mapping:
            del mapping[session_key]
            _save_map(mapping)


def update_map_meta(session_key: str, **fields):
    """更新 sessions_map v2 条目的元数据字段（model / permission_mode 等）"""
    with _map_lock:
        mapping = _load_map()
        raw = mapping.get(session_key)
        if raw is None:
            return
        entry = raw if isinstance(raw, dict) else {"session_id": raw}
        entry.update(fields)
        mapping[session_key] = entry
        _save_map(mapping)


def create_gateway_agent(
    session_key: str,
    model: str = "",
    max_steps: int = 100,
    permission_mode: str = "allow",
    quiet: bool = True,
    auto_approve_plan: bool = True,
) -> "Agent":
    """
    创建 gateway 专用的非交互 Agent。
    如果 session_key 有历史会话，自动恢复上下文，
    并按 sessions_map v2 / session_data 中的记录回填运行期切换过的模型。
    """
    from agent import create_agent

    agent = create_agent(
        name="helloworld agent",
        model=model or None,
        max_steps=max_steps,
        permission=True,
        memory=True,
        skills=True,
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
    resumed = False
    saved_model = ""
    saved_perm_mode = ""
    saved_reasoning_level = None
    with _map_lock:
        mapping = _load_map()
        raw = mapping.get(session_key)
        # 兼容旧版扁平字符串值
        meta = raw if isinstance(raw, dict) else (
            {"session_id": raw} if raw else {})
        old_session_id = meta.get("session_id")

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
                    # 模型回填来源：map v2 元数据优先（运行期切换即时写入），
                    # 其次 session_data.model_id（save_session 时落盘）
                    saved_model = meta.get("model") or session_data.get("model_id") or ""
                    saved_perm_mode = meta.get("permission_mode") or ""
                    candidate = meta.get("reasoning_level")
                    if isinstance(candidate, str):
                        from core.reasoning import REASONING_LEVELS
                        if candidate in REASONING_LEVELS:
                            saved_reasoning_level = candidate
                    logger.info("恢复会话: %s → session=%s (%d 条消息)",
                               session_key, old_session_id, len(agent.messages))
                except Exception as e:
                    logger.warning("恢复会话失败 %s: %s，将创建新会话", session_key, e)

        if not resumed:
            mapping[session_key] = {
                "session_id": agent.store.session_id,
                "model": agent.llm.model,
                "permission_mode": permission_mode,
            }
            _save_map(mapping)
            logger.info("创建 gateway agent: %s → session=%s, model=%s",
                       session_key, agent.store.session_id, agent.llm.model)
        elif isinstance(raw, str):
            # 旧格式惰性升级为 v2
            mapping[session_key] = {
                "session_id": old_session_id,
                "model": saved_model,
                "permission_mode": saved_perm_mode or permission_mode,
            }
            _save_map(mapping)

    # ---- 模型回填（修"运行期 /model 切换重启后丢失"缺口）----
    if resumed and (saved_model or saved_reasoning_level is not None):
        try:
            agent.switch_llm(model=saved_model or agent.llm.model,
                             reasoning_level=saved_reasoning_level)
            logger.info("恢复会话模型/推理等级: %s → %s / %s", session_key,
                        agent.llm.model, agent.llm.reasoning_level)
        except Exception as e:
            logger.warning("恢复会话回填模型 %s 失败，回落默认模型: %s",
                           saved_model, e)

    # None intentionally represents "inherit selected model configuration".
    agent._session_reasoning_override = saved_reasoning_level

    # ---- 权限档位重放（无 WebUI 审批桥时仅 unreviewed 可安全重放）----
    # Preserve the per-session mode for the WebUI initializer. The
    # initializer installs the approval bridge and reapplies the complete
    # mode (not just ``unreviewed``) before the first tool call.
    effective_permission_mode = saved_perm_mode or permission_mode
    agent._gateway_permission_mode = effective_permission_mode

    return agent

