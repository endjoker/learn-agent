# -*- coding: utf-8 -*-
"""
多模态图片处理 —— 内部格式 ↔ 各协议格式转换 + 图片大小检查/压缩

内部内容块格式：
    {"type": "text", "text": "..."}
    {"type": "image", "source": "base64", "media_type": "image/png", "data": "..."}
    {"type": "image", "source": "file", "path": "workspace/tmp/img.png"}
    # file 源需位于允许根内（set_allowed_image_roots，P1 安全）；越界降级为文本占位
"""

import base64
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Sequence

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

# 像素尺寸上限（最长边）：防解压炸弹（小文件 + 超大像素尺寸）
_MAX_PIXEL_DIM = 8192


# ================================================================
# 图片文件源路径白名单（P1 安全）
# ================================================================
# source="file" 的图片在读取前必须位于任一允许根内（realpath 归属判断）。
# 未调用 set_allowed_image_roots 时保持旧行为（独立使用向后兼容），仅
# logger.warning 提醒一次；create_agent 装配处与工具边界同源注入。
_allowed_image_roots: set = set()
_image_roots_configured: bool = False
_image_roots_warned: bool = False
_image_policy_generation: int = 0
_image_roots_lock = threading.Lock()
# 四档权限感知（2026-08-26 对齐决策）：注入 PermissionChecker 后，图片 file 源
# 路径白名单在 ask/allow/unreviewed 模式下交还四档裁决（放开），readonly 保持
# 白名单（纵深）。未注入（无裁决层的直调路径）时白名单照旧。
_vision_permission = None

# 四档权限模式集合（与 tools/builtin_tools._PERMISSION_LADDER_MODES 同义；
# 不直接 import 以免 core→tools 反向依赖）
_VISION_LADDER_MODES = {"ask", "allow", "unreviewed"}

# 越界降级占位：不抛异常，返回文本块保证对话不中断
_OUT_OF_BOUNDS_PLACEHOLDER = {
    "type": "text",
    "text": "[图片不可访问: 路径超出允许范围]",
}


def set_allowed_image_roots(roots: Sequence[str | Path]) -> None:
    """设置图片文件源（source="file"）路径白名单根（线程安全，锁保护）。

    roots: 可迭代的允许根（str 或 Path），内部统一 expanduser + resolve 为
    realpath 后存储；resolve_image_block 对 file 源同样按 realpath 判断归属，
    与工具读路径边界（create_agent 的 _boundary_roots）语义一致。

    配置后，file 源图片仅允许读取位于任一允许根内的文件；越界路径不抛异常，
    降级为文本占位块，对话继续。每次调用使白名单代次（_image_policy_generation）
    递增，旧策略下缓存的解析结果自动失效（fail-closed，避免跨会话串策略）。
    未调用过本函数时保持旧行为，仅 logger.warning 提醒一次。
    """
    global _allowed_image_roots, _image_roots_configured
    global _image_roots_warned, _image_policy_generation
    resolved = set()
    for r in roots or ():
        resolved.add(Path(r).expanduser().resolve())
    with _image_roots_lock:
        _allowed_image_roots = resolved
        _image_roots_configured = True
        _image_roots_warned = False
        _image_policy_generation += 1


def set_vision_permission(permission) -> None:
    """注入 PermissionChecker（线程安全）：图片 file 源白名单四档权限感知。

    ask/allow/unreviewed 模式下白名单交还四档裁决（放开路径限制，与工具读
    边界的对齐方向一致）；readonly 保持白名单（纵深）。未注入（无裁决层的
    直调路径）时白名单照旧生效。工具持有同一 checker 引用，/perm 切换即时生效。
    """
    global _vision_permission
    with _image_roots_lock:
        _vision_permission = permission


def _vision_defers_to_ladder() -> bool:
    """当前权限模式是否应放开图片白名单（四档感知）。"""
    permission = _vision_permission
    if permission is None:
        return False
    mode = str(getattr(permission, "permission_mode", "") or "")
    return mode in _VISION_LADDER_MODES


def _warn_no_image_roots_once() -> None:
    """未配置白名单时提醒一次（线程安全）。"""
    global _image_roots_warned
    with _image_roots_lock:
        if _image_roots_warned:
            return
        _image_roots_warned = True
    logger.warning(
        "未调用 set_allowed_image_roots：图片 file 源未启用路径白名单校验，"
        "保持旧行为（向后兼容独立使用）。建议在 create_agent 装配处注入允许根。"
    )


def _image_policy_generation_value() -> int:
    """当前白名单代次（set_allowed_image_roots 每次调用 +1）。"""
    with _image_roots_lock:
        return _image_policy_generation


def _image_path_allowed(p: Path) -> bool:
    """file 源路径是否允许读取（realpath 归属任一允许根，双保险）。

    - p.resolve()：解析符号链接/..，防止经 symlink 逃逸出允许根；
    - real.relative_to(root)：按路径分量判断归属，防止 /data/root2 被误判为
      /data/root 之内（纯字符串前缀比较的经典漏洞）。

    未调用 set_allowed_image_roots 时视为允许（保持旧行为），仅提醒一次。
    """
    with _image_roots_lock:
        configured = _image_roots_configured
        roots = _allowed_image_roots
        defer = _vision_defers_to_ladder()
    if defer:
        return True
    if not configured:
        _warn_no_image_roots_once()
        return True
    real = p.resolve()
    for root in roots:
        try:
            real.relative_to(root)
            return True
        except ValueError:
            continue
    return False


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

        # 防解压炸弹：像素尺寸超限直接硬错误上抛（不被下面的通用 except 吞掉）
        w, h = img.size
        if max(w, h) > _MAX_PIXEL_DIM:
            raise ValueError(
                f"图片像素过大（{w}x{h}，最长边 {max(w, h)}px），"
                f"超过 {_MAX_PIXEL_DIM}px 限制。请压缩后重试或发送更小的图片。"
            )

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

    except ValueError:
        raise  # 像素尺寸超限等硬错误必须上抛，不降级为“压缩失败”
    except Exception as e:
        logger.warning(f"Pillow 压缩失败: {e}")
        return None, media_type


# ================================================================
# 内部格式 → base64 统一解析
# ================================================================

# 解析结果缓存 —— 图片块写入历史后内容不再变化，缓存可避免每轮 LLM 调用
# 都重新读盘/压缩/编码。缓存键（A3，去掉每轮 7MB 级 md5）：
#   source=file   → (绝对路径, mtime, size, 白名单代次)；文件更新或
#                   set_allowed_image_roots 变更（代次递增）后自动失效；
#   其余场景      → 数据本身身份（base64 直接以 data 字符串为键，零哈希开销）。
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

    P1 安全（source="file"）：已配置 set_allowed_image_roots 时，文件 realpath
    必须位于任一允许根内；越界不抛异常，直接返回文本占位块
    {"type":"text","text":"[图片不可访问: 路径超出允许范围]"}，对话不中断。
    未配置白名单时保持旧行为（向后兼容），仅 logger.warning 提醒一次。
    """
    source = block.get("source", "")

    if source == "base64":
        # A3：直接以 base64 数据本身为键（身份即键），去掉每轮对 7MB 级
        # 数据做 md5 的 CPU 开销；同一字符串对象跨轮复用，无额外拷贝。
        key = ("b64", block.get("data", ""))
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
        # P1 安全：realpath 必须位于任一允许根内（_image_path_allowed 双保险）。
        # 越界不抛异常，降级为文本占位块，保证对话继续不中断；占位不入缓存。
        if not _image_path_allowed(p):
            return dict(_OUT_OF_BOUNDS_PLACEHOLDER)
        st = p.stat()
        # 缓存键含白名单代次：set_allowed_image_roots 变更后旧策略缓存自动失效
        key = ("file", str(p), st.st_mtime, st.st_size, _image_policy_generation_value())
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
                resolved = resolve_image_block(item)
                if resolved.get("type") == "text":
                    # 路径越界等降级：resolve_image_block 直接返回文本占位块，
                    # 按文本块继续流转（空白块跳过）
                    text = resolved.get("text", "")
                    if text.strip():
                        yield "text", text
                else:
                    yield "image", resolved
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
