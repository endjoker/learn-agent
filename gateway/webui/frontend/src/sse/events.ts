import type { Approval, Goal, Plan, QuestionPrompt, Subagent, UnknownRecord } from "@/api/types";

export interface SseScope {
  sessionKey?: string;
  workspaceId?: string;
  workspaceSessionId?: string;
}

export interface SseConnectionOptions extends SseScope {
  lastEventId?: number;
  since?: number;
}

export interface ScopedEventData extends UnknownRecord {
  session_key?: string;
  workspace_id?: string;
  workspace_session_id?: string;
}

export interface KnownSseEventDataMap {
  "chat.started": ScopedEventData & { text_len?: number };
  "chat.progress": ScopedEventData & { text?: string };
  "chat.done": ScopedEventData & { ok?: boolean; text?: string };
  "chat.stop_requested": ScopedEventData;
  "plan.changed": ScopedEventData & { action?: string; plan?: Plan };
  "goal.changed": ScopedEventData & { action?: string; goal?: Goal };
  "subagent.changed": ScopedEventData & { action?: string; subagent?: Subagent };
  "approval.requested": ScopedEventData & { approval?: Approval; approval_id?: string };
  "approval.resolved": ScopedEventData & { approval?: Approval; approval_id?: string; answer?: string };
  "question.requested": ScopedEventData & { question?: QuestionPrompt };
  "question.resolved": ScopedEventData & { question_id?: string };
  "session.created": ScopedEventData;
  "session.evicted": ScopedEventData & { reason?: string };
  "channel.status": ScopedEventData & { channel?: string; status?: string };
  "mcp.changed": ScopedEventData & { action?: string };
  "config.updated": ScopedEventData & { section?: string; rev?: number };
  "prompt.updated": ScopedEventData & { file?: string; applied_to?: number };
  "artifact.created": ScopedEventData & { artifact?: UnknownRecord };
  "cron.changed": ScopedEventData & { action?: string; job?: string };
}

export type KnownSseEventType = keyof KnownSseEventDataMap;

export interface SseEnvelope<TType extends string = string, TData extends ScopedEventData = ScopedEventData> {
  type: TType;
  data: TData;
  event_id: number;
  at: number;
}

export type KnownSseEvent = {
  [K in KnownSseEventType]: SseEnvelope<K, KnownSseEventDataMap[K]>;
}[KnownSseEventType];

export type ParsedSseEvent = KnownSseEvent | SseEnvelope;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export const parseSseEvent = (raw: string): ParsedSseEvent | null => {
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || typeof value.type !== "string" || !isRecord(value.data)) return null;
    const eventId = typeof value.event_id === "number" && Number.isFinite(value.event_id)
      ? value.event_id
      : undefined;
    const at = typeof value.at === "number" && Number.isFinite(value.at)
      ? value.at
      : undefined;
    // 对缺失 event_id/at 容错：默认值 + console.warn（而非整条丢弃）
    if (eventId === undefined || at === undefined) {
      console.warn(`sse event missing event_id/at, using defaults (type=${value.type})`);
    }
    return {
      ...value,
      event_id: eventId ?? 0,
      at: at ?? Date.now(),
    } as unknown as ParsedSseEvent;
  } catch {
    return null;
  }
};

const LAST_EVENT_ID_KEY = "jkagent.sse.last_event_id";

/**
 * 跨刷新/切换保留最近一次收到的事件水位（sessionStorage），
 * 重新连接时传给后端 EventBus.replay() 重放 backlog，恢复推理/工具/plan 等实时状态。
 *
 * L5：sessionStorage 写节流（≥1s 一次）。SSE 高频事件（node.delta 等）每事件一次
 * setItem 是同步 DOM 写，高吞吐下卡主线程；改为：≥1s 未写则立即写，否则挂起定时器
 * 把最新水位在节流窗口结束后补写（绝不丢失最终水位，只合并窗口内的中间值）。
 */
export const getSseLastEventId = (): number => {
  try {
    return Number(window.sessionStorage.getItem(LAST_EVENT_ID_KEY) || 0) || 0;
  } catch {
    return 0;
  }
};

const RECORD_THROTTLE_MS = 1000;
let lastRecordedAt = 0;
let pendingEventId = 0;
let pendingTimer: number | undefined;

export const flushPendingEventId = (): void => {
  if (pendingTimer !== undefined) {
    window.clearTimeout(pendingTimer);
    pendingTimer = undefined;
  }
  if (pendingEventId <= 0) return;
  try {
    window.sessionStorage.setItem(LAST_EVENT_ID_KEY, String(pendingEventId));
    lastRecordedAt = Date.now();
  } catch { /* storage unavailable */ }
  pendingEventId = 0;
};

export const recordSseEventId = (eventId: number): void => {
  if (!Number.isFinite(eventId) || eventId <= 0) return;
  pendingEventId = Math.max(pendingEventId, eventId);
  const now = Date.now();
  if (now - lastRecordedAt >= RECORD_THROTTLE_MS) {
    flushPendingEventId();
    return;
  }
  if (pendingTimer === undefined) {
    const wait = RECORD_THROTTLE_MS - (now - lastRecordedAt);
    try {
      pendingTimer = window.setTimeout(() => {
        pendingTimer = undefined;
        flushPendingEventId();
      }, wait);
    } catch {
      // setTimeout 不可用（异常环境）：直接写，退化到无节流行为
      try {
        window.sessionStorage.setItem(LAST_EVENT_ID_KEY, String(pendingEventId));
        pendingEventId = 0;
      } catch { /* storage unavailable */ }
    }
  }
};

export const buildSseUrl = (options: SseConnectionOptions = {}): string => {
  const params = new URLSearchParams();
  if (options.lastEventId !== undefined) params.set("last_event_id", String(options.lastEventId));
  else if (getSseLastEventId() > 0) params.set("last_event_id", String(getSseLastEventId()));
  if (options.since !== undefined) params.set("since", String(options.since));
  if (options.sessionKey) params.set("session_key", options.sessionKey);
  if (options.workspaceId) params.set("workspace_id", options.workspaceId);
  if (options.workspaceSessionId) params.set("workspace_session_id", options.workspaceSessionId);
  const query = params.toString();
  return query ? `/api/events?${query}` : "/api/events";
};

export const eventMatchesScope = (data: ScopedEventData, scope: SseScope): boolean => {
  if (scope.sessionKey && data.session_key !== scope.sessionKey) return false;
  if (scope.workspaceId && data.workspace_id !== scope.workspaceId) return false;
  if (scope.workspaceSessionId && data.workspace_session_id !== scope.workspaceSessionId) return false;
  return true;
};

// 页面卸载（pagehide/beforeunload）前把节流窗口内未落盘的 event_id 水位立即写入
// sessionStorage，避免刷新/关闭时丢失最近一次收到的事件水位（replay 断点回退）。
if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  window.addEventListener("pagehide", flushPendingEventId);
  window.addEventListener("beforeunload", flushPendingEventId);
}
