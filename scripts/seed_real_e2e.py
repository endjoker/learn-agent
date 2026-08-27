# -*- coding: utf-8 -*-
"""真实网关 → 真实页面 的种子脚本（覆盖修复项的广度种子）。

启动一个真实 GatewayServer（webui enabled，隔离临时 DB），再用其
conversation_bridge 直接驱动事件流，把以下内容写入真实 SQLite：
  * webui:default  —— plan runtime turn（供 planCardReal.spec.ts：内联卡片不平铺）
  * webui:render   —— 普通主会话 turn（user + reasoning + tool + assistant 最终答复，
                      供 mainChatReal.spec.ts：最终恢复渲染 / tool-card / reasoning-card）
  * ws_<wid>/<wss> —— 工作区会话 turn（供 workspaceReal.spec.ts：最终答复渲染为
                      "助手"而非"运行进度"，tool/reasoning 卡片）
随后保持服务直到被终止，供 Playwright 直连真实 /api 验证。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from gateway.server import GatewayServer
from gateway.channels.base import InboundMessage
from gateway.webui.workspace_models import Workspace
from gateway.webui.workspace_store import WorkspaceStore

_SEED_PORT = int(os.environ.get("JKA_SEED_PORT", "9120"))


def cfg(tmp: Path, name: str) -> dict:
    return {
        'host': '127.0.0.1', 'port': _SEED_PORT,
        'channels': {'debug': {'enabled': False}, 'webui': {'enabled': True}},
        'webui': {'allow_non_loopback': False,
                  'conversation': {'auto_execute_on_send_next': False,
                                   'max_global_turns': 100}},
        'scheduler': {'enabled': False}, 'heartbeat': {'enabled': False},
        'sessions': {'max_sessions': 30, 'idle_timeout_minutes': 1,
                     'persist': False, 'worker_pool_size': 1,
                     'soft_timeout_seconds': 1, 'hard_timeout_seconds': 2},
        'agent': {'max_steps': 5, 'quiet': True},
        'runtime_store': {'path': str(tmp / f'{name}.db'), 'wal': False,
                          'busy_timeout_ms': 1000},
        'task_runtime': {'enabled': True, 'max_global_concurrency': 1,
                         'max_attempts': 1, 'cancel_grace_seconds': 0,
                         'zombie_max_seconds': 1},
        'artifacts': {'root': str(tmp / f'{name}-artifacts'),
                      'max_file_bytes': 1024},
        'retention': {'enabled': True, 'interval_seconds': 60},
    }


def agent_event(bridge, msg, event_type, **data):
    bridge.on_agent_event(msg, {"type": event_type, "data": data})


def seed_plan(bridge, session_key: str) -> None:
    msg = InboundMessage(
        channel="webui", session_key=session_key, user_id="e2e",
        user_name="E2E", text="规划一个方案", message_id="seed-plan-1",
        metadata={"task_source": "plan", "plan_id": "seed-plan"})
    agent_event(bridge, msg, "reasoning_delta", text="分析需求：读取文件结构")
    agent_event(bridge, msg, "tool_call_start", tool_call_id="plan-c1", tool="read")
    agent_event(bridge, msg, "tool_call_end", tool_call_id="plan-c1", tool="read",
                arguments={"path": "a.txt"})
    agent_event(bridge, msg, "message_start", role="tool",
                message_id="result_plan-c1", tool="read")
    agent_event(bridge, msg, "message_end", role="tool",
                message_id="result_plan-c1", tool="read", content="total 8")
    bridge.on_reply(msg, "方案完成：读取 a.txt")


def seed_normal_turn(bridge, service, conv, session_key, answer: str,
                     message_id: str, call_id: str) -> None:
    """种一条普通主会话/工作区会话 Turn。

    直接 enqueue + send_next 创建活动 Turn（含 user 节点），再用 bridge 事件流
    写入 reasoning/tool/最终答复。msg 不带 task_source，走普通 Turn 路径。
    call_id 必须每次调用唯一——节点 ID 由 call_id 派生，跨 Turn 复用同一个
    call_id 会让后续 Turn 的工具节点覆盖前一个 Turn 的节点（同 node_id）。
    """
    service.enqueue(conv.conversation_id, "展示工具与答复", create_queued_node=True)
    service.send_next(conv.conversation_id)
    msg = InboundMessage(
        channel="webui", session_key=session_key, user_id="e2e",
        user_name="E2E", text="展示工具与答复", message_id=message_id,
        metadata={"conversation_id": conv.conversation_id})
    result_mid = f"result_{call_id}"
    agent_event(bridge, msg, "reasoning_delta", text="思考：先读取项目结构……")
    agent_event(bridge, msg, "tool_call_start", tool_call_id=call_id, tool="read")
    agent_event(bridge, msg, "tool_call_end", tool_call_id=call_id, tool="read",
                arguments={"path": "a.txt"})
    agent_event(bridge, msg, "message_start", role="tool",
                message_id=result_mid, tool="read")
    agent_event(bridge, msg, "message_end", role="tool",
                message_id=result_mid, tool="read",
                content="total 8\n-rw-r--r-- 1 user user 12 a.txt")
    bridge.on_reply(msg, answer)




def seed_image_turn(bridge, service, conv, session_key, answer: str,
                    message_id: str, call_id: str) -> None:
    """种一条带图片的用户消息 Turn（修正版方案 A 全链路验证）。

    走真实 enqueue(images=…) → send_next：队列信封 → artifacts 落盘 →
    image 引用节点 → user 节点 metadata.images。随后 bridge 事件流写终答。
    """
    _PNG = "iVBORw0KGgoAAAANSUhEUgAAAHgAAABQCAIAAABd+SbeAAAAnElEQVR42u3dQQ0AIAwEwSqpHIShuBKwQJqGB5nLKhgDF2lPFghAfwq9dmk80KBBCzRo0KBBgxZo0KBBgwYt0KBBgwYNWqBBgwYNGrRAgwYNGjRogQYNGjRo0AINGjRo0KAFGjRo0KBBCzRo0KBBgxZo0KBBgwYt0KBBgwYNWqBBgwYNGrRAgwYNGjRogQYNGjRo0OpCm68s0Ha9A/A0P//tAiuYAAAAAElFTkSuQmCC"
    service.enqueue(
        conv.conversation_id, "看看这张图", create_queued_node=True,
        images=[{"data": _PNG, "media_type": "image/png"}])
    service.send_next(conv.conversation_id)
    msg = InboundMessage(
        channel="webui", session_key=session_key, user_id="e2e",
        user_name="E2E", text="看看这张图", message_id=message_id,
        metadata={"conversation_id": conv.conversation_id})
    agent_event(bridge, msg, "message_start", role="assistant",
                message_id="img-a1")
    agent_event(bridge, msg, "text_delta", text="已收到图片。")
    bridge.on_reply(msg, answer)

async def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    server = GatewayServer(cfg(Path(tmp.name), "realseede2e"))
    await server.start()
    print("READY", flush=True)
    webui = server.webui
    bridge = webui.conversation_bridge
    service = webui.conversation_service

    # ---- webui:default：plan runtime turn ----
    seed_plan(bridge, "webui:default")
    print("SEEDED webui:default plan", flush=True)

    # ---- webui:render：普通主会话 turn ----
    conv_render = service.get_or_create_conversation(
        "webui:render", origin="webui", subtype="main")
    seed_normal_turn(
        bridge, service, conv_render, "webui:render",
        "✅ 项目结构已读取：共 2 个文件，其中 a.txt 为待编辑目标。",
        "seed-render-1", "render-c1")
    print("SEEDED webui:render main turn", flush=True)

    # ---- webui:imgrender：独立会话带图片 turn（图片链路 e2e，不干扰
    #      webui:render 的既有单气泡断言）----
    conv_img = service.get_or_create_conversation(
        "webui:imgrender", origin="webui", subtype="main")
    seed_image_turn(
        bridge, service, conv_img, "webui:imgrender",
        "✅ 已收到图片（1x1 验证图），缩略图应随消息展示。",
        "seed-img-1", "img-c1")
    print("SEEDED webui:imgrender image turn", flush=True)

    # ---- 工作区会话 turn ----
    ws = Workspace(
        workspace_id="ws_real_e2e", name="真实E2E工作区",
        project_path="/tmp/real-e2e-workspace",
        default_model="gpt-5.6-terra", permission_mode="unreviewed",
        chat_mode="chat", description="真实自动化测试用工作区")
    ws_mgr = WorkspaceStore(webui.workspace_db)
    workspace, session = ws_mgr.create_with_first_session(ws, {
        "name": "默认会话", "model": "gpt-5.6-terra"})
    conv_ws = service.get_or_create_conversation(
        session.session_key, origin="webui", subtype="workspace",
        workspace_id=workspace.workspace_id)
    seed_normal_turn(
        bridge, service, conv_ws, session.session_key,
        "✅ 工作区项目概要：本次为真实自动化测试，最终答复应渲染为助手而非运行进度。",
        "seed-ws-1", "ws-c1")
    print(f"SEEDED workspace={workspace.workspace_id} session={session.session_id} "
          f"key={session.session_key}", flush=True)

    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
