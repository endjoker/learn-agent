# -*- coding: utf-8 -*-
"""
C2 退役落地：sessions/*.json 旧会话转录 → 统一会话库（conversation_sessions）。

背景
----
统一会话已以 runtime.db 的 conversation_sessions 为唯一权威；旧 MessageStore
转录（sessions/*.json + gateway/sessions_map.json）已由
MessageStore.set_file_persistence(False) 停写（dispatcher 统一路径前置，
SQLite 为唯一事实源）。存量 *.json 文件保留在磁盘上，本模块提供扫描与导入：

- scan_legacy_sessions  找出未被 runtime DB conversations 覆盖的 *.json
  （按 session_id 对照 conversation_sessions.session_key，必要时读
  sessions_map.json 反查 session_key）。
- count_legacy          启动检测用：未迁移文件数量，任何异常返回 0
  （fail-closed，不阻断服务启动）。
- migrate_sessions      把 legacy transcript 导入为 origin='legacy_import'
  的 conversation_sessions + 单一 completed turn + assistant node
  （node.text = 可见文本拼接，截断 100k 字符）；幂等（session_key 已存在跳过）。

覆盖判定（session_key 由 sessions_map.json 反查 session_id 得到；映射缺失时
回退为文件 stem，即 MessageStore.save_session 的 {session_id}.json 命名）：
1. 反查得到的 session_key 已在 conversation_sessions 中 → 已覆盖；
2. 该 session_id 是某个 DB 会话 key 内嵌的 wss_* / 16 位 hex 段（如工作区会话
   workspace:ws_*:wss_xxx）→ 已覆盖；
3. 否则 → 未覆盖（待迁移）。

安全语义
--------
- 只读旧文件：迁移绝不删除/改写 sessions/*.json 与 sessions_map.json
  （退役时间表：迁移确认后由 scripts/cleanup_legacy_sessions.py 统一清理）。
- 幂等：导入前按 session_key 双重检查（scan 之外再查一次），已存在跳过；
  单文件失败不阻断其余文件，错误计入报告。
- 四档权限与 fail-closed 语义不变：本模块只做数据搬运，不涉及权限判定。
- 沙箱默认关：迁移不启动任何子进程，纯文件 + SQLite 操作。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from core.config_loader import _find_project_root, load_config
from core.runtime import RuntimeStore
from gateway.conversation.models import (
    TurnNodeType,
    TurnStatus,
    gen_conversation_id,
    gen_node_id,
    gen_turn_id,
    utc_now,
)
from gateway.conversation.store import ConversationStore

logger = logging.getLogger("jk_agent.gateway")

# 导入会话的 origin / subtype / execution_scope。
# origin='legacy_import' 为 C2 契约指定的归档来源标记；subtype 取最通用的
# ConversationSubtype.OTHER（纯字符串存储，后端不校验枚举）；execution_scope
# 取 gateway:default（非工作区/非 system 域，不参与 workspace/system 域并发计数）。
LEGACY_IMPORT_ORIGIN = "legacy_import"
LEGACY_IMPORT_SUBTYPE = "other"
LEGACY_IMPORT_EXECUTION_SCOPE = "gateway:default"

# assistant node 可见文本拼接上限（字符）
MAX_TRANSCRIPT_CHARS = 100_000

# 工作区会话 key 内嵌的会话 id 段（wss_<12+ hex>）
_WSS_SEGMENT_RE = re.compile(r"^wss_[0-9a-f]{12,}$")
# MessageStore 生成的 session_id（16 位 hex）
_HEX_ID_RE = re.compile(r"^[0-9a-f]{16}$")

DEFAULT_DB_PATH = "./workspace/.agent/state/runtime.db"
DEFAULT_SESSIONS_DIR = "sessions"
DEFAULT_MAP_FILE = "gateway/sessions_map.json"


# ============================================================
# 路径与配置解析
# ============================================================


def resolve_paths(*, project_root: Optional[Path] = None) -> Dict[str, Any]:
    """解析迁移相关路径（config.json runtime_store.path / sessions/ 等）。

    与 gateway/server.py 同一套解析规则：相对路径基于项目根展开。
    """
    root = Path(project_root) if project_root is not None else _find_project_root()
    cfg = load_config()
    store_cfg = cfg.get("runtime_store") or {}
    raw_db = str(store_cfg.get("path") or DEFAULT_DB_PATH)
    db_path = Path(raw_db)
    if not db_path.is_absolute():
        db_path = root / db_path
    return {
        "project_root": root,
        "db_path": db_path.resolve(),
        "sessions_dir": root / DEFAULT_SESSIONS_DIR,
        "map_file": root / DEFAULT_MAP_FILE,
        "store_cfg": store_cfg,
    }


# ============================================================
# 会话映射与覆盖判定
# ============================================================


def load_sessions_map(map_file) -> Dict[str, Any]:
    """读取 sessions_map.json（v2 dict 结构，兼容旧版扁平字符串值）。

    v2: {session_key: {"session_id": ..., "model": ..., "permission_mode": ...}}
    旧版: {session_key: "session_id"}
    解析失败返回空映射（不影响扫描，session_key 回退为文件 stem）。
    """
    fp = Path(map_file)
    if not fp.is_file():
        return {}
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        logger.debug("sessions_map.json 解析失败，按空映射处理: %s", fp)
        return {}


def _entry_session_id(entry: Any) -> Optional[str]:
    """从映射条目提取 session_id（v2 dict 或旧版扁平字符串）。"""
    if isinstance(entry, dict):
        sid = entry.get("session_id")
        return str(sid) if sid else None
    if isinstance(entry, str):
        return entry
    return None


def _derive_session_key(session_id: str, map_data: Dict[str, Any]) -> str:
    """按 session_id 反查 sessions_map 得到 session_key；缺失时回退文件 stem。"""
    for key, entry in map_data.items():
        if _entry_session_id(entry) == session_id:
            return key
    return session_id


def _known_covered_ids(db_keys: set, map_data: Dict[str, Any]) -> set:
    """DB 会话 key 已隐含覆盖的 session_id 集合。

    - sessions_map 中 session_key 已存在于 DB 的条目 → 其 session_id 已覆盖；
    - DB key 内嵌的 wss_* / 16 位 hex 段（工作区会话 wss_xxx）→ 已覆盖。
    """
    ids = set()
    for key in db_keys:
        for seg in str(key).split(":"):
            if _WSS_SEGMENT_RE.match(seg) or _HEX_ID_RE.match(seg):
                ids.add(seg)
        sid = _entry_session_id(map_data.get(key))
        if sid:
            ids.add(sid)
    return ids


def _read_db_session_keys(db_path: Path) -> set:
    """只读方式读取 conversation_sessions 的全部 session_key（sqlite3 RO 直连）。

    不打开 RuntimeStore（避免启动检测路径触发 schema 迁移等副作用）；
    WAL 模式下并发读安全。文件不存在 / 表缺失 / 其他异常 → 空集合。
    """
    if not Path(db_path).is_file():
        return set()
    try:
        conn = sqlite3.connect(
            f"file:{Path(db_path)}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT session_key FROM conversation_sessions").fetchall()
            return {str(r[0]) for r in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("读取 conversation_sessions 失败（按空集合处理）: %s", db_path)
        return set()


# ============================================================
# 扫描
# ============================================================


def scan_legacy_sessions(
    sessions_dir=None, *, db_path=None, map_file=None
) -> List[Dict[str, Any]]:
    """找出未被 runtime DB conversations 覆盖的 sessions/*.json。

    返回按文件名排序的记录列表，每条：
        filepath / session_id / session_key / covered / covered_reason /
        parse_error（None 或错误信息）。
    """
    paths = resolve_paths()
    sessions_dir = Path(sessions_dir) if sessions_dir else Path(paths["sessions_dir"])
    db_path = Path(db_path) if db_path else Path(paths["db_path"])
    map_file = Path(map_file) if map_file else Path(paths["map_file"])

    db_keys = _read_db_session_keys(db_path)
    map_data = load_sessions_map(map_file)
    covered_ids = _known_covered_ids(db_keys, map_data)

    records: List[Dict[str, Any]] = []
    if not sessions_dir.is_dir():
        logger.debug("会话目录不存在，跳过扫描: %s", sessions_dir)
        return records
    for fp in sorted(sessions_dir.glob("*.json")):
        if fp.name.startswith("."):  # 跳过原子写临时文件 .*.json
            continue
        records.append(_scan_one(fp, map_data, db_keys, covered_ids))
    return records


def _scan_one(fp: Path, map_data: Dict[str, Any], db_keys: set,
              covered_ids: set) -> Dict[str, Any]:
    session_id = fp.stem
    parse_error: Optional[str] = None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("session_id"):
            session_id = str(data["session_id"])
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        parse_error = str(exc)

    session_key = _derive_session_key(session_id, map_data)
    covered, reason = False, ""
    if session_key in db_keys:
        covered, reason = True, "db_session_key"
    elif session_id in covered_ids:
        covered, reason = True, "covered_session_id"
    return {
        "filepath": str(fp),
        "session_id": session_id,
        "session_key": session_key,
        "covered": covered,
        "covered_reason": reason,
        "parse_error": parse_error,
    }


def count_legacy(sessions_dir=None, *, db_path=None, map_file=None) -> int:
    """未迁移的旧会话文件数量（server 启动检测用）。

    fail-closed：任何异常返回 0（不阻断服务启动），失败细节仅记 debug 日志。
    """
    try:
        records = scan_legacy_sessions(
            sessions_dir, db_path=db_path, map_file=map_file)
        return sum(1 for r in records if not r["covered"])
    except Exception:
        logger.debug("count_legacy 检查失败（按 0 处理，不阻断启动）",
                     exc_info=True)
        return 0


# ============================================================
# 可见文本拼接
# ============================================================


def _message_visible_text(msg: Dict[str, Any]) -> str:
    """单条 legacy message 的可见文本（role 前缀 + 文本/tool_calls 摘要）。"""
    role = str(msg.get("role") or "")
    parts: List[str] = []
    content = msg.get("content")
    if isinstance(content, str):
        if content:
            parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif btype == "image_url":
                parts.append("[image]")
            elif btype == "input_audio":
                parts.append("[audio]")
            # 其他块类型（二进制等）无可见文本，跳过
    for tc in (msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        args = fn.get("arguments") or ""
        parts.append(f"[tool_call: {name}({args})]")
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        return ""
    return f"{role}: {text}" if role else text


def _build_transcript_text(data: Dict[str, Any]) -> tuple:
    """legacy transcript 可见文本拼接，截断 MAX_TRANSCRIPT_CHARS。

    返回 (text, truncated)。截断取头部 100k 字符（保留会话起点，
    丢失尾部由 metadata.legacy_import.truncated 标记）。
    """
    lines: List[str] = []
    for msg in (data.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        line = _message_visible_text(msg)
        if line:
            lines.append(line)
    text = "\n\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        return text[:MAX_TRANSCRIPT_CHARS], True
    return text, False


# ============================================================
# 迁移
# ============================================================


class _DbAdapter:
    """最小 db 适配器（connection/transaction），复用 RuntimeStore 的
    SQLite 连接配置，避免引入 gateway.webui 依赖链。"""

    def __init__(self, runtime: RuntimeStore):
        self._runtime = runtime

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._runtime.connection() as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._runtime.connection() as connection:
            yield connection


def _legacy_metadata(session_id: str, filepath: str, data: Dict[str, Any],
                     truncated: bool) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "file": Path(filepath).name,
        "model_id": str(data.get("model_id") or ""),
        "message_count": int(data.get("message_count") or 0),
        "schema_version": data.get("schema_version"),
        "created_at": str(data.get("created_at") or ""),
        "truncated": bool(truncated),
    }


def _import_one(store: ConversationStore, session_key: str, session_id: str,
                filepath: str, data: Dict[str, Any]) -> tuple:
    """导入单个 legacy transcript（幂等）。返回 (status, detail)。

    status: "migrated" | "skipped"。异常向上抛出，由调用方计入报告。
    """
    # 幂等双重检查：scan 之后、导入之前再确认一次（防并发/重复执行）
    if store.get_conversation_by_key(session_key) is not None:
        return "skipped", "session_key 已存在（幂等跳过）"

    text, truncated = _build_transcript_text(data)
    meta = _legacy_metadata(session_id, filepath, data, truncated)
    now = utc_now()
    conv = None
    try:
        conv, created = store.create_conversation(
            session_key,
            origin=LEGACY_IMPORT_ORIGIN,
            subtype=LEGACY_IMPORT_SUBTYPE,
            execution_scope=LEGACY_IMPORT_EXECUTION_SCOPE,
            route_metadata={"legacy_import": meta},
        )
        if not created:
            return "skipped", "session_key 已存在（幂等跳过）"
        with store.transaction() as conn:
            turn = store.create_turn(
                conn, conv.conversation_id, status=TurnStatus.DONE.value)
            node = store.create_node(
                conn,
                conversation_id=conv.conversation_id,
                type=TurnNodeType.ASSISTANT.value,
                status="done",
                turn_id=turn.turn_id,
                position=1,
                text=text,
                metadata={"legacy_import": meta},
            )
            store.update_turn_status(
                conn, turn.turn_id, TurnStatus.DONE.value,
                finished_at=now, final_assistant_node_id=node.node_id)
        return ("migrated",
                f"conversation={conv.conversation_id} turn={turn.turn_id} "
                f"node={node.node_id} chars={len(text)}")
    except Exception:
        # 清理半成品 conversation（turn/node 所在事务已回滚）
        if conv is not None:
            try:
                with store.transaction() as conn:
                    store.delete_conversation(conn, conv.conversation_id)
            except Exception:
                pass
        raise


def migrate_sessions(dry_run: bool = False, *, sessions_dir=None,
                     db_path=None, map_file=None) -> Dict[str, Any]:
    """把未覆盖的 legacy transcript 导入统一会话库（幂等）。

    dry_run=True 只扫描与报告（不打开写库、不落任何数据）。
    返回报告 dict：dry_run / db_path / sessions_dir / scanned / covered /
    pending / migrated / skipped / errors / items[]。
    """
    paths = resolve_paths()
    sessions_dir = Path(sessions_dir) if sessions_dir else Path(paths["sessions_dir"])
    db_path = Path(db_path) if db_path else Path(paths["db_path"])
    map_file = Path(map_file) if map_file else Path(paths["map_file"])

    store = None
    runtime = None
    if not dry_run:
        try:
            busy_timeout_ms = int(paths["store_cfg"].get("busy_timeout_ms", 5000))
        except (TypeError, ValueError):
            busy_timeout_ms = 5000
        runtime = RuntimeStore(
            db_path,
            wal=bool(paths["store_cfg"].get("wal", True)),
            busy_timeout_ms=int(paths["store_cfg"].get("busy_timeout_ms", 5000)),
        )
        store = ConversationStore(_DbAdapter(runtime))

    report: Dict[str, Any] = {
        "dry_run": bool(dry_run),
        "db_path": str(db_path),
        "sessions_dir": str(sessions_dir),
        "scanned": 0,
        "covered": 0,
        "pending": 0,
        "migrated": 0,
        "skipped": 0,
        "errors": 0,
        "items": [],
    }
    try:
        records = scan_legacy_sessions(
            sessions_dir, db_path=db_path, map_file=map_file)
        report["scanned"] = len(records)
        for rec in records:
            item = {
                "filepath": rec["filepath"],
                "session_id": rec["session_id"],
                "session_key": rec["session_key"],
                "status": "",
                "detail": "",
            }
            if rec["covered"]:
                report["covered"] += 1
                item["status"] = "covered"
                item["detail"] = f"已覆盖（{rec['covered_reason']}）"
                report["items"].append(item)
                continue
            report["pending"] += 1
            if rec["parse_error"]:
                report["errors"] += 1
                item["status"] = "error"
                item["detail"] = f"JSON 解析失败: {rec['parse_error']}"
                report["items"].append(item)
                continue
            if dry_run:
                report["migrated"] += 1
                item["status"] = "pending"
                item["detail"] = "待迁移（dry-run，未写库）"
                report["items"].append(item)
                continue
            try:
                with open(rec["filepath"], "r", encoding="utf-8") as f:
                    data = json.load(f)
                status, detail = _import_one(
                    store, rec["session_key"], rec["session_id"],
                    rec["filepath"], data)
                if status == "migrated":
                    report["migrated"] += 1
                else:
                    report["skipped"] += 1
                item["status"] = status
                item["detail"] = detail
            except Exception as exc:
                report["errors"] += 1
                item["status"] = "error"
                item["detail"] = f"导入失败: {exc}"
            report["items"].append(item)
    finally:
        if runtime is not None:
            runtime.close()
    return report