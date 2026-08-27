import { describe, expect, it, vi } from "vitest";

import { GatewayStore } from "@/gateway/store";
import type { ConversationSnapshot, GatewayEvent } from "@/gateway/types";

// 通知合帧（优化方案 #1）：高频进度事件（node.delta 等）一帧最多通知一轮
// 订阅者；结构事件（turn.status/chat.done/version_gap）保持同步即时通知；
// state 本身永远同步更新（getState 新鲜）；reset 同步 flush 挂起通知。

const event = (type: string, scope: "session" | "turn", version: number, data: Record<string, unknown>, turnId?: string): GatewayEvent => ({
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

const nextFrame = () => new Promise<void>((resolve) => {
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => resolve());
  else setTimeout(resolve, 0);
});

describe("GatewayStore notification coalescing (#1)", () => {
  it("applies progress events to state synchronously but defers listener notification", () => {
    const store = new GatewayStore();
    const listener = vi.fn();
    store.subscribe(listener);
    store.applyEvent(event("turn.status", "turn", 1, { status: "answering", turn_id: "t1" }, "t1"));
    // 结构事件：同步通知
    expect(listener).toHaveBeenCalledTimes(1);
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n1", type: "assistant", delta: "你", seq: 1 }, "t1"));
    // state 立即可见（getState 永远新鲜）
    expect(store.getState().nodesById["n1"]?.text).toBe("你");
    // 通知被推迟到下一帧
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("coalesces N rapid progress deltas into a single frame notification", async () => {
    const store = new GatewayStore();
    const listener = vi.fn();
    store.subscribe(listener);
    for (let i = 1; i <= 5; i++) {
      store.applyEvent(event("node.delta", "turn", i + 1, { node_id: "n1", type: "assistant", delta: String(i), seq: i }, "t1"));
    }
    expect(store.getState().nodesById["n1"]?.text).toBe("12345");
    await nextFrame();
    expect(listener).toHaveBeenCalledTimes(1);
    // 第二帧：又有新 delta → 再通知一次
    store.applyEvent(event("node.delta", "turn", 7, { node_id: "n1", type: "assistant", delta: "6", seq: 6 }, "t1"));
    await nextFrame();
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("flush() dispatches pending notifications synchronously", () => {
    const store = new GatewayStore();
    const listener = vi.fn();
    store.subscribe(listener);
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n1", delta: "x" }, "t1"));
    expect(listener).not.toHaveBeenCalled(); // 挂起中
    store.flush();
    expect(listener).toHaveBeenCalledTimes(1); // 同步派发
    // 无挂起时 flush 是无害空操作
    store.flush();
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("reset() flushes pending notifications before clearing (no stale notify into new session)", async () => {
    const store = new GatewayStore();
    const listener = vi.fn();
    store.subscribe(listener);
    store.applyEvent(event("node.delta", "turn", 2, { node_id: "n1", delta: "x" }, "t1"));
    store.reset();
    // reset 同步 flush：挂起通知在清空前派发，之后不会再有陈旧通知落地
    const callsAfterReset = listener.mock.calls.length;
    expect(callsAfterReset).toBeGreaterThanOrEqual(1);
    await nextFrame();
    expect(listener.mock.calls.length).toBe(callsAfterReset);
    expect(store.getState().conversationsById).toEqual({});
  });

  it("chat.done / version_gap keep synchronous notification", () => {
    const store = new GatewayStore();
    const listener = vi.fn();
    store.subscribe(listener);
    store.applyEvent(event("version_gap", "session", 99, {}));
    expect(listener).toHaveBeenCalledTimes(1);
    store.applyEvent(event("chat.done", "turn", 3, { status: "done", full_text: "done", final_assistant_node_id: "n1" }, "t1"));
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("snapshot application notifies synchronously", () => {
    const store = new GatewayStore();
    const listener = vi.fn();
    store.subscribe(listener);
    const snapshot: ConversationSnapshot = {
      conversation: {
        conversation_id: "conv-1", session_key: "webui:default", origin: "webui", subtype: "main",
        workspace_id: null, execution_scope: "gateway:default", route_metadata: {},
        session_version: 0, created_at: "", updated_at: "",
      },
      session_version: 0,
      queue: [],
      live_turn: null,
      turn_version: 0,
      nodes: [],
      queued_nodes: [],
      pending_approvals: [],
      server_time: "",
    };
    store.applySnapshot(snapshot);
    expect(listener).toHaveBeenCalledTimes(1);
  });
});
