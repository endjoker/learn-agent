import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi, ApiError } from "@/api/client";
import type { OwnershipContext, QuestionAnswer, QuestionsResponse } from "@/api/types";
import { toast } from "@/components/toast";
import {
  createInitialQuestionQueueState,
  reduceQuestionQueue,
  type QuestionQueueState,
} from "@/questions/questionQueue";
import {
  matchesScope,
  normalizeQuestion,
  scopeKeyOf,
  type QuestionScope,
} from "@/questions/questionTypes";
import type { ParsedSseEvent } from "@/sse/events";

export interface QuestionQueue {
  state: QuestionQueueState;
  /** Reload pending questions from GET /api/questions (refresh-safe restore). */
  recover: () => Promise<void>;
  /** Feed SSE events: question.requested / question.resolved. */
  onSse: (event: ParsedSseEvent) => void;
  /** Submit an answer via POST /api/questions/{id}. */
  answer: (question: import("@/api/types").QuestionPrompt, payload: QuestionAnswer) => Promise<void>;
  /** Drop a question locally (only for cancelable questions). */
  dismiss: (question: import("@/api/types").QuestionPrompt) => void;
  clearError: () => void;
}

/**
 * Pending-question queue for one scope (chat session or workspace session).
 *
 * - Scope changes reset the queue and re-run GET /api/questions recovery, so
 *   questions never bleed across sessions.
 * - `question.requested` enqueues (deduped); `question.resolved` removes.
 * - A failed POST keeps the question and records the error (submittingId is
 *   cleared), leaving the modal mounted so the user's input survives.
 */
export function useQuestionQueue(client: ApiClient = defaultApi, scope: QuestionScope = {}) {
  const [state, dispatch] = useReducer(reduceQuestionQueue, undefined, createInitialQuestionQueueState);
  const clientRef = useRef(client);
  clientRef.current = client;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const scopeKey = scopeKeyOf(scope);
  // 已取消问题的本地标记：防止取消请求在途时，兜底轮询 GET /api/questions
  // 把它重新加回队列（取消 → 轮询复活 → 弹窗关不掉）。
  const dismissedRef = useRef<Set<string>>(new Set());

  const recover = useCallback(async () => {
    const currentScope = scopeRef.current;
    dispatch({ type: "recoverStart" });
    try {
      // 后端 GET /api/questions 支持 session_key/workspace_id/workspace_session_id
      // 过滤：只看本会话/本工作区待办，减少跨会话数据。
      const query: Record<string, string> = {};
      if (currentScope.sessionKey) query.session_key = currentScope.sessionKey;
      if (currentScope.workspaceId) query.workspace_id = currentScope.workspaceId;
      if (currentScope.workspaceSessionId) query.workspace_session_id = currentScope.workspaceSessionId;
      const data = await clientRef.current.get<QuestionsResponse>("/api/questions", { silent: true, query });
      const raw = Array.isArray(data) ? data : Array.isArray(data?.questions) ? data.questions : [];
      const questions = raw
        .map(normalizeQuestion)
        .filter((question): question is NonNullable<ReturnType<typeof normalizeQuestion>> =>
          question !== null
          && !dismissedRef.current.has(question.question_id)
          && matchesScope(question, currentScope));
      dispatch({ type: "recoverDone", questions });
    } catch {
      // GET /api/questions may be unavailable (e.g. not yet deployed): the
      // queue simply starts empty and live SSE still works.
      dispatch({ type: "recoverDone", questions: [] });
    }
  }, []);

  // Fresh scope → fresh queue → recover pending questions for that scope.
  const previousScopeKey = useRef(scopeKey);
  useEffect(() => {
    if (previousScopeKey.current !== scopeKey) {
      previousScopeKey.current = scopeKey;
      dispatch({ type: "reset" });
    }
    void recover();
  }, [recover, scopeKey]);

  // 兜底轮询 + 可见性触发：SSE 断线/吞事件时周期重拉 /api/questions，保证问题弹窗
  // 即使浏览器 SSE 未送达事件也能实时出现（无需手动刷新/切换页面）。
  // 轮询加退避：2.5s 起 ×1.5 封顶 30s（L5：拉长上限进一步降频，SSE 正常时少打扰后端）；
  // visibilitychange hidden 时暂停。
  useEffect(() => {
    let delay = 2500;
    let timer: number | undefined;
    let disposed = false;
    const tick = () => {
      if (disposed || document.visibilityState === "hidden") { timer = undefined; return; }
      void recover();
      delay = Math.min(30000, Math.round(delay * 1.5));
      timer = window.setTimeout(tick, delay);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        if (timer === undefined) {
          delay = 2500;
          void recover();
          timer = window.setTimeout(tick, delay);
        }
      } else if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
    };
    timer = window.setTimeout(tick, delay);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [recover]);

  const onSse = useCallback((event: ParsedSseEvent) => {
    if (event.type === "question.requested") {
      const raw = event.data.question ?? event.data;
      const question = normalizeQuestion(raw);
      if (question && !dismissedRef.current.has(question.question_id)) {
        dispatch({ type: "requested", question, scope: scopeRef.current });
      }
      return;
    }
    if (event.type === "question.resolved") {
      const rawId = event.data.question_id ?? (event.data as { id?: unknown }).id;
      if (typeof rawId === "string" && rawId) {
        dismissedRef.current.delete(rawId); // 已 resolved：清除取消标记
        dispatch({ type: "resolved", questionId: rawId });
      }
    }
  }, []);

  const answer = useCallback(async (question: import("@/api/types").QuestionPrompt, payload: QuestionAnswer) => {
    dispatch({ type: "answerStart", questionId: question.question_id });
    try {
      // 归属信息 fail-closed：后端要求携带 session_key 或工作区/消息上下文，否则 403。
      // session_key 优先取 pending 记录，缺失时回退到当前会话上下文（scope.sessionKey）。
      // 注意 snapshot_id 也必须原样回传：桥记录携带 snapshot_id 而请求缺失
      // 会被判 context_mismatch → 403（"无法答复"的根因之一）。
      const ownership: OwnershipContext = {
        session_key: question.session_key || scopeRef.current.sessionKey || undefined,
        workspace_id: question.workspace_id,
        workspace_session_id: question.workspace_session_id,
        snapshot_id: question.snapshot_id,
        message_id: question.message_id,
      };
      const body: Record<string, unknown> = { selected_option_ids: payload.selected_option_ids };
      if (payload.custom_text !== undefined && payload.custom_text !== "") body.custom_text = payload.custom_text;
      for (const [key, value] of Object.entries(ownership)) {
        if (value) body[key] = value;
      }
      await clientRef.current.post(`/api/questions/${encodeURIComponent(question.question_id)}`, body);
      dispatch({ type: "answerOk", questionId: question.question_id });
    } catch (error) {
      // 403 归属不匹配/缺少归属：该项无法在当前会话答复，提示并从队列移除。
      if (error instanceof ApiError && error.status === 403) {
        toast(`问题已失效或不属于当前会话：${error instanceof Error ? error.message : "归属校验失败"}`, "err");
        dispatch({ type: "resolved", questionId: question.question_id });
        return;
      }
      dispatch({
        type: "answerError",
        questionId: question.question_id,
        message: error instanceof Error ? error.message : "提交失败",
      });
    }
  }, []);

  const dismiss = useCallback((question: import("@/api/types").QuestionPrompt) => {
    // 本地先移除（UI 即时响应），再通知后端取消：唤醒 ask() 等待线程以
    // status=cancelled 返回给 ask_question 工具，LLM 明确知道用户取消了
    // 提问（修复"取消后 LLM 不知道 → 重复弹窗"）。取消请求失败不回滚
    // UI——问题最多等到 300s 超时由后端自行 resolved。
    dismissedRef.current.add(question.question_id);
    if (dismissedRef.current.size > 200) dismissedRef.current.clear();
    dispatch({ type: "dismiss", questionId: question.question_id });
    const ownership: OwnershipContext = {
      session_key: question.session_key || scopeRef.current.sessionKey || undefined,
      workspace_id: question.workspace_id,
      workspace_session_id: question.workspace_session_id,
      snapshot_id: question.snapshot_id,
      message_id: question.message_id,
    };
    const body: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(ownership)) {
      if (value) body[key] = value;
    }
    void clientRef.current.post(
      `/api/questions/${encodeURIComponent(question.question_id)}/cancel`,
      body, { silent: true },
    ).catch(() => { /* 404/409：问题已不存在或已答复，本地移除即可 */ });
  }, []);

  const clearError = useCallback(() => dispatch({ type: "clearError" }), []);

  return useMemo<QuestionQueue>(
    () => ({ state, recover, onSse, answer, dismiss, clearError }),
    [answer, clearError, dismiss, onSse, recover, state],
  );
}
