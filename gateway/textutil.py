# -*- coding: utf-8 -*-
"""
文本工具 —— 长文分片、错误脱敏、多平台格式转换
"""

import json
import re


# ============================================================
# 长文分片
# ============================================================

def split_text(text: str, max_len: int = 3500) -> list[str]:
    """
    将长文本按段落/句子边界分片，每片不超过 max_len 字符。
    用于飞书（~3500）和微信（~1500）的消息长度限制。
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # 尝试在 max_len 范围内找段落/句子断点
        cut = max_len
        # 优先段落断点
        idx = remaining.rfind("\n\n", 0, max_len)
        if idx > max_len // 3:
            cut = idx + 2
        else:
            # 次优：换行
            idx = remaining.rfind("\n", 0, max_len)
            if idx > max_len // 3:
                cut = idx + 1
            else:
                # 兜底：句号/问号/感叹号
                for sep in ("。", "！", "？", ". ", "! ", "? "):
                    idx = remaining.rfind(sep, 0, max_len)
                    if idx > max_len // 3:
                        cut = idx + len(sep)
                        break
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks


# ============================================================
# 错误脱敏
# ============================================================

def sanitize_error(err: Exception) -> str:
    """错误信息脱敏：去除路径、密钥等敏感内容"""
    msg = str(err)
    # 去除文件路径
    msg = re.sub(r'[A-Za-z]:\\[^\s"\']+', "<path>", msg)
    msg = re.sub(r'/[^\s"\']+/[^\s"\']+', "<path>", msg)
    # 去除疑似 API key
    msg = re.sub(r'sk-[a-zA-Z0-9]{20,}', "sk-****", msg)
    # 截断
    if len(msg) > 500:
        msg = msg[:500] + "…"
    return msg


# ============================================================
# 多平台格式转换
# ============================================================

def md_to_feishu_card(
    text: str,
    model: str = "",
    elapsed: float = 0,
    agent_name: str = "JKagent",
) -> dict:
    """
    Markdown 文本 -> 飞书 Card 2.0 JSON dict。

    设计参考 dify-on-lark / LangBot:
      header(蓝色) + markdown 正文(按段落拆分) + hr + 灰色 footer
    """
    # 飞书 md 元素在第一个 \n\n 处截断，需按段落拆成多个 md 元素
    paragraphs = re.split(r"\n\n+", text.strip())
    elements = []
    for para in paragraphs:
        para = para.strip()
        if para:
            elements.append({"tag": "markdown", "content": para})

    # footer: 模型 + 耗时
    footer_parts = []
    if model:
        footer_parts.append(model)
    if elapsed > 0:
        footer_parts.append(f"{elapsed:.1f}s")
    footer_text = " · ".join(footer_parts) if footer_parts else ""

    if footer_text:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [{
                    "tag": "markdown",
                    "content": f'<font color="grey-600">{footer_text}</font>',
                    "text_size": "notation",
                }],
            }],
        })

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": agent_name},
            "template": "blue",
        },
        "body": {"elements": elements},
    }
    return card


def md_to_plain(text: str) -> str:
    """
    Markdown -> 纯文本（微信等不支持富文本的平台）。

    去掉格式符号，保留可读性。
    """
    # 代码块：去掉 fence 标记，保留内容
    text = re.sub(r"```\w*\n?", "", text)
    # inline code：去掉反引号
    text = text.replace("`", "")
    # bold/italic：去掉星号
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    # strikethrough
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # links: [text](url) → text (url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    # heading: # text → text
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 表格分隔行删除
    text = re.sub(r"^\|[-:| ]+\|$", "", text, flags=re.MULTILINE)
    # 表格行：去掉 | 边界，保留内容
    text = re.sub(r"^\|\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s?\|$", "", text, flags=re.MULTILINE)
    text = text.replace(" | ", "  ")
    # 清理多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
