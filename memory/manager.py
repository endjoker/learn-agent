# -*- coding: utf-8 -*-
"""
记忆管理器 —— 跨会话长期记忆的核心

目录结构：
    memory/
        config.json              # 系统配置
        daily/
            index.json           # 对话索引 [{id, date, user_call, weight, md}]
            2026-07-12.md        # 按日归档的详细记忆

线程安全：search 持读锁（可并发），save_conversation/update_weight 持写锁；
index.json 解析结果、分词语料与 BM25 对象在进程内缓存，
仅在 index.json mtime / daily 目录签名（mtime 集合）变化时重建；
搜索命中的权重 +1 延迟批量写回（阈值触发或 5s 定时落盘）。

条目块格式（P1-8）：daily .md 中 user_call 字段以 JSON 编码写入
（json.dumps ensure_ascii=False，杜绝内容伪造 `---` 块结构）；
解析侧兼容旧格式裸文本（_parse_entry_value）。daily .md 的读-改-写
全程与 index.json 一样用 fcntl.flock 跨进程互斥（P2-4）；入库前
user_call 与 messages 统一过 guard.sanitize_output 凭据脱敏（P2-3）。

2026-08 形态改造 + 保留期：
- 内容：只入 [user] 提问、[assistant] 终答（kind=final）、[tool:*] 结果
  短桩；过程旁白/system/内部注入一律不入——全量转录权威在 SQLite 统一会话，
  记忆层是检索索引 + 人工速览，单条 ≤2000 字符、条目块 ≤8KB。
- 清理：≥15 天负分、≥30 天零分删除；正分永久；每日至多自动一次
  （config.json.last_cleanup_date 记账），日期解析失败 fail-closed 保留。
"""


import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from rank_bm25 import BM25Okapi
import jieba

from core.atomic_io import atomic_write_bytes, atomic_write_json
from core.message_store import _content_to_text
# P2-3 入库脱敏：复用 core.sandbox.guard 的 SECRET_PATTERNS / sanitize_output
# （与 bash 工具输出脱敏同一套凭据正则：sk- / AKIA / AIza / ghp_ / xox*- /
# ya29. / 私钥块 / api_key|secret|password|token 赋值），避免复制实现产生漂移。
from core.sandbox.guard import sanitize_output

# 跨进程文件锁（POSIX）：多实例共享同一 memory 目录时，index.json 写入
# 用 fcntl.flock 互斥，防止两个进程并发 os.replace 造成交错/半截文件。
# 非 POSIX 平台（无 fcntl）退化为纯原子写（单实例语义）。
try:
    import fcntl
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _HAVE_FCNTL = False

logger = logging.getLogger("jk_agent.memory")

# ---- 2026-08 形态改造：记忆条目容量预算 -----------------------------------
# 单条可见文本上限（[user]/[assistant] 行）：SQLite 统一会话是全量转录唯一
# 权威，记忆层定位为检索索引 + 人工速览，不再保存全文。
_MSG_TEXT_LIMIT = 2000
# 工具结果短桩：常规结果截 160 字符；报错类（❌/⛔/Traceback/Error…）放宽到
# 400——报错原文恰是事后最常回忆的内容，且通常头部即含关键信息。
_TOOL_STUB_MAX = 160
_TOOL_STUB_ERROR_MAX = 400
# 单条目 message 块总量预算（UTF-8 字节）：同会话同天 upsert 全量覆写，
# 无预算会让条目随轮次无限膨胀（实测曾出现单文件 52KB）。
_BLOCK_BUDGET_BYTES = 8192

# ---- 保留期清理规则（与用户对齐，2026-08）---------------------------------
# - ≥15 天且 weight<0      → 删除（含 ≤-5 的软删除态，磁盘一并回收）
# - ≥30 天且 weight==0     → 删除（从未被检索命中过的沉睡条目）
# - ≥30 天且 weight>0      → 永久保留（被证明有用的工作集不设上限）
# - <15 天                  → 全部保留（观察期）
# 配合 search 命中自动 +1 的语义：一条记忆必须在窗口期内被用过一次才能活，
# 记忆系统由"只增不减的档案库"转为"用进废退的工作集"。
_CLEAN_NEGATIVE_DAYS = 15
_CLEAN_ZERO_DAYS = 30


def _tokenize(text: str) -> List[str]:
    """
    分词器：支持中英文混合文本
    - 中文：jieba 分词
    - 英文：按空格/标点拆分
    - 数字和字母保留原样
    """
    return list(jieba.cut(text))


def _sanitize_message_for_store(msg):
    """P2-3：对单条 message 的 content 做入库脱敏（复用 guard.sanitize_output）。

    content 统一经 _content_to_text 转纯文本后打码（与 _serialize_messages 的
    读取路径一致，落盘语义不变）；非 dict 消息原样返回。
    """
    if isinstance(msg, dict):
        sanitized = dict(msg)
        sanitized["content"] = sanitize_output(_content_to_text(msg.get("content", "")))
        return sanitized
    return msg


def _encode_entry_value(value: str) -> str:
    """P1-8a 写入侧编码：JSON 编码（ensure_ascii=False）后嵌入条目块。

    json.dumps 输出自带成对引号且会转义裸换行/引号/反斜杠，
    杜绝 user_call 内容伪造 `---` 分隔符、`ID:` 行等块结构。
    """
    return json.dumps(value, ensure_ascii=False)


def _decode_entry_value(raw: str) -> Optional[str]:
    """P1-8b 解析侧：尝试把条目字段值当 JSON 字符串解码（新格式）。

    成功返回解码后的原文；返回 None 表示不是合法 JSON 字符串
    （即旧格式存量块的裸文本），由调用方按旧格式回退处理。
    """
    s = raw.strip()
    if s.startswith('"'):
        try:
            v = json.loads(s)
            if isinstance(v, str):
                return v
        except ValueError:
            pass
    return None


def _parse_entry_value(raw: str) -> str:
    """P1-8b 解析侧统一入口：优先 JSON 解码，失败则按旧格式原样使用。

    - 新格式：值为 _encode_entry_value 的 json.dumps 产物，解码还原原文；
    - 旧格式（存量文件）：裸文本外面包一层引号 → 剥掉最外层一对引号；
      连引号都不完整（如旧多行值的中间片段）时原样返回。
    """
    decoded = _decode_entry_value(raw)
    if decoded is not None:
        return decoded
    s = raw.strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


class _RWLock:
    """读写锁：读读并发、读写/写写互斥（非重入）。

    - read(): 多个读者可同时持有
    - write(): 独占，等待所有读者释放；等待中的写者会阻止新读者进入
      （写优先，避免读者饥饿）

    用于将 search（读）与 save_conversation/update_weight（写）的锁分离。
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._write_waiters = 0

    @contextmanager
    def read(self):
        with self._cond:
            while self._writer or self._write_waiters:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def write(self):
        with self._cond:
            self._write_waiters += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
                self._writer = True
            finally:
                self._write_waiters -= 1
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


def _validate_workspace_id(workspace_id: str) -> None:
    """校验 workspace_id，防止路径穿越。"""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace_id 不能为空")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if set(workspace_id) - allowed:
        raise ValueError(f"非法的 workspace_id: {workspace_id!r}")


def workspace_memory_dir(workspace_id: str) -> Path:
    """返回工作区专用长期记忆目录：memory/workspaces/<workspace_id>。"""
    _validate_workspace_id(workspace_id)
    root = Path(__file__).resolve().parent / "workspaces"
    return root / workspace_id


def delete_workspace_memory(workspace_id: str) -> bool:
    """安全删除工作区长期记忆目录（仅允许删除 memory/workspaces/<workspace_id>）。"""
    _validate_workspace_id(workspace_id)
    root = (Path(__file__).resolve().parent / "workspaces").resolve()
    target = workspace_memory_dir(workspace_id).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"拒绝删除记忆目录边界之外的路径: {target}")
    if target.exists():
        shutil.rmtree(target)
        return True
    return False


class MemoryManager:
    """跨会话记忆管理器"""

    # 形态改造常量（模块级定义，此处类别名供 self.* 引用）
    _MSG_TEXT_LIMIT = _MSG_TEXT_LIMIT
    _TOOL_STUB_MAX = _TOOL_STUB_MAX
    _TOOL_STUB_ERROR_MAX = _TOOL_STUB_ERROR_MAX

    def __init__(self, memory_dir: str = ""):
        if memory_dir:
            self._memory_dir = Path(memory_dir).resolve()
        else:
            # 基于源文件定位，不受 os.chdir 影响
            self._memory_dir = Path(__file__).resolve().parent
        self._daily_dir = self._memory_dir / "daily"
        self._index_path = self._daily_dir / "index.json"
        self._config_path = self._memory_dir / "config.json"

        # 读写锁：search 走读锁（可并发），save_conversation/update_weight 走写锁
        self._rwlock = _RWLock()

        # ---- 进程内缓存（B7：BM25 缓存化）----
        # 缓存键 = index.json mtime + daily 目录 .md 文件的 (name, mtime_ns) 集合
        self._cache_signature: Optional[tuple] = None
        self._cached_index: Optional[List[Dict]] = None        # 解析后的 index.json
        self._cached_tokens_by_id: Dict[int, List[str]] = {}   # 分词语料（id -> tokens）
        self._cached_summaries: Dict[int, str] = {}            # 摘要缓存（免反复读 .md）
        self._cached_bm25: Optional[object] = None             # BM25Okapi 对象
        self._cached_bm25_ids: frozenset = frozenset()         # BM25 覆盖的可见条目 id

        # ---- 权重延迟批量写回（B7）----
        self._pending_deltas: Dict[int, int] = {}   # {id: delta}，仅作触发/记账
        self._dirty = False                         # 内存 index 与磁盘不一致
        self._flush_threshold = 32                  # pending 达到阈值立即落盘
        self._flush_interval = 5.0                  # 定时落盘周期（秒）
        self._last_flush_ts = time.monotonic()
        self._flush_thread: Optional[threading.Thread] = None
        # P3-8：懒启动定时线程的互斥锁（check-then-act 无锁会起两个定时线程）
        self._flush_thread_lock = threading.Lock()
        self._stop_flush = threading.Event()

        self._ensure_initialized()

    # ============================================================
    # 初始化
    # ============================================================

    def _ensure_initialized(self):
        """确保目录结构和初始化文件存在"""
        self._daily_dir.mkdir(parents=True, exist_ok=True)

        # config.json
        if not self._config_path.exists():
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": 1,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }, f, ensure_ascii=False, indent=2)

        # index.json（文件锁 + 原子写，跨进程互斥）
        if not self._index_path.exists():
            self._locked_index_write([])

    @property
    def is_available(self) -> bool:
        return self._memory_dir.exists()

    # ============================================================
    # 保存 —— 每轮对话结束时自动调用（同会话同天合并到一条记忆）
    # ============================================================

    def save_conversation(
        self,
        user_call: str,
        messages: List[Dict],
        session_id: str,
    ) -> int:
        """
        保存/更新一轮对话到记忆

        同会话同一天的多轮对话合并到同一条记忆条目（upsert 语义）：
        - 首次 → 新建条目 + 用户消息设为 user_call
        - 续对话 → 更新已有条目（user_call 拼接新消息，message 全量覆写）

        日期变动时自动新建条目。

        返回:
            记忆条目 ID
        """
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%Y-%m-%d %H:%M")

        # P2-3 入库脱敏：index.json 与 daily .md 落盘前统一打码凭据
        # （user_call 与 messages 内容都过 sanitize_output，先脱敏再截断）
        user_call = sanitize_output(user_call if isinstance(user_call, str) else "")
        messages = [_sanitize_message_for_store(m) for m in messages]

        with self._rwlock.write():
            # 外部文件变更检测（写锁上下文内）
            self._ensure_fresh_locked()
            index = self._load_index()

            # 查找已有条目：(session_id + date) 匹配
            existing = None
            for entry in index:
                if entry.get("session_id") == session_id and entry.get("date") == date_str:
                    existing = entry
                    break

            if existing:
                # ====== 更新已有条目 ======
                entry_id = existing["id"]

                # 拼接 user_call
                old_call = existing.get("user_call", "")
                new_call = user_call[:1000]
                if old_call and new_call not in old_call:
                    existing["user_call"] = f"{old_call} | {new_call}"
                elif not old_call:
                    existing["user_call"] = new_call

                self._save_index(index)

                # 替换 daily .md 中的块
                block = self._build_entry_block(
                    entry_id=entry_id,
                    time_str=time_str,
                    session_id=session_id,
                    user_call=existing["user_call"],
                    messages=messages,
                )
                self._update_or_append_daily(date_str, entry_id, block)

            else:
                # ====== 新建条目 ======
                next_id = max((e.get("id", 0) for e in index), default=0) + 1

                index.append({
                    "id": next_id,
                    "session_id": session_id,
                    "date": date_str,
                    "user_call": user_call[:1000],
                    "weight": 0,
                    "md": f"daily/{date_str}.md",
                })
                self._save_index(index)

                block = self._build_entry_block(
                    entry_id=next_id,
                    time_str=time_str,
                    session_id=session_id,
                    user_call=user_call,
                    messages=messages,
                )
                self._update_or_append_daily(date_str, next_id, block)
                entry_id = next_id

            # 增量刷新该条目的语料缓存（避免全量重建），保持缓存键一致
            self._refresh_corpus_for_entry(entry_id)
            # 本次保存已同步落盘：内存与磁盘一致，清空延迟写回标记
            self._pending_deltas.clear()
            self._dirty = False

        # C4：保留期清理搭归档便车，每天至多一次（闸门记账在 config.json）
        self._maybe_auto_cleanup(now)

        return entry_id

    def _build_entry_block(
        self,
        entry_id: int,
        time_str: str,
        session_id: str,
        user_call: str,
        messages: List[Dict],
    ) -> str:
        """
        构建 --- 分隔的记忆条目块

        message 包含本轮之前所有轮次的完整对话（全量覆写，非追加）。

        P1-8：user_call 先 JSON 编码再嵌入——裸文本内插会让含 `---` /
        引号 / 换行的用户输入伪造块结构（解析侧用 _decode_entry_value 还原，
        兼容旧格式裸文本）。
        """
        msg_text = self._serialize_messages(messages)
        return (
            f"---\n"
            f"ID: \"{entry_id}\"\n"
            f"日期: \"{time_str}\"\n"
            f"session_id: \"{session_id}\"\n"
            f"user_call: {_encode_entry_value(user_call)}\n"
            f"weight: \"0\"\n"
            f"message: |\n"
            f"  {msg_text.replace(chr(10), chr(10) + '  ')}\n"
            f"---\n\n"
        )

    @staticmethod
    def _is_error_text(text: str) -> bool:
        """判断工具结果是否为报错类（短桩长度放宽的依据）。"""
        head = text.lstrip()[:16].lower()
        return text.startswith(("❌", "⛔", "⏭️")) or head.startswith(
            ("traceback", "error", "fatal", "exception"))

    @staticmethod
    def _truncate_tail(text: str, limit: int) -> str:
        """尾部省略式截断（保头部：命令行/文件名/关键结论多在开头）。"""
        if len(text) <= limit:
            return text
        if limit < 80:
            return text[:limit]
        head = max(1, int(limit * 0.85))
        return text[:head] + f"……[截断，省略 {len(text) - head} 字符]"

    def _serialize_messages(self, messages: List[Dict]) -> str:
        """
        将本轮消息序列化为检索友好的紧凑档案（2026-08 形态改造）。

        保留：
        - [user] 用户提问（单条 ≤_MSG_TEXT_LIMIT）
        - [assistant] 仅终答（kind="final"；≤_MSG_TEXT_LIMIT）
        - [tool:<名>] 工具结果单行短桩（常规 ≤160 / 报错类 ≤400 字符）——
          补上"助手未复述的关键事实"这一检索盲区（报错原文、配置值等）

        不入库：
        - system prompt / 内部注入（history_summary 等 internal 标记）
        - assistant 过程旁白（kind="tool_calls" 的原生 tool-call 载体与
          历史 intermediate 标记）——全量转录权威在 SQLite 统一会话，
          记忆层只留结论；且同会话同天为全量覆写合并，旁白不弃会让条目
          随轮次无限膨胀（实测曾出现单文件 52KB）
        - （历史）ACTION/INPUT 行过滤已删：旧 ReAct 文本协议遗产，原生
          tool-call 循环不产出该格式，反而会误伤以"输入："开头的正常回答
        - （历史）反斜杠双重转义 hack 已删：无任何反转义读取方，只会让
          存储出现 D:\\path 假象
        """
        parts: List[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            name = msg.get("name", "")

            if role == "system":
                continue
            if msg.get("internal"):
                continue

            content = _content_to_text(msg.get("content", ""))
            # 单行化在纯文本域完成；多模态 content 已由 _content_to_text 提取

            if role == "tool" or (role == "user" and name == "tool_result"):
                stub = " ".join(content.split())
                if not stub:
                    continue
                limit = (self._TOOL_STUB_ERROR_MAX if self._is_error_text(stub)
                         else self._TOOL_STUB_MAX)
                label = name if (role == "tool" and name) else (
                    "" if name == "tool_result" else name) or "tool"
                parts.append(f"[tool:{label}] {self._truncate_tail(stub, limit)}")
                continue

            if role == "assistant":
                # A1：只保留终答；过程旁白（原生 tool-call 载体/中间标记）不入库
                if msg.get("tool_calls") or msg.get("kind") == "tool_calls":
                    continue
                kind = msg.get("kind")
                if kind not in (None, "", "final"):
                    continue
                if not content.strip():
                    continue
                parts.append(f"[assistant] "
                             f"{self._truncate_tail(content, self._MSG_TEXT_LIMIT)}")
                continue

            # 其余角色（user 等）：保留但同样限长
            if not content.strip():
                continue
            parts.append(f"[{role}] "
                         f"{self._truncate_tail(content, self._MSG_TEXT_LIMIT)}")

        return self._enforce_block_budget(parts)

    @staticmethod
    def _enforce_block_budget(parts: List[str]) -> str:
        """A3：单条目 message 块总量预算（UTF-8 字节近似），超限尾部截断。

        从前往后累计（最早的轮次建立上下文，优先保留）；溢出的剩余部分以
        一行标记收尾，绝不产出无限膨胀的条目。
        """
        budget = _BLOCK_BUDGET_BYTES
        used = 0
        kept: List[str] = []
        for part in parts:
            cost = len(part.encode("utf-8")) + 1
            if used + cost > budget and kept:
                kept.append("……[本条目超出记忆块预算，后续轮次未归档]")
                break
            if cost > budget:
                part = part[:budget // 3] + "……[超长]"
                cost = len(part.encode("utf-8")) + 1
            kept.append(part)
            used += cost
        return "\n".join(kept)

    # ============================================================
    # 搜索
    # ============================================================

    def search(self, query: str, date: str = "", limit: int = 5) -> List[Dict]:
        """
        按关键词和/或日期搜索记忆

        使用 BM25 算法（主流搜索引擎核心算法）替代子串匹配：
        - 自动处理词频（TF）：高频词重要性更高
        - 文档长度归一化：短文匹配更精准
        - 逆文档频率（IDF）：常见词权重自动降低
        - 不要求连续匹配："学习计划 AI" 能匹配 "AI学习"、"计划"

        筛选规则：
        - weight < -5 的记忆被隐藏（软删除），不返回给 LLM
        - 按综合优先级排序：BM25 得分 + 权重 + 时间

        命中后自动给匹配的记忆增加权重（+1）。
        """
        self._refresh_if_stale()

        with self._rwlock.read():
            index = self._load_index()

            if not index:
                return []

            # 1. 日期 + 隐藏低权重 预过滤（语义不变）
            candidates = []
            for entry in index:
                if entry.get("weight", 0) < -5:
                    continue
                if date and date not in entry.get("date", ""):
                    continue
                candidates.append(entry)

            if not candidates:
                return []

            # 2. BM25 评分（对 user_call + message 摘要联合检索，支持中英文）
            #    缓存命中时零分词、零重建；仅日期筛选/可见集合变化时用缓存分词重建
            if query:
                scores = self._score_candidates(candidates, query)
                if scores is None:
                    return []  # 空语料兜底：无有效分词时返回空，避免 BM25 除零
                # 全零分：查询词与语料无任何匹配，不返回结果、不增加权重（语义不变）
                if not any(scores):
                    return []
                # 得分按 id 存本地字典，不写入 index 条目（避免 _bm25_score 被持久化）
                scores_by_id = {
                    e.get("id"): s for e, s in zip(candidates, scores)
                }
            else:
                # 无查询词时全部视为命中
                scores_by_id = {}

            # 3. 综合排序：BM25 得分 + 权重 + 时间（日期项归一化降权，语义不变）
            candidates.sort(
                key=lambda e: self._priority(e, scores_by_id),
                reverse=True,
            )

            # 4. 取 top N
            matched = candidates[:limit]

        # 5. 权重 +1（写锁短临界区，延迟批量落盘）+ 构建输出
        return self._apply_hits(matched)

    def _score_candidates(self, candidates: List[Dict], query: str):
        """对候选集做 BM25 评分；返回 scores 列表或 None（无有效语料）。

        - 无日期筛选且可见集合未变化：直接复用缓存 BM25（零分词、零重建）
        - 日期筛选/可见集合变化：用缓存分词构建子集 BM25（不重新分词，
          与旧实现"按候选集构建 BM25"的语义一致）
        """
        ids = {e.get("id") for e in candidates}
        if ids == self._cached_bm25_ids and self._cached_bm25 is not None:
            bm25 = self._cached_bm25
        else:
            subset_tokens = [
                self._cached_tokens_by_id.get(e.get("id")) or []
                for e in candidates
            ]
            # 空语料兜底：无有效分词时返回 None，避免 BM25 除零
            if not subset_tokens or not any(subset_tokens):
                return None
            try:
                bm25 = BM25Okapi(subset_tokens)
            except (ZeroDivisionError, ValueError):
                return None
        try:
            return bm25.get_scores(_tokenize(query))
        except (ZeroDivisionError, ValueError):
            return None

    @staticmethod
    def _priority(entry: Dict, scores_by_id: Dict) -> float:
        """综合优先级：BM25 得分 + 权重 + 时间（日期项归一化，语义不变）。"""
        bm25 = scores_by_id.get(entry.get("id"), 1.0)
        try:
            date_num = int(str(entry.get("date", "")).replace("-", ""))
        except (TypeError, ValueError):
            date_num = 0
        # 日期项除以 1e8 归一化到 ~0.2 以内，只作同分时的微弱新鲜度偏好
        return bm25 * 10 + entry.get("weight", 0) + date_num / 1e8

    def _apply_hits(self, matched: List[Dict]) -> List[Dict]:
        """命中条目权重 +1（延迟批量写回）并构建输出（写锁短临界区）。"""
        if not matched:
            return []
        matched_ids = {e.get("id") for e in matched}
        with self._rwlock.write():
            index = self._cached_index
            if index is None:
                return []
            bumped = {}
            for entry in index:
                if entry.get("id") in matched_ids:
                    entry["weight"] = entry.get("weight", 0) + 1
                    bumped[entry["id"]] = entry["weight"]
                    self._pending_deltas[entry["id"]] = (
                        self._pending_deltas.get(entry["id"], 0) + 1
                    )
            self._dirty = True
            # 输出权重取加 1 后的值（语义同旧实现）；摘要走缓存，零文件 IO
            output = []
            for m in matched:
                mid = m.get("id")
                output.append({
                    "id": mid,
                    "date": m.get("date"),
                    "user_call": m.get("user_call"),
                    "weight": bumped.get(mid, m.get("weight", 0)),
                    "summary": self._cached_summaries.get(mid, ""),
                })
        self._mark_pending_and_maybe_flush()
        return output

    # ============================================================
    # 权重延迟批量写回（B7）：阈值触发或 5s 定时落盘
    # ============================================================

    def _mark_pending_and_maybe_flush(self):
        """有未落盘变更时确保定时线程在跑；达到阈值立即落盘。"""
        if self._pending_deltas:
            self._start_flush_thread()
        self._maybe_flush()

    def _maybe_flush(self):
        if len(self._pending_deltas) >= self._flush_threshold:
            self._flush()

    def _start_flush_thread(self):
        """启动定时落盘线程（懒启动，daemon 不阻塞进程退出）。

        P3-8：check-then-act 全程持锁，且先把引用放入 _flush_thread 再 start，
        保证并发调用只可能创建一个定时线程。
        """
        with self._flush_thread_lock:
            if self._flush_thread is None:
                thread = threading.Thread(
                    target=self._flush_loop,
                    name="memory-index-flush",
                    daemon=True,
                )
                self._flush_thread = thread
                thread.start()

    def _flush_loop(self):
        while not self._stop_flush.wait(self._flush_interval):
            try:
                self._flush()
            except Exception:
                # 落盘失败不中断定时循环，下个周期重试
                pass

    def _flush(self):
        """批量落盘：将内存权威 index 原子写回（无变更时跳过，幂等）。"""
        if not self._dirty:
            return
        with self._rwlock.write():
            if not self._dirty or self._cached_index is None:
                return
            self._save_index(self._cached_index)
            self._dirty = False
            self._pending_deltas.clear()
            self._last_flush_ts = time.monotonic()

    def flush(self):
        """立即落盘所有未写回的权重变更（幂等，可随时调用）。"""
        self._flush()

    def close(self):
        """停止定时落盘线程并立即落盘（进程退出前调用）。"""
        self._stop_flush.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=5)
        self._flush()

    # ============================================================
    # 进程内缓存（B7：BM25 缓存化）
    # ============================================================

    def _refresh_if_stale(self):
        """外部文件变更检测：缓存键（mtime 签名）或可见集合变化时重建（双检锁）。

        search / get_stats 等只持读锁的路径在进入临界区前调用。
        """
        sig = self._index_signature()
        if (self._cached_index is not None
                and sig == self._cache_signature
                and not self._bm25_needs_rebuild()):
            return
        with self._rwlock.write():
            sig = self._index_signature()
            if self._cached_index is None or sig != self._cache_signature:
                self._rebuild_cache_from_disk(sig)
            elif self._bm25_needs_rebuild():
                self._rebuild_bm25()

    def _ensure_fresh_locked(self):
        """写锁上下文内保证缓存与磁盘一致（外部文件变更检测）。"""
        sig = self._index_signature()
        if self._cached_index is None or sig != self._cache_signature:
            self._rebuild_cache_from_disk(sig)
        elif self._bm25_needs_rebuild():
            self._rebuild_bm25()

    def _index_signature(self):
        """缓存键：index.json mtime + daily 目录 .md 文件的 (name, mtime_ns) 集合。"""
        try:
            index_st = self._index_path.stat()
        except OSError:
            return None
        try:
            md_sigs = tuple(sorted(
                (p.name, p.stat().st_mtime_ns)
                for p in self._daily_dir.glob("*.md")
            ))
        except OSError:
            md_sigs = ()
        return (index_st.st_mtime_ns, md_sigs)

    def _rebuild_cache_from_disk(self, sig=None):
        """从磁盘全量重建缓存：解析 index.json + 分词语料 + 摘要 + BM25。"""
        entries = self._parse_index_file()
        # 重新应用未落盘的权重增量，避免外部变更吞掉延迟写入
        if self._pending_deltas:
            for e in entries:
                delta = self._pending_deltas.get(e.get("id"))
                if delta:
                    e["weight"] = e.get("weight", 0) + delta
        self._cached_index = entries
        self._rebuild_corpus(entries)
        self._cache_signature = sig if sig is not None else self._index_signature()

    def _rebuild_corpus(self, entries: List[Dict]):
        """全量重建分词语料 + 摘要缓存 + BM25（按日单次读 .md 文件）。"""
        summaries: Dict[int, str] = {}
        tokens_by_id: Dict[int, List[str]] = {}
        by_date: Dict[str, List[Dict]] = {}
        for e in entries:
            by_date.setdefault(e.get("date", ""), []).append(e)
        for date_str, day_entries in by_date.items():
            day_summaries = self._read_daily_summaries(date_str)
            for e in day_entries:
                eid = e.get("id")
                summary = day_summaries.get(eid) or ""
                summaries[eid] = summary
                tokens_by_id[eid] = _tokenize(f"{e.get('user_call', '')} {summary}")
        self._cached_summaries = summaries
        self._cached_tokens_by_id = tokens_by_id
        self._rebuild_bm25()

    def _rebuild_bm25(self):
        """从缓存分词重建 BM25（语料 = 可见集合，即 weight >= -5）。"""
        if self._cached_index is None or not self._cached_tokens_by_id:
            self._cached_bm25 = None
            self._cached_bm25_ids = frozenset()
            return
        visible_ids = frozenset(
            e.get("id") for e in self._cached_index
            if e.get("weight", 0) >= -5
        )
        tokens = [
            self._cached_tokens_by_id[e.get("id")]
            for e in self._cached_index
            if e.get("id") in visible_ids
        ]
        if tokens and any(tokens):
            self._cached_bm25 = BM25Okapi(tokens)
        else:
            self._cached_bm25 = None
        self._cached_bm25_ids = visible_ids

    def _bm25_needs_rebuild(self) -> bool:
        """可见集合（weight >= -5）变化时 BM25 需要重建（权重越界属低频事件）。"""
        if self._cached_index is None:
            return True
        current = frozenset(
            e.get("id") for e in self._cached_index
            if e.get("weight", 0) >= -5
        )
        return current != self._cached_bm25_ids

    def _refresh_corpus_for_entry(self, entry_id: int):
        """save_conversation 后增量刷新单个条目的摘要/分词并重建 BM25。"""
        if self._cached_index is None:
            return
        for e in self._cached_index:
            if e.get("id") == entry_id:
                day_summaries = self._read_daily_summaries(e.get("date", ""))
                summary = day_summaries.get(entry_id) or ""
                self._cached_summaries[entry_id] = summary
                self._cached_tokens_by_id[entry_id] = _tokenize(
                    f"{e.get('user_call', '')} {summary}"
                )
                break
        self._rebuild_bm25()

    def _read_daily_summaries(self, date_str: str) -> Dict[int, str]:
        """单次读取 daily/{date}.md，解析出该文件内所有条目的摘要（{id: 摘要}）。

        块边界以列 0 的 `---` 为准（_build_entry_block 生成格式），消息内容统一
        2 空格缩进，因此消息中的横向分隔线/`ID:` 等文本不会被误判为块边界。
        语义与旧 _read_entry_summary 一致（message 行 + 500 字截断），且只读
        文件一次，用于全量语料重建，避免每条目一次整文件扫描。
        """
        md_path = self._daily_dir / f"{date_str}.md"
        if not md_path.exists():
            return {}
        result: Dict[int, str] = {}
        cur_id = None
        message_lines = []
        in_block = False
        capture = False
        in_user_call = False   # user_call 值可跨多行（用户输入含换行，未转义）
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                for line in f:
                    raw = line.rstrip("\r\n")
                    if in_user_call:
                        # 多行 user_call 值内部：忽略其中的 `---`/`ID:` 等文本
                        if raw.endswith('"') and not raw.endswith('\\"') and len(raw.rstrip()) > 0:
                            in_user_call = False
                        continue
                    if raw == "---":
                        if in_block:
                            # 块结束：封存当前条目（首个 ID 优先，兼容重复块）
                            if cur_id is not None:
                                result.setdefault(
                                    cur_id, self._finalize_summary(message_lines)
                                )
                            cur_id = None
                            message_lines = []
                            capture = False
                            in_block = False
                        else:
                            in_block = True
                        continue
                    if in_block:
                        if raw.startswith("ID:"):
                            id_val = raw.split(":", 1)[1].strip().strip('"')
                            try:
                                cur_id = int(id_val)
                            except ValueError:
                                cur_id = None
                        elif raw.startswith("user_call:"):
                            # P1-8b：优先按 JSON 解码判断值是否在本行内闭合
                            # （新格式 json.dumps 保证单行完整）；解码失败
                            # （旧格式裸文本）回退"未转义引号结尾"启发式，
                            # 值不以引号结尾则进入跨行值状态
                            if _decode_entry_value(raw.split(":", 1)[1]) is None:
                                if not (raw.rstrip().endswith('"') and not raw.rstrip().endswith('\\"')):
                                    in_user_call = True
                        elif raw.startswith("message:"):
                            capture = True
                        elif capture:
                            stripped = raw.strip()
                            if stripped:
                                message_lines.append(stripped)
        except Exception:
            return {}
        # EOF 兜底：文件末尾块未闭合
        if in_block and cur_id is not None:
            result.setdefault(cur_id, self._finalize_summary(message_lines))
        return result

    @staticmethod
    def _finalize_summary(message_lines: List[str]) -> str:
        """把 message 行序列化为摘要（行边界截断，不再拦腰斩断代码块/句子）。"""
        if not message_lines:
            return ""
        result = "\n".join(message_lines).strip()
        if len(result) <= 500:
            return result
        # 在 500 字符窗口内找最后一个换行作为切点；找不到（超长单行）才硬切，
        # 且保底不少于 200 字符，避免一行超长时几乎不保留内容。
        cut = result.rfind("\n", 0, 500)
        if cut < 200:
            cut = 500
        return result[:cut] + f"……（共{len(result)}字）"

    # ============================================================
    # 保留期清理（2026-08）：负分 15 天 / 零分 30 天 / 正分永久
    # ============================================================

    def cleanup(self) -> Dict:
        """按保留期清理记忆，返回统计 {removed_negative, removed_zero,
        removed_files}。

        - 年龄按条目 date（YYYY-MM-DD，写入侧本地时区）对今天计算；
        - 日期解析失败一律保留（fail-closed：宁可漏删不可误删）；
        - 双删：index.json 条目 + daily .md 内条目块（复用写侧同款块定位）；
          md 清空后连文件删除，避免残留块慢慢积成第二个"只增不减"；
        - 全程持写锁；清理后重建语料缓存（BM25 覆盖集合随之收敛）。
        """
        stats = {"removed_negative": 0, "removed_zero": 0, "removed_files": 0}
        now = datetime.now()

        def _age_days(value) -> Optional[float]:
            try:
                d = datetime.strptime(str(value), "%Y-%m-%d")
            except (TypeError, ValueError):
                return None
            return (now - d).total_seconds() / 86400.0

        with self._rwlock.write():
            self._ensure_fresh_locked()
            index = self._load_index()
            keep: List[Dict] = []
            drop_by_date: Dict[str, List[int]] = {}
            for e in index:
                age = _age_days(e.get("date"))
                try:
                    weight = int(e.get("weight", 0) or 0)
                except (TypeError, ValueError):
                    weight = 0
                if age is not None and age >= _CLEAN_NEGATIVE_DAYS and weight < 0:
                    drop_by_date.setdefault(str(e.get("date", "")), []).append(
                        e.get("id"))
                    stats["removed_negative"] += 1
                elif age is not None and age >= _CLEAN_ZERO_DAYS and weight == 0:
                    drop_by_date.setdefault(str(e.get("date", "")), []).append(
                        e.get("id"))
                    stats["removed_zero"] += 1
                else:
                    keep.append(e)

            if not drop_by_date:
                return stats

            # 1) 索引先落盘（半途崩溃的后果是"块残留"而非"索引悬空"，安全向）
            self._save_index(keep)
            self._cached_index = keep
            # 2) 语料/BM25 按幸存集合重建（此刻 md 还没动，读摘要仍可得）
            self._rebuild_corpus(keep)
            # 3) 双删 daily 块；空文件回收
            freed = 0
            for date_str, ids in drop_by_date.items():
                if self._remove_entry_blocks(date_str, ids):
                    freed += 1
            stats["removed_files"] = freed
            # daily/.md mtime 已变 + 幸存语料已建：刷新签名，避免把清理误判为外部变更
            self._cache_signature = self._index_signature()
        if any(stats.values()):
            logger.info("记忆保留期清理完成: %s", stats)
        return stats

    def _remove_entry_blocks(self, date_str: str, entry_ids: List[int]) -> bool:
        """从 daily/{date}.md 删除指定 id 的条目块；空了返回 True（文件已删）。"""
        md_path = self._daily_dir / f"{date_str}.md"
        if not md_path.exists() or not entry_ids:
            return False
        with open(md_path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        remaining = [eid for eid in entry_ids if eid is not None]
        changed = False
        for eid in remaining:
            span = self._find_block_line_span(lines, eid)
            if span is not None:
                del lines[span[0]:span[1]]
                changed = True
        if not changed:
            return False
        content = "\n".join(lines).strip("\n").strip()
        if not content:
            md_path.unlink(missing_ok=True)
            return True
        atomic_write_bytes(md_path, (content + "\n\n").encode("utf-8"))
        return False

    def _maybe_auto_cleanup(self, now: Optional[datetime] = None) -> Optional[Dict]:
        """每日至多一次的自动清理闸门（config.json.last_cleanup_date 记账）。

        搭 save_conversation 生命周期便车，不引入线程/定时器；任何异常都不
        允许影响归档主流程。
        """
        try:
            cfg: Dict = {}
            if self._config_path.exists():
                cfg = json.loads(
                    self._config_path.read_text(encoding="utf-8") or "{}")
            today = (now or datetime.now()).strftime("%Y-%m-%d")
            if cfg.get("last_cleanup_date") == today:
                return None
            stats = self.cleanup()
            cfg.setdefault("version", 1)
            cfg["last_cleanup_date"] = today
            atomic_write_json(self._config_path, cfg)
            return stats
        except Exception as exc:
            logger.warning("记忆自动清理跳过（不影响归档）: %s", exc)
            return None

    # ============================================================
    # 权重更新
    # ============================================================

    def update_weight(self, memory_id: int, delta: int) -> bool:
        """
        更新记忆权重

        参数:
            memory_id: 记忆条目 ID
            delta: +1（有用）或 -1（无用）

        返回:
            True 找到并更新，False 未找到
        """
        found = False
        with self._rwlock.write():
            self._ensure_fresh_locked()
            index = self._load_index()
            for entry in index:
                if entry.get("id") == memory_id:
                    entry["weight"] = entry.get("weight", 0) + delta
                    self._pending_deltas[memory_id] = (
                        self._pending_deltas.get(memory_id, 0) + delta
                    )
                    self._dirty = True
                    found = True
                    break
        if found:
            self._mark_pending_and_maybe_flush()
        return found

    # ============================================================
    # 统计
    # ============================================================

    def get_stats(self) -> Dict:
        """返回记忆系统统计信息"""
        self._refresh_if_stale()
        with self._rwlock.read():
            index = self._load_index()
        total = len(index)
        if total == 0:
            return {"total_entries": 0, "date_range": "", "avg_weight": 0}

        dates = [e.get("date", "") for e in index if e.get("date")]
        weights = [e.get("weight", 0) for e in index]
        return {
            "total_entries": total,
            "date_range": f"{min(dates)} ~ {max(dates)}" if dates else "",
            "avg_weight": round(sum(weights) / len(weights), 1) if weights else 0,
            "config_file": str(self._config_path),
            "index_file": str(self._index_path),
        }

    # ============================================================
    # 内部文件操作
    # ============================================================

    def _load_index(self) -> List[Dict]:
        """返回权威内存索引（缓存命中时零磁盘 IO；兼容既有内部调用与测试）。

        调用方应保证缓存已刷新：写锁上下文先调 _ensure_fresh_locked，
        读锁上下文先调 _refresh_if_stale。
        """
        if self._cached_index is None:
            self._rebuild_cache_from_disk()
        return self._cached_index

    def _parse_index_file(self) -> List[Dict]:
        """解析 index.json（磁盘读取部分，供全量重建使用）。"""
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_index(self, entries: List[Dict]):
        """覆写 index.json（文件锁 + 原子写：tmp + os.replace，避免半截文件）"""
        self._locked_index_write(entries)
        # 文件 mtime 已变：刷新缓存键，避免把自身写入误判为外部变更
        self._cache_signature = self._index_signature()

    def _locked_index_write(self, entries: List[Dict]):
        """跨进程互斥写 index.json（fcntl.flock 锁文件 + 原子写）。

        多实例共享同一工作目录时，两个进程可能同时写 index.json；
        flock 保证同一时刻只有一个进程执行 os.replace（防交错/半截文件）。
        进程内串行仍由 _rwlock 负责；跨进程的读-改-写原子性不在此保证
        （以 mtime 签名 + 进程内缓存做最后写入者胜，见 docs/multi-instance.md）。
        """
        lock_path = self._index_path.with_name(self._index_path.name + ".lock")
        if not _HAVE_FCNTL:
            atomic_write_json(self._index_path, entries)
            return
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            # 锁文件创建失败不阻断写入（原子写兜底）
            atomic_write_json(self._index_path, entries)
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            atomic_write_json(self._index_path, entries)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _update_or_append_daily(self, date_str: str, entry_id: int, block: str):
        """
        更新或追加记忆块到 daily/{date_str}.md

        更新：查找 ID 匹配的块并替换（全量覆写）
        追加：文件不存在或未匹配到 ID 时追加到尾部

        P1-8c：不再用非贪婪 DOTALL 正则定位（注入内容会诱使匹配提前截断，
        残尾变成垃圾块），改为按精确锚点扫描块边界（`ID: "N"` 行起点 +
        下一个列 0 的 `---` 或 EOF，见 _find_block_line_span）。
        P2-4：读全文→替换→覆写全程用 flock 跨进程互斥（与 index.json 的
        _locked_index_write 同一先例），防止并发丢更新/交错写。
        """
        md_path = self._daily_dir / f"{date_str}.md"
        block = block.rstrip("\n") + "\n"

        with self._locked_daily_write(md_path):
            if not md_path.exists():
                atomic_write_bytes(md_path, block.encode("utf-8"))
                self._cache_signature = self._index_signature()
                return

            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read()

            lines = content.split("\n")
            span = self._find_block_line_span(lines, entry_id)
            if span is not None:
                start, end = span
                # 用新块整段替换旧块（含首尾 --- 分隔线）
                lines[start:end] = block.rstrip("\n").split("\n")
                content = "\n".join(lines)
            else:
                content = content.rstrip() + "\n\n" + block

            # 原子写回（tmp + os.replace）
            atomic_write_bytes(md_path, content.encode("utf-8"))
        # daily .md mtime 已变：刷新缓存键（分词语料依赖 daily 文件内容）
        self._cache_signature = self._index_signature()

    @staticmethod
    def _find_block_line_span(lines: List[str], entry_id: int):
        """
        在行列表中精确定位 `ID: "{entry_id}"` 条目块的行区间 [start, end)。

        - 起点：列 0 的 `---` 且下一行恰为 `ID: "<entry_id>"`；
        - 终点：其后第一个列 0 的 `---`（块结束分隔线，含入区间）或 EOF。

        兼容旧格式：user_call 值可能含未转义裸换行（其中出现列 0 的
        `---`/`ID:` 行属消息文本而非结构），沿用与 _read_daily_summaries
        一致的"引号结尾"启发式跳过多行 user_call 值内部行，避免误判成块边界。

        未找到返回 None。重复 ID 取第一处（与解析侧 setdefault 语义一致）。
        """
        target_header = f'ID: "{entry_id}"'
        n = len(lines)
        i = 0
        while i < n - 1:
            if lines[i] == "---" and lines[i + 1] == target_header:
                in_user_call = False
                j = i + 2
                while j < n:
                    raw = lines[j]
                    if in_user_call:
                        # 多行 user_call 值内部：直到某行以未转义引号结尾才结束
                        if raw.endswith('"') and not raw.endswith('\\"') and raw.strip():
                            in_user_call = False
                        j += 1
                        continue
                    if raw == "---":
                        return (i, j + 1)   # 含块尾 `---`
                    if raw.startswith("user_call:") and not (
                        raw.rstrip().endswith('"')
                        and not raw.rstrip().endswith('\\"')
                    ):
                        in_user_call = True
                    j += 1
                return (i, n)  # EOF 兜底：块未闭合
            i += 1
        return None

    @contextmanager
    def _locked_daily_write(self, md_path: Path):
        """跨进程互斥包裹 daily md 的读-改-写全程（P2-4）。

        与 _locked_index_write 相同的 flock 先例：锁文件 <name>.lock +
        fcntl.flock LOCK_EX；非 POSIX 平台或锁不可用时退化为纯原子写
        （单实例语义，进程内仍由 _rwlock 保证互斥）。
        """
        lock_path = md_path.with_name(md_path.name + ".lock")
        fd = None
        if _HAVE_FCNTL:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                # 锁获取失败不阻断写入（原子写兜底）
                if fd is not None:
                    os.close(fd)
                    fd = None
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
