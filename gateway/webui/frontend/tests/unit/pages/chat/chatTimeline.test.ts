import { describe, expect, it, vi } from "vitest";

import { historyToTimeline, mergeTerminalTurn, nodeToMessage, turnToTimeline } from "@/pages/chat/chatTimeline";
import type { Turn, TurnNode } from "@/gateway/types";

const turn: Turn = {
  turn_id: "t1",
  conversation_id: "c1",
  status: "done",
  turn_version: 5,
  runtime_snapshot_id: null,
  started_at: "2026-01-01T00:00:00+00:00",
  finished_at: "2026-01-01T00:00:01+00:00",
  final_assistant_node_id: "n3",
  error_code: null,
  parent_conversation_id: null,
  parent_turn_id: null,
};

const node = (over: Partial<TurnNode>): TurnNode => ({
  node_id: "n",
  conversation_id: "c1",
  turn_id: "t1",
  type: "assistant",
  position: 1,
  status: "done",
  text: "",
  metadata: {},
  created_at: "",
  updated_at: "",
  ...over,
});

describe("mergeTerminalTurn", () => {
  it("appends a newly finished turn at the end of the history", () => {
    const old: Array<{ turn: Turn; nodes: TurnNode[] }> = [
      { turn: { ...turn, turn_id: "t0" }, nodes: [node({ node_id: "a0", type: "assistant", text: "旧" })] },
    ];
    const done = { turn: { ...turn, turn_id: "t9" }, nodes: [node({ node_id: "a9", type: "assistant", text: "新" })] };
    const merged = mergeTerminalTurn(old, done);
    expect(merged.map((item) => item.turn.turn_id)).toEqual(["t0", "t9"]);
  });

  it("replaces an existing turn entry in place (no duplicates)", () => {
    const existing = { turn: { ...turn, turn_id: "t1" }, nodes: [node({ node_id: "a1", type: "assistant", text: "草稿" })] };
    const authoritative = { turn: { ...turn, turn_id: "t1", status: "done" as const }, nodes: [node({ node_id: "a1", type: "assistant", text: "权威终态" })] };
    const merged = mergeTerminalTurn([existing], authoritative);
    expect(merged).toHaveLength(1);
    expect(merged[0]?.nodes[0]?.text).toBe("权威终态");
  });
});

describe("turnToTimeline", () => {
  it("keeps chronological order: user then assistant", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "u1", type: "user", text: "第一句", position: 1 }),
      node({ node_id: "a1", type: "assistant", text: "回复", position: 2 }),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["message", "message"]);
    expect(items[0]?.kind === "message" && items[0].message.content).toBe("第一句");
    expect(items[1]?.kind === "message" && items[1].message.content).toBe("回复");
  });

  it("renders tool nodes with name, input, result and collapsed summary", () => {
    const items = turnToTimeline(turn, [
      node({
        node_id: "tool1",
        type: "tool",
        status: "done",
        position: 2,
        metadata: { tool: "bash", call_id: "call-1", params_summary: "{\"cmd\":\"ls\"}", result_summary: "total 8" },
      }),
    ]);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "tool",
      name: "bash",
      input: "{\"cmd\":\"ls\"}",
      result: "total 8",
      summary: "{\"cmd\":\"ls\"}",
      pending: false,
    });
  });

  it("marks running tool nodes pending and falls back to call_id for the name", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "tool2", type: "tool", status: "running", position: 1, metadata: { call_id: "call-9" } }),
    ]);
    expect(items[0]).toMatchObject({ kind: "tool", name: "call-9", pending: true, summary: "" });
  });

  it("marks tool calls as error when the result text carries an error prefix", () => {
    // 超时 / 拒绝 / 失败 结果：即使 meta.error 缺失也应标红（与成功卡区分）。
    const items = turnToTimeline(turn, [
      node({ node_id: "tool-t", type: "tool", status: "done", position: 1, metadata: { tool: "bash", call_id: "c1", result_summary: "❌ 工具执行超时（>60s）" } }),
    ]);
    expect(items[0]).toMatchObject({ kind: "tool", isError: true });
    if (items[0]?.kind === "tool") expect(items[0].error).toContain("超时");
  });

  it("keeps successful tool calls non-error", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "tool-ok", type: "tool", status: "done", position: 1, metadata: { tool: "bash", call_id: "c1", result_summary: "total 8" } }),
    ]);
    expect(items[0]).toMatchObject({ kind: "tool", isError: false });
  });

  it("prefers explicit meta.error over the result-text heuristic", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "tool-e", type: "tool", status: "done", position: 1, metadata: { tool: "bash", call_id: "c1", error: "command denied", result_summary: "blocked" } }),
    ]);
    if (items[0]?.kind === "tool") expect(items[0].error).toBe("command denied");
  });

  it("skips empty text nodes", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "e1", type: "assistant", text: "", position: 1 }),
      node({ node_id: "u1", type: "user", text: "hi", position: 2 }),
    ]);
    expect(items).toHaveLength(1);
  });
});

describe("turnToTimeline runtime projection (dsh alignment)", () => {
  it("renders runtime-marked assistant node as a card, not a plain message", () => {
    const items = turnToTimeline(turn, [
      node({
        node_id: "rt-a",
        type: "assistant",
        text: "方案完成",
        position: 1,
        status: "done",
        metadata: { runtime_type: "plan", runtime_id: "plan-1", runtime_status: "done" },
      }),
    ]);
    expect(items).toHaveLength(1);
    expect(items[0]?.kind).toBe("projection");
    if (items[0]?.kind === "projection") {
      expect(items[0].runtime_type).toBe("plan");
      expect(items[0].runtime_id).toBe("plan-1");
      expect(items[0].runtime_status).toBe("done");
      expect(items[0].finalText).toBe("方案完成");
    }
  });

  it("folds runtime tool/reasoning detail into the step card (not flattened into the timeline)", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "rt-u", type: "user", text: "请规划", position: 1 }),
      node({ node_id: "rt-r", type: "reasoning", text: "分析", position: 2 }),
      node({ node_id: "rt-t", type: "tool", text: "", position: 3, metadata: { tool: "read", call_id: "c1", params_summary: "a.txt", runtime_type: "plan", runtime_id: "plan-1" } }),
      node({ node_id: "rt-a", type: "assistant", text: "完成", position: 4, metadata: { runtime_type: "plan", runtime_id: "plan-1", runtime_status: "done" } }),
    ]);
    const kinds = items.map((i) => i.kind);
    // 中间步骤的工具/思考收进 plan 卡片，不直接平铺在时间线
    expect(kinds).toEqual(["message", "projection"]);
    const card = items.find((i) => i.kind === "projection");
    if (card && card.kind === "projection") {
      expect(card.detailNodes?.map((n) => n.type)).toEqual(["reasoning", "tool"]);
      expect(card.finalText).toBe("完成");
    }
  });

  it("folds live streaming tools into a running card (same key as the step card)", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "rt-t", type: "tool", text: "", position: 1, status: "running", metadata: { tool: "read", call_id: "c1", runtime_type: "goal", runtime_id: "g1" } }),
    ], { live: true });
    expect(items).toHaveLength(1);
    expect(items[0]?.kind).toBe("projection");
    if (items[0]?.kind === "projection") {
      expect(items[0].status).toBe("running");
      expect(items[0].runtime_type).toBe("goal");
      expect(items[0].runtime_id).toBe("g1");
      expect(items[0].detailNodes).toHaveLength(1);
    }
  });

  it("keeps non-runtime turns as plain message/tool items", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "u1", type: "user", text: "hi", position: 1 }),
      node({ node_id: "a1", type: "assistant", text: "回复", position: 2 }),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["message", "message"]);
  });

  it("does NOT mark the live streaming assistant message as final (throttle/degraded path)", () => {
    // 收官修复：live=true 时不标 final —— Markdown 节流与 >8KB 降级在流式期间生效，
    // 终态并入历史后才标 final 触发权威同步渲染。
    const items = turnToTimeline(turn, [
      node({ node_id: "u1", type: "user", text: "hi", position: 1, status: "done" }),
      node({ node_id: "a1", type: "assistant", text: "**流式**", position: 2, status: "streaming" }),
    ], { live: true });
    const msg = items.find((i) => i.kind === "message" && i.message.role === "assistant");
    if (msg && msg.kind === "message") {
      expect((msg.message as { kind?: string }).kind).toBeUndefined();
    }
  });

  it("marks the terminal (history) assistant message as final", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "u1", type: "user", text: "hi", position: 1, status: "done" }),
      node({ node_id: "a1", type: "assistant", text: "**终态**", position: 2, status: "done" }),
    ]);
    const msg = items.find((i) => i.kind === "message" && i.message.role === "assistant");
    if (msg && msg.kind === "message") {
      expect((msg.message as { kind?: string }).kind).toBe("final");
    }
  });

  it("surfaces runtime status/error from metadata (failed/blocked states)", () => {
    const items = turnToTimeline(turn, [
      node({
        node_id: "rt-f",
        type: "assistant",
        text: "方案失败",
        position: 1,
        status: "done",
        metadata: {
          runtime_type: "plan", runtime_id: "plan-9",
          status: "failed", error_code: "AGENT_EXECUTION_FAILED", message: "执行超时",
        },
      }),
    ]);
    expect(items).toHaveLength(1);
    if (items[0]?.kind === "projection") {
      expect(items[0].status).toBe("failed");
      expect(items[0].errorCode).toBe("AGENT_EXECUTION_FAILED");
      expect(items[0].message).toBe("执行超时");
    }
  });

  it("reads goal_round from metadata for the round badge", () => {
    const items = turnToTimeline(turn, [
      node({
        node_id: "rt-g",
        type: "assistant",
        text: "第 2 轮完成",
        position: 1,
        status: "done",
        metadata: { runtime_type: "goal", runtime_id: "g1", goal_round: 2 },
      }),
    ]);
    if (items[0]?.kind === "projection") expect(items[0].goalRound).toBe(2);
  });

  it("recognizes the A5 archive placeholder as a goalArchived item", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "a-arch", type: "assistant", text: "[Goal g-abc 第3轮终答已归档，详见目标页]", position: 1, status: "done" }),
    ]);
    expect(items).toHaveLength(1);
    expect(items[0]?.kind).toBe("goalArchived");
    if (items[0]?.kind === "goalArchived") expect(items[0].goalId).toBe("g-abc");
  });

  it("warns and falls back to a bubble for unknown node types", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      const items = turnToTimeline(turn, [
        node({ node_id: "u1", type: "mystery" as never, text: "未知节点", position: 1, status: "done" }),
      ]);
      expect(items).toHaveLength(1);
      expect(items[0]?.kind).toBe("message");
      expect(warn).toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });
});

describe("historyToTimeline", () => {
  it("flattens history pages in chronological order", () => {
    const items = historyToTimeline([
      { turn: { ...turn, turn_id: "t-old" }, nodes: [node({ node_id: "u1", type: "user", text: "旧" })] },
      { turn: { ...turn, turn_id: "t-new" }, nodes: [node({ node_id: "u2", type: "user", text: "新" })] },
    ]);
    const texts = items.filter((i) => i.kind === "message").map((i) => (i.kind === "message" ? i.message.content : ""));
    expect(texts).toEqual(["旧", "新"]);
  });
});

describe("nodeToMessage", () => {
  it("maps user/user_steering to user role, others to assistant", () => {
    expect(nodeToMessage(node({ type: "user", text: "a" })).role).toBe("user");
    expect(nodeToMessage(node({ type: "user_steering", text: "b" })).role).toBe("user");
    expect(nodeToMessage(node({ type: "assistant", text: "c" })).role).toBe("assistant");
    expect(nodeToMessage(node({ type: "reasoning", text: "d" })).role).toBe("assistant");
  });
});
describe("stripMdSummary", () => {
  it("strips markdown syntax for one-line card summaries", async () => {
    const { stripMdSummary } = await import("@/pages/chat/toolSummary");
    expect(stripMdSummary("已完成 **step_1**：新建 `output/plan.md` 已记录")).toBe("已完成 step_1：新建 output/plan.md 已记录");
    expect(stripMdSummary("结果 ```py\nx = 1``` 完成")).toBe("结果 完成");
    expect(stripMdSummary("见 [文档](http://x/y) 与 *斜体*")).toBe("见 文档 与 斜体");
  });
});

describe("turnToTimeline runtime_final flatten (dsh 终答平铺)", () => {
  const rtFinal = (over: Partial<TurnNode>): TurnNode =>
    node({
      type: "assistant", status: "done",
      metadata: { runtime_type: "plan", runtime_id: "plan-1", runtime_status: "done", runtime_final: true },
      ...over,
    });

  it("renders a runtime_final node as a step card plus a plain final message", () => {
    // plan 最终 step / goal 每轮（后端 runtime_final 标记）：工具明细折叠进
    // 投影卡，终答平铺为正式渲染消息（持久可见），不再"消失"。
    const items = turnToTimeline(turn, [
      rtFinal({ node_id: "rt-final", text: "**最终结论**：和为 10", position: 2 }),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["projection", "message"]);
    const msg = items[1];
    if (msg && msg.kind === "message") {
      expect(msg.message.role).toBe("assistant");
      expect(msg.message.content).toBe("**最终结论**：和为 10");
      // 终态（非 live）下标记 final，触发权威全量同步渲染。
      expect((msg.message as { kind?: string }).kind).toBe("final");
    }
  });

  it("folds the final step's tool detail into its card before the final message", () => {
    // 最终 step 的工具/思考明细折叠进其投影卡（不直接平铺在时间线），
    // 答复本身平铺为正式消息。
    const items = turnToTimeline(turn, [
      node({ node_id: "rt-r", type: "reasoning", text: "核对计算", position: 1, metadata: { runtime_type: "plan", runtime_id: "plan-1" } }),
      node({ node_id: "rt-t", type: "tool", text: "", position: 2, status: "done", metadata: { tool: "calculate", call_id: "c9", params_summary: "1+2", result_summary: "3", runtime_type: "plan", runtime_id: "plan-1" } }),
      rtFinal({ node_id: "rt-final", text: "完成", position: 3 }),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["projection", "message"]);
    if (items[0]?.kind === "projection") {
      expect(items[0].detailNodes?.map((n) => n.type)).toEqual(["reasoning", "tool"]);
    }
  });

  it("keeps intermediate replies as cards and the final reply as card + message", () => {
    const items = turnToTimeline(turn, [
      node({
        node_id: "rt-step1", type: "assistant", text: "step_1 完成", position: 1, status: "done",
        metadata: { runtime_type: "plan", runtime_id: "plan-1", runtime_status: "done" },
      }),
      rtFinal({ node_id: "rt-step2", text: "全部完成", position: 2 }),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["projection", "projection", "message"]);
    if (items[0]?.kind === "projection") expect(items[0].finalText).toBe("step_1 完成");
  });

  it("does not mark the flattened live final message as final (throttle path)", () => {
    const items = turnToTimeline(turn, [
      rtFinal({ node_id: "rt-final", text: "流式终答", position: 1, metadata: { runtime_type: "plan", runtime_id: "plan-1", runtime_status: "done", runtime_final: true } }),
    ], { live: true });
    const msg = items.find((i) => i.kind === "message");
    if (msg && msg.kind === "message") {
      expect((msg.message as { kind?: string }).kind).toBeUndefined();
    }
  });

  it("renders goal rounds as card-only without message bubble (参考 plan 模式)", () => {
    // goal 每轮都带 runtime_final：轮次输出收进 Goal 卡片，不单独平铺消息气泡。
    const items = turnToTimeline(turn, [
      rtFinal({
        node_id: "goal-r1", text: "第 1 步已完成：当前步数之和为 1。", position: 1,
        metadata: { runtime_type: "goal", runtime_id: "g1", runtime_status: "done", runtime_final: true, goal_round: 1 },
      }),
    ]);
    expect(items).toHaveLength(1);
    expect(items[0]?.kind).toBe("projection");
    if (items[0]?.kind === "projection") {
      expect(items[0].runtime_type).toBe("goal");
      expect(items[0].finalText).toBe("第 1 步已完成：当前步数之和为 1。");
      expect(items[0].goalRound).toBe(1);
    }
  });

  it("moves trailing reasoning before the assistant reply (思考卡不沉底)", () => {
    // 真实数据：message_start 先建 assistant 节点（position 2），reasoning 后建
    // （position 3）→ 渲染前把末尾 reasoning 移到回复之前。
    const items = turnToTimeline(turn, [
      node({ node_id: "u1", type: "user", text: "你有哪些技能？", position: 1 }),
      node({ node_id: "a1", type: "assistant", text: "共 21 个技能…", position: 2 }),
      node({ node_id: "r1", type: "reasoning", text: "用户问有哪些技能", position: 3 }),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["message", "reasoning", "message"]);
    // reasoning 在回复之前，且回复仍是最后一条（final 标记不受影响）
    const last = items[items.length - 1]!;
    expect(last.kind).toBe("message");
    if (last.kind === "message") {
      expect((last.message as { kind?: string }).kind).toBe("final");
    }
  });

  it("keeps reasoning in place when tools sit between it and the reply (agentic 时序)", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "a1", type: "assistant", text: "先答一部分", position: 1 }),
      node({ node_id: "t1", type: "tool", text: "", position: 2, metadata: { tool: "bash", call_id: "c1" } }),
      node({ node_id: "r1", type: "reasoning", text: "根据工具结果思考", position: 3 }),
      node({ node_id: "a2", type: "assistant", text: "最终答复", position: 4 }),
    ]);
    // reasoning 在 tool 与最终答复之间：属 agentic 时序，不移动
    expect(items.map((i) => i.kind)).toEqual(["message", "tool", "reasoning", "message"]);
  });
});

describe("turnToTimeline 方案 B：后端权威卡片分类标记", () => {
  it("metadata.intermediate=true 的 assistant 渲染为条卡（不再按顺序推断）", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "u1", type: "user", text: "继续", position: 1 }),
      // 即使它是该 turn 最后一条 assistant，后端 intermediate 标记优先
      node({
        node_id: "a1", type: "assistant", text: "过程性输出", position: 2,
        metadata: { intermediate: true },
      }),
    ]);
    const last = items[items.length - 1]!;
    expect(last.kind).toBe("message");
    if (last.kind === "message") {
      expect((last.message as { kind?: string }).kind).toBe("intermediate");
    }
  });

  it("metadata.final=true 的 assistant 渲染为正式答复气泡；混合数据下未标记节点不误标", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "a0", type: "assistant", text: "中间输出", position: 1 }),
      node({
        node_id: "a1", type: "assistant", text: "最终答复", position: 2,
        metadata: { final: true },
      }),
    ]);
    const kinds = items.map((i) => (i.kind === "message" ? (i.message as { kind?: string }).kind : i.kind));
    // 已标记节点按标记渲染；同 turn 的未标记节点保持 undefined（普通气泡），
    // 不做顺序误标（新数据全量由后端 metadata 驱动）
    expect(kinds).toEqual([undefined, "final"]);
  });

  it("无标记的旧数据仍走顺序推断兜底（最后一条 final、其余 intermediate）", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "a0", type: "assistant", text: "旧中间输出", position: 1 }),
      node({ node_id: "a1", type: "assistant", text: "旧最终答复", position: 2 }),
    ]);
    const kinds = items.map((i) => (i.kind === "message" ? (i.message as { kind?: string }).kind : i.kind));
    expect(kinds).toEqual(["intermediate", "final"]);
  });

  it("runtime 节点 metadata.final=true（append_runtime_node 统一标记）平铺为 final 气泡", () => {
    const items = turnToTimeline(turn, [
      node({ node_id: "t1", type: "tool", text: "", position: 1, metadata: { tool: "bash", call_id: "c1", runtime_type: "goal", runtime_id: "g1" } }),
      node({
        node_id: "a1", type: "assistant", text: "本轮终答", position: 2,
        metadata: { runtime_type: "goal", runtime_id: "g1", runtime_final: true, final: true },
      }),
    ]);
    // goal 终答收进投影卡（不平铺），投影卡承载明细
    expect(items.map((i) => i.kind)).toEqual(["projection"]);
    const proj = items[0]!;
    if (proj.kind === "projection") {
      expect(proj.finalText).toBe("本轮终答");
    }
  });
});
