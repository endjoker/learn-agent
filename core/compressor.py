"""
上下文压缩器 —— 管理 Agent 的上下文窗口

提供两种压缩策略：
  1. 轻量压缩（方案A）：规则替换旧工具结果为短摘要
  2. 全量压缩（/compact）：LLM 生成结构化摘要替代早期对话

用法：
    compressor = Compressor(llm, tail_n=8)
    old_total, new_total = compressor.light_compress(messages)  # 单遍压缩，返回节省量
    compressor.full_compress(messages)        # LLM 结构化摘要
"""

import logging
import re
from pathlib import Path
from typing import List, Dict, Optional

from core.message_store import _content_to_text, estimate_tokens

logger = logging.getLogger("jk_agent")


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
# 增量压缩用的合并摘要提示词模板（A4）
# ============================================================

INCREMENTAL_SUMMARY_PROMPT = """你正在维护一份随对话推进持续更新的中文结构化摘要。以下是"已有摘要"和"新增对话片段"，请将新增片段合并进已有摘要，输出更新后的完整中文摘要。

要求：
- 保留已有摘要中仍然相关的全部关键信息（文件名、函数名、行号、错误信息、待办任务、当前正在做的事等）
- 融入新增片段中的新信息，删除已被取代/已过时的内容
- 简洁精准，代码片段保持原样，不要多余解释
- 直接输出合并后的摘要正文，不要输出任何说明文字

## 已有摘要
{old_summary}

## 新增对话片段
{new_segment}

直接输出合并后的中文摘要："""


# ============================================================
# 轻量压缩：工具结果摘要 + 已消费图片降级
# ============================================================

# 工具结果已压缩 stub 的标记（B9 幂等判断依据）
_COMPRESSED_MARKER = "【工具执行结果 - 已压缩】"

# 图片降级占位前缀（A3：图片被模型消费一轮后不再每轮重发 base64）
_IMAGE_PLACEHOLDER_PREFIX = "[图片已在前文查看]"

# ---- 轻量压缩压力门控（修复"工具输出被过早压缩 → 模型反复重读同一文件"）----
# 旧行为：每轮工具批次后无条件把"后面出现过 assistant"的工具结果压成 stub。
# 这会让模型在同一个 Turn 内刚读的文件内容立刻消失，只能再次 read_file，
# 而新读到的内容下一步又被压缩 → 无限重复读文件。
# 新策略：
#   1. 压力门控 —— 仅当历史预算占用 ≥ LIGHT_RESULT_RATIO 才压缩工具结果
#      （预算未知时保持旧的始终压缩行为，作为保守兜底）；
#   2. 近端保护 —— 无论占用多少，最近 LIGHT_KEEP_RECENT_RESULTS 条"仍有内容"
#      的工具结果永不压缩（模型的当前工作集必须保持可见）。
LIGHT_RESULT_RATIO = 0.60
LIGHT_KEEP_RECENT_RESULTS = 12


def tool_compress_needed(live_tokens: int, max_tokens: int,
                         ratio: float = LIGHT_RESULT_RATIO) -> bool:
    """判断当前是否需要对工具结果做轻量压缩（压力门控）。

    max_tokens 非正（未配置历史预算）时视为有压力，保持旧的"始终压缩"行为；
    有预算时仅在占用达到 ratio 阈值后才压缩。
    """
    if not max_tokens or max_tokens <= 0:
        return True
    return live_tokens >= max_tokens * ratio


def _content_tokens(msg: Dict) -> int:
    """消息内容 token 估算（str/list 原生处理，与 message_store 口径一致）。

    按 token 统计而非字符数：图片 block 估算 1000 token，降级为文本占位符后
    立即体现节省；工具结果压缩同理（大段输出 → 短元数据）。
    """
    return estimate_tokens(msg.get("content", ""))


def _image_placeholder_name(block: dict) -> str:
    """从图片 block 提取文件名：file 源取 basename，base64 源取 media_type。"""
    if block.get("source") == "file" and block.get("path"):
        return Path(str(block["path"])).name
    mt = block.get("media_type", "")
    if mt and "/" in mt:
        return mt.split("/")[-1]
    return "图片"


def _image_placeholder_block(block: dict) -> dict:
    """生成图片降级占位文本 block。"""
    return {"type": "text", "text": f"{_IMAGE_PLACEHOLDER_PREFIX}（{_image_placeholder_name(block)}）"}


def _downgrade_consumed_images(msg: Dict) -> bool:
    """把已消费消息中的 image block 替换为文本占位符（就地修改）。

    幂等（B9）：占位符块是 type="text"，再次扫描时不会命中 image 分支。
    返回是否发生了替换。
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    changed = False
    new_blocks = []
    for block in content:
        # 幂等（B9）：占位符块是 type="text"，天然不匹配 image 分支，不会重复替换
        if isinstance(block, dict) and block.get("type") == "image":
            new_blocks.append(_image_placeholder_block(block))
            changed = True
        else:
            new_blocks.append(block)
    if changed:
        msg["content"] = new_blocks
    return changed


def _is_tool_result(msg: Dict) -> bool:
    """Covers both tool-result wire formats.

    * legacy: ``{"role": "user", "name": "tool_result", ...}``
    * native: ``{"role": "tool", "tool_call_id": ..., ...}``

    The native format carries ``kind == "tool_result"`` on result messages
    appended by the native tool-call loop; a plain ``role == "tool"`` message
    is a tool result by construction.
    """
    role = msg.get("role")
    if role == "user" and msg.get("name") == "tool_result":
        return True
    return role == "tool"


def _extract_tool_meta(content: str, msg: Dict | None = None) -> dict:
    """从工具结果中提取元数据（工具名、输入参数）"""
    meta = {"tool": "", "input": "", "is_batch": False, "count": 0}

    lines = content.split("\n")
    for line in lines:
        if line.startswith("工具:"):
            meta["tool"] = line[3:].strip()
        elif line.startswith("输入摘要:"):
            meta["input"] = line[5:].strip()

    # 原生 tool 结果：消息本身携带工具名（msg["name"] / msg["tool_call_id"]），
    # content 是原始 observation，不含"工具:"行，需要从消息字段回填，否则
    # 压成摘要后丢失"这是哪个工具的返回"信息，模型上下文退化。
    if not meta["tool"] and msg is not None:
        meta["tool"] = str(msg.get("name") or "") or "未知"

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

    # B9 幂等：已含压缩标记的 stub 直接原样返回，避免重复包裹
    if _COMPRESSED_MARKER in content:
        return msg

    # 估算原始 token 数（粗略）
    chinese = sum(1 for c in content if '一' <= c <= '鿿')
    other = len(content) - chinese
    original_tokens = int(chinese / 1.5 + other / 4)

    # 提取元数据（原生 tool 结果回填消息上的工具名）
    meta = _extract_tool_meta(content, msg)

    parts = [_COMPRESSED_MARKER]
    if meta["is_batch"]:
        parts.append(f"批量执行: {meta['count']} 个工具")
    else:
        parts.append(f"工具: {meta['tool'] or '未知'}")
    if meta["input"]:
        parts.append(f"输入摘要: {meta['input']}")
    parts.append(f"原始长度: {len(content)} 字符 / ~{original_tokens} tokens")
    # 明确告知模型内容已释放：需要时重新调用工具获取，而不是凭空推测
    parts.append("（内容已释放；如仍需要该结果，请重新调用该工具获取）")

    return {
        **msg,
        "content": "\n".join(parts),
    }


def light_compress_tool_results(
    messages: List[Dict],
    *,
    compress_results: bool = True,
    keep_recent_results: int = 0,
) -> tuple[int, int]:
    """
    轻量压缩：将已消费的工具结果替换为短摘要，并把已消费消息中的图片
    block 降级为文本占位符（**就地修改**，A3）。

    判断标准：如果消息之后有 assistant 消息（LLM 已看到内容），
    说明该内容已被"消费"，可以安全压缩/降级。

    同时覆盖两种工具结果格式：
      * 旧版：``role="user"`` + ``name="tool_result"``
      * 新版原生：``role="tool"``（带 ``tool_call_id`` / ``kind="tool_result"``）

    B9-part：单遍反向扫描同时完成消费判断与压缩/降级，返回
    (old_total, new_total)（按 message_store 口径的 token 估算，供调用方
    统计节省量）；对已含压缩标记的 stub 与已降级的图片占位符幂等跳过。

    压力门控参数（修复"过早压缩 → 模型反复重读同一文件"）：
        compress_results: False 时跳过工具结果压缩（图片降级仍执行）。
        keep_recent_results: 从尾部起最近 N 条"仍有内容"的工具结果永不
            压缩（模型当前工作集保护；已压缩 stub 不占保护名额）。

    参数:
        messages: 消息列表（会被就地修改）
    返回:
        (压缩前 token 估算, 压缩后 token 估算)
    """
    old_total = 0
    new_total = 0
    seen_assistant = False
    recent_content_results = 0  # 从尾部起已扫过的"仍有内容"的工具结果数
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        old_len = _content_tokens(msg)
        old_total += old_len

        if msg.get("role") == "assistant":
            seen_assistant = True
            new_total += old_len
            continue
        if not seen_assistant:
            # 尚无后续 assistant —— 模型还未消费，保留原样
            new_total += old_len
            continue

        # 已被后续 assistant 消费：
        if _is_tool_result(msg):
            if not compress_results:
                new_total += old_len
                continue
            if keep_recent_results > 0:
                # 近端保护：最近 N 条仍有内容的工具结果保持原样（已压缩
                # stub 不占名额，保证保护窗口留给模型还看得见的内容）。
                content_text = _content_to_text(msg.get("content", ""))
                if _COMPRESSED_MARKER not in content_text:
                    recent_content_results += 1
                    if recent_content_results <= keep_recent_results:
                        new_total += old_len
                        continue
                # 已压缩 stub 落到这里：_compress_tool_result 幂等原样返回。
            compressed = _compress_tool_result(msg)
            messages[i] = compressed
            new_total += _content_tokens(compressed)
        elif _downgrade_consumed_images(msg):
            new_total += _content_tokens(messages[i])
        else:
            new_total += old_len

    return old_total, new_total


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

    # 如果 tail 第一条是 tool_result，向前包含对应的 tool_use（assistant）。
    # 兼容两种格式：
    #   新版原生 function calling：role="tool"（tool_call_id 关联前面的
    #     assistant tool_calls 消息）；
    #   旧版：role="user" + name="tool_result"（ACTION 消息）。
    # 否则压缩后 tail 开头会留下孤立 tool 消息，provider 直接 400
    # （"Messages with role 'tool' must be a response to a preceding
    #   message with 'tool_calls'"），导致 Plan/Goal 轮次失败。
    if tail_start < n:
        first = messages[tail_start]
        if first.get("role") == "tool":
            # 一组连续的 tool 结果整体前移，再带上配对的那条 assistant。
            while tail_start > 0 and messages[tail_start - 1].get("role") == "tool":
                tail_start -= 1
            if tail_start > 0 and messages[tail_start - 1].get("role") == "assistant":
                tail_start -= 1
        elif first.get("role") == "user" and first.get("name") == "tool_result":
            if tail_start > 0:
                tail_start -= 1  # 包含前面的 assistant（tool_use）

    return tail_start


# 摘要输入 history_text 总量上限（字符）：超限时从最旧消息开始丢弃，
# 保留更贴近 tail 的近期消息，避免摘要请求超出摘要模型的输入窗口。
DEFAULT_SUMMARY_HISTORY_LIMIT = 60000


# A4-1：摘要输入上限动态化：min(model_ctx // 8, 300000)，替换原固定 6 万字符。
# 大上下文模型获得更充分的摘要输入；小模型按窗口 1/8 收敛，避免摘要请求
# 超出摘要模型的输入窗口。model_context_length 未知/非正时回退旧常量（保持
# 既有行为）。
def _dynamic_summary_history_limit(model_context_length: Optional[int]) -> int:
    if not model_context_length or model_context_length <= 0:
        return DEFAULT_SUMMARY_HISTORY_LIMIT
    return min(model_context_length // 8, 300000)


def _format_messages_for_summary(
    messages: List[Dict],
    max_total_chars: int = DEFAULT_SUMMARY_HISTORY_LIMIT,
) -> str:
    """将消息列表格式化为可读文本，供摘要 LLM 使用。

    history 总量超过 max_total_chars 时从最旧消息开始截断
    （保留更贴近 tail 的近期消息），并在末尾注明省略条数。
    """
    collected = []
    total = 0
    MAX_LEN = 2000
    for msg in reversed(messages):  # 从最新往最旧累积，超限时丢弃最旧的
        role = msg.get("role", "unknown")
        name = msg.get("name", "")
        content = _content_to_text(msg.get("content", ""))

        label = role.upper()
        if name:
            label += f" ({name})"

        # 太长就截断，避免超出摘要模型的输入窗口
        if len(content) > MAX_LEN:
            content = content[:MAX_LEN] + f"\n……（截断，共 {len(content)} 字符）"

        entry = f"[{label}]\n{content}\n"
        if total + len(entry) > max_total_chars:
            if collected:
                break  # 至少保留一条最近的
            entry = entry[:max(0, max_total_chars)] + "\n……（截断）\n"
        collected.append(entry)
        total += len(entry)

    collected.reverse()
    return "\n".join(collected)


# ============================================================
# Compressor 类（整合两种策略）
# ============================================================

class Compressor:
    """上下文压缩器，管理 Agent 的上下文窗口"""

    # 全量压缩触发阈值（相对于 max_history_tokens）
    WARN_RATIO = 0.60     # 60% 时提示用户
    AUTO_RATIO = 0.80     # 80% 时自动压缩（仅在 provider turn 前检查）
    EMERGENCY_RATIO = 0.95 # 95% 仅作为最终截断保护
    LIGHT_ALWAYS = True   # 轻量压缩始终运行

    def __init__(self, llm, tail_n: int = _DEFAULT_TAIL_N):
        """
        参数:
            llm: JKAgentLLM 实例（用于全量压缩）
            tail_n: 全量压缩时保留的尾部消息数
        """
        self._llm = llm
        self._tail_n = tail_n

    # ---- 轻量压缩 ----

    def light_compress(
        self,
        messages: List[Dict],
        *,
        compress_results: bool = True,
        keep_recent_results: int = 0,
    ) -> tuple[int, int]:
        """
        轻量压缩：规则替换旧工具结果 + 降级已消费图片（单遍扫描）。

        压力门控参数见 light_compress_tool_results：
          compress_results=False 时跳过工具结果压缩（仅降级图片）；
          keep_recent_results 保护最近 N 条仍有内容的工具结果不被压缩。

        返回:
            (old_total, new_total)：压缩前后 token 估算（message_store 口径）
        """
        return light_compress_tool_results(
            messages,
            compress_results=compress_results,
            keep_recent_results=keep_recent_results,
        )

    # ---- 全量压缩 ----

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

        # ---- 2. 构建摘要提示（A4-1：输入上限随模型窗口动态化）----
        history_text = _format_messages_for_summary(
            compressible,
            max_total_chars=_dynamic_summary_history_limit(
                getattr(self._llm, "context_length", 0) or 0))
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
        # 按 role=="system" 定位系统消息（不假定它在下标 0）
        system = next((m for m in messages if m.get("role") == "system"), None)
        if system is None:
            system = messages[0]  # 兜底：无 system 消息时保留首条
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

        # ---- 5. 锚点失效——下次 API 调用重新校准（走公开方法，不碰私有字段） ----
        store.reset_anchor()

        return True

    # ---- 增量压缩（A4）----

    def incremental_summarize(self, old_summary: str,
                              new_segment: List[Dict]) -> Optional[str]:
        """A4-2：增量摘要——将新增对话片段合并进旧摘要，返回更新后的摘要文本。

        old_summary 为空（尚无 history_summary）时返回 None，调用方据此退化为
        全量压缩路径（与现有行为一致，不额外消耗一次独立的从头摘要）。

        参数:
            old_summary: 现有 history_summary 的文本内容（可能为空）
            new_segment: 摘要之后新增的消息列表（按时间序）
        返回:
            合并后的摘要文本；无法生成时返回 None
        """
        old_summary = (old_summary or "").strip()
        if not old_summary:
            return None  # 无旧摘要 → 退化为现行为（全量压缩）

        history_text = _format_messages_for_summary(
            new_segment,
            max_total_chars=_dynamic_summary_history_limit(
                getattr(self._llm, "context_length", 0) or 0))
        if not history_text.strip():
            return None

        prompt = INCREMENTAL_SUMMARY_PROMPT.format(
            old_summary=old_summary[:DEFAULT_SUMMARY_HISTORY_LIMIT],
            new_segment=history_text)
        try:
            summary = self._llm.think(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                stream=False,       # 非流式，直接拿完整结果
                silent=True,        # 不输出模式标签
                timeout=300,        # 与全量压缩一致：给 5 分钟超时
            )
        except Exception as e:
            logger.warning("增量摘要失败: %s", e)
            return None
        if not summary:
            return None
        return summary.strip()

    def incremental_compress(
        self,
        store,
        messages: List[Dict],
        verbose: bool = False,
        commit_guard=None,
    ) -> bool:
        """A4-3：增量压缩——以现有 history_summary 为基底更新摘要。

        与 full_compress 的差异：不重读全量历史，只对摘要之后的片段做 LLM
        合并摘要（省 token/省时）；无 history_summary / 旧摘要为空时退化为
        full_compress（现行为）。

        参数:
            store: MessageStore 实例（提交成功后 reset_anchor）
            messages: 消息列表（会被替换，保持列表对象不变）
            verbose: 是否输出提示信息
            commit_guard: 可选零参回调；提交（替换消息列表）前调用，返回
                False 时放弃提交（并发护栏，调用方自行决定是否重试）

        返回:
            True 表示成功更新了摘要
        """
        n = len(messages)
        if n < 4:
            return False

        summary_idx = next(
            (i for i, m in enumerate(messages)
             if m.get("kind") == "history_summary"),
            None)
        if summary_idx is None:
            # 尚无 history_summary → 退化为现行为（全量压缩）
            return self.full_compress(store, messages, verbose=verbose)

        old_summary = _content_to_text(messages[summary_idx].get("content", ""))
        segment = messages[summary_idx + 1:]
        if len(segment) < 2:
            return False  # 摘要后无实质新内容

        # 安全 tail（与 full_compress 同语义：不割裂 tool_use ↔ tool_result 对）
        tail_start = find_safe_tail_boundary(segment, self._tail_n)
        if tail_start < 1:
            return False  # 整段都作为 tail 保留，无可压缩内容
        compressible = segment[:tail_start]
        tail = segment[tail_start:]

        if verbose:
            print(f"  📐 增量压缩: {len(compressible)} 条消息 → 合并进摘要，"
                  f"保留 tail {len(tail)} 条")

        new_summary = self.incremental_summarize(old_summary, compressible)
        if not new_summary:
            return False

        # ---- 提交（替换消息列表，保持列表对象不变）----
        if commit_guard is not None and not commit_guard():
            return False  # 并发护栏：放弃提交，调用方决定是否重试

        system = next((m for m in messages if m.get("role") == "system"), None)
        if system is None:
            system = messages[0]  # 兜底：无 system 消息时保留首条
        new_messages = [system, {
            "role": "user",
            "kind": "history_summary",
            "internal": True,
            "content": (
                f"【历史对话摘要】以下是之前的完整对话记录，已压缩为摘要：\n\n"
                f"{new_summary}\n\n"
                f"【摘要结束】请结合上述历史继续当前对话。"
            ),
        }] + tail

        messages.clear()
        messages.extend(new_messages)
        store.reset_anchor()
        return True
