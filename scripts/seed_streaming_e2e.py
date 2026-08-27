# -*- coding: utf-8 -*-
"""流式 e2e 专用网关（streamingReal.spec.ts 配套）。

与 seed_real_e2e.py 的区别：本网关带**真实 LLM 配置**（取自项目 config.json）
且 auto_execute_on_send_next=True、permission_mode=allow——专供"真实流式回合"
的浏览器回归（流式期间展开卡片、虚拟列表布局、停止直通）。

用法：JKA_STREAM_PORT=19121 python scripts/seed_streaming_e2e.py &
环境变量：
  JKA_STREAM_PORT      监听端口（默认 19121）
  JKA_STREAM_MODEL     覆盖默认模型（默认沿用 config.json 的 llm.model_id）
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from gateway.server import GatewayServer


def cfg(tmp: Path, name: str) -> dict:
    from core.config_loader import load_config

    llm = load_config().get("llm", {})
    if os.environ.get("JKA_STREAM_MODEL") and isinstance(llm, dict):
        llm = {**llm, "model_id": os.environ["JKA_STREAM_MODEL"]}
    workspace = tmp / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "note.txt").write_text("streaming-e2e-seed\n", encoding="utf-8")
    return {
        'host': '127.0.0.1', 'port': int(os.environ.get("JKA_STREAM_PORT", "19121")),
        'channels': {'debug': {'enabled': False}, 'webui': {'enabled': True}},
        'webui': {'allow_non_loopback': False,
                  'conversation': {'auto_execute_on_send_next': True,
                                   'max_global_turns': 20}},
        'scheduler': {'enabled': False}, 'heartbeat': {'enabled': False},
        'sessions': {'max_sessions': 10, 'idle_timeout_minutes': 5,
                     'persist': False, 'worker_pool_size': 1},
        'agent': {'max_steps': 30, 'quiet': True, 'permission_mode': 'allow'},
        'permission': {'workspace': str(workspace)},
        'workspace': {'path': str(workspace)},
        'llm': llm,
        'runtime_store': {'path': str(tmp / f'{name}.db'), 'wal': False,
                          'busy_timeout_ms': 1000},
        'task_runtime': {'enabled': True, 'max_global_concurrency': 1,
                         'max_attempts': 1, 'cancel_grace_seconds': 0,
                         'zombie_max_seconds': 1},
        'artifacts': {'root': str(tmp / f'{name}-artifacts'),
                      'max_file_bytes': 1048576},
        'retention': {'enabled': False},
    }


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="jkagent-stream-e2e-")
    config = cfg(Path(tmp), "stream")
    # 隔离 sessions_map.json：它是项目级文件（gateway/sessions_map.json），
    # 跨所有启动共享——里面可能残留旧会话的 model 偏好（如 0731），会让
    # agent_factory 恢复会话模型时覆盖本 launcher 指定的模型。重定向到 tmp。
    import gateway.agent_factory as _af
    _af._MAP_FILE = Path(tmp) / "sessions_map.json"
    # 关键：agent 侧 JKAgentLLM 读的是全局 load_config()（config.json），不是
    # gateway 子段的 llm——模型覆盖必须写进全局配置缓存，否则仍会用 config.json
    # 的默认模型（其 token-plan 配额已耗尽）。
    model = os.environ.get("JKA_STREAM_MODEL")
    if model:
        from core import config_loader as _cl
        global_cfg = _cl.load_config()
        if isinstance(global_cfg.get("llm"), dict):
            global_cfg["llm"]["model_id"] = model
        _cl._config_cache = global_cfg
        _cl._config_loaded = True
        print(f"STREAM_E2E_MODEL_OVERRIDE {model}", flush=True)
    server = GatewayServer(config)
    asyncio.create_task(server.start())
    await asyncio.sleep(0)
    print(f"STREAM_E2E_READY port={config['port']} workspace={config['permission']['workspace']}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
