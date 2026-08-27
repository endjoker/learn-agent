# -*- coding: utf-8 -*-
"""并发会话 WIP 测试隔离（已于 2026-08-26 解除）。

背景：另一工作线程此前进行的三项改造（session_persistence none_content /
agent_factory SQLite 回放恢复 / permission_optimization）已完成——三个曾被
隔离的测试文件经两轮独立实测（绕过 skip 后 32/32 全部通过），回归保护恢复
生效。原 _WIP_PREFIXES 隔离块整体移除；如未来再现未完成的并发改造，可按同
样模式（nodeid 前缀 + pytest.mark.skip）临时恢复。
"""

