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
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Callable


# 会话文件存放目录
DEFAULT_SESSION_DIR = Path(__file__).resolve().parent.parent / "sessions"


def _content_to_text(content) -> str:
    """将 content（str 或 list）统一转为纯文本（用于搜索/匹配）"""
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


def generate_session_id() -> str:
    """生成 8 位随机 hex 作为会话 ID"""
    return secrets.token_hex(4)


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
        self.max_tokens = max_tokens
        self._on_update = on_update
        self._created_at = datetime.now()

        # 会话标识
        self.session_id = session_id or generate_session_id()

        # 模型配置（保存和恢复用）
        self.model_id = ""
        self.model_provider = ""
        self.model_base_url = ""
        self.model_llm_type = ""

        # 任务清单数据（Agent 层管理 TaskList 对象，store 只存 raw dict）
        self.task_list: Optional[Dict] = None

        # ---- 锚点：最后一次 API 返回的精确 token 计数 ----
        self._anchor_total: int = 0       # input_tokens + output_tokens
        self._anchor_msg_count: int = 0   # 当时的消息总数

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
        self._anchor_total = 0
        self._anchor_msg_count = 0
        self.task_list = None
        self._notify()

    def replace_range(self, start: int, end: int, new_msgs: List[Dict]):
        """
        替换 [start, end) 范围内的消息为 new_msgs。

        用于全量压缩：用摘要消息替代早期对话记录。
        保持列表对象不变（避免外部引用断开）。

        参数:
            start:    起始 index（含）
            end:      结束 index（不含）
            new_msgs: 替换用的新消息列表
        """
        self._messages[start:end] = new_msgs
        self._notify()

    # ============================================================
    # 查询
    # ============================================================

    def get_history(self, n: Optional[int] = None) -> List[Dict]:
        if n is None:
            return self._messages.copy()
        return self._messages[-n:].copy()

    def search(self, keyword: str, role: Optional[str] = None) -> List[Dict]:
        results = []
        for msg in self._messages:
            if role and msg.get("role") != role:
                continue
            if keyword.lower() in _content_to_text(msg.get("content", "")).lower():
                results.append(msg)
        return results

    def get_by_role(self, role: str) -> List[Dict]:
        return [m for m in self._messages if m.get("role") == role]

    def get_tool_calls(self) -> List[Dict]:
        return [m for m in self._messages
                if m.get("role") == "assistant" and "ACTION" in _content_to_text(m.get("content", ""))]

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

        self._anchor_total = input_tokens + output_tokens
        self._anchor_msg_count = len(self._messages)

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

        # 锚点之后新增的消息，用估算补上
        new_messages = self._messages[self._anchor_msg_count:]
        new_tokens = sum(_estimate_tokens(m.get("content", "")) for m in new_messages)
        return self._anchor_total + new_tokens

    def _estimate_all_tokens(self) -> int:
        """纯估算所有消息的 token 数（无锚点时回退）"""
        return sum(_estimate_tokens(m.get("content", "")) for m in self._messages)

    # ============================================================
    # 上下文占用统计
    # ============================================================

    def stats(self) -> Dict:
        total_tokens = self.live_tokens()  # 锚点 + 估算，比纯估算更准
        breakdown = {}
        for msg in self._messages:
            role = msg.get("role", "unknown")
            tokens = _estimate_tokens(msg.get("content", ""))
            if role not in breakdown:
                breakdown[role] = {"count": 0, "tokens": 0}
            breakdown[role]["count"] += 1
            breakdown[role]["tokens"] += tokens

        return {
            "total_messages": len(self._messages),
            "total_tokens": total_tokens,
            "max_tokens": self.max_tokens,
            "usage_ratio": total_tokens / self.max_tokens if self.max_tokens > 0 else 0,
            "remaining_tokens": max(0, self.max_tokens - total_tokens),
            "breakdown": breakdown,
            "anchored": self._anchor_total > 0,
            "anchored_tokens": self._anchor_total,
        }

    # ============================================================
    # 会话持久化
    # ============================================================

    def to_session_data(self) -> Dict:
        """
        将会话序列化为字典（不含 system prompt）
        """
        messages = [
            {
                "role": msg["role"],
                "name": msg.get("name"),
                "content": msg["content"],
                "tokens": _estimate_tokens(msg.get("content", "")),
            }
            for msg in self._messages
            if msg.get("role") != "system"
        ]

        result = {
            "session_id": self.session_id,
            "created_at": self._created_at.isoformat(),
            "model_id": self.model_id,
            "model_provider": self.model_provider,
            "model_base_url": self.model_base_url,
            "model_llm_type": self.model_llm_type,
            "max_tokens": self.max_tokens,
            "message_count": len(messages),
            "messages": messages,
        }
        if self.task_list:
            result["task_list"] = self.task_list
        return result

    def save_session(self, filepath: Optional[str] = None) -> str:
        """
        保存会话到文件（全量覆写，只保存内存当前内容）

        参数:
            filepath: 保存路径，不传则自动生成 sessions/{id}.json

        返回:
            实际保存的文件路径
        """
        if not filepath:
            session_dir = DEFAULT_SESSION_DIR
            session_dir.mkdir(parents=True, exist_ok=True)
            filepath = str(session_dir / f"{self.session_id}.json")

        data = self.to_session_data()
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return filepath

    def load_session_data(self, data: Dict):
        """
        从字典加载会话数据

        恢复模型配置和消息列表（不含 system prompt，由 Agent 负责插入）。
        """
        self.session_id = data.get("session_id", self.session_id)
        self.model_id = data.get("model_id", "")
        self.model_provider = data.get("model_provider", "")
        self.model_base_url = data.get("model_base_url", "")
        self.model_llm_type = data.get("model_llm_type", "")
        self.max_tokens = data.get("max_tokens", self.max_tokens)

        # 恢复消息（不含 system prompt）
        # 注意：使用 clear + extend 保持列表对象不变，避免外部引用断开
        loaded = data.get("messages", [])
        self._messages.clear()
        self._messages.extend(
            {"role": m["role"], "content": m["content"], **({"name": m["name"]} if m.get("name") else {})}
            for m in loaded
        )

        # 恢复创建时间
        created = data.get("created_at")
        if created:
            try:
                self._created_at = datetime.fromisoformat(created)
            except (ValueError, TypeError):
                pass

        # 恢复任务清单（Agent 层会用这个 raw dict 重建 TaskList 对象）
        self.task_list = data.get("task_list")

        # 锚点失效——会话加载后需要重新校准
        self._anchor_total = 0
        self._anchor_msg_count = 0

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
        """删除指定会话文件"""
        session_dir = Path(session_dir or DEFAULT_SESSION_DIR)
        filepath = session_dir / f"{session_id}.json"
        if filepath.exists():
            filepath.unlink()
            return True
        return False

    # ============================================================
    # 变更通知
    # ============================================================

    def set_on_update(self, callback: Optional[Callable[["MessageStore"], None]]):
        self._on_update = callback

    def _notify(self):
        if self._on_update:
            try:
                self._on_update(self)
            except Exception:
                pass

    # ============================================================
    # 显示
    # ============================================================

    def format_stats(self) -> str:
        s = self.stats()
        ratio = s["usage_ratio"] * 100
        # 锚点状态指示
        anchor_indicator = ""
        if s.get("anchored"):
            anchor_indicator = f"  📌 锚点: {s['anchored_tokens']:,} tokens（API 实测）"
        parts = [
            f"📊 上下文状态  session: {self.session_id}",
            f"  ─────────────────",
            f"  消息: {s['total_messages']} 条",
            f"  占用: {s['total_tokens']:,} / {s['max_tokens']:,} tokens ({ratio:.1f}%)",
            f"  剩余: {s['remaining_tokens']:,} tokens",
        ]
        if anchor_indicator:
            parts.append(anchor_indicator)
        parts.append(f"  按角色（估算值）:")
        for role, info in sorted(s["breakdown"].items()):
            icon = {"system": "⚙️", "user": "👤", "assistant": "🤖", "tool": "🔧"}.get(role, "❓")
            parts.append(f"    {icon} {role:12s} {info['count']:3d} 条  {info['tokens']:>8,} tokens")
        return "\n".join(parts)

    def __str__(self) -> str:
        s = self.stats()
        return f"MessageStore({self.session_id}, {s['total_messages']} msg, {s['total_tokens']}/{s['max_tokens']} tokens)"
