# -*- coding: utf-8 -*-
"""
多模态图片处理 —— 内部格式 ↔ 各协议格式转换 + 图片大小检查/压缩

内部内容块格式：
    {"type": "text", "text": "..."}
    {"type": "image", "source": "base64", "media_type": "image/png", "data": "..."}
    {"type": "image", "source": "file", "path": "workspace/tmp/img.png"}
"""

import base64
import hashlib
import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any

logger = logging.getLogger("jk_agent")

# 扩展名 → MIME 映射（IMAGE_EXTENSIONS 由其派生，保持单一事实来源）
# 注意：不含 .svg —— OpenAI/Anthropic/Gemini 的 vision 接口均不接受 SVG，
# SVG 文件应按文本读取
_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
}

# 图片扩展名集合
IMAGE_EXTENSIONS = set(_MIME_MAP)

# 大小阈值（字节）
_WARN_SIZE = 5 * 1024 * 1024       # 5MB — 建议压缩
_REJECT_SIZE = 20 * 1024 * 1024     # 20MB — 硬拒绝

# 压缩参数
_MAX_DIM_FIRST = 1536    # 第一轮压缩最长边
_MAX_DIM_SECOND = 1024   # 第二轮压缩最长边
_JPEG_QUALITY = 85


def is_image_file(path: str) -> bool:
    """按扩展名判断是否为图片文件"""
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def guess_media_type(path: str) -> str:
    """根据扩展名推断 MIME 类型"""
    ext = Path(path).suffix.lower()
    return _MIME_MAP.get(ext, "image/png")


# ================================================================
# 图片大小检查 + 压缩
# ================================================================

def check_and_compress_image(
    raw_bytes: bytes,
    media_type: str = "image/png",
) -> tuple[bytes, str, str]:
    """检查图片大小，必要时压缩。

    返回:
        (processed_bytes, media_type, warning_msg)
        warning_msg 为空表示无需警告。

    策略：
        ≤ 5MB  → 直接使用
        5-20MB → Pillow 压缩（最长边 1536→1024 渐进）
        > 20MB → 拒绝（抛 ValueError）
    """
    size = len(raw_bytes)
    if size > _REJECT_SIZE:
        raise ValueError(
            f"图片过大（{size / 1024 / 1024:.1f}MB），超过 20MB 限制。"
            f"请压缩后重试或发送更小的图片。"
        )

    if size <= _WARN_SIZE:
        return raw_bytes, media_type, ""

    # 需要压缩 — 尝试 Pillow
    compressed, new_type = _try_pillow_compress(raw_bytes, media_type)
    if compressed is not None:
        ratio = (1 - len(compressed) / size) * 100
        warning = (
            f"图片已自动压缩：{size / 1024 / 1024:.1f}MB → "
            f"{len(compressed) / 1024 / 1024:.1f}MB（压缩 {ratio:.0f}%）"
        )
        logger.info(warning)
        return compressed, new_type, warning

    # Pillow 不可用 — 仅警告
    warning = (
        f"⚠️ 图片较大（{size / 1024 / 1024:.1f}MB），"
        f"建议压缩后使用。安装 Pillow 可自动压缩：pip install Pillow"
    )
    logger.warning(warning)
    return raw_bytes, media_type, warning


def _try_pillow_compress(
    raw_bytes: bytes, media_type: str
) -> tuple[bytes | None, str]:
    """尝试用 Pillow 压缩图片。返回 (compressed_bytes, new_media_type) 或 (None, ...)"""
    try:
        from PIL import Image
        import io
    except ImportError:
        return None, media_type

    try:
        img = Image.open(io.BytesIO(raw_bytes))

        # RGBA/P 模式转 RGB（JPEG 不支持透明通道）
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
            out_type = "image/jpeg"
        else:
            out_type = media_type

        for max_dim in (_MAX_DIM_FIRST, _MAX_DIM_SECOND):
            w, h = img.size
            if max(w, h) > max_dim:
                ratio = max_dim / max(w, h)
                new_w, new_h = int(w * ratio), int(h * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)

            buf = io.BytesIO()
            if out_type == "image/jpeg":
                img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            else:
                fmt = out_type.split("/")[-1].upper()
                if fmt == "JPG":
                    fmt = "JPEG"
                img.save(buf, format=fmt)

            result = buf.getvalue()
            if len(result) <= _WARN_SIZE:
                return result, out_type

        # 两轮压缩后仍超 5MB — 返回最后结果（比原始小就行）
        return result, out_type

    except Exception as e:
        logger.warning(f"Pillow 压缩失败: {e}")
        return None, media_type


# ================================================================
# 内部格式 → base64 统一解析
# ================================================================

# 解析结果缓存 —— 图片块写入历史后内容不再变化，缓存可避免每轮 LLM 调用
# 都重新读盘/压缩/编码。file 源以 (路径, mtime) 为键，文件更新后自动失效。
_resolve_cache: OrderedDict = OrderedDict()
_CACHE_MAX_ENTRIES = 8   # 每项为压缩后的 base64（≤~7MB），限制条目数控制内存


def _cache_lookup(key: tuple):
    hit = _resolve_cache.get(key)
    if hit is not None:
        _resolve_cache.move_to_end(key)
    return hit


def _cache_store(key: tuple, value: dict) -> None:
    _resolve_cache[key] = value
    _resolve_cache.move_to_end(key)
    while len(_resolve_cache) > _CACHE_MAX_ENTRIES:
        _resolve_cache.popitem(last=False)


def resolve_image_block(block: dict) -> dict:
    """将 file source 的图片块统一转为 base64 格式。

    如果已经是 base64，仅做大小检查。解析结果会被缓存。
    返回新的 block（source=base64）。

    异常（由 _iter_content_blocks 统一降级处理）：
        FileNotFoundError: 文件缺失
        ValueError:        图片超过 20MB / 未知 source
    """
    source = block.get("source", "")

    if source == "base64":
        key = ("b64", hashlib.md5(block.get("data", "").encode("ascii")).hexdigest())
        cached = _cache_lookup(key)
        if cached is not None:
            return cached
        raw = base64.b64decode(block["data"])
        mt = block.get("media_type", "image/png")
    elif source == "file":
        file_path = block["path"]
        p = Path(file_path)
        if not p.exists():
            # 回退：尝试从项目根解析（兼容旧会话中的相对路径）
            p = Path(__file__).resolve().parent.parent.parent / file_path
        if not p.exists():
            raise FileNotFoundError(f"图片文件不存在: {file_path}")
        key = ("file", str(p), p.stat().st_mtime)
        cached = _cache_lookup(key)
        if cached is not None:
            return cached
        raw = p.read_bytes()
        mt = guess_media_type(str(p))
    else:
        raise ValueError(f"未知的 image source: {source}")

    # 大小检查 + 压缩
    raw, mt, _ = check_and_compress_image(raw, mt)

    resolved = {
        "type": "image",
        "source": "base64",
        "media_type": mt,
        "data": base64.b64encode(raw).decode("ascii"),
    }
    _cache_store(key, resolved)
    return resolved


# ================================================================
# 内部格式 → 各协议格式
# ================================================================

def _iter_content_blocks(content: Any):
    """统一遍历多模态 content，yield ("text", str) 或 ("image", resolved_block)。

    - 图片块解析失败（文件缺失、超过 20MB 等）降级为文本占位，
      避免单张坏图片让整个会话后续每一轮调用都永久崩溃。
    - 空白文本块被跳过（Anthropic 拒绝空 text block）。
    """
    for item in content:
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text", "")
            if text.strip():
                yield "text", text
        elif item_type == "image":
            try:
                yield "image", resolve_image_block(item)
            except Exception as e:
                logger.warning(f"图片块不可用，降级为文本占位: {e}")
                yield "text", f"[图片不可用: {e}]"


def content_to_openai(content: Any) -> Any:
    """内部 content → OpenAI vision 格式。str 原样返回。"""
    if isinstance(content, str):
        return content

    blocks = []
    for kind, payload in _iter_content_blocks(content):
        if kind == "text":
            blocks.append({"type": "text", "text": payload})
        else:
            blocks.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{payload['media_type']};base64,{payload['data']}"
                },
            })
    return blocks


def content_to_anthropic(content: Any) -> Any:
    """内部 content → Anthropic vision 格式。str 原样返回。"""
    if isinstance(content, str):
        return content

    blocks = []
    for kind, payload in _iter_content_blocks(content):
        if kind == "text":
            blocks.append({"type": "text", "text": payload})
        else:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": payload["media_type"],
                    "data": payload["data"],
                },
            })
    return blocks


def content_to_gemini_parts(content: Any):
    """内部 content → Gemini Part 列表。需要 google.genai types。"""
    from google.genai import types

    if isinstance(content, str):
        return [types.Part.from_text(text=content)]

    parts = []
    for kind, payload in _iter_content_blocks(content):
        if kind == "text":
            parts.append(types.Part.from_text(text=payload))
        else:
            parts.append(types.Part.from_bytes(
                data=base64.b64decode(payload["data"]),
                mime_type=payload["media_type"],
            ))
    return parts
