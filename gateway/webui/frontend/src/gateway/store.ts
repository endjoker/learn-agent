// Gateway Store —— 统一会话前端状态（设计方案 20.1/20.2/18.3/19.2）。
//
// - React Context + useSyncExternalStore + Selector 精确订阅（不引入 Zustand/Redux）；
// - Gateway SSE 是业务 Store 的唯一更新通道；HTTP 只结束 loading / 返回错误；
// - 版本缺口（version > 当前水位+1）→ 置 gap，由订阅方拉取 Snapshot 后 applySnapshot 修复；
// - 旧版本事件（version <= 当前水位）直接丢弃，不重复应用。
// - L5 增量索引：turnIdsByConv / nodeIdsByConv / nodeIdsByTurn / queueIdsByConv
//   由事件应用路径增量维护，trimToCap 与 selectTurnNodes/selectQueue/selectLiveTurn
//   不再每事件全表扫描（store.ts:58-94,170,407-412 优化点）。

import { useSyncExternalStore } from "react";
import {
  ConversationSnapshot,
  ConversationSession,
  GatewayEvent,
  GatewayEventData,
  QueueItem,
  Turn,
  TurnNode,
} from "@/gateway/types";

export interface GatewayStoreState {
  conversationsById: Record<string, ConversationSession>;
  turnsById: Record<string, Turn>;
  nodesById: Record<string, TurnNode>;
  queueItemsById: Record<string, QueueItem>;
  deliveriesById: Record<string, Record<string, unknown>>;
  pendingApprovalsById: Record<string, Array<Record<string, unknown>>>;
  /** conversation_id -> session 版本水位 */
  sessionVersions: Record<string, number>;
  /** turn_id -> turn 版本水位 */
  turnVersions: Record<string, number>;
  /** conversation_id -> 存在版本缺口，需要拉取快照。
   *  缺口按来源区分：session 级缺口只有 session 级事件成功应用或修复性快照
   *  才能清除（turn 级连续事件不得误清）；turn 级跳变缺口由连续 turn 事件清除。 */
  gaps: Record<string, GapFlags>;
  /** 增量索引：conversation_id -> 该会话 turn id 列表（插入序） */
  turnIdsByConv: Record<string, string[]>;
  /** 增量索引：conversation_id -> 该会话 node id 列表（插入序） */
  nodeIdsByConv: Record<string, string[]>;
  /** 增量索引：turn_id -> 该 turn 的 node id 列表（插入序，selectTurnNodes 用） */
  nodeIdsByTurn: Record<string, string[]>;
  /** 增量索引：conversation_id -> 该会话 queue item id 列表 */
  queueIdsByConv: Record<string, string[]>;
}

/** 会话版本缺口标记（按来源区分）：session=事件流缺口；turn=turn 级跳变缺口 */
export interface GapFlags {
  session?: boolean;
  turn?: boolean;
}

/** 缺口标记更新：patch 并入该会话标记；两类缺口互不覆盖，全部为空时移除键。 */
const setGapFlags = (
  gaps: Record<string, GapFlags>,
  convId: string,
  patch: GapFlags,
): Record<string, GapFlags> => {
  const next: GapFlags = { ...gaps[convId], ...patch };
  if (!next.session && !next.turn) {
    if (!(convId in gaps)) return gaps;
    const copy = { ...gaps };
    delete copy[convId];
    return copy;
  }
  return { ...gaps, [convId]: next };
};

const emptyState = (): GatewayStoreState => ({
  conversationsById: {},
  turnsById: {},
  nodesById: {},
  queueItemsById: {},
  deliveriesById: {},
  pendingApprovalsById: {},
  sessionVersions: {},
  turnVersions: {},
  gaps: {},
  turnIdsByConv: {},
  nodeIdsByConv: {},
  nodeIdsByTurn: {},
  queueIdsByConv: {},
});

// 每会话上限裁剪（LRU 语义：终态优先淘汰，其次按 updated_at 最旧）：
// 防止 store 被非本会话事件/超长会话的 turn/node/queue 无限撑大。
const MAX_TURNS_PER_CONVERSATION = 250;
const MAX_NODES_PER_CONVERSATION = 3000;
const MAX_QUEUE_ITEMS_PER_CONVERSATION = 120;
// 每会话投递记录滚动上限（delivery.status 高频渠道事件，只保留最近 N 条）。
const MAX_DELIVERIES_PER_CONVERSATION = 200;
const TERMINAL_TURN_STATUS = new Set(["done", "stopped", "error", "interrupted"]);
const TERMINAL_NODE_STATUS = new Set(["done"]);
/** 队列项终态（归档后仅留在本地缓存，不再属于活动队列）。 */
export const TERMINAL_QUEUE_STATUS = new Set(["sent", "injected", "failed", "deleted"]);

/** 索引追加：调用方保证 id 尚不在列表中（事件热路径，O(1)）。 */
const addIndexId = (list: string[] | undefined, id: string): string[] =>
  list ? [...list, id] : [id];

// 事件应用时刷新实体 updated_at（trimToCap 的 LRU 按 updated_at 排序，SSE 事件
// 不携带该字段时以本地时间戳标记"最近活跃"，避免终端节点永远排最旧）。
const nowIso = (): string => new Date().toISOString();
const refreshedAt = (value: unknown): string => (typeof value === "string" && value ? value : nowIso());

// L5：trimToCap 改为基于增量索引（turnIdsByConv/nodeIdsByConv/queueIdsByConv）
// 计算候选 key，不再对 record 全表 Object.keys().filter(conversation_id)。
// evicted 返回被淘汰 id 列表，供 enforceCaps 同步清理 nodeIdsByTurn 等派生索引。
const trimToCap = <T extends { conversation_id: string; updated_at?: string | null }>(
  record: Record<string, T>,
  ids: string[] | undefined,
  cap: number,
  isTerminal: (entry: T) => boolean,
): { record: Record<string, T>; ids: string[] | undefined; evicted: string[] } => {
  const keys = ids ?? [];
  if (keys.length <= cap) return { record, ids, evicted: [] };
  const excess = keys.length - cap;
  const byAge = (a: string, b: string) =>
    String(record[a]?.updated_at ?? "").localeCompare(String(record[b]?.updated_at ?? ""));
  const evict: string[] = [];
  const terminal = keys.filter((id) => isTerminal(record[id]!)).sort(byAge);
  for (const id of terminal) {
    if (evict.length >= excess) break;
    evict.push(id);
  }
  if (evict.length < excess) {
    const evictSetPartial = new Set(evict);
    const rest = keys.filter((id) => !evictSetPartial.has(id)).sort(byAge);
    for (const id of rest) {
      if (evict.length >= excess) break;
      evict.push(id);
    }
  }
  if (evict.length === 0) return { record, ids, evicted: [] };
  const evictSet = new Set(evict);
  const next: Record<string, T> = {};
  for (const [id, value] of Object.entries(record)) if (!evictSet.has(id)) next[id] = value;
  return { record: next, ids: keys.filter((id) => !evictSet.has(id)), evicted: evict };
};

const enforceCaps = (state: GatewayStoreState, convId: string): GatewayStoreState => {
  const turns = trimToCap(state.turnsById, state.turnIdsByConv[convId], MAX_TURNS_PER_CONVERSATION, (t) => TERMINAL_TURN_STATUS.has(String(t.status)));
  const nodes = trimToCap(state.nodesById, state.nodeIdsByConv[convId], MAX_NODES_PER_CONVERSATION, (n) => TERMINAL_NODE_STATUS.has(String(n.status)));
  const queue = trimToCap(state.queueItemsById, state.queueIdsByConv[convId], MAX_QUEUE_ITEMS_PER_CONVERSATION, (q) => TERMINAL_QUEUE_STATUS.has(String(q.status)));
  // 收官优化：节点被淘汰时同步清理 nodeIdsByTurn（否则 selectTurnNodes 索引无界增长，
  // 依赖"跳过缺失 id"容错只是掩盖泄漏）。
  let nodeIdsByTurn = state.nodeIdsByTurn;
  if (nodes.evicted.length > 0) {
    const evictSet = new Set(nodes.evicted);
    const nextTurnIndex: Record<string, string[]> = {};
    for (const [turnId, ids] of Object.entries(state.nodeIdsByTurn)) {
      const kept = ids.filter((id) => !evictSet.has(id));
      if (kept.length > 0) nextTurnIndex[turnId] = kept;
    }
    nodeIdsByTurn = nextTurnIndex;
  }
  if (turns.record === state.turnsById && nodes.record === state.nodesById && queue.record === state.queueItemsById
    && turns.ids === state.turnIdsByConv[convId] && nodes.ids === state.nodeIdsByConv[convId] && queue.ids === state.queueIdsByConv[convId]
    && nodeIdsByTurn === state.nodeIdsByTurn) {
    return state;
  }
  return {
    ...state,
    turnsById: turns.record,
    nodesById: nodes.record,
    queueItemsById: queue.record,
    nodeIdsByTurn,
    turnIdsByConv: { ...state.turnIdsByConv, [convId]: turns.ids ?? state.turnIdsByConv[convId] ?? [] },
    nodeIdsByConv: { ...state.nodeIdsByConv, [convId]: nodes.ids ?? state.nodeIdsByConv[convId] ?? [] },
    queueIdsByConv: { ...state.queueIdsByConv, [convId]: queue.ids ?? state.queueIdsByConv[convId] ?? [] },
  };
};

export class GatewayStore {
  private state: GatewayStoreState = emptyState();
  private listeners = new Set<() => void>();
  // ---- 通知合帧（借鉴 deepseek-harness notifier 双通道调度）----
  // 高频进度事件（node.delta/node.tool/node.user_steering）N 次折叠为一帧一次
  // 通知；结构类事件保持同步通知。state 本身永远同步更新（getState 新鲜），
  // 只推迟 listener 通知，满足 useSyncExternalStore 快照契约。
  private framePending = false;
  private frameHandle: number | null = null;
  private frameUsesTimeout = false;

  getState = (): GatewayStoreState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private notifyListeners(): void {
    for (const listener of this.listeners) listener();
  }

  /** 同步通知（结构类事件 / 快照 / 用户回显）。 */
  private emit(next: GatewayStoreState): void {
    this.state = next;
    this.notifyListeners();
  }

  /** 合帧通知：state 立即生效，一帧最多通知一轮订阅者。 */
  private emitCoalesced(next: GatewayStoreState): void {
    this.state = next;
    if (this.framePending) return;
    this.framePending = true;
    const hidden = typeof document !== "undefined" && document.hidden;
    if (typeof requestAnimationFrame === "function" && !hidden) {
      this.frameUsesTimeout = false;
      this.frameHandle = requestAnimationFrame(() => this.flush());
    } else {
      // 后台标签页 rAF 不触发：退化 setTimeout(0)，保证通知最终可达。
      this.frameUsesTimeout = true;
      this.frameHandle = window.setTimeout(() => this.flush(), 0);
    }
  }

  private cancelScheduledFrame(): void {
    if (this.frameHandle === null) return;
    if (this.frameUsesTimeout) window.clearTimeout(this.frameHandle);
    else cancelAnimationFrame(this.frameHandle);
    this.frameHandle = null;
  }

  /** 同步派发挂起的合帧通知（reset / 会话切换前调用，避免陈旧通知落到新会话）。 */
  flush(): void {
    if (!this.framePending) return;
    this.cancelScheduledFrame();
    this.framePending = false;
    this.notifyListeners();
  }

  reset(): void {
    this.flush();
    this.emit(emptyState());
  }

  // ------------------------------------------------------------
  // 事件应用（版本门控）
  // ------------------------------------------------------------

  applyEvent(event: GatewayEvent): void {
    const data = event.data;
    if (!data || !data.conversation_id) return;
    const convId = data.conversation_id;
    const version = data.version ?? 0;
    const state = this.state;

    // version_gap：后端背压丢事件广播，跳过版本门控直接置缺口 → 快照修复
    if (event.type === "version_gap") {
      this.emit({ ...state, gaps: setGapFlags(state.gaps, convId, { session: true }) });
      return;
    }

    // 版本门控：session 级事件（水位初始 -1，首个 version=0/1 事件均可应用）
    if (data.scope === "session") {
      const current = state.sessionVersions[convId] ?? -1;
      if (version <= current) return; // 旧事件丢弃
      if (version > current + 1) {
        this.emit({ ...state, gaps: setGapFlags(state.gaps, convId, { session: true }) });
        return; // 缺口：等待快照
      }
    }
    // 版本门控：turn 级事件（node.delta 高频小步进）。
    // 放宽为同 turn 版本非递减即接受（乱序/跳变不丢当前事件）；
    // 真缺口（version_gap / store 缺 turn）仍由订阅方拉快照修复。
    let turnJumped = false;
    if (data.scope === "turn" && data.turn_id) {
      const current = state.turnVersions[data.turn_id];
      if (current !== undefined) {
        if (version < current) return; // 仅丢弃严格更旧的事件
        turnJumped = version > current + 1;
      }
    }

    let next = this.mutate(state, event, data, convId);
    // 应用成功 → 推进水位并清除缺口。
    // 缺口按来源清除：session 级事件只清 session 缺口；turn 级事件只管
    // turn 缺口（跳变置位、连续清除），不得误清 session 级缺口——否则
    // session 流缺口会被后续高频 node.delta 永久掩盖，快照修复不再触发。
    if (data.scope === "session") {
      next = {
        ...next,
        sessionVersions: { ...next.sessionVersions, [convId]: Math.max(version, next.sessionVersions[convId] ?? -1) },
        gaps: setGapFlags(next.gaps, convId, { session: false }),
      };
    } else if (data.scope === "turn" && data.turn_id) {
      next = {
        ...next,
        turnVersions: { ...next.turnVersions, [data.turn_id]: Math.max(version, next.turnVersions[data.turn_id] ?? -1) },
        // 跳变：标记 turn 缺口等待快照修复中间缺失事件；连续事件则清除 turn 缺口
        gaps: setGapFlags(next.gaps, convId, { turn: turnJumped }),
      };
    }
    // 通知分级路由（借鉴 harness assistant.ts 的 publication 声明）：
    // node.delta / node.tool / node.user_steering 是高频小步进进度事件 → 合帧；
    // turn.status / chat.done / version_gap / 快照等结构事件 → 同步即时通知。
    const nextState = enforceCaps(next, convId);
    if (event.type === "node.delta" || event.type === "node.tool" || event.type === "node.user_steering" || event.type === "node.image") {
      this.emitCoalesced(nextState);
    } else {
      this.emit(nextState);
    }
  }

  private mutate(
    state: GatewayStoreState,
    event: GatewayEvent,
    data: GatewayEventData,
    convId: string,
  ): GatewayStoreState {
    const type = event.type;
    const biz = data.data ?? {};
    let next = { ...state };

    if (type === "conversation.upserted") {
      const conv = state.conversationsById[convId] ?? ({} as ConversationSession);
      next = { ...next, conversationsById: { ...state.conversationsById, [convId]: { ...conv, ...(biz.conversation ?? {}), conversation_id: convId } } };
    } else if (type === "queue.updated") {
      const queue: QueueItem[] = Array.isArray(biz.queue) ? biz.queue as QueueItem[] : [];
      const queueItemsById: Record<string, QueueItem> = {};
      for (const item of queue) queueItemsById[item.queue_item_id] = item;
      // 事件全量 = 活动队列；其它会话的项原样保留，本会话已不在活动队列中的
      // 终端归档项（sent/injected/failed/deleted）保留在本地缓存，不再被事件覆盖。
      for (const existing of Object.values(state.queueItemsById)) {
        if (existing.conversation_id !== convId) {
          queueItemsById[existing.queue_item_id] = existing;
        } else if (!queueItemsById[existing.queue_item_id] && TERMINAL_QUEUE_STATUS.has(existing.status)) {
          queueItemsById[existing.queue_item_id] = existing;
        }
      }
      // 增量索引重建（queue.updated 为整队列替换事件，事件频率低，重建可接受）
      const queueIds: string[] = [];
      for (const item of Object.values(queueItemsById)) {
        if (item.conversation_id === convId) queueIds.push(item.queue_item_id);
      }
      next = { ...next, queueItemsById, queueIdsByConv: { ...state.queueIdsByConv, [convId]: queueIds } };
    } else if (type === "turn.status") {
      const rawTurnId = String(biz.turn_id ?? data.turn_id ?? "");
      const existing = state.turnsById[rawTurnId];
      const turn = existing ?? {} as Turn;
      const turnId = turn.turn_id || rawTurnId;
      if (!turnId) return next;
      const updated: Turn = {
        ...turn,
        turn_id: turnId,
        conversation_id: convId,
        status: (biz.status as Turn["status"]) ?? turn.status,
        error_code: (biz.error_code as string | null | undefined) ?? turn.error_code,
        turn_version: data.version ?? turn.turn_version,
        // 事件应用刷新 updated_at：LRU 裁剪按最近活跃排序（事件不携带时戳本地时间）
        updated_at: refreshedAt(biz.updated_at),
      };
      let turnIdsByConv = state.turnIdsByConv;
      if (!existing) {
        turnIdsByConv = { ...state.turnIdsByConv, [convId]: addIndexId(state.turnIdsByConv[convId], turnId) };
      }
      next = {
        ...next,
        turnsById: { ...state.turnsById, [turnId]: updated },
        turnIdsByConv,
      };
    } else if (type === "node.delta" || type === "node.tool" || type === "node.user_steering" || type === "node.image") {
      const nodeId = String(biz.node_id ?? "");
      if (!nodeId) return next;
      const existing = state.nodesById[nodeId];
      const node = existing ?? ({ metadata: {} } as TurnNode);
      const turnId = node.turn_id || String(data.turn_id || "");
      // 契约①：新后端 node.delta 携带 delta(本次增量)+seq(节点内递增序号)，
      // 前端按 (node_id, seq) 追加增量文本；旧后端携带全量 text → 维持替换逻辑。
      // seq <= 已见水位 → EventBus replay/乱序的重复 delta，文本增量直接丢弃
      // （chat.done 后同版本重放事件也不会污染权威全量文本）。
      const delta = typeof biz.delta === "string" ? biz.delta : undefined;
      const seq = typeof biz.seq === "number" ? biz.seq : undefined;
      const prevSeq = typeof node.delta_seq === "number" ? node.delta_seq : 0;
      const isFreshDelta = delta !== undefined && (seq === undefined || seq > prevSeq);
      const text = isFreshDelta
        ? (node.text ?? "") + delta
        : delta !== undefined
          ? node.text // 重复/乱序 delta：文本不动
          : typeof biz.text === "string" ? biz.text
            : (typeof biz.full_text === "string" ? biz.full_text : (node.text ?? undefined));
      const updated: TurnNode = {
        ...node,
        node_id: nodeId,
        conversation_id: convId,
        turn_id: turnId || node.turn_id || null,
        type: (biz.type as TurnNode["type"]) ?? node.type ?? inferNodeType(type),
        text,
        status: (biz.status as string | undefined) ?? node.status ?? "streaming",
        // SSE 事件携带 position，保证 turnToTimeline/selectTurnNodes 按真实顺序排序
        //（否则 live 节点 position=0，mergeUserNode 的 user 节点 position>0 反而排最后）。
        position: typeof biz.position === "number" ? biz.position : node.position,
        delta_seq: seq !== undefined ? Math.max(prevSeq, seq) : node.delta_seq,
        // 事件应用刷新 updated_at（LRU 裁剪按最近活跃排序）
        updated_at: refreshedAt(biz.updated_at),
        // 收官：runtime 状态字段一并并入 metadata（status/error_code/goal_round/
        // runtime_status 等，后端 H-B 配套提供后即可消费，未提供前按现有字段容错）
        // 方案 B：intermediate/final 为后端权威卡片分类标记，随 node.delta 下发
        metadata: biz.params_summary !== undefined || biz.result_summary !== undefined || biz.tool !== undefined
          || biz.error_code !== undefined || biz.goal_round !== undefined
          || biz.runtime_status !== undefined || biz.message !== undefined
          || biz.intermediate !== undefined || biz.final !== undefined
          ? { ...(node.metadata ?? {}), ...pick(biz, ["params_summary", "result_summary", "call_id", "result_ref", "error", "result_size_bytes", "tool", "error_code", "goal_round", "runtime_status", "runtime_type", "runtime_id", "message", "status", "intermediate", "final"]) }
          : (node.metadata ?? {}),
      };
      let nodeIdsByConv = state.nodeIdsByConv;
      let nodeIdsByTurn = state.nodeIdsByTurn;
      if (!existing) {
        nodeIdsByConv = { ...state.nodeIdsByConv, [convId]: addIndexId(state.nodeIdsByConv[convId], nodeId) };
        if (turnId) nodeIdsByTurn = { ...state.nodeIdsByTurn, [turnId]: addIndexId(state.nodeIdsByTurn[turnId], nodeId) };
      }
      next = {
        ...next,
        nodesById: { ...state.nodesById, [nodeId]: updated },
        nodeIdsByConv,
        nodeIdsByTurn,
      };
    } else if (type === "chat.done") {
      const turnId = String(data.turn_id ?? "");
      const existingTurn = state.turnsById[turnId];
      const turn = existingTurn ?? {} as Turn;
      // 终态尊重事件携带的 status（done/stopped/error），不再硬编码 done
      const terminal = (biz.status as Turn["status"] | undefined) ?? "done";
      const updated: Turn = {
        ...turn,
        turn_id: turnId || turn.turn_id,
        conversation_id: convId,
        status: terminal,
        turn_version: data.version ?? turn.turn_version,
        finished_at: biz.finished_at as string | undefined ?? turn.finished_at,
        error_code: (biz.error_code as string | null | undefined) ?? turn.error_code,
        updated_at: refreshedAt(biz.updated_at),
      };
      let nodesById = state.nodesById;
      let nodeIdsByConv = state.nodeIdsByConv;
      let nodeIdsByTurn = state.nodeIdsByTurn;
      const finalNodeId = biz.final_assistant_node_id as string | undefined;
      // 终态事件（chat.done）仍携带权威全量 text → 覆盖流式增量拼接结果
      if (finalNodeId && typeof biz.full_text === "string") {
        const existingNode = state.nodesById[finalNodeId];
        const node = existingNode ?? {} as TurnNode;
        nodesById = { ...state.nodesById, [finalNodeId]: { ...node, node_id: finalNodeId, turn_id: turnId, type: "assistant", status: "done", text: biz.full_text,
          // 方案 B：终态权威标记最终答复（后端 complete_turn 已落库，事件侧同步）
          metadata: { ...(node.metadata ?? {}), final: true, intermediate: false } } };
        if (!existingNode) {
          nodeIdsByConv = { ...state.nodeIdsByConv, [convId]: addIndexId(state.nodeIdsByConv[convId], finalNodeId) };
          if (turnId) nodeIdsByTurn = { ...state.nodeIdsByTurn, [turnId]: addIndexId(state.nodeIdsByTurn[turnId], finalNodeId) };
        }
      }
      // 终态：该 turn 残留的 streaming 节点收敛为 done（清理"正在生成…"占位）
      if (turnId) {
        let cleaned = false;
        for (const n of Object.values(nodesById)) {
          if (n.turn_id === turnId && n.status === "streaming") {
            nodesById = { ...nodesById, [n.node_id]: { ...n, status: "done" } };
            cleaned = true;
          }
        }
        if (cleaned) nodesById = { ...nodesById };
      }
      let turnIdsByConv = state.turnIdsByConv;
      if (!existingTurn) {
        turnIdsByConv = { ...state.turnIdsByConv, [convId]: addIndexId(state.turnIdsByConv[convId], turnId) };
      }
      next = {
        ...next,
        turnsById: { ...state.turnsById, [turnId]: updated },
        nodesById,
        turnIdsByConv,
        nodeIdsByConv,
        nodeIdsByTurn,
      };
    } else if (type === "conversation.cleared") {
      // 清空该会话全部历史（/clear 与"清空聊天"统一入口）：移除本地缓存的
      // turns / nodes / queue / approvals，保留 conversation 行。
      // 增量索引同步重建（clear 为低频操作，全量重建可接受）。
      const turnsById: Record<string, Turn> = {};
      const nodesById: Record<string, TurnNode> = {};
      const turnIdsByConv: Record<string, string[]> = {};
      const nodeIdsByConv: Record<string, string[]> = {};
      const nodeIdsByTurn: Record<string, string[]> = {};
      for (const [id, t] of Object.entries(state.turnsById)) {
        if (t.conversation_id !== convId) {
          turnsById[id] = t;
          turnIdsByConv[t.conversation_id] = addIndexId(turnIdsByConv[t.conversation_id], id);
        }
      }
      for (const [id, n] of Object.entries(state.nodesById)) {
        if (n.conversation_id !== convId) {
          nodesById[id] = n;
          nodeIdsByConv[n.conversation_id] = addIndexId(nodeIdsByConv[n.conversation_id], id);
          if (n.turn_id) nodeIdsByTurn[n.turn_id] = addIndexId(nodeIdsByTurn[n.turn_id], id);
        }
      }
      const queueItemsById: Record<string, QueueItem> = {};
      const queueIdsByConv: Record<string, string[]> = {};
      for (const [id, q] of Object.entries(state.queueItemsById)) {
        if (q.conversation_id !== convId) {
          queueItemsById[id] = q;
          queueIdsByConv[q.conversation_id] = addIndexId(queueIdsByConv[q.conversation_id], id);
        }
      }
      const pendingApprovalsById = { ...state.pendingApprovalsById, [convId]: [] };
      next = { ...next, turnsById, nodesById, queueItemsById, pendingApprovalsById, turnIdsByConv, nodeIdsByConv, nodeIdsByTurn, queueIdsByConv };
    } else if (type === "approval.requested") {
      const list = state.pendingApprovalsById[convId] ?? [];
      next = { ...next, pendingApprovalsById: { ...state.pendingApprovalsById, [convId]: [...list, biz] } };
    } else if (type === "approval.resolved") {
      const list = (state.pendingApprovalsById[convId] ?? []).filter((a) => a.approval_id !== biz.approval_id);
      next = { ...next, pendingApprovalsById: { ...state.pendingApprovalsById, [convId]: list } };
    } else if (type === "delivery.status") {
      const deliveryId = String(biz.delivery_id ?? `${convId}:${biz.message_id}`);
      const entry: Record<string, unknown> = {
        ...biz,
        delivery_id: deliveryId,
        conversation_id: convId,
        updated_at: refreshedAt(biz.updated_at),
      };
      const deliveriesById = { ...state.deliveriesById, [deliveryId]: entry };
      // 每会话 200 条滚动：超出后按 updated_at 最旧淘汰（仅淘汰本会话条目）
      const convDeliveries = Object.values(deliveriesById)
        .filter((d) => (d as { conversation_id?: string }).conversation_id === convId);
      if (convDeliveries.length > MAX_DELIVERIES_PER_CONVERSATION) {
        const excess = convDeliveries.length - MAX_DELIVERIES_PER_CONVERSATION;
        const byAge = (a: Record<string, unknown>, b: Record<string, unknown>) =>
          String(a.updated_at ?? "").localeCompare(String(b.updated_at ?? ""));
        const evictIds = convDeliveries
          .slice().sort(byAge).slice(0, excess)
          .map((d) => String((d as { delivery_id?: string }).delivery_id ?? ""));
        for (const id of evictIds) delete deliveriesById[id];
      }
      next = { ...next, deliveriesById };
    }
    return next;
  }

  // ------------------------------------------------------------
  // 快照应用（恢复 / 缺口修复）
  // ------------------------------------------------------------

  applySnapshot(snapshot: ConversationSnapshot): void {
    const conv = snapshot.conversation;
    const convId = conv.conversation_id;
    const state = this.state;
    const next: GatewayStoreState = {
      ...state,
      conversationsById: { ...state.conversationsById, [convId]: conv },
      // 水位只进不退（max）：快照响应期间可能有更新的事件已先应用到 store
      //（切换会话/快照在途竞态），回退水位会让后续旧版本事件被当作新事件
      // 重复应用；快照数据本身经幂等合并，保留更高水位不会破坏缺口修复
      //（缺口场景本地水位必然低于快照版本）。
      sessionVersions: { ...state.sessionVersions, [convId]: Math.max(snapshot.session_version, state.sessionVersions[convId] ?? -1) },
      queueItemsById: { ...state.queueItemsById },
      turnsById: { ...state.turnsById },
      nodesById: { ...state.nodesById },
      // 快照是权威全量修复：同时清除该会话的 session/turn 两类缺口
      gaps: setGapFlags(state.gaps, convId, { session: false, turn: false }),
      turnIdsByConv: state.turnIdsByConv,
      nodeIdsByConv: state.nodeIdsByConv,
      nodeIdsByTurn: state.nodeIdsByTurn,
      queueIdsByConv: state.queueIdsByConv,
    };
    // 收官优化：快照合并幂等去重改用 Set 查重 O(n)（替代 pushIndexId 的
    // list.includes 线性扫描，高频缺口修复/恢复路径下避免 O(n²)）。
    const seenTurns = new Set<string>(state.turnIdsByConv[convId] ?? []);
    const seenNodes = new Set<string>(state.nodeIdsByConv[convId] ?? []);
    const seenQueue = new Set<string>(state.queueIdsByConv[convId] ?? []);
    const seenTurnNodes = new Map<string, Set<string>>();
    const turnNodeSeen = (turnId: string | null | undefined, nodeId: string): boolean => {
      if (!turnId) return true; // 无 turn 归属的节点不进 nodeIdsByTurn
      let set = seenTurnNodes.get(turnId);
      if (!set) {
        set = new Set<string>(state.nodeIdsByTurn[turnId] ?? []);
        seenTurnNodes.set(turnId, set);
      }
      return set.has(nodeId);
    };
    const markTurnNode = (turnId: string, nodeId: string): void => {
      let set = seenTurnNodes.get(turnId);
      if (!set) {
        set = new Set<string>(state.nodeIdsByTurn[turnId] ?? []);
        seenTurnNodes.set(turnId, set);
      }
      set.add(nodeId);
    };
    const mergeNode = (node: TurnNode): void => {
      next.nodesById = { ...next.nodesById, [node.node_id]: node };
      if (!seenNodes.has(node.node_id)) {
        seenNodes.add(node.node_id);
        next.nodeIdsByConv = { ...next.nodeIdsByConv, [convId]: addIndexId(next.nodeIdsByConv[convId], node.node_id) };
      }
      if (node.turn_id && !turnNodeSeen(node.turn_id, node.node_id)) {
        markTurnNode(node.turn_id, node.node_id);
        next.nodeIdsByTurn = { ...next.nodeIdsByTurn, [node.turn_id]: addIndexId(next.nodeIdsByTurn[node.turn_id], node.node_id) };
      }
    };
    if (snapshot.live_turn) {
      next.turnsById = { ...next.turnsById, [snapshot.live_turn.turn_id]: snapshot.live_turn };
      // 同 sessionVersions：turn 版本水位只进不退（快照在途期间事件可能已推进）
      next.turnVersions = {
        ...next.turnVersions,
        [snapshot.live_turn.turn_id]: Math.max(snapshot.turn_version, next.turnVersions[snapshot.live_turn.turn_id] ?? 0),
      };
      if (!seenTurns.has(snapshot.live_turn.turn_id)) {
        seenTurns.add(snapshot.live_turn.turn_id);
        next.turnIdsByConv = { ...next.turnIdsByConv, [convId]: addIndexId(next.turnIdsByConv[convId], snapshot.live_turn.turn_id) };
      }
      for (const node of snapshot.nodes) mergeNode(node);
    }
    for (const node of snapshot.queued_nodes) mergeNode(node);
    for (const item of snapshot.queue) {
      next.queueItemsById = { ...next.queueItemsById, [item.queue_item_id]: item };
      if (!seenQueue.has(item.queue_item_id)) {
        seenQueue.add(item.queue_item_id);
        next.queueIdsByConv = { ...next.queueIdsByConv, [convId]: addIndexId(next.queueIdsByConv[convId], item.queue_item_id) };
      }
    }
    this.emit(enforceCaps(next, convId));
  }

  /** 乐观回显：sendNext 出队返回的 user_node 立即并入时间线，无需等 SSE。
   *  同一 turn/node 后续的权威事件（node.delta / turn.status）会覆盖/收敛它，
   *  因此这里不做版本门控（sendNext 本身就是权威出队结果）。 */
  mergeUserNode(turn: Turn, node: TurnNode): void {
    const state = this.state;
    let turnIdsByConv = state.turnIdsByConv;
    let nodeIdsByConv = state.nodeIdsByConv;
    let nodeIdsByTurn = state.nodeIdsByTurn;
    if (!state.turnsById[turn.turn_id]) {
      turnIdsByConv = { ...state.turnIdsByConv, [turn.conversation_id]: addIndexId(state.turnIdsByConv[turn.conversation_id], turn.turn_id) };
    }
    if (!state.nodesById[node.node_id]) {
      nodeIdsByConv = { ...state.nodeIdsByConv, [node.conversation_id]: addIndexId(state.nodeIdsByConv[node.conversation_id], node.node_id) };
      if (node.turn_id) nodeIdsByTurn = { ...state.nodeIdsByTurn, [node.turn_id]: addIndexId(state.nodeIdsByTurn[node.turn_id], node.node_id) };
    }
    const next: GatewayStoreState = {
      ...state,
      turnsById: { ...state.turnsById, [turn.turn_id]: turn },
      nodesById: { ...state.nodesById, [node.node_id]: node },
      turnIdsByConv,
      nodeIdsByConv,
      nodeIdsByTurn,
    };
    if (turn.turn_version != null) {
      next.turnVersions = { ...next.turnVersions, [turn.turn_id]: turn.turn_version };
    }
    this.emit(next);
  }
}

const inferNodeType = (type: string): TurnNode["type"] => {
  if (type === "node.tool") return "tool";
  if (type === "node.user_steering") return "user_steering";
  return "assistant";
};

const pick = (obj: Record<string, unknown>, keys: string[]): Record<string, unknown> => {
  const out: Record<string, unknown> = {};
  for (const key of keys) if (obj[key] !== undefined) out[key] = obj[key];
  return out;
};

// ------------------------------------------------------------
// 单例 + useSyncExternalStore 订阅
// ------------------------------------------------------------

export const gatewayStore = new GatewayStore();

// Selector 记忆：同一 state 对象下相同 key 返回同一引用（useSyncExternalStore 契约）
const selectorCache = new WeakMap<object, Map<string, unknown>>();

const memoSelect = <T>(state: GatewayStoreState, key: string, compute: () => T): T => {
  let cache = selectorCache.get(state);
  if (!cache) {
    cache = new Map();
    selectorCache.set(state, cache);
  }
  if (cache.has(key)) return cache.get(key) as T;
  const value = compute();
  cache.set(key, value);
  return value;
};

export function useGatewaySelector<T>(selector: (state: GatewayStoreState) => T): T {
  return useSyncExternalStore(gatewayStore.subscribe, () => selector(gatewayStore.getState()), () => selector(gatewayStore.getState()));
}

// ------------------------------------------------------------
// 选择器
// ------------------------------------------------------------

export const selectLiveTurn = (convId: string) =>
  (state: GatewayStoreState) => memoSelect(state, `live:${convId}`, () => {
    if (!convId) return null;
    // 索引保持插入序（与 Object.values 顺序一致），首个非终态 turn 即 live；
    // 归属双保险：turn.conversation_id 必须与请求的 convId 一致（防御索引
    // 与实体错配——多会话并发时错配会把别的会话的 live 流渲染进当前视图）。
    const ids = state.turnIdsByConv[convId] ?? [];
    for (const id of ids) {
      const turn = state.turnsById[id];
      if (!turn || turn.conversation_id !== convId) continue;
      if (!["done", "stopped", "error", "interrupted"].includes(turn.status)) return turn;
    }
    return null;
  });

export const selectTurnNodes = (turnId: string) =>
  (state: GatewayStoreState) => memoSelect(state, `nodes:${turnId}`, () => {
    const ids = state.nodeIdsByTurn[turnId] ?? [];
    const nodes: TurnNode[] = [];
    for (const id of ids) {
      const node = state.nodesById[id];
      if (node) nodes.push(node); // 索引可能含已被 trim 淘汰的 id，容错跳过
    }
    return nodes.sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
  });

// 终态 turn 权威数据订阅（跨 state 引用稳定）。返回 {turn, nodes}，
// 仅当该 turn 的 turn/node 引用实际变化时才返回新值——useSyncExternalStore
// 依赖快照稳定：无关事件（新 turn 流式 delta 等）不触发重渲染，
// 终态权威数据（chat.done full_text 晚于 turn.status 到达）到达时才重合并历史。
// P3 内存治理：模块级缓存改为容量上限 LRU（命中即刷新新鲜度，超出淘汰最旧），
// 避免长驻页面浏览大量终态 turn 后无界增长。
const TERMINAL_TURN_DATA_CACHE_MAX = 50;
const terminalTurnDataCache = new Map<string, { turn: Turn; nodes: TurnNode[] }>();

const rememberTerminalTurnData = (turnId: string, value: { turn: Turn; nodes: TurnNode[] }): void => {
  // delete + set 刷新插入序（Map 保持插入序，最旧条目位于首位）
  terminalTurnDataCache.delete(turnId);
  terminalTurnDataCache.set(turnId, value);
  while (terminalTurnDataCache.size > TERMINAL_TURN_DATA_CACHE_MAX) {
    const oldest = terminalTurnDataCache.keys().next();
    if (oldest.done) break;
    terminalTurnDataCache.delete(oldest.value);
  }
};

export const selectTurnWithNodes = (turnId: string) =>
  (state: GatewayStoreState): { turn: Turn; nodes: TurnNode[] } | null => {
    const turn = state.turnsById[turnId];
    if (!turn) return null;
    const nodes = selectTurnNodes(turnId)(state);
    const cached = terminalTurnDataCache.get(turnId);
    if (cached && cached.turn === turn && cached.nodes.length === nodes.length
      && cached.nodes.every((n, i) => n === nodes[i])) {
      return cached;
    }
    const value = { turn, nodes };
    rememberTerminalTurnData(turnId, value);
    return value;
  };

export const selectQueue = (convId: string) =>
  (state: GatewayStoreState) => memoSelect(state, `queue:${convId}`, () => {
    const ids = state.queueIdsByConv[convId] ?? [];
    const items: QueueItem[] = [];
    for (const id of ids) {
      const item = state.queueItemsById[id];
      if (item) items.push(item);
    }
    return items.sort((a, b) => a.position - b.position);
  });

export const selectHasGap = (convId: string) =>
  (state: GatewayStoreState) => {
    const flags = state.gaps[convId];
    return Boolean(flags?.session || flags?.turn);
  };

export const selectSessionVersion = (convId: string) =>
  (state: GatewayStoreState) => state.sessionVersions[convId] ?? 0;
