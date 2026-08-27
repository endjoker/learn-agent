// 统一会话连接 hook —— 设计方案 19.2 恢复流程 + 18.3 版本缺口修复。
//
// 流程：建立 SSE 并缓冲 → 获取 Session Snapshot → 应用快照 → 应用缓冲事件；
// 事件出现版本缺口（store gaps）→ 缓冲 500ms 等补齐，仍缺则拉 Snapshot 修复。

import { useCallback, useEffect, useRef, useState } from "react";

import { conversationApi } from "@/gateway/api";
import { gatewayStore } from "@/gateway/store";
import type { GatewayEvent } from "@/gateway/types";
import type { ParsedSseEvent, SseScope } from "@/sse/events";
const isGatewayEvent = (event: ParsedSseEvent): event is ParsedSseEvent & { data: GatewayEvent["data"] } =>
  typeof event.data === "object" &&
  event.data !== null &&
  typeof (event.data as { conversation_id?: unknown }).conversation_id === "string";

// 设计方案 18.3：缺口先缓冲 500ms 等补齐（快照失败/缺口持续时按指数退避
// 翻倍，封顶 2s，避免高频缺口下反复拉快照）；缓冲上限 1000 事件或 2MB
const GAP_WAIT_BASE_MS = 500;
const GAP_WAIT_MAX_MS = 2000;
const BUFFER_MAX_EVENTS = 1000;
const BUFFER_MAX_BYTES = 2 * 1024 * 1024;

export interface UseConversationOptions {
  sessionKey?: string;
  /** 指定 conversation_id 时跳过 create/lookup（已打开的会话页直连） */
  conversationId?: string;
  enabled?: boolean;
}

export function useConversation(options: UseConversationOptions) {
  const { sessionKey, conversationId, enabled = true } = options;
  const bufferRef = useRef<GatewayEvent[]>([]);
  // L5：缓冲字节数用计数维护，避免每个新事件对已缓冲事件全量 JSON.stringify
  // （原实现 O(b²)，高频 delta 事件下恢复期拖垮主线程）。
  const bufferBytesRef = useRef(0);
  // 已完成快照加载的会话（按会话判定 loaded，替代旧的单布尔 loadedRef：
  // 切换会话后旧会话的 in-flight 快照不得让新会话事件跳过缓冲直灌 store）。
  const loadedConvRef = useRef<string | null>(null);
  // 当前 effect 绑定的会话：applySnapshot await 返回后必须归属校验，
  // 防止切换会话后旧会话的迟到快照污染新会话的恢复状态（loaded 标记 /
  // 事件缓冲 / recovering 遮罩——多会话同时运行时切换页面必现内容错乱）。
  const activeConvRef = useRef<string | null>(null);
  // P2：缓冲溢出标记（一次溢出周期只告警一次；快照成功应用后复位）
  const bufferOverflowRef = useRef(false);
  // 缺口等待定时器：conversation_id → timeout id
  const gapTimerRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  // 卸载/换会话后不再调度新的快照重试（防止重试循环在组件销毁后空转）
  const disposedRef = useRef(false);
  // 恢复期只读（设计方案 19.2：恢复期间不交互，避免基于残缺状态的写操作）
  const [recovering, setRecovering] = useState(false);
  // 快照修复防抖：同一会话一次只允许一个进行中的快照拉取
  const snapshotInflightRef = useRef<Set<string>>(new Set());
  // 打破 applySnapshot ↔ 重试调度器的循环依赖（同 handlerRef 惯用法）
  const applySnapshotRef = useRef<(cid: string) => Promise<string | null>>(() => Promise.resolve(null));
  // 每会话当前退避档位（快照失败/缺口未修复 → 翻倍，封顶 GAP_WAIT_MAX_MS；
  // 快照成功 → 复位到基准档）
  const gapRetryRef = useRef<Map<string, number>>(new Map());

  // P2：快照失败恢复调度器（初始加载失败与版本缺口共用）：
  // 等待当前退避档位后重拉快照；失败则翻倍档位并继续重试，直到成功
  // 或 hook 卸载/换会话（effect 清理统一 clearTimeout）。
  const scheduleSnapshotRecovery = useCallback((cid: string) => {
    if (disposedRef.current) return;
    const existing = gapTimerRef.current.get(cid);
    if (existing !== undefined) return; // 已有等待中的恢复/重试
    // 设计方案 18.3：先缓冲一段时间等事件补齐（版本门控会丢弃旧事件）；
    // 失败后按指数退避（500ms→2s 封顶）再试，避免高频刷快照接口。
    const delay = gapRetryRef.current.get(cid) ?? GAP_WAIT_BASE_MS;
    const timer = setTimeout(() => {
      gapTimerRef.current.delete(cid);
      void applySnapshotRef.current(cid).then((fixed) => {
        if (fixed === null) {
          gapRetryRef.current.set(cid, Math.min(GAP_WAIT_MAX_MS, (gapRetryRef.current.get(cid) ?? GAP_WAIT_BASE_MS) * 2));
          scheduleSnapshotRecovery(cid);
        } else {
          gapRetryRef.current.delete(cid); // 快照成功 → 复位基准档
        }
      });
    }, delay);
    gapTimerRef.current.set(cid, timer);
  }, []);

  const applySnapshot = useCallback(async (cid: string) => {
    if (snapshotInflightRef.current.has(cid)) return null;
    snapshotInflightRef.current.add(cid);
    setRecovering(true);
    let result: Awaited<ReturnType<typeof conversationApi.snapshot>> | null = null;
    try {
      result = await conversationApi.snapshot(cid);
      // 归属校验：await 期间已切换会话 → 本次快照属于旧会话，整体丢弃。
      // 绝不能动 loaded 标记 / 事件缓冲 / recovering——那是新会话的恢复状态；
      // 否则旧快照会把新会话的事件"未经快照基线"提前灌入 store（双会话
      // 同时运行时切换页面，内容输出错乱的直接根源）。
      if (activeConvRef.current !== cid) {
        result = null;
      } else if (result.ok && result.data) {
        gatewayStore.applySnapshot(result.data);
        loadedConvRef.current = cid;
        bufferOverflowRef.current = false;
        // 应用缓冲期内到达的事件（版本门控会自动丢弃旧事件）
        for (const event of bufferRef.current) gatewayStore.applyEvent(event);
        bufferRef.current = [];
        bufferBytesRef.current = 0;
      }
    } finally {
      snapshotInflightRef.current.delete(cid);
      // 仅当前活跃会话解除恢复期遮罩（旧会话的迟到快照不得替新会话解除）
      if (activeConvRef.current === cid) setRecovering(false);
    }
    if (result && result.ok) return cid;
    // 旧会话的迟到快照：不调度恢复重试（新会话自有自己的恢复流程）
    if (activeConvRef.current !== cid) return null;
    // P2：快照失败不再静默放弃——loaded 未置位时 SSE 事件只进缓冲，
    // 溢出后被静默丢弃，UI 会永久空白。进入指数退避自动重试。
    scheduleSnapshotRecovery(cid);
    return null;
  }, [scheduleSnapshotRecovery]);
  applySnapshotRef.current = applySnapshot;

  // 版本缺口 / Turn 事件早于快照到达 → 走同一套"等待+失败退避重试"的快照恢复
  const scheduleGapRecovery = scheduleSnapshotRecovery;

  const handleEvent = useCallback((event: ParsedSseEvent) => {
    if (!isGatewayEvent(event)) return;
    const gatewayEvent = event as unknown as { type: string; data: GatewayEvent["data"] };
    const convId = gatewayEvent.data.conversation_id;
    if (convId !== activeConvRef.current || loadedConvRef.current !== convId) {
      // 其它会话的事件：store 本就按 conversation_id 隔离，直接应用安全
      //（全局单例 store）；绝不能进本会话缓冲——否则会在本会话快照
      // 完成后被误重放。
      if (convId !== activeConvRef.current) {
        gatewayStore.applyEvent(gatewayEvent as GatewayEvent);
        return;
      }
      // 当前会话快照加载前缓冲（设计方案 19.2），带上限防止无界增长
      const approxBytes = JSON.stringify(gatewayEvent).length;
      if (bufferRef.current.length < BUFFER_MAX_EVENTS
        && bufferBytesRef.current + approxBytes <= BUFFER_MAX_BYTES) {
        bufferRef.current.push(gatewayEvent as GatewayEvent);
        bufferBytesRef.current += approxBytes;
      } else if (!bufferOverflowRef.current) {
        // P2：缓冲溢出——旧实现静默丢事件导致 UI 永久空白；这里告警一次并
        // 调度快照恢复（成功应用后清空缓冲、复位溢出标记与退避档位）。
        bufferOverflowRef.current = true;
        console.warn(
          `[useConversation] snapshot pending buffer overflow, dropping events `
          + `(events=${bufferRef.current.length}/${BUFFER_MAX_EVENTS}, `
          + `bytes≈${bufferBytesRef.current}/${BUFFER_MAX_BYTES}); scheduling snapshot recovery`,
        );
        scheduleGapRecovery(convId);
      }
      return;
    }
    gatewayStore.applyEvent(gatewayEvent as GatewayEvent);
    // 版本缺口 / Turn 事件到达但 store 尚无该 Turn（快照与时序竞态）
    // → 缓冲 500ms 后拉快照修复（设计方案 18.3/19.2），无需用户手动刷新。
    const state = gatewayStore.getState();
    const turnId = gatewayEvent.data.turn_id;
    // 缺口按来源标记（store GapFlags）：session/turn 任一缺口都需要快照修复
    const gapFlags = state.gaps[convId];
    const needSnapshot = Boolean(gapFlags?.session || gapFlags?.turn)
      || (turnId != null && !state.turnsById[turnId]);
    if (needSnapshot) {
      scheduleGapRecovery(convId);
    }
  }, [scheduleGapRecovery]);

  const scope: SseScope | null = enabled && (sessionKey || conversationId)
    ? { sessionKey }
    : null;

  useEffect(() => {
    if (!enabled) return undefined;
    disposedRef.current = false;
    loadedConvRef.current = null;
    // 直连 conversationId 时归属即刻确定；sessionKey 模式等 create 返回后
    // 再绑定（期间到达的事件按"非活跃会话"直接应用，不进缓冲）。
    activeConvRef.current = conversationId ?? null;
    bufferRef.current = [];
    bufferBytesRef.current = 0;
    bufferOverflowRef.current = false;
    for (const timer of gapTimerRef.current.values()) clearTimeout(timer);
    const gapTimers = gapTimerRef.current;
    gapTimers.clear();
    gapRetryRef.current.clear();
    if (conversationId) {
      void applySnapshot(conversationId);
    } else if (sessionKey) {
      void conversationApi.create(sessionKey).then((result) => {
        if (!result.ok || !result.data) return;
        const cid = result.data.conversation.conversation_id;
        // effect 可能已被更高优先级的依赖变化重跑：仅当仍活跃才绑定并加载
        if (activeConvRef.current === null) {
          activeConvRef.current = cid;
          void applySnapshot(cid);
        }
      });
    }
    // SSE 在 useSse 中单独订阅（避免与快照时序耦合）
    return () => {
      // P2：置为 disposed 后，在途快照失败不再续期重试定时器（防止卸载后空转）
      disposedRef.current = true;
      for (const timer of gapTimers.values()) clearTimeout(timer);
      gapTimers.clear();
    };
  }, [enabled, sessionKey, conversationId, applySnapshot]);

  return { handleEvent, scope, recovering };
}
