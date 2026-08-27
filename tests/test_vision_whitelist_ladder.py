# -*- coding: utf-8 -*-
"""图片 file 源白名单的四档权限感知（对齐决策 1·方案2）回归。

PolicyEngine 对 fs:read 全模式无条件 ALLOW——项目权限只遵循四项基本权限，
图片白名单不得对读追加四档之外的限制：注入 PermissionChecker 后，
ask/allow/unreviewed 模式下白名单交还裁决（放开）；readonly 保持白名单
（纵深）；未注入（无裁决层的直调路径）保持白名单。
"""
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.protocols.vision import (
    resolve_image_block,
    set_allowed_image_roots,
    set_vision_permission,
)


def _perm(mode):
    return SimpleNamespace(permission_mode=mode) if mode else None


class VisionWhitelistLadderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_vision_tmp"
        self.tmp.mkdir(exist_ok=True)
        self.inside = self.tmp / "in.png"
        self.inside.write_bytes(b"\x89PNG fake")
        self.outside = self.tmp.parent / "_vision_outside.png"
        self.outside.write_bytes(b"\x89PNG fake")
        set_allowed_image_roots([self.tmp])

    def tearDown(self):
        set_vision_permission(None)
        set_allowed_image_roots([])
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.outside.unlink(missing_ok=True)

    def _block(self, path):
        # 真实 schema：source 为标量 "file"，路径在 block["path"]
        return {"type": "image", "source": "file", "path": str(path)}

    def test_outside_blocked_without_permission(self):
        """未注入权限的直调路径：白名单是唯一防线。"""
        block = resolve_image_block(self._block(self.outside))
        self.assertEqual(block["type"], "text")
        self.assertIn("超出允许范围", block["text"])

    def test_outside_allowed_in_ladder_modes(self):
        for mode in ("ask", "allow", "unreviewed"):
            with self.subTest(mode=mode):
                set_vision_permission(_perm(mode))
                block = resolve_image_block(self._block(self.outside))
                self.assertNotEqual(block["type"], "text", block)

    def test_outside_still_blocked_in_readonly(self):
        set_vision_permission(_perm("readonly"))
        block = resolve_image_block(self._block(self.outside))
        self.assertEqual(block["type"], "text")

    def test_inside_allowed_regardless(self):
        for mode in (None, "readonly", "ask", "allow", "unreviewed"):
            with self.subTest(mode=mode):
                set_vision_permission(_perm(mode))
                block = resolve_image_block(self._block(self.inside))
                self.assertNotEqual(block["type"], "text")


if __name__ == "__main__":
    unittest.main()
