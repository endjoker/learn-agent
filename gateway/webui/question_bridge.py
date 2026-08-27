# -*- coding: utf-8 -*-
"""
QuestionBridge —— Agent 结构化提问桥（与 ApprovalBridge 分离）

Agent 通过 ask_question 工具向用户发起结构化问题（候选项 + 自定义输入），
executor 线程阻塞等待 WebUI 用户答复。协议与只接受 y/n/a/s 的审批桥完全分离：
自由文本走 selected_option_ids / custom_text，绝不混入审批语义。

关键语义（对应 2026-08-19-webui-ts-react-migration.md P1-4 后端桥）：
- wire id 契约：id 为设计模型主字段（QuestionPrompt.id），question_id 为
  兼容别名（TS/SSE 旧引用），两者恒相等；GET /api/questions、SSE 事件与
  ask_question 工具结果同时返回两字段。POST 路径保持 /api/questions/{id}。
- SSE 事件：question.requested / question.resolved（payload 带 session/workspace
  顶层字段，复用 EventBus 的 scope 过滤）
- API：GET /api/questions（待答问题恢复）、POST /api/questions/{id}
- session/workspace/message context 校验，防止跨会话/跨页面答复
- 同一问题只能成功答复一次：重复答复返回 already_answered（API 409），
  已过期/未知返回 not_found（API 404）
- 超时 / WebUI 停止时 fail-closed：返回明确状态（timeout / fail_closed），
  绝不替用户自动选择推荐项（recommended 仅为展示提示）
"""

import threading
import time
import uuid

_QUESTION_TIMEOUT = 300        # 默认等待上限（秒），与审批桥 300s 一致
_QUESTION_TIMEOUT_MIN = 0.1    # 下限仅用于测试可快速触发超时
_QUESTION_TIMEOUT_MAX = 600    # 上限：低于 agent 硬超时
_ANSWERED_TTL = 600            # 已答复记录保留时长（重复答复检测）
_OPTIONS_MAX = 50              # 候选项数量上限（防滥用）
_QUESTION_MAX_LEN = 2000       # 问题文本长度上限

# resolve() 返回值（API 层据此映射 HTTP 状态码）
RESOLVE_OK = "ok"
RESOLVE_ALREADY_ANSWERED = "already_answered"
RESOLVE_NOT_FOUND = "not_found"
RESOLVE_CONTEXT_MISMATCH = "context_mismatch"
RESOLVE_INVALID = "invalid"

# 校验用的归属字段（与 ApprovalBridge.resolve 的 context 键保持一致）
_CONTEXT_KEYS = ("workspace_id", "workspace_session_id", "snapshot_id", "message_id")


class QuestionBridge:
    """ask_question 工具的实现：executor 线程阻塞等待 WebUI 用户答复。

    fail-closed：默认 300s 超时或 WebUIModule.stop() 时，返回明确状态
    （timeout / fail_closed），不自动选中任何候选项。
    """

    def __init__(self, module):
        self.module = module
        self._pending: dict = {}      # qid -> record（等待答复中）
        self._answered: dict = {}     # qid -> record（已答复，TTL 保留供 409 判定）
        self._lock = threading.Lock()

    # ---------- 对外主入口（executor 线程调用，阻塞至答复/超时） ----------

    def ask(self, session_key: str, question: str, options: list,
            *, title: str = None, allow_custom: bool = True,
            custom_placeholder: str = None, multiple: bool = False,
            required: bool = False, timeout_s: float = None,
            context: dict = None) -> dict:
        """发起一个结构化问题并等待用户答复。

        返回 dict（JSON 可序列化，供工具直接回给模型）：
        - status: "answered" / "cancelled" / "timeout" / "fail_closed"
        - answered 时附带 selected_option_ids / custom_text
        - cancelled：用户在 WebUI 点了"取消"——LLM 应据此跳过该问题继续，
          不要原样重复提问
        - timeout / fail_closed 时不附带任何选择（fail-closed，不替用户决策）
        """
        question = str(question or "").strip()
        if not question:
            raise ValueError("question 不能为空")
        if len(question) > _QUESTION_MAX_LEN:
            raise ValueError(f"question 过长（>{_QUESTION_MAX_LEN} 字符）")
        options = self._normalize_options(options)
        allow_custom = bool(allow_custom)
        if not options and not allow_custom:
            raise ValueError("结构化问题必须提供候选项或允许自定义输入")
        timeout = self._clamp_timeout(timeout_s)

        qid = f"q-{uuid.uuid4().hex[:10]}"
        evt = threading.Event()
        record = {
            "id": qid,
            "session_key": session_key,
            "title": str(title or "").strip(),
            "question": question,
            "options": options,
            "allow_custom": allow_custom,
            "custom_placeholder": str(custom_placeholder or "").strip(),
            "multiple": bool(multiple),
            "required": bool(required),
            "timeout_s": timeout,
            "context": dict(context or {}),
            "created_at": time.time(),
            "event": evt,
            "status": "pending",      # pending / answered / timeout / fail_closed
            "answer": None,           # {selected_option_ids, custom_text} | None
        }
        with self._lock:
            self._pending[qid] = record
        self._publish("question.requested", self._event_payload(record))

        got = evt.wait(timeout=timeout)
        waited = round(time.time() - record["created_at"], 1)
        with self._lock:
            self._pending.pop(qid, None)
            if record["status"] == "pending":
                record["status"] = "answered" if got else "timeout"        # id 为设计模型主字段（QuestionPrompt.id）；question_id 为兼容别名，
        # 与 SSE / GET /api/questions 的 wire 契约保持一致（两者恒相等）。
        result = {
            "status": record["status"],
            "id": qid,
            "question_id": qid,
            "session_key": session_key,
            "waited_s": waited,
        }
        answer = record["answer"]
        if answer:
            result["selected_option_ids"] = answer.get("selected_option_ids", [])
            result["custom_text"] = answer.get("custom_text", "")
        self._publish("question.resolved", self._resolved_event_payload(
            record, result, timeout=not got))
        return result

    # ---------- 答复入口（WebUI API 线程调用，非阻塞） ----------

    def resolve(self, qid: str, answer: dict,
                context: dict = None) -> str:
        """答复一个待答问题。返回 RESOLVE_* 状态码。

        - 未知 / 已过期问题 → RESOLVE_NOT_FOUND
        - 已答复过的问题 → RESOLVE_ALREADY_ANSWERED（one-answer 语义）
        - session/workspace/message context 与记录不匹配 → RESOLVE_CONTEXT_MISMATCH
        - 答案违反协议约束（非法选项 / 多选越界 / 必答为空 / 未允许自定义）→ RESOLVE_INVALID
        """
        selected = list(dict.fromkeys(
            str(item).strip() for item in (answer or {}).get("selected_option_ids") or []
            if str(item).strip()))
        custom = str((answer or {}).get("custom_text") or "").strip()
        with self._lock:
            record = self._pending.get(qid)
            if record is None:
                if qid in self._answered:
                    return RESOLVE_ALREADY_ANSWERED
                return RESOLVE_NOT_FOUND
            # one-answer：ask() 线程可能尚未把已答复记录移出 pending，
            # 状态标记优先于位置判断，防止唤醒窗口内的重复答复。
            if record["status"] != "pending":
                return RESOLVE_ALREADY_ANSWERED
            if not self._context_matches(record, context):
                return RESOLVE_CONTEXT_MISMATCH
            if not self._answer_valid(record, selected, custom):
                return RESOLVE_INVALID
            record["answer"] = {
                "selected_option_ids": selected,
                "custom_text": custom,
            }
            record["status"] = "answered"
            record["resolved_at"] = time.time()
            self._answered[qid] = record
            self._prune_answered()
            record["event"].set()
        return RESOLVE_OK

    # ---------- 取消入口（WebUI 用户点"取消"，非阻塞） ----------

    def cancel(self, qid: str, context: dict = None) -> str:
        """用户取消一个待答问题（WebUI 弹窗"取消"按钮）。

        与本地静默丢弃不同：取消会唤醒 ask() 等待线程并以 ``cancelled``
        状态返回给 ask_question 工具 → LLM 明确知道"用户取消了该问题"，
        不会在超时后原样重复提问（修复取消后无限弹窗循环）。

        返回 RESOLVE_* 状态码（语义同 resolve：未知/已过期 → not_found，
        已答复 → already_answered，归属不匹配 → context_mismatch）。
        """
        with self._lock:
            record = self._pending.get(qid)
            if record is None:
                if qid in self._answered:
                    return RESOLVE_ALREADY_ANSWERED
                return RESOLVE_NOT_FOUND
            if record["status"] != "pending":
                return RESOLVE_ALREADY_ANSWERED
            if not self._context_matches(record, context):
                return RESOLVE_CONTEXT_MISMATCH
            record["status"] = "cancelled"
            record["answer"] = None
            record["resolved_at"] = time.time()
            self._answered[qid] = record
            self._prune_answered()
            record["event"].set()
        result = {
            "status": "cancelled",
            "id": qid,
            "question_id": qid,
            "session_key": record["session_key"],
            "waited_s": round(time.time() - record["created_at"], 1),
        }
        self._publish("question.resolved", self._resolved_event_payload(
            record, result, timeout=False))
        return RESOLVE_OK

    # ---------- 待答问题恢复（GET /api/questions） ----------

    def list_pending(self, session_key: str = "", workspace_id: str = "",
                     workspace_session_id: str = "") -> list:
        """返回当前等待答复的问题（公共字段，无内部 event/answer）。

        可选按 session/workspace scope 过滤；空值不过滤。
        """
        now = time.time()
        out = []
        with self._lock:
            for record in self._pending.values():
                # 严格过滤：提供过滤参数时只返回精确匹配的记录（含记录无该归属字段）。
                if session_key and record["session_key"] != session_key:
                    continue
                rctx = record["context"]
                if workspace_id and rctx.get("workspace_id") != workspace_id:
                    continue
                if (workspace_session_id
                        and rctx.get("workspace_session_id") != workspace_session_id):
                    continue
                out.append(self._public_payload(
                    record, waited_s=round(now - record["created_at"], 1)))
        return out

    def count_pending(self) -> int:
        return len(self.list_pending())

    # ---------- fail-closed ----------

    def fail_close_all(self):
        """WebUI 停止时唤醒全部待答问题为 fail_closed（不替用户选择）。"""
        with self._lock:
            for record in self._pending.values():
                if record["status"] == "pending":
                    record["status"] = "fail_closed"
                    record["answer"] = None
                    record["event"].set()

    # ---------- 校验 ----------

    def _context_matches(self, record: dict, context: dict) -> bool:
        """归属校验（fail-closed 单边匹配）。

        桥记录携带的归属信息（session_key / workspace / snapshot / message id）
        是权威：任一归属键记录有值而请求缺失或不同 → 拒绝；请求携带了某归属
        键而记录没有 → 同样视为不匹配（fail-closed）。仅当双方都完全不携带
        任何归属信息时才放行（向后兼容）。
        """
        rctx = record["context"]
        for key in ("session_key",) + _CONTEXT_KEYS:
            have = str(record["session_key"] if key == "session_key"
                       else rctx.get(key) or "").strip()
            want = str((context or {}).get(key) or "").strip()
            if have or want:
                if have != want:
                    return False
        return True

    def _answer_valid(self, record: dict, selected: list, custom: str) -> bool:
        if not record["allow_custom"] and custom:
            return False
        if not record["multiple"] and len(selected) > 1:
            return False
        valid_ids = {option["id"] for option in record["options"]}
        if not all(item in valid_ids for item in selected):
            return False
        if record["required"] and not selected and not custom:
            return False
        return True

    # ---------- 序列化 ----------

    def _public_payload(self, record: dict, waited_s: float = 0.0) -> dict:
        """前端 QuestionPrompt 领域模型对应的公共字段。

        id 为设计模型主字段（QuestionPrompt.id）；question_id 为兼容别名
        （TS/SSE 旧引用），两者恒相等。

        顶层同时携带 workspace_id / workspace_session_id / snapshot_id /
        message_id（与 _event_payload 一致）：前端 normalizeQuestion 只读
        顶层字段，若 GET 恢复缺这些字段，答复 POST 的归属校验会因缺
        message_id 判 context_mismatch → 403（"刷新/轮询后无法答复"）。
        """
        rctx = record["context"]
        return {
            "id": record["id"],
            "question_id": record["id"],
            "session_key": record["session_key"],
            "title": record["title"],
            "question": record["question"],
            "options": record["options"],
            "allow_custom": record["allow_custom"],
            "custom_placeholder": record["custom_placeholder"],
            "multiple": record["multiple"],
            "required": record["required"],
            "timeout_s": record["timeout_s"],
            "context": dict(rctx),
            "created_at": record["created_at"],
            "waited_s": waited_s,
            "workspace_id": rctx.get("workspace_id", ""),
            "workspace_session_id": rctx.get("workspace_session_id", ""),
            "snapshot_id": rctx.get("snapshot_id", ""),
            "message_id": rctx.get("message_id", ""),
        }

    def _event_payload(self, record: dict) -> dict:
        """SSE question.requested：顶层带 session/workspace 字段供 scope 过滤。"""
        payload = self._public_payload(record)
        rctx = record["context"]
        payload["workspace_id"] = rctx.get("workspace_id", "")
        payload["workspace_session_id"] = rctx.get("workspace_session_id", "")
        payload["snapshot_id"] = rctx.get("snapshot_id", "")
        payload["message_id"] = rctx.get("message_id", "")
        return payload

    def _resolved_event_payload(self, record: dict, result: dict,
                                timeout: bool) -> dict:
        """SSE question.resolved：状态 + 答复内容 + scope 字段。

        id 为设计模型主字段，question_id 为兼容别名（两者恒相等）。
        """
        rctx = record["context"]
        return {
            "id": record["id"],
            "question_id": record["id"],
            "session_key": record["session_key"],
            "status": result["status"],
            "selected_option_ids": result.get("selected_option_ids", []),
            "custom_text": result.get("custom_text", ""),
            "waited_s": result["waited_s"],
            "timeout": bool(timeout),
            "workspace_id": rctx.get("workspace_id", ""),
            "workspace_session_id": rctx.get("workspace_session_id", ""),
            "snapshot_id": rctx.get("snapshot_id", ""),
            "message_id": rctx.get("message_id", ""),
        }

    # ---------- 内部工具方法 ----------

    def _publish(self, event_type: str, payload: dict):
        try:
            self.module.bus.publish(event_type, payload)
        except Exception:
            # 发布失败不应阻塞答复等待（例如停机竞态）
            import logging
            logging.getLogger("jk_agent.gateway").debug(
                "question event publish failed: %s", event_type, exc_info=True)

    @staticmethod
    def _normalize_options(options) -> list:
        out = []
        seen = set()
        for item in options or []:
            if not isinstance(item, dict):
                continue
            oid = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            if not oid or not label or oid in seen:
                continue
            seen.add(oid)
            description = str(item.get("description") or "").strip()
            out.append({
                "id": oid,
                "label": label,
                "description": description or None,
                "recommended": bool(item.get("recommended", False)),
            })
            if len(out) >= _OPTIONS_MAX:
                break
        return out

    @staticmethod
    def _clamp_timeout(timeout_s) -> float:
        if timeout_s is None:
            return _QUESTION_TIMEOUT
        try:
            value = float(timeout_s)
        except (TypeError, ValueError):
            return _QUESTION_TIMEOUT
        if value != value:  # NaN：退回默认（evt.wait 不接受 NaN）
            return _QUESTION_TIMEOUT
        return max(_QUESTION_TIMEOUT_MIN, min(value, _QUESTION_TIMEOUT_MAX))

    def _prune_answered(self):
        cutoff = time.time() - _ANSWERED_TTL
        expired = [qid for qid, record in self._answered.items()
                   if record.get("resolved_at", 0) < cutoff]
        for qid in expired:
            self._answered.pop(qid, None)
