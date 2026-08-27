# -*- coding: utf-8 -*-
"""ProcessManager cwd 边界的四档权限对齐回归。

项目权限只遵循四项基本权限（readonly/ask/allow/unreviewed 经 PolicyEngine
裁决）：PolicyEngine._PATH_KEYS 含 "cwd"，proc_start 的界外 cwd 在授权层
已按档位裁决（ask 全量 ASK / allow 界外 ASK / unreviewed 放行）——
ProcessManager 的 cwd 硬边界在这些模式下必须让位（此前一票否决导致
「用户确认了也启动不了」，stop_timeout 事故里 proc_start 被拒同源）。
readonly 与未注入权限的直调路径保持硬拒绝。
"""
import shutil
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.process_manager import ProcessManager


class _FakeSandbox:
    """ProcessManager 依赖的最小沙箱接口。"""

    def get_current_profile(self):
        return {"max_processes": 4}

    def get_idle_timeout(self):
        return 60.0

    def get_max_output_bytes(self):
        return 65536


def _perm(mode):
    return SimpleNamespace(permission_mode=mode) if mode else None


class ProcessManagerLadderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_pm_tmp"
        self.tmp.mkdir(exist_ok=True)
        self.outside = self.tmp.parent / "_pm_outside"
        self.outside.mkdir(exist_ok=True)

    def tearDown(self):
        # 清理可能残留的会话进程
        for pm in getattr(self, "_pms", []):
            with pm._lock:
                for s in list(pm._sessions.values()):
                    try:
                        s.proc.kill()
                    except Exception:
                        pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pm(self, mode):
        pm = ProcessManager(_FakeSandbox(), str(self.tmp), permission=_perm(mode))
        if not hasattr(self, "_pms"):
            self._pms = []
        self._pms.append(pm)
        return pm

    def _start(self, pm, cwd):
        sid, output = pm.start("echo ok", cwd=str(cwd))
        return sid, output

    def test_outside_cwd_allowed_modes_execute(self):
        """ask/allow/unreviewed：界外 cwd 交还授权层裁决（此处直接可达）。"""
        for mode in ("ask", "allow", "unreviewed"):
            with self.subTest(mode=mode):
                pm = self._pm(mode)
                sid, output = self._start(pm, self.outside)
                self.assertNotEqual(sid, -1, f"{mode} 模式确认后应可启动: {output}")
                self.assertNotIn("区外 cwd 不允许", output)

    def test_outside_cwd_readonly_still_rejected(self):
        pm = self._pm("readonly")
        sid, output = self._start(pm, self.outside)
        self.assertEqual(sid, -1)
        self.assertIn("区外 cwd 不允许", output)

    def test_outside_cwd_without_permission_still_rejected(self):
        """未注入权限的直调路径：硬边界是唯一防线。"""
        pm = self._pm(None)
        sid, output = self._start(pm, self.outside)
        self.assertEqual(sid, -1)
        self.assertIn("区外 cwd 不允许", output)

    def test_inside_cwd_always_works(self):
        for mode in ("readonly", "ask", "allow", "unreviewed", None):
            with self.subTest(mode=mode):
                pm = self._pm(mode)
                sid, output = self._start(pm, self.tmp)
                self.assertNotEqual(sid, -1, output)
