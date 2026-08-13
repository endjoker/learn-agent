"""
上下文压缩器 —— 管理 Agent 的上下文窗口

提供两种压缩策略：
  1. 轻量压缩（方案A）：规则替换旧工具结果为短摘要
  2. 全量压缩（/compact）：LLM 生成结构化摘要替代早期对话

用法：
    compressor = Compressor(llm, tail_n=8)
    compressor.light_compress(messages)       # 规则替换旧工具结果
    compressor.full_compress(messages)        # LLM 结构化摘要
"""

import re
from typing import List, Dict, Optional

from core.message_store import _content_to_text


# ============================================================
# 全量压缩用的摘要提示词模板
# ============================================================

SUMMARY_PROMPT = """请仔细阅读以下对话历史，生成一份中文结构化摘要，必须覆盖：

## 1. 用户核心请求与意图
## 2. 涉及的技术概念与关键代码模式
## 3. 操作过的文件（含路径、关键行号、修改内容）
## 4. 遇到的错误与修复方式
## 5. 待完成的任务
## 6. 当前正在做的事情

要求：
- 保留所有关键信息，不要遗漏文件名、函数名、错误信息
- 简洁精准，不要多余的解释
- 代码片段保持原样，不要改写

以下是待摘要的对话历史：
-----
{history}
-----

直接输出中文摘要："""


# ============================================================
# 轻量压缩：工具结果摘要
# ============================================================


def _extract_tool_meta(content: str) -> dict:
    """从工具结果中提取元数据（工具名、输入参数）"""
    meta = {"tool": "", "input": "", "is_batch": False, "count": 0}

    lines = content.split("\n")
    for line in lines:
        if line.startswith("工具:"):
            meta["tool"] = line[3:].strip()
        elif line.startswith("输入摘要:"):
            meta["input"] = line[5:].strip()

    # 批量结果
    if "批量" in content:
        meta["is_batch"] = True
        import re
        m = re.search(r"共 (\d+) 个工具", content)
        if m:
            meta["count"] = int(m.group(1))

    return meta


def _compress_tool_result(msg: Dict) -> Dict:
    """将单条工具结果压缩为纯元数据（已消费的工具结果无需保留内容）"""
    # 多模态 tool_result（list content）→ 归一化为纯文本
    content = _content_to_text(msg.get("content", ""))

    # 估算原始 token 数（粗略）
    chinese = sum(1 for c in content if '一' <= c <= '鿿')
    other = len(content) - chinese
    original_tokens = int(chinese / 1.5 + other / 4)

    # 提取元数据
    meta = _extract_tool_meta(content)

    parts = ["【工具执行结果 - 已压缩】"]
    if meta["is_batch"]:
        parts.append(f"批量执行: {meta['count']} 个工具")
    else:
        parts.append(f"工具: {meta['tool'] or '未知'}")
    if meta["input"]:
        parts.append(f"输入摘要: {meta['input']}")
    parts.append(f"原始长度: {len(content)} 字符 / ~{original_tokens} tokens")

    return {
        **msg,
        "content": "\n".join(parts),
    }


def light_compress_tool_results(messages: List[Dict]) -> None:
    """
    轻量压缩：将已消费的工具结果替换为短摘要（**就地修改**）。

    判断标准：如果 tool_result 之后有 assistant 消息（LLM 已看到结果），
    说明该结果已被"消费"，可以安全压缩。

    参数:
        messages: 消息列表（会被就地修改）
    """
    n = len(messages)
    if n < 3:
        return

    # 从后往前扫描，标记哪些 tool_result 已被 LLM 消费
    # （因为后续有 assistant 回复）
    has_assistant_ahead = [False] * n
    seen_assistant = False
    for i in range(n - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "assistant":
            seen_assistant = True
        elif msg.get("role") == "user" and msg.get("name") == "tool_result":
            has_assistant_ahead[i] = seen_assistant
        # 其他消息不改变 seen_assistant

    # 压缩已消费的工具结果
    for i in range(n):
        if has_assistant_ahead[i]:
            messages[i] = _compress_tool_result(messages[i])


# ============================================================
# 全量压缩：LLM 结构化摘要
# ============================================================

# 默认保留的尾部队列长度
_DEFAULT_TAIL_N = 8


def find_safe_tail_boundary(messages: List[Dict], tail_n: int = _DEFAULT_TAIL_N) -> int:
    """
    找到安全的 tail 起始位置，确保不割裂 tool_use ↔ tool_result 对。

    API 约束：如果消息列表包含 user(name=tool_result)，前方必须有对应的
    assistant（含 ACTION）消息作为 tool_use，否则调用会报错。

    返回:
        boundary index，messages[boundary:] 即为安全的 tail
    """
    n = len(messages)

    # 消息太少，全部保留
    if n <= tail_n + 1:  # +1 给 system
        return 0

    # 候选 tail 起始位置
    tail_start = max(0, n - tail_n)

    # 如果 tail 第一条是 tool_result，向前包含对应的 tool_use（assistant）
    if tail_start < n:
        first = messages[tail_start]
        if first.get("role") == "user" and first.get("name") == "tool_result":
            if tail_start > 0:
                tail_start -= 1  # 包含前面的 assistant（tool_use）

    return tail_start


def _format_messages_for_summary(messages: List[Dict]) -> str:
    """将消息列表格式化为可读文本，供摘要 LLM 使用"""
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        name = msg.get("name", "")
        content = _content_to_text(msg.get("content", ""))

        label = role.upper()
        if name:
            label += f" ({name})"

        # 太长就截断，避免超出摘要模型的输入窗口
        MAX_LEN = 2000
        if len(content) > MAX_LEN:
            content = content[:MAX_LEN] + f"\n……（截断，共 {len(content)} 字符）"

        parts.append(f"[{label}]\n{content}\n")
    return "\n".join(parts)


# ============================================================
# Compressor 类（整合两种策略）
# ============================================================

class Compressor:
    """上下文压缩器，管理 Agent 的上下文窗口"""

    # 全量压缩触发阈值（相对于 max_history_tokens）
    WARN_RATIO = 0.60     # 60% 时提示用户
    AUTO_RATIO = 0.80     # 80% 时自动压缩
    LIGHT_ALWAYS = True   # 轻量压缩始终运行

    def __init__(self, llm, tail_n: int = _DEFAULT_TAIL_N):
        """
        参数:
            llm: HelloAgentsLLM 实例（用于全量压缩）
            tail_n: 全量压缩时保留的尾部消息数
        """
        self._llm = llm
        self._tail_n = tail_n

    # ---- 轻量压缩 ----

    def light_compress(self, messages: List[Dict]) -> bool:
        """
        轻量压缩：规则替换旧工具结果。

        返回:
            True 表示有消息被压缩
        """
        old_total = sum(len(_content_to_text(m.get("content", ""))) for m in messages)
        light_compress_tool_results(messages)
        new_total = sum(len(_content_to_text(m.get("content", ""))) for m in messages)
        return new_total < old_total

    # ---- 全量压缩 ----

    def check_and_compact(
        self,
        store,
        messages: List[Dict],
        verbose: bool = True,
    ) -> bool:
        """
        检查上下文占用率，必要时提示或自动压缩。

        参数:
            store: MessageStore 实例
            messages: 消息列表（会被就地修改）
            verbose: 是否输出提示信息

        返回:
            True 表示执行了全量压缩
        """
        max_tokens = store.max_tokens
        if max_tokens <= 0:
            return False

        ratio = store.live_tokens() / max_tokens

        if ratio >= self.AUTO_RATIO:
            if verbose:
                print(f"\n  📐 上下文占用 {ratio:.0%}，自动执行全量压缩…")
            return self.full_compress(store, messages, verbose=verbose)

        elif ratio >= self.WARN_RATIO:
            if verbose:
                print(f"\n  ⚠️  上下文占用 {ratio:.0%}，输入 /compact 可压缩历史释放空间")
            return False

        return False

    def full_compress(
        self,
        store,
        messages: List[Dict],
        verbose: bool = True,
    ) -> bool:
        """
        全量压缩：LLM 结构化摘要 + 安全 tail。

        流程：
          1. 找到安全的 tail 边界（不割裂 tool_use/tool_result 对）
          2. 边界前的消息发送给 LLM 做结构化摘要
          3. 用摘要 + tail 替换原始消息

        参数:
            store: MessageStore 实例
            messages: 消息列表（会被替换）
            verbose: 是否输出提示信息

        返回:
            True 表示压缩成功
        """
        n = len(messages)
        if n < 4:
            if verbose:
                print("  ℹ️  消息太少，无需压缩")
            return False

        # ---- 1. 找到安全边界 ----
        tail_start = find_safe_tail_boundary(messages, self._tail_n)
        if tail_start < 1:
            if verbose:
                print("  ℹ️  当前上下文很短，无需压缩")
            return False

        compressible = messages[:tail_start]
        tail = messages[tail_start:]

        if verbose:
            print(f"  📐 压缩: {len(compressible)} 条消息 → 摘要，保留 tail {len(tail)} 条")

        # ---- 2. 构建摘要提示 ----
        history_text = _format_messages_for_summary(compressible)
        prompt = SUMMARY_PROMPT.format(history=history_text)

        # ---- 3. 调用 LLM 生成摘要 ----
        try:
            summary = self._llm.think(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                stream=False,       # 非流式，直接拿完整结果
                silent=True,        # 不输出模式标签
                timeout=300,        # 摘要生成文本量大，给 5 分钟超时
            )
        except Exception as e:
            if verbose:
                print(f"  ❌ 压缩失败: {e}")
            return False

        if not summary:
            if verbose:
                print("  ❌ 压缩失败: LLM 返回空")
            return False

        summary = summary.strip()

        if verbose:
            summary_preview = summary[:200].replace("\n", " ")
            print(f"  ✅ 全量压缩完成（摘要: {summary_preview}…）")

        # ---- 4. 替换消息列表 ----
        system = messages[0]  # 保留原始 system prompt
        new_messages = [system]

        # 摘要作为一条 user 消息插入（清晰标明是历史摘要）
        new_messages.append({
            "role": "user",
            "kind": "history_summary",
            "internal": True,
            "content": (
                f"【历史对话摘要】以下是之前的完整对话记录，已压缩为摘要：\n\n"
                f"{summary}\n\n"
                f"【摘要结束】请结合上述历史继续当前对话。"
            ),
        })

        new_messages.extend(tail)

        # 替换（保持列表对象不变，以免外部引用断开）
        messages.clear()
        messages.extend(new_messages)

        # ---- 5. 锚点失效——下次 API 调用重新校准 ----
        store._anchor_total = 0
        store._anchor_msg_count = 0

        return True
