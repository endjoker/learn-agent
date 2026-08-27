import { describe, expect, it, vi } from "vitest";

import type { Turn, TurnNode } from "@/gateway/types";
import { turnToTimeline } from "@/pages/chat/chatTimeline";

// 节点级 item 引用缓存（优化方案 #3/#6）：live 构建下「node 引用相同 ⇒ item
// 相同」，每 delta 只有内容变化的节点（流式尾部）产生新引用，其余行 TimelineRow
// 的浅比较 memo 直接短路；终态构建不走缓存（markFinalAnswer 重推 final 标记）。

const makeTurn = (): Turn => ({
  turn_id: "t1",
  conversation_id: "conv-1",
  status: "answering",
  turn_version: 0,
  started_at: "",
  finished_at: null,
  final_assistant_node_id: null,
  error_code: null,
  parent_conversation_id: null,
  parent_turn_id: null,
});

const node = (over: Partial<TurnNode>): TurnNode => ({
  node_id: over.node_id ?? "",
  conversation_id: "conv-1",
  turn_id: "t1",
  type: over.type ?? "assistant",
  position: over.position ?? 0,
  status: over.status ?? "done",
  text: over.text ?? "",
  metadata: over.metadata ?? {},
  created_at: "",
  updated_at: "",
});

describe("turnToTimeline reference stability (#3)", () => {
  it("reuses identical item references for unchanged nodes across live builds", () => {
    const turn = makeTurn();
    const nUser = node({ node_id: "u1", type: "user", text: "问题", position: 1 });
    const nTail = node({ node_id: "a1", type: "assistant", text: "答", position: 2, status: "streaming" });
    const first = turnToTimeline(turn, [nUser, nTail], { live: true });
    // 相同 node 引用再次构建（新数组、同节点）：item 引用全部复用
    const second = turnToTimeline(turn, [nUser, nTail], { live: true });
    expect(second).not.toBe(first); // 数组本身是新的
    expect(second.map((i) => i.key)).toEqual(first.map((i) => i.key));
    expect(second[0]).toBe(first[0]);
    expect(second[1]).toBe(first[1]);
  });

  it("only the changed (tail) node gets a new item; earlier rows stay identical", () => {
    const turn = makeTurn();
    const nUser = node({ node_id: "u1", type: "user", text: "问题", position: 1 });
    const tailV1 = node({ node_id: "a1", type: "assistant", text: "答", position: 2, status: "streaming" });
    const v1 = turnToTimeline(turn, [nUser, tailV1], { live: true });
    // delta 追加：store 不可变替换 → 尾部节点新引用
    const tailV2 = node({ node_id: "a1", type: "assistant", text: "答案更长了", position: 2, status: "streaming" });
    const v2 = turnToTimeline(turn, [nUser, tailV2], { live: true });
    expect(v2[0]).toBe(v1[0]); // 未变化行：同一对象
    expect(v2[1]).not.toBe(v1[1]); // 流式尾行：新对象
    const tailItem = v2[1];
    expect(tailItem?.kind === "message" ? tailItem.message.content : "").toBe("答案更长了");
  });

  it("tool rows keep stable references while other nodes stream", () => {
    const turn = makeTurn();
    const nTool = node({
      node_id: "tool1", type: "tool", position: 1, status: "running",
      metadata: { tool: "bash", params_summary: JSON.stringify({ command: "ls" }) },
    });
    const a1 = node({ node_id: "a1", type: "assistant", text: "x", position: 2, status: "streaming" });
    const v1 = turnToTimeline(turn, [nTool, a1], { live: true });
    const a2 = node({ node_id: "a1", type: "assistant", text: "xy", position: 2, status: "streaming" });
    const v2 = turnToTimeline(turn, [nTool, a2], { live: true });
    expect(v2.find((i) => i.kind === "tool")).toBe(v1.find((i) => i.kind === "tool"));
  });

  it("terminal (non-live) builds are not cached — fresh items each time", () => {
    const turn = makeTurn();
    const n = node({ node_id: "a1", type: "assistant", text: "最终答复", position: 1 });
    const v1 = turnToTimeline(turn, [n]);
    const v2 = turnToTimeline(turn, [n]);
    // 终态路径每次新建（markFinalAnswer 需要按整轮重推标记），不共享 item
    expect(v1.length).toBe(1);
    expect(v2.length).toBe(1);
  });

  it("reasoning live flag survives caching and updates when node status flips", () => {
    const turn = makeTurn();
    const streaming = node({ node_id: "r1", type: "reasoning", text: "思考中…", position: 1, status: "streaming" });
    const v1 = turnToTimeline(turn, [streaming], { live: true });
    const firstItem = v1[0];
    expect(firstItem?.kind === "reasoning" && firstItem.live === true).toBe(true);
    // 状态翻转 → 新节点引用 → 新 item（live=false 不缓存，直接重建）
    const done = node({ node_id: "r1", type: "reasoning", text: "思考完成", position: 1, status: "done" });
    const v2 = turnToTimeline(turn, [done], { live: true });
    expect(v2[0]).not.toBe(firstItem);
    const rebuilt = v2[0];
    expect(rebuilt?.kind === "reasoning" && !rebuilt.live).toBe(true);
  });

  it("cache is keyed per node object — different turns never cross-contaminate", () => {
    const t1 = makeTurn();
    const t2 = { ...makeTurn(), turn_id: "t2" };
    const nA = node({ node_id: "shared-ref-style", type: "assistant", text: "同构节点", position: 1 });
    const v1 = turnToTimeline(t1, [nA], { live: true });
    const nB = node({ node_id: "shared-ref-style", type: "assistant", text: "同构节点", position: 1 });
    const v2 = turnToTimeline(t2, [nB], { live: true });
    expect(v1[0]).not.toBe(v2[0]); // 不同 node 对象各自缓存
    expect(v1[0]?.key).toContain("t1");
    expect(v2[0]?.key).toContain("t2");
  });

  it("unknown node types still fall back to message bubbles without throwing", () => {
    const turn = makeTurn();
    const spy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      const n = node({ node_id: "x1", type: "status", text: "状态文本", position: 1 });
      const items = turnToTimeline(turn, [n], { live: true });
      expect(items).toHaveLength(1);
      expect(items[0]?.kind).toBe("message");
    } finally {
      spy.mockRestore();
    }
  });
});
