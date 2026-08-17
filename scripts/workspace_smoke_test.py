# -*- coding: utf-8 -*-
"""
workspace_smoke_test.py —— 本地工作区 smoke 测试（Phase 6）。

检查：
- runtime.db schema version = 9，5 张工作区表存在
- 创建临时 Profile / Workspace / Session（自动清理）
- 路径校验 API 正常

不访问真实用户项目，不打印密钥。
"""

import argparse
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def check_schema(db_path: Path):
    print(f"[smoke] schema check: {db_path}")
    if not db_path.exists():
        print("[smoke] runtime.db 不存在，跳过 schema 检查")
        return
    with closing(sqlite3.connect(db_path)) as conn:
        ver = conn.execute(
            "SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if ver != 9:
            print(f"[smoke] FAIL: schema version={ver}（期望 9）")
            sys.exit(1)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"workspaces", "agent_profiles", "workspace_sessions",
                    "workspace_runtime_snapshots", "agent_profile_versions"}
        missing = expected - tables
        if missing:
            print(f"[smoke] FAIL: 缺少表 {missing}")
            sys.exit(1)
    print("[smoke] schema OK")


def smoke_store(db_path: Path):
    """创建临时对象并清理。"""
    from gateway.webui.workspace_models import AgentProfile, Workspace
    from gateway.webui.workspace_store import (
        WorkspaceDatabase, WorkspaceStore, AgentProfileStore,
        WorkspaceSessionStore,
    )
    print("[smoke] store smoke…")
    db = WorkspaceDatabase(db_path=db_path)
    ws_store = WorkspaceStore(db)
    prof_store = AgentProfileStore(db)
    sess_store = WorkspaceSessionStore(db)
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "smoke-proj"
        proj.mkdir()
        prof = prof_store.create(AgentProfile(
            profile_id="smoke_agent", name="smoke-agent",
            system_prompt="smoke", tools=["read"]))
        ws, sess = ws_store.create_with_first_session(
            Workspace(workspace_id="smoke_ws", name="smoke",
                      project_path=str(proj),
                      default_agent_profile_id=prof.profile_id),
            {"name": "s1", "agent_profile_id": prof.profile_id})
        assert ws.workspace_id == "smoke_ws"
        assert sess.session_key == "workspace:smoke_ws:" + sess.session_id
        # 清理（归档即可，物理删除由运维决定）
        sess_store.archive("smoke_ws", sess.session_id)
        ws_store.archive("smoke_ws")
        print("[smoke] store smoke OK")


def main():
    parser = argparse.ArgumentParser(description="workspace smoke test")
    parser.add_argument("--db", default="workspace/.agent/state/runtime.db",
                        help="runtime.db 路径")
    parser.add_argument("--project-root", default=".",
                        help="项目根（用于解析相对 db 路径）")
    args = parser.parse_args()
    db = Path(args.db)
    if not db.is_absolute():
        db = Path(args.project_root) / db
    check_schema(db.resolve())
    smoke_store(db.resolve())
    print("[smoke] ALL OK")


if __name__ == "__main__":
    main()
