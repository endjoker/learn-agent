#!/usr/bin/env python3
"""接口冒烟测试：针对运行中的网关做只读探测 + 安全断言。

用法：
    .venv/bin/python scripts/api_smoke.py [--base http://127.0.0.1:9120]

覆盖：
- 基础可达性（/health、/ui/、静态资源）
- 核心 API（/api/status、/api/sessions、/api/modes、/api/agents、/api/workspaces、
  /api/skills、/api/mcp、/api/config、/api/plans、/api/goals）
- SSE 事件流首事件可达（/api/events）
- 安全断言：配置回传中密钥字段必须已脱敏；Origin:null 的写请求必须被拒（CSRF）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time

import requests

SECRET_KEY_RE = re.compile(
    r"(api_key|apikey|token|secret|password|authorization|encrypt_key|appkey)",
    re.IGNORECASE,
)
MASK_HINTS = ("*", "<masked", "…", "...", "(空)")


class Result:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.passed.append(name)
        print(f"  ✅ {name} {detail}")

    def bad(self, name: str, detail: str = "") -> None:
        self.failed.append(name)
        print(f"  ❌ {name} {detail}")


def check_json(res: requests.Response, name: str, r: Result) -> dict | list | None:
    if res.status_code != 200:
        r.bad(name, f"HTTP {res.status_code}")
        return None
    try:
        res.json()
    except ValueError:
        r.bad(name, "响应非 JSON")
        return None
    r.ok(name)
    return res.json()


def walk_secrets(obj, path=""):
    """产出 (路径, 值)：所有键名疑似凭据的字符串叶子。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}/{k}"
            if isinstance(v, str) and SECRET_KEY_RE.search(k) and v:
                yield p, v
            else:
                yield from walk_secrets(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_secrets(v, f"{path}[{i}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9120")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    r = Result()

    print(f"== 接口冒烟测试 @ {base} ==")

    # --- 可达性 ---
    try:
        res = requests.get(f"{base}/health", timeout=10)
        (r.ok if res.status_code == 200 else r.bad)("GET /health", f"HTTP {res.status_code}")
    except Exception as e:  # noqa: BLE001
        r.bad("GET /health", repr(e))
        print("\n网关不可达，终止。")
        return 1

    try:
        res = requests.get(f"{base}/ui/", timeout=10)
        html_ok = res.status_code == 200 and "<div id" in res.text or "script" in res.text
        (r.ok if res.status_code == 200 and html_ok else r.bad)(
            "GET /ui/ 返回 HTML", f"HTTP {res.status_code}")
    except Exception as e:  # noqa: BLE001
        r.bad("GET /ui/", repr(e))

    # --- 核心 API ---
    for name, path in [
        ("GET /api/status", "/api/status"),
        ("GET /api/sessions", "/api/sessions"),
        ("GET /api/modes", "/api/modes"),
        ("GET /api/agents", "/api/agents"),
        ("GET /api/workspaces", "/api/workspaces"),
        ("GET /api/skills", "/api/skills"),
        ("GET /api/mcp", "/api/mcp"),
        ("GET /api/plans", "/api/plans?session_key=__smoke__"),
        ("GET /api/goals", "/api/goals?session_key=__smoke__"),
        ("GET /api/approvals", "/api/approvals"),
        ("GET /api/questions", "/api/questions"),
    ]:
        try:
            check_json(requests.get(base + path, timeout=15), name, r)
        except Exception as e:  # noqa: BLE001
            r.bad(name, repr(e))

    # --- 配置脱敏断言 ---
    try:
        cfg = check_json(requests.get(f"{base}/api/config", timeout=10),
                         "GET /api/config", r)
        if cfg is not None:
            leaks = []
            for p, v in walk_secrets(cfg):
                if not any(h in v for h in MASK_HINTS):
                    leaks.append(p)
            (r.ok if not leaks else r.bad)(
                "配置回传密钥已脱敏",
                "" if not leaks else f"明文字段: {leaks[:5]}")
    except Exception as e:  # noqa: BLE001
        r.bad("配置脱敏检查", repr(e))

    # --- SSE 首事件 ---
    got_event = {}

    def _sse():
        try:
            with requests.get(f"{base}/api/events", stream=True, timeout=10) as res:
                if res.status_code != 200:
                    got_event["err"] = f"HTTP {res.status_code}"
                    return
                # 连接建立即有 ": connected" 注释行；任何一行（含注释/ping）
                # 都证明 SSE 通道活性，无需等待业务事件。
                for line in res.iter_lines(decode_unicode=True):
                    if line:
                        got_event["first"] = line[:80]
                        return
                got_event["err"] = "连接关闭且无任何行"
        except Exception as e:  # noqa: BLE001
            got_event["err"] = repr(e)

    t = threading.Thread(target=_sse, daemon=True)
    t.start()
    t.join(timeout=12)
    if "first" in got_event:
        r.ok(f"SSE /api/events 首事件可达 ({got_event['first'][:40]}…)")
    else:
        r.bad("SSE /api/events", got_event.get("err", "超时无事件"))

    # --- CSRF: Origin:null 写请求应被拒 ---
    try:
        res = requests.post(
            f"{base}/api/sessions/__smoke__/clear",
            headers={"Origin": "null", "Content-Type": "application/json"},
            data=json.dumps({}),
            timeout=10,
        )
        (r.ok if res.status_code == 403 else r.bad)(
            "CSRF: Origin:null 写请求被拒(403)", f"实际 HTTP {res.status_code}")
    except Exception as e:  # noqa: BLE001
        r.bad("CSRF Origin:null 检查", repr(e))

    # --- 同源写请求不应被误伤（无 Origin 头的本地调用放行到业务层）---
    try:
        res = requests.post(
            f"{base}/api/sessions/__smoke_nonexistent__/stop",
            headers={"Content-Type": "application/json"},
            data=json.dumps({}),
            timeout=10,
        )
        # 不要求特定业务码，只要不是 403（说明同源/无 Origin 未被一刀切拦截）
        (r.ok if res.status_code != 403 else r.bad)(
            "无 Origin 的本地写请求未被 CSRF 拦截", f"HTTP {res.status_code}")
    except Exception as e:  # noqa: BLE001
        r.bad("本地写请求检查", repr(e))

    print(f"\n== 结果: {len(r.passed)} 通过 / {len(r.failed)} 失败 ==")
    for name in r.failed:
        print(f"   失败项: {name}")
    return 0 if not r.failed else 2


if __name__ == "__main__":
    sys.exit(main())
