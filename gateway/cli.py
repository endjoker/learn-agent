# -*- coding: utf-8 -*-
"""
Gateway CLI 入口 —— jkagent-gateway [子命令]

子命令:
  (无)                 启动 gateway 服务
  login weixin         微信扫码登录（获取凭据）
  doctor               检查配置和环境（--dry-run）
  migrate-sessions     迁移旧会话转录 sessions/*.json → 统一会话库
                       （--dry-run 只预览不写库）
"""

import logging
import sys
from pathlib import Path

logger = logging.getLogger("jk_agent.gateway")


def main(args: list[str] | None = None, debug: bool = False) -> int:
    """Gateway console entry point; accepts argv for tests and embedding."""
    if args is None:
        args = sys.argv[1:]
    # 日志初始化：basicConfig 语义交由 core.debug.setup_logging 的进程级
    # once-guard 统一管理（幂等：重复调用直接返回，避免 handler 叠加）。
    # gateway-run.log 落盘于 gateway/ 目录，RotatingFileHandler 10MB × 5，
    # 与 sandbox-audit.log 同策略。
    from core.debug import setup_logging, set_debug
    setup_logging(
        debug=debug,
        log_file=str(Path(__file__).resolve().parent / "gateway-run.log"),
        console_format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if debug:
        set_debug(True)

    if not args or args[0] == "run":
        return _cmd_run(args[1:], debug)
    elif args[0] == "login":
        return _cmd_login(args[1:])
    elif args[0] == "doctor":
        return _cmd_doctor(args[1:])
    elif args[0] == "migrate-sessions":
        return _cmd_migrate_sessions(args[1:])
    else:
        print(f"❌ 未知子命令: {args[0]}")
        print("用法: jkagent-gateway [run|login weixin|doctor|migrate-sessions]")
        return 1


def _cmd_run(extra_args: list[str], debug: bool) -> int:
    """启动 gateway 服务"""
    from gateway.config import get_gateway_config
    from gateway.server import GatewayServer

    config = get_gateway_config()
    if not config.get("enabled", True):
        print("❌ gateway 已在配置中禁用 (gateway.enabled=false)")
        return 1

    # 命令行 --port 覆盖
    port = None
    for i, a in enumerate(extra_args):
        if a == "--port" and i + 1 < len(extra_args):
            try:
                port = int(extra_args[i + 1])
            except ValueError:
                print(f"❌ 无效端口: {extra_args[i + 1]}")
                return 1
    # is not None：--port 0（绑定随机可用端口）也必须生效
    if port is not None:
        config["port"] = port

    # C2 退役检查：未迁移的旧会话转录文件 > 0 时告警（不打断启动）。
    # 选择在 cli 侧做（本组文件边界内，零跨组接线）；session_migrate.count_legacy()
    # 为公共 API，server 组如需在服务内部提示亦可一行接入。
    try:
        from gateway.session_migrate import count_legacy
        legacy_count = count_legacy()
        if legacy_count > 0:
            logger.warning(
                "检测到 %d 个旧会话转录文件（sessions/*.json）未迁移到统一"
                "会话库，建议先预览后迁移: "
                "jkagent-gateway migrate-sessions --dry-run",
                legacy_count)
    except Exception:
        logger.debug("旧会话迁移检查失败（忽略，不阻断启动）", exc_info=True)

    server = GatewayServer(config)
    try:
        server.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_login(args: list[str]) -> int:
    """登录子命令"""
    if not args or args[0] != "weixin":
        print("用法: jkagent-gateway login weixin")
        return 1
    return _login_weixin()


def _login_weixin() -> int:
    """微信扫码登录"""
    try:
        from weixin_ilink import login as weixin_login
    except ImportError:
        print("❌ 缺少 weixin-ilink 依赖，请运行:")
        print("   pip install weixin-ilink[qr]")
        return 1

    import os
    creds_dir = os.path.join(os.path.dirname(__file__), "creds")
    os.makedirs(creds_dir, exist_ok=True)
    creds_file = os.path.join(creds_dir, "weixin.json")

    print("\n🔐 微信登录")
    print("  请使用微信扫描下方二维码…\n")
    try:
        weixin_login(save_to=creds_file)
        print(f"\n✅ 登录成功，凭据已保存到 {creds_file}")
        print("   现在可以启动 gateway: jkagent-gateway run")
        return 0
    except Exception as e:
        print(f"\n❌ 登录失败: {e}")
        return 1


def _cmd_migrate_sessions(args: list[str]) -> int:
    """迁移旧会话转录（sessions/*.json → runtime DB conversations）"""
    from gateway.session_migrate import migrate_sessions

    dry_run = "--dry-run" in args
    print(f"\n📦 会话存储迁移（C2 退役）{'—— dry-run 预览，不写库' if dry_run else ''}\n")
    report = migrate_sessions(dry_run=dry_run)

    print(f"  扫描目录:   {report['sessions_dir']}")
    print(f"  会话库:     {report['db_path']}")
    print(f"  扫描文件:   {report['scanned']} 个"
          f"（已覆盖 {report['covered']} / 待迁移 {report['pending']}）")
    if dry_run:
        print(f"  将迁移:     {report['migrated']} 个（dry-run，未写库）")
    else:
        print(f"  已迁移:     {report['migrated']} 个"
              f"（跳过 {report['skipped']}，错误 {report['errors']}）")
    if report["errors"]:
        print(f"  ⚠️  错误: {report['errors']} 个")

    print()
    for item in report["items"]:
        mark = {
            "covered": "⬜", "pending": "🔜", "migrated": "✅",
            "skipped": "⏭️", "error": "❌",
        }.get(item["status"], "·")
        detail = f"  {item['detail']}" if item["detail"] else ""
        print(f"  {mark} {item['session_id']:<22} → {item['session_key']}{detail}")

    if dry_run:
        print()
        print("  预览无误后执行（迁移幂等，可重复运行）:")
        print("    jkagent-gateway migrate-sessions")
    return 1 if report["errors"] else 0


def _cmd_doctor(args: list[str]) -> int:
    """配置检查"""
    from gateway.config import get_gateway_config

    dry_run = "--dry-run" in args
    config = get_gateway_config()
    ok = True

    print("\n🔍 Gateway 配置检查\n")

    # 基础
    print(f"  服务地址:   {config.get('host', '127.0.0.1')}:{config.get('port', 9120)}")
    print(f"  线程池:     {config.get('worker_pool_size', 4)}")
    print(f"  会话上限:   {config.get('sessions', {}).get('max_sessions', 50)}")

    # 飞书
    feishu = config.get("channels", {}).get("feishu", {})
    if feishu.get("enabled", False):
        has_id = bool(feishu.get("app_id"))
        has_secret = bool(feishu.get("app_secret"))
        status = "✅" if (has_id and has_secret) else "❌ 缺少 app_id/app_secret"
        print(f"  飞书:       {status}")
        if not (has_id and has_secret):
            ok = False
    else:
        print(f"  飞书:       ⬜ 未启用")

    # 微信
    weixin = config.get("channels", {}).get("weixin", {})
    if weixin.get("enabled", False):
        import os
        creds = weixin.get("credentials_file", "gateway/creds/weixin.json")
        exists = os.path.exists(creds)
        status = "✅" if exists else f"❌ 凭据文件不存在: {creds}"
        print(f"  微信:       {status}")
        if not exists:
            print(f"             运行: jkagent-gateway login weixin")
            ok = False
    else:
        print(f"  微信:       ⬜ 未启用")

    # 调试通道
    debug = config.get("channels", {}).get("debug", {})
    print(f"  调试通道:   {'✅ 已启用' if debug.get('enabled', True) else '⬜ 未启用'}")

    # 定时任务
    sched = config.get("scheduler", {})
    if sched.get("enabled", True):
        from croniter import croniter
        jobs = sched.get("jobs") or []
        issues = []
        for j in jobs:
            nm = j.get("name", "?")
            if not j.get("prompt"):
                issues.append(f"{nm}: 缺少 prompt")
                continue
            try:
                croniter(j.get("schedule", ""))
            except Exception:
                issues.append(f"{nm}: cron 表达式无效 {j.get('schedule')!r}")
            d = j.get("deliver") or {}
            if d.get("mode") == "announce" and not (d.get("channel") and d.get("target")):
                issues.append(f"{nm}: announce 投递必须显式指定 channel+target")
        if issues:
            ok = False
            print(f"  定时任务:   ❌ {len(jobs)} 个 job，{len(issues)} 个问题")
            for i in issues:
                print(f"             - {i}")
        else:
            print(f"  定时任务:   ✅ 已启用（{len(jobs)} 个 job）")
    else:
        print(f"  定时任务:   ⬜ 未启用")

    # Retention: doctor exposes active policy and candidates without deleting anything.
    try:
        from core.config_loader import _find_project_root, load_config
        from core.runtime import ArtifactStore, RetentionManager, RuntimeStore
        root_cfg = load_config()
        retention = root_cfg.get("retention", {})
        store_cfg = root_cfg.get("runtime_store", {})
        configured = Path(str(store_cfg.get("path") or "./workspace/.agent/state/runtime.db"))
        store_path = configured if configured.is_absolute() else _find_project_root() / configured
        store = RuntimeStore(store_path, wal=store_cfg.get("wal", True), busy_timeout_ms=store_cfg.get("busy_timeout_ms", 5000))
        artifact_cfg = root_cfg.get("artifacts", {})
        report = RetentionManager(store, ArtifactStore(store, artifact_cfg.get("root") or None), terminal_days=retention.get("terminal_days", 30), artifact_days=retention.get("artifact_days", 30)).collect(dry_run=True)
        print(f"  Retention:   {'✅' if retention.get('enabled', True) else '⬜'} {retention.get('terminal_days', 30)} 天终态 / {retention.get('artifact_days', 30)} 天 Artifact；候选 task={len(report['tasks'])} artifact={len(report['artifacts'])}，引用保护={len(report['protected'])}")
    except Exception as exc:
        print(f"  Retention:   ⚠️ 检查失败: {exc}")

    # 心跳
    hb = config.get("heartbeat", {})
    if hb.get("enabled", True):
        from gateway.heartbeat import _resolve_prompt_file
        pf = _resolve_prompt_file(hb.get("prompt_file", "workspace/HEARTBEAT.md"))
        exists = pf.exists()
        status = "✅" if exists else f"⚠️ 清单文件不存在: {pf}"
        print(f"  心跳:       {status}（每 {hb.get('every', '30m')}，"
              f"时段 {hb.get('active_hours', 'always')}）")
    else:
        print(f"  心跳:       ⬜ 未启用")

    # WebUI
    webui_ch = config.get("channels", {}).get("webui", {})
    if webui_ch.get("enabled", True):
        from gateway.webui import STATIC_DIR
        host = config.get("host", "127.0.0.1")
        allow_nb = config.get("webui", {}).get("allow_non_loopback", False)
        loopback = host in ("127.0.0.1", "::1", "localhost")
        if not loopback and not allow_nb:
            print(f"  WebUI:      ❌ host={host} 非环回且未开 allow_non_loopback，"
                  f"WebUI 将被 403")
            ok = False
        elif not loopback and allow_nb:
            print(f"  WebUI:      ⚠️  红色告警: host={host} 非环回且已开 "
                  f"allow_non_loopback。WebUI 可写 config/改 prompt/切权限，"
                  f"暴露到非环回请确认风险")
        else:
            print(f"  WebUI:      ✅ http://{host}:{config.get('port', 9120)}/ui/")
        if not STATIC_DIR.exists():
            print(f"             ⚠️  static 目录不存在: {STATIC_DIR}")
            ok = False
    else:
        print(f"  WebUI:      ⬜ 未启用")

    if dry_run:
        print(f"\n  (dry-run 模式，不启动服务)")

    print()
    return 0 if ok else 1
