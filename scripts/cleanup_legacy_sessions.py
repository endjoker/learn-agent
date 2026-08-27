#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理旧会话转录文件（sessions/*.json）。

⚠️ 2026-08-26 退役修正：本脚本曾计划同时清空 runtime.db 的
channel_delivery / runtime_events / tasks / sessions 四张表——经核查这四张表
是现役任务运行模型（dispatcher 补投/幂等、TaskRuntime 租约、scheduler、
heartbeat 均在活跃写入），已从删除清单移除。gateway/sessions_map.json 仍是
非工作区会话元数据的现役存储，同样不再删除。

现在只做一件事：物理删除 sessions/*.json 旧会话转录文件。
（SQLite 统一会话为唯一权威，恢复路径已改为纯 SQLite 回放，
sessions/*.json 不再被任何代码读取。）

用法：
    .venv/bin/python scripts/cleanup_legacy_sessions.py [--dry-run]

不带 --dry-run 会先打印清单并要求交互确认（y/N）；
--dry-run 只打印将删除的文件清单，不做任何写操作。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect_file_targets(project_root: Path) -> list[Path]:
    """收集将被物理删除的文件清单（只读探测，不做任何修改）。"""
    return sorted((project_root / "sessions").glob("*.json"))


def execute_cleanup(project_root: Path, file_targets: list[Path]) -> dict:
    """真正执行删除：仅物理删除 sessions/*.json 转录文件（不碰数据库）。"""
    removed_files = 0
    for f in file_targets:
        f.unlink()
        removed_files += 1
    return {"files": removed_files}


def print_plan(file_targets: list[Path]) -> None:
    print("📋 将删除的文件:")
    if not file_targets:
        print("  （无匹配文件）")
    for f in file_targets:
        try:
            rel = f.relative_to(ROOT)
        except ValueError:
            rel = f
        print(f"  - {rel}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="清理旧会话转录文件（sessions/*.json）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将删除的文件清单，不做任何写操作")
    args = parser.parse_args()

    file_targets = collect_file_targets(ROOT)

    if args.dry_run:
        print("🔍 DRY-RUN 预览模式：不会删除任何文件。\n")
        print_plan(file_targets)
        print("✅ 预览结束（未做任何修改）。去掉 --dry-run 并确认后才执行。")
        return 0

    print("=" * 64)
    print("⚠️  警告：本次运行将【物理删除】sessions/*.json 旧会话转录文件，")
    print("⚠️  操作不可恢复！如需先预览清单，请加 --dry-run 参数重新运行。")
    print("=" * 64)
    print_plan(file_targets)
    try:
        answer = input("确认执行物理删除？(y/N): ").strip().lower()
    except EOFError:
        answer = ""  # 非交互环境下无输入，按取消处理
    if answer != "y":
        print("已取消：未删除任何文件。")
        return 1

    result = execute_cleanup(ROOT, file_targets)
    print("✅ 清理完成")
    print("  - 删除文件:", result["files"], "个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
