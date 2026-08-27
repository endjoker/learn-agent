import { useEffect, useRef } from "react";

import { buildSseUrl, parseSseEvent, recordSseEventId, type ParsedSseEvent, type SseScope } from "@/sse/events";

/** EventSource 断线重建：指数退避 1s → 最大 30s（组件卸载即停止）。 */
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30_000;

/**
 * Subscribe to server-sent events scoped to an optional session/workspace.
 * Mirrors the legacy HA.onSSE subscription lifecycle: the connection is
 * opened on mount / scope change and closed on unmount.
 */
export function useSse(scope: SseScope | null, onEvent: (event: ParsedSseEvent) => void) {
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  const scopeKey = scope ? `${scope.sessionKey ?? ""}|${scope.workspaceId ?? ""}|${scope.workspaceSessionId ?? ""}` : null;

  useEffect(() => {
    if (scopeKey === null) return undefined;
    let disposed = false;
    let source: EventSource | null = null;
    let retryDelay = RETRY_BASE_MS;
    let retryTimer: number | undefined;

    const connect = () => {
      if (disposed) return;
      source = new EventSource(buildSseUrl(scope ?? {}));
      source.onmessage = (event: MessageEvent<string>) => {
        const parsed = parseSseEvent(event.data);
        if (parsed) {
          recordSseEventId(parsed.event_id);
          handlerRef.current(parsed);
          // 有效事件到达 → 回退退避基准：连接恢复中收到数据说明链路已通，
          // 下一次断线仍从 RETRY_BASE_MS 起步（避免长期卡在指数退避高位）。
          retryDelay = RETRY_BASE_MS;
        }
      };
      source.onerror = () => {
        // 一律主动 close 后重建：浏览器原生重连会复用建立时的旧 URL（陈旧
        // last_event_id），恢复后重复重放 backlog；这里关闭连接，重连时经
        // buildSseUrl 实时读取最新事件水位（sessionStorage 节流写入）。
        if (source) {
          source.close();
          source = null;
        }
        if (disposed || retryTimer !== undefined) return;
        // 指数退避重建（1s→30s 封顶）；收到有效事件后由 onmessage 复位基准档
        retryTimer = window.setTimeout(connect, retryDelay);
        retryDelay = Math.min(RETRY_MAX_MS, retryDelay * 2);
      };
    };
    connect();
    return () => {
      disposed = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      source?.close();
    };
    // The stringified scope is the real dependency (object identity is unstable).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey]);
}
