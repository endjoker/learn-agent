// 统一会话 API 客户端 —— 设计方案第 30.1 节 REST 契约。

import { api, type ApiError } from "@/api/client";
import type {
  ConversationSession,
  ConversationSnapshot,
  HistoryPage,
  QueueItem,
  Turn,
  TurnNode,
} from "@/gateway/types";

export interface ApiResult<T> {
  ok: boolean;
  data?: T;
  error?: { code: string; message: string };
  status: number;
}

/** 统一错误码 → 用户可读文案（设计方案 23：前端对 409/503 的 toast 反馈）。 */
const ERROR_LABELS: Record<string, string> = {
  queue_limit: "队列已满（最多 20 条）",
  queue_item_conflict: "队列项已变更（版本冲突），请刷新后重试",
  undo_expired: "撤销窗口（5 秒）已过期",
  steering_limit: "每个 Turn 最多插入 10 次 Steering",
  steering_interrupt_timeout: "Steering 中断超时，任务已中止",
  execution_scope_concurrency_limit: "执行域并发已满，请等待运行中的任务完成",
  gateway_concurrency_saturated: "全局并发已满，请等待运行中的任务完成",
  idempotency_conflict: "相同操作正在处理中（幂等冲突）",
  approval_not_pending: "审批已处理或不存在",
  conversation_not_found: "会话不存在或已删除",
  validation_failed: "请求参数不合法",
};

let lastToast: { text: string; at: number } | null = null;

const showErrorToast = (code: string, message: string): void => {
  const label = ERROR_LABELS[code] ?? (message || code);
  // 1 秒内同文案去重，避免批量失败刷屏
  const now = Date.now();
  if (lastToast && lastToast.text === label && now - lastToast.at < 1000) return;
  lastToast = { text: label, at: now };
  try {
    // 动态引入避免循环依赖（toast 依赖 api client，api 也依赖它）
    void import("@/components/toast").then(({ toast }) => toast(label, "err"));
  } catch {
    /* silent */
  }
};

const wrap = async <T>(promise: Promise<T>): Promise<ApiResult<T>> => {
  try {
    const data = await promise;
    return { ok: true, data, status: 200 };
  } catch (error) {
    const apiError = error as ApiError;
    const code = apiError.code ?? "unknown";
    // 仅对用户可操作的控制类错误弹 toast；网络/5xx 静默（避免误报）
    if (apiError.status && apiError.status >= 400 && apiError.status < 500
      && ERROR_LABELS[code]) {
      showErrorToast(code, apiError.message);
    }
    return {
      ok: false,
      error: { code, message: apiError.message },
      status: apiError.status,
    };
  }
};

export const conversationApi = {
  create(sessionKey: string, options?: { origin?: string; subtype?: string; workspaceId?: string; routeMetadata?: Record<string, unknown> }) {
    return wrap<{ conversation: ConversationSession }>(api.post("/api/conversations", {
      session_key: sessionKey,
      origin: options?.origin,
      subtype: options?.subtype,
      workspace_id: options?.workspaceId,
      route_metadata: options?.routeMetadata,
    }));
  },

  lookup(sessionKey: string) {
    return wrap<{ conversation: ConversationSession }>(api.get("/api/conversations/lookup", { query: { session_key: sessionKey } }));
  },

  list(options?: { limit?: number; offset?: number; origin?: string }) {
    return wrap<{ conversations: ConversationSession[] }>(api.get("/api/conversations", {
      query: { limit: options?.limit, offset: options?.offset, origin: options?.origin },
    }));
  },

  snapshot(conversationId: string) {
    return wrap<ConversationSnapshot>(api.get(`/api/conversations/${conversationId}/snapshot`));
  },

  history(conversationId: string, options?: { cursor?: string; limit?: number }) {
    return wrap<HistoryPage>(api.get(`/api/conversations/${conversationId}/turns`, {
      query: { cursor: options?.cursor, limit: options?.limit },
    }));
  },

  enqueue(conversationId: string, text: string, options?: { operationId?: string; channel?: string; messageId?: string; senderId?: string; senderName?: string; images?: Array<{ data: string; media_type?: string }> }) {
    return wrap<{ queue_item: QueueItem }>(api.post(`/api/conversations/${conversationId}/queue`, {
      text,
      operation_id: options?.operationId,
      channel: options?.channel,
      message_id: options?.messageId,
      sender_id: options?.senderId,
      sender_name: options?.senderName,
      images: options?.images,
    }));
  },

  sendNext(conversationId: string, options?: { operationId?: string; runtimeSnapshotId?: string; channelNodeId?: string }) {
    return wrap<{ dispatched: boolean; turn?: Turn; user_node?: TurnNode; image_nodes?: TurnNode[] }>(api.post(
      `/api/conversations/${conversationId}/queue/send-next`,
      { operation_id: options?.operationId, runtime_snapshot_id: options?.runtimeSnapshotId, channel_node_id: options?.channelNodeId },
    ));
  },

  editQueueItem(conversationId: string, queueItemId: string, options: { expectedRevision: number; text?: string }) {
    return wrap<{ queue_item: QueueItem }>(api.patch(`/api/conversations/${conversationId}/queue/${queueItemId}`, {
      expected_revision: options.expectedRevision,
      text: options.text,
    }));
  },

  deleteQueueItem(conversationId: string, queueItemId: string, expectedRevision: number) {
    return wrap<{ queue_item: QueueItem }>(api.request(
      "DELETE", `/api/conversations/${conversationId}/queue/${queueItemId}`,
      { expected_revision: expectedRevision },
    ));
  },

  undoDelete(conversationId: string, queueItemId: string) {
    return wrap<{ queue_item: QueueItem }>(api.post(`/api/conversations/${conversationId}/queue/${queueItemId}/undo`));
  },

  moveQueueItem(conversationId: string, queueItemId: string, direction: "up" | "down") {
    return wrap<{ queue: QueueItem[] }>(api.post(`/api/conversations/${conversationId}/queue/${queueItemId}/move`, { direction }));
  },

  clearQueue(conversationId: string) {
    return wrap<{ cleared: number }>(api.post(`/api/conversations/${conversationId}/queue/clear`));
  },

  clear(conversationId: string) {
    return wrap<{ cleared: boolean; counts: Record<string, number> }>(api.post(`/api/conversations/${conversationId}/clear`));
  },

  prepareSteering(conversationId: string, queueItemIds: string[]) {
    return wrap<{ turn: Turn; steering: QueueItem[] }>(api.post(`/api/conversations/${conversationId}/steering`, {
      queue_item_ids: queueItemIds,
    }));
  },

  commitSteering(conversationId: string, queueItemIds: string[]) {
    return wrap<{ injected: TurnNode[] }>(api.post(`/api/conversations/${conversationId}/steering/commit`, { queue_item_ids: queueItemIds }));
  },

  abortSteering(conversationId: string, queueItemIds: string[]) {
    return wrap<{ aborted: boolean }>(api.post(`/api/conversations/${conversationId}/steering/abort`, { queue_item_ids: queueItemIds }));
  },

  stop(conversationId: string, options?: { operationId?: string }) {
    return wrap<{ turn: Turn | null }>(api.post(`/api/conversations/${conversationId}/stop`, {
      operation_id: options?.operationId,
    }));
  },

  resolveApproval(conversationId: string, approvalId: string, decision: "approved" | "denied") {
    return wrap<{ approval: Record<string, unknown> }>(api.post(`/api/conversations/${conversationId}/approvals/${approvalId}`, {
      decision,
    }));
  },

  result(conversationId: string, turnId: string, resultRef: string) {
    return wrap<{ result: Record<string, unknown> }>(api.get(`/api/conversations/${conversationId}/turns/${turnId}/results/${resultRef}`));
  },
};
