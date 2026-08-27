# -*- coding: utf-8 -*-
"""
统一会话 REST API —— 设计方案第 30.1/30.3 节。

所有写操作：
- 携带 conversation_id + operation_id（幂等记录保留 24 小时）；
- 返回前数据库已提交（"数据库提交先于事件广播"）；
- 写操作不再有控制租约校验（控制租约已废弃）；
- 错误统一 {"error": <code>, "message": <text>}。
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from gateway.conversation.errors import (
    ApprovalConflict,
    ConversationError,
    ExecutionScopeLimit,
    GatewaySaturated,
    IdempotencyConflict,
    QueueConflict,
    QueueLimit,
    ResourceNotFound,
    ResultNotOwned,
    SteeringLimit,
    SteeringTimeout,
    TurnNotFound,
    UndoExpired,
    ValidationFailed,
)

logger = logging.getLogger("jk_agent.gateway")


def _register_fire_and_forget(module, task) -> None:
    """登记 fire-and-forget 任务：stop() 时统一取消；结束自动移出集合。

    防止队列 Turn 投递这类后台任务在 WebUI 停止后仍悬挂执行。
    """
    registry = getattr(module, "_fire_and_forget_tasks", None)
    if registry is None:
        return
    registry.add(task)
    task.add_done_callback(lambda done: registry.discard(done))


def _err(exc: ConversationError) -> web.Response:
    return web.json_response({"error": exc.code, "message": str(exc)},
                             status=exc.http_status)


def _ok(data: dict = None) -> web.Response:
    return web.json_response(data or {})


async def _body(request: web.Request) -> dict:
    if not getattr(request, "can_read_body", False):
        return {}
    try:
        data = await request.json()
    except Exception:
        raise ValidationFailed("请求体必须是 JSON")
    return data if isinstance(data, dict) else {}


def _conv_id(request: web.Request) -> str:
    return request.match_info.get("conversation_id") or ""


def register_routes(app: web.Application, module) -> None:
    svc = module.conversation_service
    bus = module.bus

    async def handle_create(request: web.Request) -> web.Response:
        body = await _body(request)
        session_key = str(body.get("session_key") or "").strip()
        if not session_key:
            raise ValidationFailed("session_key 为必填项")
        try:
            # 未显式指定时按 session_key 前缀推断 origin/subtype（与渠道桥一致）
            from gateway.conversation.bridge import ConversationBridge
            inferred_origin, inferred_subtype = ConversationBridge._origin_subtype(session_key)
            workspace_id = body.get("workspace_id") or None
            # 工作区会话：未显式传 workspace_id 时从 session_key 推断
            # （否则 workspace_id=None → 工作区执行上下文不挂载 → 模型/权限不生效）。
            if not workspace_id and session_key.startswith("workspace:"):
                _parts = session_key.split(":", 2)
                if len(_parts) >= 2:
                    workspace_id = _parts[1]
            conversation = svc.get_or_create_conversation(
                session_key,
                origin=str(body.get("origin") or inferred_origin),
                subtype=str(body.get("subtype") or inferred_subtype),
                workspace_id=workspace_id,
                route_metadata=body.get("route_metadata") or None,
            )
        except ConversationError as exc:
            return _err(exc)
        return _ok({"conversation": conversation.to_dict()})

    async def handle_lookup(request: web.Request) -> web.Response:
        session_key = (request.query.get("session_key") or "").strip()
        if not session_key:
            raise ValidationFailed("session_key 为必填项")
        conversation = svc.store.get_conversation_by_key(session_key)
        if conversation is None:
            return web.json_response({"error": "conversation_not_found",
                                      "message": session_key}, status=404)
        return _ok({"conversation": conversation.to_dict()})

    async def handle_list(request: web.Request) -> web.Response:
        """会话导航列表（设计方案 21.1）。"""
        try:
            limit = int(request.query.get("limit") or 100)
            offset = int(request.query.get("offset") or 0)
        except ValueError:
            raise ValidationFailed("limit/offset 必须是整数")
        origin = request.query.get("origin") or None
        conversations = svc.store.list_conversations(
            limit=limit, offset=offset, origin=origin)
        return _ok({"conversations": [c.to_dict() for c in conversations]})

    async def handle_snapshot(request: web.Request) -> web.Response:
        try:
            return _ok(svc.snapshot(_conv_id(request)))
        except ConversationError as exc:
            return _err(exc)

    async def handle_history(request: web.Request) -> web.Response:
        try:
            before = request.query.get("cursor") or None
            limit = int(request.query.get("limit") or 30)
            return _ok(svc.history(_conv_id(request), before=before, limit=limit))
        except ConversationError as exc:
            return _err(exc)
        except ValueError:
            return _err(ValidationFailed("limit 必须是整数"))

    async def handle_enqueue(request: web.Request) -> web.Response:
        body = await _body(request)
        text = str(body.get("text") or "")
        images = body.get("images") or None
        if images is not None and not isinstance(images, list):
            return web.json_response(
                {"error": "validation_failed", "message": "images 必须是数组"},
                status=400)
        try:
            item = svc.enqueue(
                _conv_id(request), text,
                operation_id=body.get("operation_id") or None,
                channel=body.get("channel") or None,
                message_id=body.get("message_id") or None,
                sender_id=body.get("sender_id") or None,
                sender_name=body.get("sender_name") or None,
                create_queued_node=bool(body.get("create_queued_node", False)),
                images=images,
            )
        except ConversationError as exc:
            return _err(exc)
        return _ok({"queue_item": item.to_dict()})

    async def handle_get_image(request: web.Request) -> web.Response:
        """会话图片读取（归属校验后回原图，供前端缩略图/原图查看）。

        GET /api/conversations/{cid}/images/{node_id}
        - node_id 必须属于该 conversation（防跨会话枚举）；
        - 节点必须是 image 节点且 metadata.ref 存在；
        - ref 内容不可变，浏览器可长缓存。
        """
        cid = _conv_id(request)
        node_id = request.match_info.get("node_id") or ""
        image_store = getattr(svc, "image_store", None)
        if image_store is None:
            return web.json_response(
                {"error": "not_supported", "message": "图片服务未启用"}, status=501)
        try:
            node = svc.store.get_node(node_id)
        except Exception:
            return web.json_response(
                {"error": "not_found", "message": "图片不存在"}, status=404)
        ref = (node.metadata or {}).get("ref")
        if node.conversation_id != cid or not ref:
            # 归属不符与不存在同响应（不泄露存在性）
            return web.json_response(
                {"error": "not_found", "message": "图片不存在"}, status=404)
        try:
            path = image_store.resolve(cid, str(ref))
        except FileNotFoundError:
            return web.json_response(
                {"error": "not_found", "message": "图片文件已清理"}, status=404)
        except ValueError as exc:
            logger.warning("图片引用非法: %s: %s", ref, exc)
            return web.json_response(
                {"error": "invalid_ref", "message": "图片引用非法"}, status=400)
        media_type = str((node.metadata or {}).get("media_type") or "image/png")
        return web.FileResponse(
            path, headers={"Cache-Control": "private, max-age=31536000, immutable",
                           "Content-Type": media_type})

    async def handle_send_next(request: web.Request) -> web.Response:
        body = await _body(request)
        cid = _conv_id(request)
        try:
            result = svc.send_next(
                cid,
                runtime_snapshot_id=body.get("runtime_snapshot_id") or None,
                channel_node_id=body.get("channel_node_id") or None,
                operation_id=body.get("operation_id") or None,
                parent_conversation_id=body.get("parent_conversation_id") or None,
                parent_turn_id=body.get("parent_turn_id") or None,
            )
        except ConversationError as exc:
            return _err(exc)
        if result is None:
            return _ok({"dispatched": False})
        turn, node = result
        # 图片节点随响应返回：调用方在乐观合并 user 节点后一并合并，
        # 缩略图无需等 SSE/刷新即可显示（消灭事件时序竞态）。
        image_nodes = [n for n in svc.store.get_turn_nodes(turn.turn_id)
                       if n.type == "image"]
        # 出队创建 Turn 后投递给 Agent 执行（设计方案 8.5：出队事务 → 执行）。
        # fire-and-forget：投递 FIFO 即返回，流式事件经 SSE 驱动前端。
        # 测试环境可用 webui.conversation.auto_execute_on_send_next=false 关闭。
        if getattr(module, "conversation_auto_execute", True):
            try:
                task = asyncio.get_running_loop().create_task(
                    module.dispatcher.execute_conversation_turn(cid))
                _register_fire_and_forget(module, task)
            except Exception:
                logger.exception("队列 Turn 投递执行失败: %s", cid)
        return _ok({"dispatched": True, "turn": turn.to_dict(),
                    "user_node": node.to_dict(),
                    "image_nodes": [n.to_dict() for n in image_nodes]})

    async def handle_edit_queue(request: web.Request) -> web.Response:
        body = await _body(request)
        qid = request.match_info.get("queue_item_id") or ""
        try:
            item = svc.edit_queue_item(
                _conv_id(request), qid,
                expected_revision=int(body.get("expected_revision") or 0),
                text=body.get("text") if "text" in body else None,
                operation_id=body.get("operation_id") or None)
        except ConversationError as exc:
            return _err(exc)
        return _ok({"queue_item": item.to_dict()})

    async def handle_delete_queue(request: web.Request) -> web.Response:
        body = await _body(request)
        qid = request.match_info.get("queue_item_id") or ""
        try:
            item = svc.delete_queue_item(
                _conv_id(request), qid,
                expected_revision=int(body.get("expected_revision") or 0))
        except ConversationError as exc:
            return _err(exc)
        return _ok({"queue_item": item.to_dict()})

    async def handle_undo_delete(request: web.Request) -> web.Response:
        qid = request.match_info.get("queue_item_id") or ""
        try:
            item = svc.undo_delete(_conv_id(request), qid)
        except ConversationError as exc:
            return _err(exc)
        return _ok({"queue_item": item.to_dict()})

    async def handle_move_queue(request: web.Request) -> web.Response:
        body = await _body(request)
        qid = request.match_info.get("queue_item_id") or ""
        direction = str(body.get("direction") or "up")
        try:
            items = svc.move_queue_item(_conv_id(request), qid, direction)
        except ConversationError as exc:
            return _err(exc)
        return _ok({"queue": [it.to_dict() for it in items]})

    async def handle_clear_queue(request: web.Request) -> web.Response:
        try:
            cleared = svc.clear_waiting(_conv_id(request))
        except ConversationError as exc:
            return _err(exc)
        return _ok({"cleared": cleared})

    async def handle_clear_history(request: web.Request) -> web.Response:
        """清空会话全部历史（/clear 与"清空聊天"统一入口，设计方案 §30）。"""
        conv_id = _conv_id(request)
        try:
            counts = svc.clear_history(conv_id)
        except ConversationError as exc:
            return _err(exc)
        # 联动清空驻留 Agent 的内存上下文：统一模型与 Agent MessageStore 是
        # 两份存储，只清模型不清 Agent，下一条消息仍带全部旧上下文（/clear 失效）。
        try:
            conv = svc.store.get_conversation(conv_id)
        except Exception:
            conv = None
        if conv is not None and getattr(conv, "session_key", ""):
            from gateway.webui.glue import clear_cached_agent_context
            await clear_cached_agent_context(module, conv.session_key)
        return _ok({"cleared": True, "counts": counts})

    async def handle_inject(request: web.Request) -> web.Response:
        """直接插入当前 Turn（Steering 第一阶段 prepare，设计方案 9.1）。"""
        qid = request.match_info.get("queue_item_id") or ""
        try:
            active, items = svc.prepare_steering(_conv_id(request), [qid])
        except ConversationError as exc:
            return _err(exc)
        return _ok({"turn": active.to_dict(),
                    "steering": [it.to_dict() for it in items]})

    async def handle_steering_prepare(request: web.Request) -> web.Response:
        body = await _body(request)
        qids = [str(x) for x in (body.get("queue_item_ids") or [])]
        try:
            active, items = svc.prepare_steering(_conv_id(request), qids)
        except ConversationError as exc:
            return _err(exc)
        # 设计方案 9.1/9.2：记录 10 秒等待窗口并打断当前模型输出，
        # 已运行工具自然结束后由 runner 自动注入（commit）并续跑。
        svc.register_steering_wait(_conv_id(request), qids)
        runner = getattr(module, "conversation_runner", None)
        if runner is not None:
            runner.request_stop(_conv_id(request))
        return _ok({"turn": active.to_dict(),
                    "steering": [it.to_dict() for it in items],
                    "waiting": True})

    async def handle_steering_commit(request: web.Request) -> web.Response:
        body = await _body(request)
        qids = [str(x) for x in (body.get("queue_item_ids") or [])]
        try:
            nodes = svc.commit_steering(_conv_id(request), qids)
        except ConversationError as exc:
            return _err(exc)
        return _ok({"injected": [n.to_dict() for n in nodes]})

    async def handle_steering_abort(request: web.Request) -> web.Response:
        body = await _body(request)
        qids = [str(x) for x in (body.get("queue_item_ids") or [])]
        try:
            svc.abort_steering(_conv_id(request), qids)
        except ConversationError as exc:
            return _err(exc)
        return _ok({"aborted": True})

    async def handle_stop(request: web.Request) -> web.Response:
        body = await _body(request)
        try:
            turn = svc.request_stop(
                _conv_id(request),
                operation_id=body.get("operation_id") or None)
        except ConversationError as exc:
            return _err(exc)
        # 联动统一会话桥：停止确认后终态为 stopped（设计方案 7.6）
        bridge = getattr(module, "conversation_bridge", None)
        if bridge is not None and turn is not None:
            bridge.mark_stop_requested(_conv_id(request))
        # 真正打断运行中的 Agent（协作式停止，AgentLoop 下一步检查点退出）
        runner = getattr(module, "conversation_runner", None)
        if runner is not None:
            runner.request_stop(_conv_id(request))
        return _ok({"turn": turn.to_dict() if turn else None})

    async def handle_approval(request: web.Request) -> web.Response:
        body = await _body(request)
        approval_id = request.match_info.get("approval_id") or ""
        try:
            approval = svc.resolve_approval(
                _conv_id(request), approval_id,
                str(body.get("decision") or "approved"))
        except ConversationError as exc:
            return _err(exc)
        return _ok({"approval": approval.to_dict()})

    async def handle_result(request: web.Request) -> web.Response:
        turn_id = request.match_info.get("turn_id") or ""
        result_ref = request.match_info.get("result_ref") or ""
        try:
            result = svc.get_result(_conv_id(request), turn_id, result_ref)
        except ConversationError as exc:
            return _err(exc)
        return _ok({"result": result})

    @web.middleware
    async def error_middleware(request: web.Request, handler):
        try:
            return await handler(request)
        except ConversationError as exc:
            return _err(exc)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("conversation API 未处理异常: %s %s",
                             request.method, request.path)
            return web.json_response({"error": "internal_error",
                                      "message": "internal error"}, status=500)

    routes = [
        web.post("/api/conversations", handle_create),
        web.get("/api/conversations", handle_list),
        web.get("/api/conversations/lookup", handle_lookup),
        web.get("/api/conversations/{conversation_id}/snapshot", handle_snapshot),
        web.get("/api/conversations/{conversation_id}/turns", handle_history),
        web.post("/api/conversations/{conversation_id}/queue", handle_enqueue),
        web.get("/api/conversations/{conversation_id}/images/{node_id}", handle_get_image),
        web.post("/api/conversations/{conversation_id}/queue/send-next", handle_send_next),
        web.patch("/api/conversations/{conversation_id}/queue/{queue_item_id}", handle_edit_queue),
        web.delete("/api/conversations/{conversation_id}/queue/{queue_item_id}", handle_delete_queue),
        web.post("/api/conversations/{conversation_id}/queue/{queue_item_id}/undo", handle_undo_delete),
        web.post("/api/conversations/{conversation_id}/queue/{queue_item_id}/move", handle_move_queue),
        web.post("/api/conversations/{conversation_id}/queue/clear", handle_clear_queue),
        web.post("/api/conversations/{conversation_id}/clear", handle_clear_history),
        web.post("/api/conversations/{conversation_id}/queue/{queue_item_id}/inject", handle_inject),
        web.post("/api/conversations/{conversation_id}/steering", handle_steering_prepare),
        web.post("/api/conversations/{conversation_id}/steering/commit", handle_steering_commit),
        web.post("/api/conversations/{conversation_id}/steering/abort", handle_steering_abort),
        web.post("/api/conversations/{conversation_id}/stop", handle_stop),
        web.post("/api/conversations/{conversation_id}/approvals/{approval_id}", handle_approval),
        web.get("/api/conversations/{conversation_id}/turns/{turn_id}/results/{result_ref}", handle_result),
    ]
    for route in routes:
        app.router.add_route(route.method, route.path, route.handler)
    app.middlewares.append(error_middleware)
