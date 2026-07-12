# -*- coding: utf-8 -*-
"""
记忆管理器 —— 跨会话长期记忆的核心

目录结构：
    memory/
        config.json              # 系统配置
        daily/
            index.json           # 对话索引 [{id, date, user_call, weight, md}]
            2026-07-12.md        # 按日归档的详细记忆

线程安全：通过 threading.Lock 保护所有文件操作（工具在 ThreadPoolExecutor 中并发执行）
"""

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from rank_bm25 import BM25Okapi
import jieba


def _tokenize(text: str) -> List[str]:
    """
    分词器：支持中英文混合文本
    - 中文：jieba 分词
    - 英文：按空格/标点拆分
    - 数字和字母保留原样
    """
    return list(jieba.cut(text))


class MemoryManager:
    """跨会话记忆管理器"""

    def __init__(self, memory_dir: str = "memory"):
        self._memory_dir = Path(memory_dir).resolve()
        self._daily_dir = self._memory_dir / "daily"
        self._index_path = self._daily_dir / "index.json"
        self._config_path = self._memory_dir / "config.json"
        self._lock = threading.Lock()
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

        # index.json
        if not self._index_path.exists():
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)

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

        with self._lock:
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
                new_call = user_call[:200]
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
                    "user_call": user_call[:200],
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
        """
        msg_text = self._serialize_messages(messages)
        return (
            f"---\n"
            f"ID: \"{entry_id}\"\n"
            f"日期: \"{time_str}\"\n"
            f"session_id: \"{session_id}\"\n"
            f"user_call: \"{user_call}\"\n"
            f"weight: \"0\"\n"
            f"message: |\n"
            f"  {msg_text.replace(chr(10), chr(10) + '  ')}\n"
            f"---\n\n"
        )

    @staticmethod
    def _filter_assistant_content(content: str) -> str:
        """
        过滤 assistant 消息，只保留推理过程和最终回答

        ✓ 保留：THOUGHT / 思考、FINAL_ANSWER / 最终回答
        ✗ 丢弃：ACTION / 行动、INPUT / 输入
        """
        lines = content.split("\n")
        kept = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # 判断是否以 ACTION/行动 或 INPUT/输入 开头
            if re.match(
                r"^(?:ACTION|行动|INPUT|输入)[：:]",
                stripped,
                re.IGNORECASE,
            ):
                continue
            kept.append(stripped)
        return "\n".join(kept)

    def _serialize_messages(self, messages: List[Dict]) -> str:
        """
        将消息序列化为简洁文本

        保留：
        - [user] 用户提问
        - [assistant] 推理过程（THOUGHT）+ 最终回答（FINAL_ANSWER），
          过滤掉工具调用指令（ACTION / INPUT）

        过滤掉：
        - system prompt
        - tool_result（工具执行结果）
        """
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            name = msg.get("name", "")

            # 跳过 system prompt
            if role == "system":
                continue
            # 跳过工具执行结果
            if role == "user" and name == "tool_result":
                continue

            content = msg.get("content", "")
            # 转义反斜杠，防止 Windows 路径（如 D:\path）被后续操作当成非法转义
            content = content.replace("\\", "\\\\")
            # 对 assistant 消息过滤掉 ACTION/INPUT
            if role == "assistant":
                content = self._filter_assistant_content(content)
                if not content.strip():
                    continue  # 过滤后为空则跳过整条

            parts.append(f"[{role}] {content}")

        return "\n".join(parts)

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
        with self._lock:
            index = self._load_index()

            if not index:
                return []

            # 1. 日期 + 隐藏低权重 预过滤
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
            if query:
                corpus = [
                    f"{e.get('user_call', '')} {self._read_entry_summary(e)}"
                    for e in candidates
                ]
                tokenized_corpus = [_tokenize(doc) for doc in corpus]
                bm25 = BM25Okapi(tokenized_corpus)
                tokenized_query = _tokenize(query)
                scores = bm25.get_scores(tokenized_query)

                # 给每个 candidate 附加 BM25 得分
                for i, entry in enumerate(candidates):
                    entry["_bm25_score"] = scores[i]
            else:
                # 无查询词时全部视为命中
                for entry in candidates:
                    entry["_bm25_score"] = 1.0

            # 3. 综合排序：BM25 得分 + 权重 + 时间
            def _priority(entry):
                date_str = entry.get("date", "0000-00-00")
                date_num = int(date_str.replace("-", "")) if date_str else 0
                bm25 = entry.get("_bm25_score", 0)
                return bm25 * 10 + entry.get("weight", 0) + date_num / 100000

            candidates.sort(key=_priority, reverse=True)

            # 4. 取 top N
            matched = candidates[:limit]

            # 5. 自动增加权重（命中即有用信号）
            matched_ids = {e["id"] for e in matched}
            for entry in index:
                if entry.get("id") in matched_ids:
                    entry["weight"] = entry.get("weight", 0) + 1
            self._save_index(index)

            # 6. 构建输出（清理临时字段）
            output = []
            for entry in matched:
                summary = self._read_entry_summary(entry)
                output.append({
                    "id": entry.get("id"),
                    "date": entry.get("date"),
                    "user_call": entry.get("user_call"),
                    "weight": entry.get("weight", 0),  # 已加1后的值
                    "summary": summary,
                })

        return output

    def _read_entry_summary(self, entry: Dict) -> str:
        """从 daily .md 中读取指定条目的消息摘要"""
        md_path = self._daily_dir / f"{entry['date']}.md"
        if not md_path.exists():
            return ""

        target_id = str(entry.get("id", ""))
        in_block = False
        message_lines = []
        capture = False

        try:
            with open(md_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped == "---":
                        if in_block:
                            # 块结束
                            break
                        in_block = True
                        continue

                    if in_block:
                        if stripped.startswith("ID:"):
                            # 检查是否匹配
                            id_val = stripped.split(":", 1)[1].strip().strip('"')
                            if id_val != target_id:
                                in_block = False
                        elif stripped.startswith("message:"):
                            capture = True
                        elif capture and stripped.startswith("---"):
                            break
                        elif capture:
                            # 去掉 YAML 缩进前缀
                            text = stripped
                            if text:
                                message_lines.append(text)
        except Exception:
            pass

        if message_lines:
            result = "\n".join(message_lines).strip()
            if len(result) > 500:
                result = result[:500] + f"……（共{len(result)}字）"
            return result
        return ""

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
        with self._lock:
            index = self._load_index()
            for entry in index:
                if entry.get("id") == memory_id:
                    entry["weight"] = entry.get("weight", 0) + delta
                    self._save_index(index)
                    return True
        return False

    # ============================================================
    # 统计
    # ============================================================

    def get_stats(self) -> Dict:
        """返回记忆系统统计信息"""
        with self._lock:
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
        """加载 index.json"""
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_index(self, entries: List[Dict]):
        """覆写 index.json"""
        with open(self._index_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

    def _update_or_append_daily(self, date_str: str, entry_id: int, block: str):
        """
        更新或追加记忆块到 daily/{date_str}.md

        更新：查找 ID 匹配的块并替换（全量覆写）
        追加：文件不存在或未匹配到 ID 时追加到尾部
        """
        md_path = self._daily_dir / f"{date_str}.md"
        block = block.rstrip("\n") + "\n"

        if not md_path.exists():
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(block)
            return

        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 尝试查找并替换已有块
        pattern = re.compile(
            rf'---\nID: "{re.escape(str(entry_id))}"\n.*?\n---',
            re.DOTALL,
        )
        if pattern.search(content):
            # 使用 lambda：避免 re.sub 将 Windows 路径 \p 等当成非法转义处理
            content = pattern.sub(lambda m: block.rstrip(), content)
        else:
            content = content.rstrip() + "\n\n" + block

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(content)
