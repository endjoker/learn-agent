# -*- coding: utf-8 -*-
"""会话图片存取（修正版方案 A）。

原图落盘 `<db目录>/images/<conversation_id>/<image_id>.<ext>`，历史里只存
引用（turn_nodes 的 image 节点 metadata：ref/mime/size）。SQLite 不保存
base64 正文——列表/回放/渲染都拖引用而非兆级字段。

- ref 形如 `conv_xxx/att_<hex>.png`（相对 images 根，双段防跨会话枚举）；
- 读取端点必须先做 conversation 归属校验，再走 resolve()（内部再拒绝
  ref 越出该会话目录，双保险）。
"""
from __future__ import annotations

import binascii
import base64
import logging
import os
import re
import uuid
from pathlib import Path

logger = logging.getLogger("jk_agent.gateway")

_REF_RE = re.compile(r"^[A-Za-z0-9_\-]+/[A-Za-z0-9_\-]+\.(png|jpg|jpeg|webp|gif)$")

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


class ImageStore:
    """会话图片的文件存取（挂在 runtime db 同目录的 images/ 下）。"""

    def __init__(self, root: Path):
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    # ---- 写 ------------------------------------------------------------

    def save(self, conversation_id: str, data_b64: str, media_type: str) -> dict:
        """base64 → 落盘，返回引用三元组 {ref, media_type, size}。

        非法输入抛 ValueError（调用方已在信封校验层过滤，此处是双保险）。
        """
        ext = _MIME_EXT.get(media_type)
        if not ext:
            raise ValueError(f"不支持的图片类型: {media_type}")
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"图片 base64 解码失败: {exc}") from exc
        conv_dir = self._safe_conv_dir(conversation_id)
        conv_dir.mkdir(parents=True, exist_ok=True)
        image_id = f"att_{uuid.uuid4().hex[:12]}"
        path = conv_dir / f"{image_id}.{ext}"
        path.write_bytes(raw)
        return {"ref": f"{conversation_id}/{path.name}",
                "media_type": media_type, "size": len(raw)}

    # ---- 读 ------------------------------------------------------------

    def resolve(self, conversation_id: str, ref: str) -> Path:
        """解析 ref 为文件路径；归属不符/路径越界抛 ValueError。"""
        if not _REF_RE.match(str(ref or "")):
            raise ValueError("非法图片引用")
        conv_dir = self._safe_conv_dir(conversation_id)
        target = (self._root / ref).resolve()
        if target.parent != conv_dir:
            raise ValueError("图片引用与会话不匹配")
        if not target.exists():
            raise FileNotFoundError(ref)
        return target

    def load_b64(self, conversation_id: str, ref: str) -> str:
        return base64.b64encode(self.resolve(conversation_id, ref).read_bytes()).decode("ascii")

    # ---- 删除 ----------------------------------------------------------

    def delete_conversation(self, conversation_id: str) -> int:
        """级联删除会话图片目录（随 delete_conversation_by_key 联动）。

        返回删除的文件数；目录不存在返回 0。与 resolve 同一套归属校验，
        只允许删除 images/<conversation_id> 边界内。"""
        conv_dir = self._safe_conv_dir(conversation_id)
        removed = 0
        if conv_dir.exists():
            for path in conv_dir.iterdir():
                if path.is_file():
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        logger.warning("会话图片删除失败: %s", path)
            try:
                conv_dir.rmdir()
            except OSError:
                pass  # 目录非空（残留子目录）时保守保留
        return removed

    # ---- 内部 ----------------------------------------------------------

    def _safe_conv_dir(self, conversation_id: str) -> Path:
        if not re.match(r"^[A-Za-z0-9_\-]+$", str(conversation_id or "")):
            raise ValueError("非法会话 id")
        conv_dir = (self._root / conversation_id).resolve()
        if conv_dir.parent != self._root:
            raise ValueError("会话目录越界")
        os.makedirs(conv_dir, exist_ok=True)
        return conv_dir
