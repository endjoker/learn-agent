import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi, ApiError } from "@/api/client";
import type { Approval, OwnershipContext } from "@/api/types";
import { toast } from "@/components/toast";
import type { QuestionScope } from "@/questions/questionTypes";
import { scopeKeyOf } from "@/questions/questionTypes";
import type { ParsedSseEvent } from "@/sse/events";

export type ApprovalAnswer = "y" | "n" | "a" | "s";

interface ApprovalState {
  items: Approval[];
  submittingId?: string;
  error?: string;
}

type Action =
  | { type: "replace"; items: Approval[] }
  | { type: "requested"; item: Approval }
  | { type: "resolved"; id: string }
  | { type: "submit"; id: string }
  | { type: "error"; id: string; error: string };

const approvalId = (approval: Approval): string =>
  typeof approval.id === "string" && approval.id
    ? approval.id
    : typeof approval.approval_id === "string" ? approval.approval_id : "";

const normalize = (raw: unknown): Approval | null => {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Approval;
  const id = approvalId(value);
  if (!id) return null;
  return { ...value, id, approval_id: id };
};

const matchesScope = (approval: Approval, scope: QuestionScope): boolean => {
  if (scope.sessionKey && approval.session_key && approval.session_key !== scope.sessionKey) return false;
  if (scope.workspaceId && approval.workspace_id && approval.workspace_id !== scope.workspaceId) return false;
  if (scope.workspaceSessionId && approval.workspace_session_id && approval.workspace_session_id !== scope.workspaceSessionId) return false;
  return true;
};

const reducer = (state: ApprovalState, action: Action): ApprovalState => {
  if (action.type === "replace") return { items: action.items };
  if (action.type === "requested") {
    const id = approvalId(action.item);
    return state.items.some((item) => approvalId(item) === id)
      ? state
      : { ...state, items: [...state.items, action.item] };
  }
  if (action.type === "resolved") return { ...state, items: state.items.filter((item) => approvalId(item) !== action.id), submittingId: undefined, error: undefined };
  if (action.type === "submit") return { ...state, submittingId: action.id, error: undefined };
  return { ...state, submittingId: undefined, error: action.error };
};

export function useApprovalQueue(client: ApiClient = defaultApi, scope: QuestionScope = {}) {
  const [state, dispatch] = useReducer(reducer, { items: [] });
  const clientRef = useRef(client);
  clientRef.current = client;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const scopeKey = scopeKeyOf(scope);

  const recover = useCallback(async () => {
    try {
      // 后端 GET /api/approvals 支持 session_key 过滤：只看本会话待办。
      const query: Record<string, string> = {};
      if (scopeRef.current.sessionKey) query.session_key = scopeRef.current.sessionKey;
      const data = await clientRef.current.get<{ approvals?: unknown[] }>("/api/approvals", { silent: true, query });
      const items = (data.approvals ?? [])
        .map(normalize)
        .filter((item): item is Approval => item !== null && matchesScope(item, scopeRef.current));
      dispatch({ type: "replace", items });
    } catch {
      dispatch({ type: "replace", items: [] });
    }
  }, []);

  useEffect(() => { void recover(); }, [recover, scopeKey]);

  // 兜底轮询 + 可见性触发：SSE 断线/吞事件时周期重拉 /api/approvals，保证审批弹窗
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
    if (event.type === "approval.requested") {
      const raw = event.data.approval ?? event.data;
      const item = normalize(raw);
      if (item && matchesScope(item, scopeRef.current)) dispatch({ type: "requested", item });
      return;
    }
    if (event.type === "approval.resolved") {
      const rawId = event.data.approval_id ?? (event.data as { id?: unknown }).id;
      if (typeof rawId === "string" && rawId) dispatch({ type: "resolved", id: rawId });
    }
  }, []);

  const answer = useCallback(async (approval: Approval, value: ApprovalAnswer) => {
    const id = approvalId(approval);
    if (!id) return;
    dispatch({ type: "submit", id });
    // 归属信息 fail-closed：后端要求携带 session_key 或工作区/消息上下文，否则 403。
    // session_key 优先取 pending 记录，缺失时回退到当前会话上下文（scope.sessionKey）。
    const ownership: OwnershipContext = {
      session_key: approval.session_key || scopeRef.current.sessionKey || undefined,
      workspace_id: approval.workspace_id,
      workspace_session_id: approval.workspace_session_id,
      snapshot_id: approval.snapshot_id,
      message_id: approval.message_id,
    };
    const body: Record<string, unknown> = { answer: value };
    for (const [key, value] of Object.entries(ownership)) {
      if (value) body[key] = value;
    }
    try {
      await clientRef.current.post(`/api/approvals/${encodeURIComponent(id)}`, body);
      dispatch({ type: "resolved", id });
    } catch (error) {
      // 403 归属不匹配/缺少归属：该项无法在当前会话答复，提示并从队列移除。
      if (error instanceof ApiError && error.status === 403) {
        toast(`审批已失效或不属于当前会话：${error instanceof Error ? error.message : "归属校验失败"}`, "err");
        dispatch({ type: "resolved", id });
        return;
      }
      dispatch({ type: "error", id, error: error instanceof Error ? error.message : "审批提交失败" });
    }
  }, []);

  return useMemo(() => ({ state, recover, onSse, answer }), [answer, onSse, recover, state]);
}
