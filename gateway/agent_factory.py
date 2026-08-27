# -*- coding: utf-8 -*-
"""
Agent 工厂 —— 为 gateway 会话创建非交互 Agent 实例
支持 session_key → 会话元数据 持久化映射，重启后恢复会话

sessions_map v2 结构（兼容旧版扁平字符串值）：
    { session_key: { "session_id": "...", "model": "...",
                     "permission_mode": "allow" } }

会话历史恢复（SQLite 唯一权威）：
统一会话链路（WebUI 主会话 / 工作区 / 渠道队列）执行时以 SQLite
（runtime.db 的 conversation_sessions/turns/turn_nodes）为唯一权威，
主动 set_file_persistence(False) 停写 sessions/*.json。恢复侧因此只做
SQLite 全量回放；sessions_map.json 仅保留兜底元数据（session_id 关联 +
model/permission 兜底），不再参与历史合并。
- 一次性会话键（sched:/subagent:/debug:/heartbeat: 前缀，见
  _EPHEMERAL_KEY_PREFIXES）不写 map、加载时惰性剪枝——它们的历史在
  统一会话库，map 条目只会泄漏（此前 sched/subagent 各积压十几条死条目）。
回放映射：user/user_steering → user 消息；assistant → assistant 消息
（metadata.final/intermediate 随 kind 标记）；tool 连续段 →
assistant(tool_calls) + tool 结果对（params_summary 可解析为 JSON 时还原
原生结构；被摘要截断而残缺时降级为纯文本备注，绝不产生孤立/残缺的
tool 协议消息）；reasoning/status 等仅 UI 节点不进入模型上下文。
system prompt 仍由工厂插入并去重。写入侧与恢复侧同以 SQLite 闭环。
"""

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.atomic_io import atomic_write_json

if TYPE_CHECKING:
    # 仅供类型注解使用；运行期在 create_gateway_agent 内延迟导入避免循环依赖。
    from agent import Agent

logger = logging.getLogger("jk_agent.gateway")

# session_key → 会话元数据 的持久化映射文件
_MAP_FILE = Path(__file__).parent / "sessions_map.json"
_map_lock = threading.Lock()

_WORKSPACE_PREFIX = "workspace:"

# 一次性会话键前缀：生命周期随单次触发/任务结束即终结，无需跨重启恢复
# 元数据，因此不写 sessions_map（历史以统一会话库为准）。历史上这些键曾
# 持续写 map 造成无限增长（sched×17 / subagent×10 死条目实证）。
_EPHEMERAL_KEY_PREFIXES = ("sched:", "subagent:", "debug:", "heartbeat:")


def _is_ephemeral_session_key(session_key: str) -> bool:
    return isinstance(session_key, str) and session_key.startswith(
        _EPHEMERAL_KEY_PREFIXES)


def prune_ephemeral_map_entries(mapping: dict) -> bool:
    """惰性剪枝：剔除 map 中一次性会话键的死条目。返回是否有变更。"""
    dead = [k for k in mapping if _is_ephemeral_session_key(k)]
    for k in dead:
        del mapping[k]
    if dead:
        logger.info("sessions_map 清理一次性会话死条目 %d 个", len(dead))
    return bool(dead)


def is_workspace_session_key(session_key: str) -> bool:
    """判断 session_key 是否属于工作区会话（workspace:{workspace_id}:{session_id}）。

    工作区会话元数据以 SQLite workspace_sessions 为唯一事实源，
    绝不读写 sessions_map.json（Phase 1 统一约束 #1）。
    """
    return isinstance(session_key, str) and session_key.startswith(_WORKSPACE_PREFIX)


# ============================================================
# SQLite 历史回放（统一会话恢复路径，写入侧/恢复侧同源闭环）
# ============================================================

class _ReplayDbAdapter:
    """最小 db 适配器（connection/transaction），复用 RuntimeStore 的 SQLite
    连接配置，避免引入 gateway.webui 依赖链（与 session_migrate 同模式）。"""

    def __init__(self, runtime):
        self._runtime = runtime

    def connection(self):
        return self._runtime.connection()

    def transaction(self):
        return self._runtime.connection()


_replay_store = None
_replay_store_tried = False
_replay_store_lock = threading.Lock()


def _default_conversation_store():
    """惰性构建回放用的 ConversationStore（复用 runtime.db 与其连接配置）。

    dispatcher 注入优先（create_gateway_agent 的 conversation_store 参数，
    取 ConversationBridge.service.store，与执行侧同一实例）；脚本/测试直连
    工厂时按 config.json 的 runtime_store.path 兜底构建。构建失败缓存结果，
    避免每次建 Agent 都重复报错。"""
    global _replay_store, _replay_store_tried
    if _replay_store is not None or _replay_store_tried:
        return _replay_store
    with _replay_store_lock:
        if _replay_store is not None or _replay_store_tried:
            return _replay_store
        _replay_store_tried = True
        try:
            from core.config_loader import _find_project_root, load_config
            from core.runtime import RuntimeStore
            from gateway.conversation.store import ConversationStore
            root = _find_project_root()
            store_cfg = load_config().get("runtime_store") or {}
            db_path = Path(str(store_cfg.get("path")
                               or "./workspace/.agent/state/runtime.db"))
            if not db_path.is_absolute():
                db_path = root / db_path
            runtime = RuntimeStore(
                db_path,
                wal=bool(store_cfg.get("wal", True)),
                busy_timeout_ms=int(store_cfg.get("busy_timeout_ms", 5000) or 5000),
            )
            _replay_store = ConversationStore(_ReplayDbAdapter(runtime))
            logger.info("会话恢复回放存储已就绪: %s", db_path)
        except Exception as e:
            logger.warning("初始化会话恢复回放存储失败（跳过 SQLite 回放）: %s", e)
        return _replay_store


def _validated_reasoning(meta: dict):
    """sessions_map 元数据中的 reasoning_level 校验（非法值返回 None）。"""
    candidate = (meta or {}).get("reasoning_level")
    if isinstance(candidate, str):
        try:
            from core.reasoning import REASONING_LEVELS
        except Exception:
            return None
        if candidate in REASONING_LEVELS:
            return candidate
    return None


# 回放进入模型上下文的节点类型；reasoning/status 等仅 UI 展示，不回放。
_REPLAY_NODE_TYPES = frozenset({"user", "user_steering", "assistant", "tool"})


def _tool_run_messages(base_text: str, run_nodes) -> list:
    """工具节点连续段 → assistant(tool_calls)+tool 结果消息序列。

    params_summary 能解析为 JSON 时还原原生 tool_calls 结构（与运行期
    agent.messages 形态一致）；被 200 字符摘要截断而残缺时降级为纯文本
    备注——绝不产生孤立或参数残缺的 tool 协议消息（provider 会 400）。
    """
    entries: list = []
    results: list = []
    notes: list = []
    for node in run_nodes:
        meta = getattr(node, "metadata", None) or {}
        name = str(meta.get("tool") or "tool")
        raw_args = str(meta.get("params_summary") or "")
        result_text = str(meta.get("result_summary") or "")
        call_id = str(meta.get("call_id") or "").strip() or f"replay-{node.node_id}"
        parsed = None
        if raw_args:
            try:
                parsed = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed = None
        if isinstance(parsed, (dict, list)):
            entries.append({
                "id": call_id, "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(parsed, ensure_ascii=False)},
            })
            results.append({
                "role": "tool", "tool_call_id": call_id, "name": name,
                "content": result_text or "(历史回放：工具结果未记录)",
                "kind": "tool_result",
                "is_error": bool(meta.get("error_code")),
            })
        else:
            note = f"[历史回放] 调用工具 {name}"
            if raw_args:
                note += f"，参数(摘要): {raw_args}"
            if result_text:
                note += f"\n[历史回放] 工具结果(摘要): {result_text}"
            notes.append(note)
    parts = ([base_text] if base_text else []) + notes
    content = "\n".join(p for p in parts if p) or None
    messages: list = []
    if entries:
        # 与运行期转录同形：assistant 文本随 tool_calls 载体携带（content 可空）
        messages.append({"role": "assistant", "content": content,
                         "kind": "tool_calls", "tool_calls": entries})
        messages.extend(results)
    elif content:
        messages.append({"role": "assistant", "content": content,
                         "kind": "intermediate"})
    return messages


def _turn_nodes_to_messages(nodes) -> list:
    """单个 Turn 内节点序列 → agent.messages 片段（不含 system）。

    assistant 文本后紧跟工具段时并入 tool_calls 载体（与运行期"中间输出
    随载体携带"形态一致，避免连续两条 assistant 消息）；final/intermediate
    标记来自节点 metadata（complete_turn/mark_intermediate 权威写入）。
    """
    items = [n for n in nodes if getattr(n, "type", "") in _REPLAY_NODE_TYPES]
    messages: list = []
    idx = 0
    while idx < len(items):
        node = items[idx]
        ntype = getattr(node, "type", "")
        if ntype in ("user", "user_steering"):
            text = str(getattr(node, "text", "") or "")
            # 修正版方案 A：随消息发送的图片以占位降级回放——原图落盘
            # artifacts（人可看），但默认不还原进模型上下文（结论已在历史
            # assistant 节点里，还原只会白耗 token；确需细看由用户重发）。
            meta = getattr(node, "metadata", None) or {}
            images = meta.get("images") or []
            if images:
                names = "、".join(
                    Path(str(m.get("ref") or "")).name or "图片"
                    for m in images if isinstance(m, dict))
                text = f"{text}\n[图片已存档: {names}]" if text.strip() else \
                    f"[图片已存档: {names}]"
            if text.strip():
                messages.append({"role": "user", "content": text})
            idx += 1
        elif ntype == "assistant":
            nxt = items[idx + 1] if idx + 1 < len(items) else None
            if nxt is not None and getattr(nxt, "type", "") == "tool":
                run = []
                k = idx + 1
                while k < len(items) and getattr(items[k], "type", "") == "tool":
                    run.append(items[k])
                    k += 1
                messages.extend(_tool_run_messages(str(node.text or ""), run))
                idx = k
            else:
                text = str(getattr(node, "text", "") or "")
                if text.strip():
                    meta = getattr(node, "metadata", None) or {}
                    msg = {"role": "assistant", "content": text}
                    if meta.get("intermediate"):
                        msg["kind"] = "intermediate"
                    elif meta.get("final"):
                        msg["kind"] = "final"
                    messages.append(msg)
                idx += 1
        else:  # 无前导 assistant 文本的孤立工具段（异常轮次也要保序）
            run = []
            k = idx
            while k < len(items) and getattr(items[k], "type", "") == "tool":
                run.append(items[k])
                k += 1
            messages.extend(_tool_run_messages("", run))
            idx = k
    return messages


def _collect_sqlite_history(store, session_key: str, *,
                            anchor_user_text: str = "",
                            max_turns: int = 200) -> Optional[dict]:
    """按序读取统一会话各 Turn 的 turn_nodes，重建 agent.messages 序列。

    anchor_user_text 非空时（JSON 转录可用、需增量合并）：在 SQLite user
    节点中找该文本的最后一次匹配，仅回放其后 Turn —— JSON 已覆盖锚点及
    之前的历史，追加尾段即可无缝拼接且不重复。锚点未命中视为不可安全合
    并（anchored=False，调用方回落纯 JSON）。

    返回 {messages, anchored, newest_started_at}；会话不存在/无 Turn 返回 None。
    任何 SQLite 异常都吞掉并记 debug（恢复失败不阻断 Agent 创建）。
    """
    if store is None:
        return None
    try:
        conv = store.get_conversation_by_key(session_key)
    except Exception as e:
        logger.debug("SQLite 会话查询失败 %s: %s", session_key, e)
        return None
    if conv is None:
        return None
    try:
        turns = store.list_turns(conv.conversation_id, limit=max_turns)
    except Exception as e:
        logger.debug("SQLite turns 读取失败 %s: %s", conv.conversation_id, e)
        return None
    if not turns:
        return None
    turns = list(reversed(turns))  # list_turns 倒序 → 按 started_at 升序回放
    turn_nodes = []
    for turn in turns:
        try:
            turn_nodes.append((turn, store.get_turn_nodes(turn.turn_id)))
        except Exception as e:
            logger.debug("SQLite 节点读取失败 %s: %s", turn.turn_id, e)
    newest = ""
    for turn, _nodes in turn_nodes:
        newest = getattr(turn, "started_at", "") or newest

    if anchor_user_text:
        cut = -1
        for i, (_turn, nodes) in enumerate(turn_nodes):
            for n in nodes:
                if getattr(n, "type", "") == "user"                         and str(getattr(n, "text", "") or "").strip() == anchor_user_text:
                    cut = i  # 取最后一次匹配 → 最小化追加尾
        if cut < 0:
            return {"messages": [], "anchored": False,
                    "newest_started_at": newest}
        turn_nodes = turn_nodes[cut + 1:]
    messages: list = []
    for _turn, nodes in turn_nodes:
        messages.extend(_turn_nodes_to_messages(nodes))
    return {"messages": messages, "anchored": True, "newest_started_at": newest}


def _insert_system_prompt(agent) -> None:
    """insert 前查重：避免旧数据已含 system prompt 时重复插入。"""
    if not agent.messages or agent.messages[0].get("role") != "system" \
            or agent.messages[0].get("content") != agent.system_prompt:
        agent.messages.insert(0, {"role": "system",
                                  "content": agent.system_prompt})


def _apply_history_messages(agent, messages: list) -> None:
    """以回放消息整体替换会话历史（保持列表对象引用不变），并插入 system。"""
    agent.messages.clear()
    agent.messages.extend(messages)
    _insert_system_prompt(agent)


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
    if is_workspace_session_key(session_key):
        logger.info("工作区会话不写 sessions_map，忽略 remove_map_entry: %s", session_key)
        return
    with _map_lock:
        mapping = _load_map()
        if session_key in mapping:
            del mapping[session_key]
            _save_map(mapping)


def update_map_meta(session_key: str, **fields):
    """更新 sessions_map v2 条目的元数据字段（model / permission_mode 等）"""
    if is_workspace_session_key(session_key):
        logger.info("工作区会话不写 sessions_map，忽略 update_map_meta: %s", session_key)
        return
    with _map_lock:
        mapping = _load_map()
        raw = mapping.get(session_key)
        if raw is None:
            return
        entry = raw if isinstance(raw, dict) else {"session_id": raw}
        entry.update(fields)
        mapping[session_key] = entry
        _save_map(mapping)


def _preheat_mcp(agent, session_key: str):
    """B6/L6#1：agent 创建后立即后台预热 MCP，不阻塞返回。

    原首轮 run() 的 _init_mcp_if_needed 会同步 run_in_mcp_loop(..., timeout=60)，
    最坏 60s 阻塞在首轮关键路径上。这里改为：消费 agent._mcp_pending_init，
    并把完整 init（建管理器 → initialize_all → discover → 注册工具 → 重建
    System Prompt）以 fire-and-forget 方式投到 MCP 常驻事件循环后台执行。

    效果（对应 _init_mcp_if_needed 的新语义，gateway 路径无需再等待）：
      - 预热已完成 → run() 看到 _mcp_pending_init 为空直接返回（毫秒级确认）；
      - 预热进行中 → run() 同样不等待，以当前已注册工具先行（记 debug），
        预热完成时原地补注册/重建 prompt，下轮 run 自动确认；
      - 60s 阻塞上限不再出现在首轮，仅保留给显式 reload（reload_mcp 不变）。
    """
    configs = getattr(agent, "_mcp_pending_init", None)
    if not configs:
        return
    from core.mcp_client import (MCPClientManager,
                                 fire_and_forget_in_mcp_loop)
    from tools.mcp_tools import MCPTool
    agent._mcp_pending_init = None  # 预热消费待初始化配置，run() 不再重复初始化
    try:
        fire_and_forget_in_mcp_loop(
            agent._async_init_mcp(configs, MCPClientManager, MCPTool))
        logger.info("MCP 后台预热已投递: %s (%d 个服务器配置)",
                    session_key, len(configs))
    except Exception as e:
        # 投递失败（如 MCP 循环已关闭）：回滚标志，首轮 run() 走原同步初始化
        agent._mcp_pending_init = configs
        logger.warning("MCP 后台预热投递失败，回退首轮同步初始化: %s", e)


def create_gateway_agent(
    session_key: str,
    model: str = "",
    max_steps: int = 100,
    permission_mode: str = "allow",
    quiet: bool = True,
    auto_approve_plan: bool = True,
    runtime_context: object = None,
    mcp_servers: list = None,
    profile_prompt: str = None,
    allowed_tools: list = None,
    allowed_skills: list = None,
    reasoning_level: str = None,
    conversation_store=None,
) -> "Agent":
    """
    创建 gateway 专用的非交互 Agent。
    如果 session_key 有历史会话，自动恢复上下文，
    并按 sessions_map v2 / session_data 中的记录回填运行期切换过的模型。

    runtime_context: 可选的 WorkspaceRuntimeContext（Phase 0 起支持）。
        传入时 create_agent 使用显式三目录分离，不再依赖进程 CWD。
    mcp_servers: 可选 MCP 服务器配置列表（覆盖 config.json）。
    profile_prompt: 可选 Agent Profile 的 System Prompt（Phase 2/4）。
    allowed_tools / allowed_skills: 可选能力子集（Phase 4 快照冻结）。
    conversation_store: 可选 ConversationStore（SQLite 统一会话权威存储），
        供恢复分支回放会话历史；不传时按 runtime_store 配置惰性兜底构建
        （dispatcher 注入 bridge.service.store 以复用同一实例）。
    """
    from agent import create_agent

    agent = create_agent(
        name="JKagent",
        model=model or None,
        reasoning_level=reasoning_level,
        max_steps=max_steps,
        permission=True,
        memory=True,
        skills=True,
        sandbox=True,
        hooks=True,
        non_interactive=True,
        quiet=quiet,
        runtime_context=runtime_context,
        mcp_servers=mcp_servers,
        profile_prompt=profile_prompt,
        allowed_tools=allowed_tools,
        allowed_skills=allowed_skills,
    )
    agent.auto_approve_plan = auto_approve_plan

    # B6/L6#1：MCP 首轮 init 移出关键路径——创建后立即后台预热（不阻塞返回）
    _preheat_mcp(agent, session_key)

    # 权限档位：这里只做同步生效的 default_mode 预置；最终以 WebUI 初始化器
    # 的 apply_permission_mode（conversation prefs 单一事实源）为准。注意不要
    # 在此用 permission.set_rule 做 allowlist——set_rule 只是注册元数据，
    # PolicyEngine 四档裁决不读取它（历史死代码已清理）。
    if permission_mode in ("allow", "readonly", "ask", "unreviewed"):
        agent.permission.default_mode = permission_mode

    # ---- Phase 1/4：工作区会话专用分支（唯一事实源 SQLite，不读写 sessions_map）----
    if is_workspace_session_key(session_key):
        parts = session_key.split(":")
        ws_id = parts[1] if len(parts) > 1 else ""
        session_id = parts[2] if len(parts) > 2 else ""
        # 净化：session_id 仅取 basename，防止路径穿越逃逸会话目录
        session_id = Path(session_id).name if session_id else ""
        resumed = False
        replay_store = conversation_store or _default_conversation_store()
        # 文件转录已退役：SQLite 统一会话全量回放是唯一恢复路径。
        hist = _collect_sqlite_history(replay_store, session_key)
        if hist and hist.get("messages"):
            if session_id:
                agent.store.session_id = session_id
            _apply_history_messages(agent, hist["messages"])
            resumed = True
            logger.info("恢复工作区会话(SQLite 回放): %s → session=%s (%d 条消息)",
                        session_key, agent.store.session_id, len(agent.messages) - 1)
        if not resumed:
            if session_id:
                agent.store.session_id = session_id
            logger.info("创建工作区 agent: %s → session=%s, model=%s",
                        session_key, agent.store.session_id, agent.llm.model)
        # 模型/权限回填
        if model:
            try:
                agent.switch_llm(model=model, reasoning_level=reasoning_level)
            except Exception as e:
                logger.warning("工作区会话模型回填失败: %s", e)
        agent._gateway_permission_mode = permission_mode
        return agent

    # ---- 会话恢复：检查是否有该 session_key 的历史 session_id ----
    resumed = False
    saved_model = ""
    saved_perm_mode = ""
    saved_reasoning_level = None
    replay_store = conversation_store or _default_conversation_store()
    # 映射只是兜底元数据：无锁读快照 + 锁外做 SQLite 回放（旧实现把
    # _collect_sqlite_history 的多次 DB 往返包在 _map_lock 内，串行化了
    # 所有非工作区 Agent 的创建）。临界区只保留 map 的读改写。
    raw = _load_map().get(session_key)
    # 兼容旧版扁平字符串值
    meta = raw if isinstance(raw, dict) else (
        {"session_id": raw} if raw else {})
    old_session_id = meta.get("session_id")

    if old_session_id:
        # 净化：仅取 basename，防止持久化映射中的脏 session_id 逃逸会话目录
        old_session_id = Path(str(old_session_id)).name
    # 文件转录已退役：SQLite 统一会话全量回放是唯一恢复路径；
    # sessions_map 仅保留 session_id 关联与模型/权限兜底元数据。
    hist = _collect_sqlite_history(replay_store, session_key)
    if hist and hist.get("messages"):
        if old_session_id:
            agent.store.session_id = old_session_id
        _apply_history_messages(agent, hist["messages"])
        resumed = True
        saved_model = meta.get("model") or ""
        saved_perm_mode = meta.get("permission_mode") or ""
        saved_reasoning_level = _validated_reasoning(meta)
        logger.info("恢复会话(SQLite 回放): %s → session=%s (%d 条消息)",
                    session_key, agent.store.session_id,
                    len(agent.messages) - 1)
    elif old_session_id:
        # 只有"曾有映射却查无历史"才是异常信号；全新 key 首次创建属正常
        # 路径，不打 warning（此前每个新会话都误报一次"恢复会话失败"）。
        logger.warning("恢复会话失败 %s: SQLite 无历史，将创建新会话",
                       session_key)

    with _map_lock:
        mapping = _load_map()
        if not resumed:
            # 一次性会话键（sched:/subagent:/debug:/heartbeat:）不写 map：
            # 历史在统一会话库，map 条目只会在收尾路径缺失时无限泄漏。
            if not _is_ephemeral_session_key(session_key):
                mapping[session_key] = {
                    "session_id": agent.store.session_id,
                    "model": agent.llm.model,
                    "permission_mode": permission_mode,
                }
            logger.info("创建 gateway agent: %s → session=%s, model=%s",
                        session_key, agent.store.session_id, agent.llm.model)
        elif isinstance(raw, str):
            # 旧格式惰性升级为 v2（一次性键由下方剪枝移除，无需升级）
            mapping[session_key] = {
                "session_id": old_session_id,
                "model": saved_model,
                "permission_mode": saved_perm_mode or permission_mode,
            }
        # 惰性剪枝一次性会话死条目（修复 sched:/subagent: 等无限增长）
        if prune_ephemeral_map_entries(mapping) or (
                not resumed and not _is_ephemeral_session_key(session_key)):
            _save_map(mapping)

    # ---- 模型/推理回填（单一事实源：会话统一偏好 conversation prefs 为准）----
    # sessions_map.json 仅保留 session_id 关联（消息历史恢复）；模型/权限/推理以
    # 调用方传入的统一偏好为准（dispatcher 的 _session_prefs 即 conversation prefs），
    # 避免两套存储不同步导致"顶部显示新模型、实际用旧模型"。
    resume_model = (model or "").strip() or saved_model
    resume_reasoning = reasoning_level or saved_reasoning_level
    if resumed and (resume_model or resume_reasoning is not None):
        try:
            agent.switch_llm(model=resume_model or agent.llm.model,
                             reasoning_level=resume_reasoning)
            logger.info("恢复会话模型/推理等级: %s → %s / %s", session_key,
                        agent.llm.model, agent.llm.reasoning_level)
        except Exception as e:
            logger.warning("恢复会话回填模型 %s 失败，回落默认模型: %s",
                           resume_model, e)

    # None intentionally represents "inherit selected model configuration".
    agent._session_reasoning_override = resume_reasoning

    # ---- 权限档位（单一事实源：conversation prefs 为准，sessions_map 仅兜底）----
    # Preserve the per-session mode for the WebUI initializer. The
    # initializer installs the approval bridge and reapplies the complete
    # mode (not just ``unreviewed``) before the first tool call.
    effective_permission_mode = permission_mode or saved_perm_mode
    agent._gateway_permission_mode = effective_permission_mode

    return agent

