import { describe, expect, it } from "vitest";

import { GatewayStore, selectHasGap, selectLiveTurn, selectQueue, selectSessionVersion, selectTurnNodes, selectTurnWithNodes } from "@/gateway/store";
import type { ConversationSnapshot, GatewayEvent } from "@/gateway/types";

const conv = {
  conversation_id: "conv-1",
  session_key: "webui:default",
  origin: "webui" as const,
  subtype: "main" as const,
  workspace_id: null,
  execution_scope: "gateway:default",
  route_metadata: {},
  session_version: 0,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

const event = (type: string, scope: "session" | "turn" | "runtime" | "delivery", version: number, data: Record<string, unknown>, turnId?: string): GatewayEvent => ({
  type,
  data: {
    conversation_id: "conv-1",
    session_key: "webui:default",
    origin: "webui",
    subtype: "main",
    workspace_id: null,
    turn_id: turnId,
    scope,
    version,
    data,
  },
});

const makeStore = () => new GatewayStore();

/** 真实客户端流程（设计方案 19.2）：先建立 SSE + 获取 Snapshot，再应用事件。 */
const seed = (store: GatewayStore, opts: { sessionVersion?: number; liveTurn?: boolean } = {}) => {
  store.applySnapshot({
    conversation: { ...conv, session_version: opts.sessionVersion ?? 0 },
    session_version: opts.sessionVersion ?? 0,
    queue: [],
    live_turn: opts.liveTurn
      ? { turn_id: "t1", conversation_id: "conv-1", status: "queued", turn_version: 0, started_at: "", finished_at: null, final_assistant_node_id: null, error_code: null, parent_conversation_id: null, parent_turn_id: null }
      : null,
    turn_version: 0,
    nodes: [],
    queued_nodes: [],
    pending_approvals: [],
    server_time: "",
  });
};

describe("GatewayStore applyEvent", () => {
  it("upserts conversation on conversation.upserted", () => {
    const store = makeStore();
    store.applyEvent(event("conversation.upserted", "session", 0, {}));
    expect(store.getState().conversationsById["conv-1"]?.conversation_id).toBe("conv-1");
  });

  it("replaces the active queue on queue.updated", () => {
    const store = makeStore();
    seed(store);
    store.applyEvent(event("queue.updated", "session", 1, {
      queue: [
        { queue_item_id: "q1", conversation_id: "conv-1", position: 1, revision: 1, status: "waiting", text: "a", created_at: "", updated_at: "" },
        { queue_item_id: "q2", conversation_id: "conv-1", position: 2, revision: 1, status: "waiting", text: "b", created_at: "", updated_at: "" },
      ],
    }));
    const queue = selectQueue("conv-1")(store.getState());
    expect(queue.map((q) => q.queue_item_id)).toEqual(["q1", "q2"]);
  });

  it("tracks live turn and nodes in order", () => {
    const store = makeStore();
    seed(store, { liveTurn: true });
    store.applyEvent(event("turn.status", "turn", 1, { status: "thinking", turn_id: "t1" }, "t1"));
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n1", type: "reasoning", text: "想" }, "t1"));
    store.applyEvent(event("node.delta", "turn", 3, { node_id: "n1", type: "reasoning", text: "想继续" }, "t1"));
    store.applyEvent(event("node.delta", "turn", 4, { node_id: "n2", type: "assistant", text: "答" }, "t1"));
    const live = selectLiveTurn("conv-1")(store.getState());
    expect(live?.turn_id).toBe("t1");
    const nodes = selectTurnNodes("t1")(store.getState());
    expect(nodes.map((n) => [n.type, n.text])).toEqual([
      ["reasoning", "想继续"],
      ["assistant", "答"],
    ]);
  });

  it("orders live nodes by position from SSE events (user/mergeUserNode not pushed last)", () => {
    const store = makeStore();
    seed(store, { liveTurn: true });
    // mergeUserNode 的 user 节点 position=1；SSE 节点带真实 position（否则都按 0 排，
    // 会把 user 节点挤到最后 → 用户输入显示在时间线最底部）。
    store.applyEvent(event("node.delta", "turn", 1, { node_id: "n-tool", type: "tool", position: 3 }, "t1"));
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n-reason", type: "reasoning", position: 2 }, "t1"));
    store.applyEvent(event("node.delta", "turn", 3, { node_id: "n-assist", type: "assistant", position: 4 }, "t1"));
    store.mergeUserNode(
      { turn_id: "t1", conversation_id: "conv-1", status: "queued", turn_version: 1, started_at: "", finished_at: null, final_assistant_node_id: null, error_code: null, parent_conversation_id: null, parent_turn_id: null },
      { node_id: "n-user", conversation_id: "conv-1", turn_id: "t1", type: "user", position: 1, status: "dispatched", text: "用户输入", metadata: {}, created_at: "", updated_at: "" },
    );
    const nodes = selectTurnNodes("t1")(store.getState());
    expect(nodes.map((n) => [n.type, n.position])).toEqual([
      ["user", 1], ["reasoning", 2], ["tool", 3], ["assistant", 4],
    ]);
  });

  it("chat.done overrides the final assistant node text (authority)", () => {
    const store = makeStore();
    seed(store, { liveTurn: true });
    store.applyEvent(event("turn.status", "turn", 1, { status: "answering", turn_id: "t1" }, "t1"));
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n2", type: "assistant", text: "流式草稿" }, "t1"));
    store.applyEvent(event("chat.done", "turn", 3, { full_text: "权威最终回复", final_assistant_node_id: "n2" }, "t1"));
    const nodes = selectTurnNodes("t1")(store.getState());
    expect(nodes[0]?.text).toBe("权威最终回复");
    expect(store.getState().turnsById["t1"]?.status).toBe("done");
  });

  it("appends node text by delta+seq (contract ①) and drops duplicate/out-of-order deltas", () => {
    const store = makeStore();
    seed(store, { liveTurn: true });
    // 新后端契约：node.delta 只带 delta(本次增量)+seq(节点内递增序号)，无全量 text
    store.applyEvent(event("node.delta", "turn", 1, { node_id: "n1", type: "assistant", delta: "你", seq: 1, position: 2 }, "t1"));
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n1", type: "assistant", delta: "好", seq: 2, position: 2 }, "t1"));
    // 重复/乱序 delta（seq <= 已见水位）→ 文本增量丢弃
    store.applyEvent(event("node.delta", "turn", 3, { node_id: "n1", type: "assistant", delta: "坏", seq: 2, position: 2 }, "t1"));
    expect(selectTurnNodes("t1")(store.getState())[0]?.text).toBe("你好");
    // 节点终态 upsert（finalize_node）仍携带权威全量 text → 替换而非追加
    store.applyEvent(event("node.delta", "turn", 4, { node_id: "n1", type: "assistant", text: "全量终态", status: "done", position: 2 }, "t1"));
    expect(selectTurnNodes("t1")(store.getState())[0]?.text).toBe("全量终态");
  });

  it("chat.done resets streamed delta text to the authoritative full_text", () => {
    const store = makeStore();
    seed(store, { liveTurn: true });
    store.applyEvent(event("turn.status", "turn", 1, { status: "answering", turn_id: "t1" }, "t1"));
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n2", type: "assistant", delta: "流式", seq: 1, position: 3 }, "t1"));
    store.applyEvent(event("node.delta", "turn", 3, { node_id: "n2", type: "assistant", delta: "草稿", seq: 2, position: 3 }, "t1"));
    store.applyEvent(event("chat.done", "turn", 4, { full_text: "权威最终回复", final_assistant_node_id: "n2" }, "t1"));
    const nodes = selectTurnNodes("t1")(store.getState());
    expect(nodes[0]?.text).toBe("权威最终回复");
    expect(store.getState().turnsById["t1"]?.status).toBe("done");
  });

  it("drops stale events and flags gaps on version jumps", () => {
    const store = makeStore();
    // 版本跳跃 → 缺口（水位不推进，等待快照修复）
    store.applyEvent(event("queue.updated", "session", 3, { queue: [] }));
    expect(selectHasGap("conv-1")(store.getState())).toBe(true);
    // 快照修复缺口
    store.applySnapshot({
      conversation: { ...conv, session_version: 3 },
      session_version: 3,
      queue: [{ queue_item_id: "q1", conversation_id: "conv-1", position: 1, revision: 1, status: "waiting", text: "ok", created_at: "", updated_at: "" }],
      live_turn: null, turn_version: 0, nodes: [], queued_nodes: [],
      pending_approvals: [], server_time: "",
    });
    expect(selectHasGap("conv-1")(store.getState())).toBe(false);
    expect(selectSessionVersion("conv-1")(store.getState())).toBe(3);
    // 旧事件丢弃
    store.applyEvent(event("queue.updated", "session", 2, { queue: [{ queue_item_id: "stale", conversation_id: "conv-1", position: 9, revision: 1, status: "waiting", text: "stale", created_at: "", updated_at: "" }] }));
    expect(selectQueue("conv-1")(store.getState())).toHaveLength(1);
    // 连续事件正常应用
    store.applyEvent(event("queue.updated", "session", 4, { queue: [] }));
    expect(selectHasGap("conv-1")(store.getState())).toBe(false);
    expect(selectSessionVersion("conv-1")(store.getState())).toBe(4);
  });

  it("accepts non-decreasing node.delta versions; jumps flag a gap for snapshot repair", () => {
    const store = makeStore();
    seed(store, { liveTurn: true });
    // 跳变版本（2 → 4）不再丢弃：当前事件接受，缺口标记等待快照修复
    store.applyEvent(event("node.delta", "turn", 4, { node_id: "n1", type: "assistant", text: "v4" }, "t1"));
    expect(selectTurnNodes("t1")(store.getState()).map((n) => n.text)).toEqual(["v4"]);
    expect(selectHasGap("conv-1")(store.getState())).toBe(true);
    // 严格更旧事件仍丢弃；连续非递减事件接受并清除缺口
    store.applyEvent(event("node.delta", "turn", 3, { node_id: "n2", type: "assistant", text: "v3-stale" }, "t1"));
    expect(selectTurnNodes("t1")(store.getState()).map((n) => n.text)).toEqual(["v4"]);
    store.applyEvent(event("node.delta", "turn", 5, { node_id: "n3", type: "assistant", text: "v5" }, "t1"));
    expect(selectTurnNodes("t1")(store.getState()).map((n) => n.text)).toEqual(["v4", "v5"]);
    expect(selectHasGap("conv-1")(store.getState())).toBe(false);
  });

  it("session-level gaps survive consecutive turn events until session repair", () => {
    const store = makeStore();
    seed(store, { liveTurn: true });
    // session 级跳变 → 置位 session 缺口（水位不推进，等待快照/session 事件修复）
    store.applyEvent(event("queue.updated", "session", 9, { queue: [] }));
    expect(selectHasGap("conv-1")(store.getState())).toBe(true);
    expect(store.getState().gaps["conv-1"]).toEqual({ session: true });
    // 后续连续 turn 级事件不得误清 session 缺口（否则快照修复永不触发）
    store.applyEvent(event("node.delta", "turn", 1, { node_id: "n1", type: "assistant", text: "a" }, "t1"));
    expect(selectHasGap("conv-1")(store.getState())).toBe(true);
    // session 级事件（版本与种子连续）成功应用后清除 session 缺口
    store.applyEvent(event("queue.updated", "session", 1, { queue: [] }));
    expect(selectHasGap("conv-1")(store.getState())).toBe(false);
    expect(store.getState().gaps["conv-1"]).toBeUndefined();
  });

  it("keeps terminal queue items of the conversation on queue.updated", () => {
    const store = makeStore();
    seed(store);
    store.applyEvent(event("queue.updated", "session", 1, { queue: [
      { queue_item_id: "q1", conversation_id: "conv-1", position: 1, revision: 1, status: "sent", text: "已发送归档", created_at: "", updated_at: "" },
    ] }));
    // 事件全量不再覆盖终端归档项：q1（sent）保留，q2 入队
    store.applyEvent(event("queue.updated", "session", 2, { queue: [
      { queue_item_id: "q2", conversation_id: "conv-1", position: 1, revision: 1, status: "waiting", text: "新消息", created_at: "", updated_at: "" },
    ] }));
    const ids = selectQueue("conv-1")(store.getState()).map((q) => q.queue_item_id).sort();
    expect(ids).toEqual(["q1", "q2"]);
  });

  it("caps per-conversation turns with LRU semantics", () => {
    const store = makeStore();
    seed(store);
    for (let i = 0; i < 260; i += 1) {
      store.applyEvent(event("turn.status", "turn", i + 1, { status: "done", turn_id: `t${i}` }, `t${i}`));
    }
    const turns = Object.values(store.getState().turnsById).filter((t) => t.conversation_id === "conv-1");
    expect(turns.length).toBeLessThanOrEqual(250);
  });

  it("selectTurnWithNodes is reference-stable across unrelated emits and updates on chat.done", () => {
    const store = makeStore();
    seed(store, { liveTurn: true });
    store.applyEvent(event("turn.status", "turn", 1, { status: "answering", turn_id: "t1" }, "t1"));
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n2", type: "assistant", delta: "流式", seq: 1, position: 3 }, "t1"));
    const first = selectTurnWithNodes("t1")(store.getState());
    expect(first).not.toBeNull();
    // 无关事件（queue 更新）→ 引用稳定（不触发重渲染/重合并）
    store.applyEvent(event("queue.updated", "session", 3, { queue: [{ queue_item_id: "q1", conversation_id: "conv-1", position: 1, revision: 1, status: "waiting", text: "x", created_at: "", updated_at: "" }] }));
    expect(selectTurnWithNodes("t1")(store.getState())).toBe(first);
    // chat.done 权威文本覆盖 → 节点引用变化 → 返回新值（触发历史重合并）
    store.applyEvent(event("chat.done", "turn", 4, { full_text: "权威最终回复", final_assistant_node_id: "n2" }, "t1"));
    const second = selectTurnWithNodes("t1")(store.getState());
    expect(second).not.toBe(first);
    expect(second?.nodes[0]?.text).toBe("权威最终回复");
    expect(second?.turn.status).toBe("done");
    // 同 state 重复读取稳定
    expect(selectTurnWithNodes("t1")(store.getState())).toBe(second);
  });

  it("selectTurnWithNodes returns null for unknown turns", () => {
    const store = makeStore();
    seed(store);
    expect(selectTurnWithNodes("nope")(store.getState())).toBeNull();
  });

  it("conversation.cleared wipes local turns/nodes/queue but keeps conversation", () => {
    const store = makeStore();
    seed(store);
    store.applyEvent(event("turn.status", "turn", 1, { status: "done", turn_id: "t1" }, "t1"));
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n1", type: "user", text: "hi", status: "done" }, "t1"));
    store.applyEvent(event("queue.updated", "session", 1, { queue: [{ queue_item_id: "q1", conversation_id: "conv-1", position: 1, revision: 1, status: "waiting", text: "wait", created_at: "", updated_at: "" }] }));
    expect(selectTurnNodes("t1")(store.getState()).length).toBeGreaterThan(0);
    store.applyEvent(event("conversation.cleared", "session", 2, { counts: { turns: 1, turn_nodes: 1, queue_items: 1 } }));
    const state = store.getState();
    expect(selectTurnNodes("t1")(state)).toEqual([]);
    expect(state.turnsById["t1"]).toBeUndefined();
    expect(state.queueItemsById["q1"]).toBeUndefined();
    // 会话行保留
    expect(state.conversationsById["conv-1"]).toBeDefined();
  });
});

describe("GatewayStore applySnapshot", () => {
  it("replaces conversation state and clears the gap", () => {
    const store = makeStore();
    store.applyEvent(event("queue.updated", "session", 9, { queue: [] })); // 缺口
    expect(selectHasGap("conv-1")(store.getState())).toBe(true);
    const snapshot: ConversationSnapshot = {
      conversation: { ...conv, session_version: 12 },
      session_version: 12,
      queue: [{ queue_item_id: "q1", conversation_id: "conv-1", position: 1, revision: 3, status: "waiting", text: "恢复", created_at: "", updated_at: "" }],
      live_turn: { turn_id: "t9", conversation_id: "conv-1", status: "thinking", turn_version: 5, started_at: "", finished_at: null, final_assistant_node_id: null, error_code: null, parent_conversation_id: null, parent_turn_id: null },
      turn_version: 5,
      nodes: [
        { node_id: "n1", conversation_id: "conv-1", turn_id: "t9", type: "user", position: 1, status: "dispatched", text: "恢复问", metadata: {}, source_channel: null, source_message_id: null, sender_id: null, sender_name: null, created_at: "", updated_at: "" },
        { node_id: "n2", conversation_id: "conv-1", turn_id: "t9", type: "assistant", position: 2, status: "streaming", text: "半截", metadata: {}, source_channel: null, source_message_id: null, sender_id: null, sender_name: null, created_at: "", updated_at: "" },
      ],
      queued_nodes: [],
      pending_approvals: [],
      server_time: "2026-01-01T00:00:00+00:00",
    };
    store.applySnapshot(snapshot);
    expect(selectHasGap("conv-1")(store.getState())).toBe(false);
    expect(selectSessionVersion("conv-1")(store.getState())).toBe(12);
    expect(selectLiveTurn("conv-1")(store.getState())?.turn_id).toBe("t9");
    expect(selectTurnNodes("t9")(store.getState())).toHaveLength(2);
    expect(selectQueue("conv-1")(store.getState())[0]?.text).toBe("恢复");
  });
});

describe("GatewayStore caps & rolling (收官优化)", () => {
  it("trims nodeIdsByTurn when nodes are evicted by the per-conversation cap", () => {
    const store = makeStore();
    seed(store);
    const nodes = Array.from({ length: 3100 }, (_, i) => ({
      node_id: `n${i}` ,
      conversation_id: "conv-1",
      turn_id: `t${i % 5}` ,
      type: "assistant" as const,
      position: i,
      status: "done",
      text: `text ${i}` ,
      metadata: {},
      created_at: "",
      updated_at: "",
    }));
    store.applySnapshot({
      conversation: { ...conv, session_version: 0 },
      session_version: 0,
      queue: [],
      live_turn: null,
      turn_version: 0,
      nodes: [],
      queued_nodes: nodes,
      pending_approvals: [],
      server_time: "",
    });
    const state = store.getState();
    expect(Object.keys(state.nodesById).length).toBeLessThanOrEqual(3000);
    // nodeIdsByTurn 同步清理：索引里不存在已淘汰的 node id
    const totalIndexed = Object.values(state.nodeIdsByTurn).reduce((n, ids) => n + ids.length, 0);
    expect(totalIndexed).toBeLessThanOrEqual(3000);
    for (const ids of Object.values(state.nodeIdsByTurn)) {
      for (const id of ids) expect(state.nodesById[id]).toBeDefined();
    }
  });

  it("selectTurnWithNodes cache is capped (LRU eviction beyond 50 entries)", () => {
    const store = makeStore();
    seed(store);
    store.applyEvent(event("chat.done", "turn", 1, { full_text: "first", final_assistant_node_id: "nn-first" }, "tt-first"));
    const first = selectTurnWithNodes("tt-first")(store.getState());
    expect(first).not.toBeNull();
    // 涌入大量终态 turn 并逐个订阅 → 超出容量上限后最旧条目被淘汰
    for (let i = 0; i < 55; i += 1) {
      const turnId = `tt-${i}`;
      store.applyEvent(event("chat.done", "turn", i + 2, { full_text: `f${i}`, final_assistant_node_id: `nn-${i}` }, turnId));
      selectTurnWithNodes(turnId)(store.getState());
    }
    // 同数据再次选择返回新引用（旧缓存已被 LRU 淘汰，缓存不再无界增长）
    expect(selectTurnWithNodes("tt-first")(store.getState())).not.toBe(first);
  });

  it("rolls deliveries per conversation to at most 200 entries (keeps latest)", () => {
    const store = makeStore();
    seed(store);
    for (let i = 0; i < 210; i += 1) {
      store.applyEvent(event("delivery.status", "delivery", i + 1, {
        message_id: `m${i}` ,
        state: "delivered",
        channel: "feishu",
      }));
    }
    const deliveries = Object.values(store.getState().deliveriesById);
    expect(deliveries.length).toBeLessThanOrEqual(200);
    expect(deliveries.some((d) => String((d as { message_id?: string }).message_id) === "m209")).toBe(true);
    expect(deliveries.some((d) => String((d as { message_id?: string }).message_id) === "m0")).toBe(false);
  });

  it("refreshes updated_at when events touch a turn", () => {
    const store = makeStore();
    seed(store);
    store.applyEvent(event("turn.status", "turn", 1, { status: "thinking", turn_id: "t1" }, "t1"));
    expect(store.getState().turnsById["t1"]?.updated_at).toBeTruthy();
  });
});
describe("GatewayStore selectors", () => {
  it("returns stable references for the same state (useSyncExternalStore contract)", () => {
    const store = makeStore();
    store.applyEvent(event("queue.updated", "session", 1, { queue: [] }));
    const state = store.getState();
    const first = selectQueue("conv-1")(state);
    const second = selectQueue("conv-1")(state);
    expect(first).toBe(second);
  });
});
