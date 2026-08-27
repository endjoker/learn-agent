"""
消息存储模块 —— 统一管理 Agent 的对话历史和上下文占用统计

负责：
1. 消息的增删查改
2. Token 占用统计（按角色分类）
3. 变更通知（用于 UI 订阅）
4. 会话持久化（save/load）
"""

import json
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Callable

from core.atomic_io import atomic_write_json


# 会话文件存放目录
DEFAULT_SESSION_DIR = Path(__file__).resolve().parent.parent / "sessions"
_SESSION_LOCKS: dict[str, threading.RLock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    """Return a process-local lock for a session path."""
    key = str(path.resolve())
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(key, threading.RLock())


def _content_to_text(content) -> str:
    """将 content（str 或 list）统一转为纯文本（用于搜索/匹配）"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content)


def _estimate_tokens(text) -> int:
    """粗略估算 token 数（中文 1.5 字/token，英文 4 字符/token）。

    支持 str 和 list（多模态 content blocks）。
    """
    if not text:
        return 0
    # 多模态 content: list of blocks
    if isinstance(text, list):
        total = 0
        for block in text:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += _estimate_tokens(block.get("text", ""))
                elif block.get("type") == "image":
                    # 实际视觉 token 随分辨率可达数千；宁高勿低，避免自动压缩阈值失灵
                    total += 1000
        return total
    # 纯文本
    chinese = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - chinese
    return int(chinese / 1.5 + other / 4)


def estimate_tokens(text) -> int:
    """公开的 token 估算入口（单一事实来源）。

    与 _estimate_tokens 等价；供 core.system_prompt 等外部模块引用，
    避免各模块各自复制一份估算逻辑导致口径分叉。
    """
    return _estimate_tokens(text)


def generate_session_id() -> str:
    """生成 8 位随机 hex 作为会话 ID"""
    return secrets.token_hex(4)


# 会话文件持久化的消息扩展字段（白名单）
# runtime/plan_id/plan_task_id/goal_id/goal_round：UI-only 运行期记录
# （Plan/Goal 后台活动的工具调用与最终回复），不进入模型上下文。
_PERSISTED_EXTRA_KEYS = ("kind", "tool_calls", "tool_call_id", "is_error",
                         "runtime", "plan_id", "plan_task_id", "goal_id", "goal_round")

# token 估算缓存容量上限（条目数）：消息列表受压缩阈值约束（数百条），
# 每轮 save_session 仅新增少量条目；封顶后整体清空，防止长驻进程无限增长。
_TOKEN_CACHE_MAX = 4096


class MessageStore:
    """
    消息存储 + 上下文监控 + 会话持久化

    用法：
        store = MessageStore(max_tokens=65536)
        store.append({"role": "user", "content": "你好"})
        store.save_session("sessions/abc123.json")
    """

    def __init__(
        self,
        max_tokens: int = 0,
        session_id: Optional[str] = None,
        on_update: Optional[Callable[["MessageStore"], None]] = None,
    ):
        self._messages: List[Dict] = []
        self._events: List[Dict] = []
        self.max_tokens = max_tokens
        # 模型真实上下文窗口（token），由 Agent 初始化/切换模型时同步。
        # 与 max_tokens（历史预算，= context_length - 输出预留）区分。
        self.model_context_length = 0
        self._on_update = on_update
        self._created_at = datetime.now()

        # 会话标识
        self.session_id = session_id or generate_session_id()

        # 模型配置（保存和恢复用）
        self.model_id = ""
        self.model_provider = ""
        self.model_base_url = ""
        self.model_llm_type = ""

        # ---- 锚点：最后一次 API 返回的精确 token 计数 ----
        self._anchor_total: int = 0       # input_tokens 基准（P3-2：不再含 output）
        self._anchor_msg_count: int = 0   # 当时的消息总数
        # 契约③：缓存命中累计（A1 观测用）
        self._cache_hit_total: int = 0
        self._cache_miss_total: int = 0

        # C2 契约②（已退役）：会话文件持久化开关（默认 False）。SQLite 统一
        # 会话是唯一权威，sessions/*.json 不再读写；保留 setter 仅为兼容旧
        # 调用方（agent.clear_history 的临时写回特例已随退役移除）。
        self._file_persistence_enabled = False

        # token 估算缓存（L2 链路优化）：按 (id(msg), 内容长度) 缓存每条消息
        # 的估算值，避免 to_session_data 每轮全量重算。消息被替换为新对象
        # （append/pop/replace）时 id 变化自动失效；容量封顶防长驻进程增长。
        self._token_cache: Dict[tuple, int] = {}

    # ============================================================
    # 消息列表代理
    # ============================================================

    @property
    def messages(self) -> List[Dict]:
        return self._messages

    @messages.setter
    def messages(self, value: List[Dict]):
        self._messages = value
        self._notify()

    def __len__(self) -> int:
        return len(self._messages)

    def __getitem__(self, index) -> Dict:
        return self._messages[index]

    def __iter__(self):
        return iter(self._messages)

    # ================================================================
    # 运行期配置/压缩事件（结构化审计，随会话持久化）
    # ================================================================

    @property
    def events(self) -> List[Dict]:
        return self._events

    def record_event(self, event_type: str, **fields) -> None:
        """记录一条结构化会话事件（模型切换/权限变更/推理等级/压缩等）。"""
        event = {"type": event_type, "timestamp": datetime.now().isoformat(timespec="seconds")}
        event.update(fields)
        self._events.append(event)
        self._notify()

    # ============================================================
    # 核心操作
    # ============================================================

    def append(self, msg: Dict):
        self._messages.append(msg)
        self._notify()

    def pop(self, index: int = -1) -> Dict:
        msg = self._messages.pop(index)
        self._notify()
        return msg

    def clear(self):
        self._messages.clear()
        self.reset_anchor()
        self._notify()

    # ============================================================
    # 查询
    # ============================================================

    def search(self, keyword: str, role: Optional[str] = None) -> List[Dict]:
        """按关键词搜索消息。keyword 必须是字符串。"""
        if not isinstance(keyword, str):
            raise TypeError(
                f"search keyword 必须是字符串，收到 {type(keyword).__name__}")
        results = []
        for msg in self._messages:
            if role and msg.get("role") != role:
                continue
            if keyword.lower() in _content_to_text(msg.get("content", "")).lower():
                results.append(msg)
        return results

    # ============================================================
    # 锚点追踪（API 返回的精确 token 计数）
    # ============================================================

    def set_anchor(self, usage: Optional[Dict[str, int]]):
        """
        根据 API 返回的 usage 设置 token 锚点。

        锚点是上下文计数的"基准线"：
          - 锚点之后新增的消息用估算，误差有界
          - 每轮 API 调用重置锚点，误差归零
        """
        if not usage:
            return  # usage 为空时不更新锚点，继续用老的估算

        input_tokens = usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("output_tokens", 0) or 0
        if input_tokens == 0 and output_tokens == 0:
            return

        # P3-2：锚点只记 input_tokens。旧实现把刚生成的 output_tokens 也计入
        # 基准，而该输出随后会作为新增消息被估算再叠加一次（agent 在 append
        # 回复前 set_anchor），造成双计高估、提前触发压缩/截断。
        # "输入精确基准 + 之后新增消息估算"才是下一轮输入的真实近似；
        # 字段名 _anchor_total 保留以兼容既有读取方（stats）。
        self._anchor_total = input_tokens
        self._anchor_msg_count = len(self._messages)
        # 契约③后半段：累计缓存命中/未命中 tokens，供 stats() 与 A1 效果观测
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        if hit or miss:
            self._cache_hit_total += hit
            self._cache_miss_total += miss

    def reset_anchor(self) -> None:
        """使 token 锚点失效（全量压缩/会话加载后调用，下次 API 调用重新校准）。

        公开方法：外部模块（如 Compressor）不再直接修改 _anchor_total
        私有字段，统一走此入口保持封装。
        """
        self._anchor_total = 0
        self._anchor_msg_count = 0

    def live_tokens(self) -> int:
        """
        当前上下文占用（锚点 + 新增消息的估算）。

        锚点存在时：误差局限于 API 调用之间新增的几条消息。
        无锚点时：回退到纯估算。
        """
        if self._anchor_total == 0 or self._anchor_msg_count == 0:
            return self._estimate_all_tokens()

        # 如果消息被截断（_truncate_history 移除了锚点之前的消息），
        # 锚点偏高 → 保守估计，不影响正确性。下次 API 调用自动归零误差。
        if self._anchor_msg_count >= len(self._messages):
            return self._anchor_total

        # 锚点之后新增的消息，用估算补上（UI-only runtime 记录不计入）
        new_messages = [m for m in self._messages[self._anchor_msg_count:]
                        if not m.get("runtime")]
        new_tokens = sum(_estimate_tokens(m.get("content", "")) for m in new_messages)
        return self._anchor_total + new_tokens

    def _estimate_all_tokens(self) -> int:
        """纯估算所有消息的 token 数（无锚点时回退）。

        UI-only runtime 记录（Plan/Goal 后台活动：工具调用/最终回复）不算入
        上下文占用，避免它们触发压缩/截断或误导上下文仪表。
        """
        return sum(_estimate_tokens(m.get("content", ""))
                   for m in self._messages if not m.get("runtime"))

    # ============================================================
    # 上下文占用统计
    # ============================================================

    def stats(self) -> Dict:
        total_tokens = self.live_tokens()  # 锚点 + 估算，比纯估算更准
        # UI-only runtime 记录（Plan/Goal 后台活动）不参与上下文统计
        visible = [m for m in self._messages if not m.get("runtime")]
        breakdown = {}
        for msg in visible:
            role = msg.get("role", "unknown")
            tokens = _estimate_tokens(msg.get("content", ""))
            if role not in breakdown:
                breakdown[role] = {"count": 0, "tokens": 0}
            breakdown[role]["count"] += 1
            breakdown[role]["tokens"] += tokens

        return {
            "available": True,
            "total_messages": len(visible),
            "total_tokens": total_tokens,
            "max_tokens": self.max_tokens,                 # 历史预算（压缩/截断阈值）
            "model_context_length": self.model_context_length,  # 模型真实上下文窗口
            "usage_ratio": total_tokens / self.max_tokens if self.max_tokens > 0 else 0,
            "remaining_tokens": max(0, self.max_tokens - total_tokens),
            "breakdown": breakdown,
            "anchored": self._anchor_total > 0,
            "anchored_tokens": self._anchor_total,
            # 契约③：缓存命中观测（A1 效果验证）
            "prompt_cache_hit_tokens": self._cache_hit_total,
            "prompt_cache_miss_tokens": self._cache_miss_total,
            "prompt_cache_hit_ratio": (
                round(self._cache_hit_total /
                      (self._cache_hit_total + self._cache_miss_total), 4)
                if (self._cache_hit_total + self._cache_miss_total) > 0 else None
            ),
        }

    # ============================================================
    # 会话持久化
    # ============================================================

    @property
    def file_persistence_enabled(self) -> bool:
        """公开只读属性：会话文件持久化是否开启（dispatcher 探测用，C2 契约②）。"""
        return self._file_persistence_enabled

    def set_file_persistence(self, enabled: bool) -> None:
        """设置会话文件持久化开关（C2 契约②）。

        enabled=False 时 save_session 直接返回，不再写 sessions/*.json
        （统一 runner 路径以 SQLite 为唯一权威）；内存 messages 不受影响。
        默认开启（True）。
        """
        self._file_persistence_enabled = bool(enabled)

    def _cached_tokens(self, msg: Dict) -> int:
        """按 (id(msg), 内容长度) 缓存的 token 估算（to_session_data 专用）。

        同一 dict 对象跨轮全量覆写序列化时命中缓存，避免每条消息每次全量
        重算；消息被替换为新对象（append/pop/replace/load）时 id 变化自动
        失效；内容长度参与键值可捕获原地改写。多模态 content（list of
        blocks）不可哈希缓存，直接估算。容量超过 _TOKEN_CACHE_MAX 时整体
        清空（下次按需重建），防止长驻进程无限增长。
        """
        content = msg.get("content", "")
        if content is None:
            # assistant tool_calls 消息允许 content=None（agent.py 追加
            # "content": response.text or None，OpenAI 标准表示）。序列化
            # 时按空文本估 token，不得对 None 做 len()（P0：会话持久化
            # TypeError 曾让 Plan/Goal 轮后 finally 逃逸、Turn 卡运行中）。
            content = ""
        if isinstance(content, list):
            return _estimate_tokens(content)
        key = (id(msg), len(content))
        cached = self._token_cache.get(key)
        if cached is None:
            cached = _estimate_tokens(content)
            self._token_cache[key] = cached
            if len(self._token_cache) > _TOKEN_CACHE_MAX:
                self._token_cache.clear()
        return cached

    def to_session_data(self) -> Dict:
        """
        将会话序列化为字典（不含 system prompt）
        """
        messages = [
            {**{
                "role": msg["role"], "name": msg.get("name"),
                "content": msg["content"],
                "tokens": self._cached_tokens(msg),
            }, **{key: msg[key] for key in _PERSISTED_EXTRA_KEYS if key in msg}}
            for msg in self._messages
            if msg.get("role") != "system"
        ]

        result = {
            "schema_version": 3,
            "session_id": self.session_id,
            "events": self._events,
            "created_at": self._created_at.isoformat(),
            "model_id": self.model_id,
            "model_provider": self.model_provider,
            "model_base_url": self.model_base_url,
            "model_llm_type": self.model_llm_type,
            "max_tokens": self.max_tokens,
            "model_context_length": self.model_context_length,
            "message_count": len(messages),
            "messages": messages,
        }
        return result

    def save_session(self, filepath: Optional[str] = None) -> str:
        """
        保存会话到文件（全量覆写，只保存内存当前内容）

        C2 契约②：file_persistence_enabled=False 时直接返回（不写文件），
        内存 messages 不受影响；统一 runner 路径由 dispatcher 在执行前置 False
        （SQLite 为唯一权威）。

        参数:
            filepath: 保存路径，不传则自动生成 sessions/{id}.json

        返回:
            实际保存的文件路径；持久化关闭时返回 ""。
        """
        if not self._file_persistence_enabled:
            return ""
        if not filepath:
            session_dir = DEFAULT_SESSION_DIR
            session_dir.mkdir(parents=True, exist_ok=True)
            filepath = str(session_dir / f"{self.session_id}.json")

        target = Path(filepath)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_session_data()
        # Write + fsync + replace keeps the old session recoverable if a
        # process crashes or is interrupted during persistence.
        with _lock_for(target):
            atomic_write_json(target, data, prefix=f".{target.name}.")
        return str(target)

    def load_session_data(self, data: Dict):
        """
        从字典加载会话数据

        恢复模型配置和消息列表（不含 system prompt，由 Agent 负责插入）。
        """
        self.session_id = data.get("session_id", self.session_id)
        # 会话事件（v3+）。老版本无 events 字段，迁移为空列表。
        events = data.get("events", [])
        self._events = events if isinstance(events, list) else []
        self.model_id = data.get("model_id", "")
        self.model_provider = data.get("model_provider", "")
        self.model_base_url = data.get("model_base_url", "")
        self.model_llm_type = data.get("model_llm_type", "")
        # 注意：不恢复 max_tokens / model_context_length —— 它们是运行期派生值，
        # 由 Agent 按当前模型上下文重新计算（_history_budget），磁盘旧值可能对应
        # 旧模型/旧算法，恢复时不应覆盖当前预算。
        # self.max_tokens = data.get("max_tokens", self.max_tokens)

        # 恢复消息（不含 system prompt）
        # 注意：使用 clear + extend 保持列表对象不变，避免外部引用断开
        loaded = data.get("messages", [])
        self._messages.clear()
        for m in loaded:
            msg = {"role": m["role"], "content": m.get("content", "")}
            if m.get("name"):
                msg["name"] = m["name"]
            for key in _PERSISTED_EXTRA_KEYS:
                if key in m:
                    msg[key] = m[key]
            # v1 tool results were stored as user/name=tool_result.
            if msg["role"] == "user" and msg.get("name") == "tool_result":
                msg.setdefault("kind", "tool_result")
            self._messages.append(msg)

        # 恢复创建时间
        created = data.get("created_at")
        if created:
            try:
                self._created_at = datetime.fromisoformat(created)
            except (ValueError, TypeError):
                pass

        # 锚点失效——会话加载后需要重新校准
        self.reset_anchor()

        self._notify()

    def load_session_file(self, filepath: str) -> bool:
        """从文件加载会话"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.load_session_data(data)
            return True
        except (FileNotFoundError, json.JSONDecodeError):
            return False

    @staticmethod
    def list_session_files(session_dir: Optional[str] = None) -> List[Dict]:
        """
        列出所有已保存的会话摘要

        返回:
            [{session_id, model_id, created_at, message_count}, ...]
            （按创建时间倒序）
        """
        session_dir = Path(session_dir or DEFAULT_SESSION_DIR)
        if not session_dir.exists():
            return []

        sessions = []
        for f in sorted(session_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.name.startswith("."):
                continue
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                sessions.append({
                    "session_id": data.get("session_id", f.stem),
                    "model_id": data.get("model_id", ""),
                    "created_at": data.get("created_at", ""),
                    "message_count": data.get("message_count", 0),
                    "filepath": str(f),
                })
            except (json.JSONDecodeError, Exception):
                continue

        return sessions

    @staticmethod
    def delete_session_file(session_id: str, session_dir: Optional[str] = None) -> bool:
        """删除指定会话文件

        路径穿越防护：session_id 必须是纯文件名（Path.name 等于自身），
        且拼接 resolve 后仍位于会话根目录内，否则抛 ValueError
        （上层 API 层对异常统一收口，不会造成目录外文件被删除）。
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id 必须是非空字符串")
        if "\x00" in session_id:
            raise ValueError(f"非法的 session_id: {session_id!r}")
        if session_id in (".", ".."):
            raise ValueError(f"非法的 session_id: {session_id!r}")
        if Path(session_id).name != session_id:
            raise ValueError(f"非法的 session_id: {session_id!r}")
        session_dir = Path(session_dir or DEFAULT_SESSION_DIR)
        filepath = session_dir / f"{session_id}.json"
        root = session_dir.resolve()
        resolved = filepath.resolve()
        if root not in resolved.parents:
            raise ValueError(f"非法的 session_id: {session_id!r}")
        if filepath.exists():
            filepath.unlink()
            return True
        return False

    # ============================================================
    # 变更通知
    # ============================================================

    def _notify(self):
        if self._on_update:
            try:
                self._on_update(self)
            except Exception:
                pass

    # ============================================================
    # 显示
    # ============================================================

    def __str__(self) -> str:
        s = self.stats()
        return f"MessageStore({self.session_id}, {s['total_messages']} msg, {s['total_tokens']}/{s['max_tokens']} tokens)"
