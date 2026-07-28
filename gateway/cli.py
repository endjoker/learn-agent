# -*- coding: utf-8 -*-
"""
Gateway CLI 入口 —— python agent.py gateway [子命令]

子命令:
  (无)          启动 gateway 服务
  login weixin  微信扫码登录（获取凭据）
  doctor        检查配置和环境（--dry-run）
"""

import logging
import sys

logger = logging.getLogger("hello_agent.gateway")


def main(args: list[str], debug: bool = False) -> int:
    """gateway 子命令主入口，返回退出码"""
    # 设置日志
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args or args[0] == "run":
        return _cmd_run(args[1:], debug)
    elif args[0] == "login":
        return _cmd_login(args[1:])
    elif args[0] == "doctor":
        return _cmd_doctor(args[1:])
    else:
        print(f"❌ 未知子命令: {args[0]}")
        print("用法: python agent.py gateway [run|login weixin|doctor]")
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
    if port:
        config["port"] = port

    server = GatewayServer(config)
    try:
        server.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_login(args: list[str]) -> int:
    """登录子命令"""
    if not args or args[0] != "weixin":
        print("用法: python agent.py gateway login weixin")
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
        print("   现在可以启动 gateway: python agent.py gateway")
        return 0
    except Exception as e:
        print(f"\n❌ 登录失败: {e}")
        return 1


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
            print(f"             运行: python agent.py gateway login weixin")
            ok = False
    else:
        print(f"  微信:       ⬜ 未启用")

    # 调试通道
    debug = config.get("channels", {}).get("debug", {})
    print(f"  调试通道:   {'✅ 已启用' if debug.get('enabled', True) else '⬜ 未启用'}")

    if dry_run:
        print(f"\n  (dry-run 模式，不启动服务)")

    print()
    return 0 if ok else 1
