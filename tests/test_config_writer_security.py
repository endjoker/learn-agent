# -*- coding: utf-8 -*-
"""P1/P2 安全修复回归测试：config_writer 密钥脱敏 + 备份轮转。

覆盖：
  - _SECRET_KEY_RE 扩充（encrypt_key / appkey / access_key 等）
  - mask_key 统一 <masked:N> 样式，不保留明文片段
  - is_masked_placeholder 新旧占位样式兼容
  - backup_file 轮转保留最近 BACKUP_KEEP=3 份（按 mtime 删旧）
  - ConfigService 读侧脱敏 + 写侧占位符保留端到端
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core.config_writer import (
    BACKUP_KEEP,
    backup_file,
    is_masked_placeholder,
    is_secret_key_name,
    mask_dict,
    mask_key,
)


class SecretKeyRegexTests(unittest.TestCase):
    def test_extended_key_names_masked(self):
        """encrypt_key/appkey/app_key/access_key 等扩充键名必须命中。"""
        sample = {
            "llm": {"models": {"m": {
                "api_key": "sk-real-1234",
                "encrypt_key": "e" * 32,
                "appkey": "a" * 20,
                "app_key": "b" * 18,
                "access_key": "c" * 16,
                "refresh_token": "rt-9999",
                "client_secret": "cs-real-secret",
                "app_secret": "as-real-secret",
            }}},
        }
        out = mask_dict(sample)
        flat = json.dumps(out, ensure_ascii=False)
        # 任何明文片段都不得残留
        for frag in ("sk-real", "e" * 8, "a" * 8, "b" * 8,
                     "c" * 8, "rt-9999", "real-secret"):
            self.assertNotIn(frag, flat)
        models = out["llm"]["models"]["m"]
        self.assertEqual(models["api_key"], "<masked:12>")
        self.assertEqual(models["encrypt_key"], f"<masked:{len('e' * 32)}>")
        self.assertEqual(models["appkey"], f"<masked:20>")

    def test_separator_variants(self):
        for name in ("api-key", "apikey", "X-Api-Key".lower(),
                     "encryption_key", "encrypted_key", "private_key",
                     "authorization", "password", "secret"):
            self.assertTrue(is_secret_key_name(name), name)

    def test_plain_keys_not_matched(self):
        for name in ("sort_key", "primary_key", "model_id", "base_url",
                     "keys_count", "tokenize_mode" if False else "max_tokens_hint"):
            # 含 token 子串的键按既有语义仍会打码；这里只验证裸 key 不命中
            if "token" in name:
                continue
            self.assertFalse(is_secret_key_name(name), name)

    def test_mask_key_never_keeps_plaintext(self):
        secret = "sk-abcd1234efgh5678"
        masked = mask_key(secret)
        self.assertEqual(masked, f"<masked:{len(secret)}>")
        self.assertNotIn("abcd", masked)
        self.assertNotIn("5678", masked)
        # 短密钥与旧 **** 样式
        self.assertEqual(mask_key("short"), "<masked:5>")
        self.assertEqual(mask_key(""), "(空)")


class MaskedPlaceholderTests(unittest.TestCase):
    def test_new_style(self):
        self.assertTrue(is_masked_placeholder("<masked:11>"))
        self.assertTrue(is_masked_placeholder("  <masked:42>  "))

    def test_legacy_styles_still_recognized(self):
        """历史前端会话可能缓存旧掩码回传，必须继续保留原值。"""
        self.assertTrue(is_masked_placeholder("****"))
        self.assertTrue(is_masked_placeholder("sk-f1…d412"))

    def test_real_values_not_placeholder(self):
        self.assertFalse(is_masked_placeholder("sk-real-key-123"))
        self.assertFalse(is_masked_placeholder(123))
        self.assertTrue(is_masked_placeholder(""))


class BackupRotationTests(unittest.TestCase):
    def test_rotation_keeps_recent_n_by_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            now = time.time()
            # 预置 5 份"陈旧"备份，mtime 依次变新
            for i in range(5):
                bak = Path(td) / f"config.json.bak-stale{i}"
                bak.write_text(f"backup-{i}", encoding="utf-8")
                os.utime(bak, (now - 6000 + i * 100, now - 6000 + i * 100))
            created = backup_file(cfg)   # 默认 keep=BACKUP_KEEP
            self.assertIsNotNone(created)
            baks = sorted(Path(td).glob("config.json.bak-*"),
                          key=lambda p: p.stat().st_mtime_ns)
            self.assertEqual(len(baks), BACKUP_KEEP)
            # 最新一份必须是刚创建的，且最旧的 stale0/stale1 已被删除
            self.assertEqual(baks[-1].name, created.name)
            names = {p.name for p in baks}
            self.assertNotIn("config.json.bak-stale0", names)
            self.assertNotIn("config.json.bak-stale1", names)

    def test_keep_zero_clears_old_backups(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            now = time.time()
            # 陈旧备份显式置于过去；copy2 会让新备份继承 cfg 的 mtime(=now)，
            # 保证"本次新建"是按 mtime 排序的最新一份
            os.utime(cfg, (now, now))
            old = Path(td) / "config.json.bak-old"
            old.write_text("old", encoding="utf-8")
            os.utime(old, (now - 5000, now - 5000))
            created = backup_file(cfg, keep=0)
            self.assertIsNotNone(created)
            remaining = list(Path(td).glob("config.json.bak-*"))
            self.assertEqual([p.name for p in remaining], [created.name])

    def test_no_source_file_no_backup(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(backup_file(Path(td) / "missing.json"))


class ConfigServiceRoundtripTests(unittest.TestCase):
    """读侧脱敏 + 写侧占位符保留的端到端（临时目录，不碰真实 config.json）。"""

    def test_mask_read_and_preserve_on_patch(self):
        from gateway.webui.config_service import ConfigService

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            real_key = "sk-live-key-abcdef"
            encrypt_key = "enc-" + "z" * 40
            cfg_path.write_text(json.dumps({
                "llm": {"models": {"m1": {
                    "api_key": real_key,
                    "encrypt_key": encrypt_key,
                    "base_url": "https://api.example.com",
                }}},
                "gateway": {"sessions": {"max_sessions": 50}},
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            def fake_path():
                return cfg_path

            svc = ConfigService()
            with mock.patch.object(ConfigService, "_force_reload",
                                   staticmethod(lambda: None)), \
                 mock.patch("core.config_writer.default_config_path", fake_path), \
                 mock.patch("gateway.webui.config_service.default_config_path",
                            fake_path):
                # ---- 读侧：GET 全脱敏，无任何明文片段 ----
                data, rev, status = svc.read_masked()
                self.assertEqual(status, "loaded")
                model = data["llm"]["models"]["m1"]
                self.assertEqual(model["api_key"], f"<masked:{len(real_key)}>")
                self.assertEqual(model["encrypt_key"],
                                 f"<masked:{len(encrypt_key)}>")
                self.assertNotIn(real_key[:6], json.dumps(data))

                # ---- 写侧：回传掩码占位符 → 原值保留（含扩充键名）----
                new_rev = asyncio_run(svc.patch_section("llm", {
                    "models": {"m1": {
                        "api_key": f"<masked:{len(real_key)}>",
                        "encrypt_key": f"<masked:{len(encrypt_key)}>",
                        "timeout": 60,
                    }},
                }))
                self.assertGreater(new_rev, 0)

                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                m1 = raw["llm"]["models"]["m1"]
                self.assertEqual(m1["api_key"], real_key)
                self.assertEqual(m1["encrypt_key"], encrypt_key)
                self.assertEqual(m1["timeout"], 60)


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
