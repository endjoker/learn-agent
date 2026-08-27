import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { gatewayStore } from "@/gateway/store";
import { useConversation } from "@/gateway/useConversation";
import type { ConversationSession } from "@/gateway/types";

const conversation: ConversationSession = {
  conversation_id: "conv-w",
  session_key: "workspace:w1:s1",
  origin: "webui",
  subtype: "workspace",
  workspace_id: "w1",
  execution_scope: "workspace:w1",
  route_metadata: {},
  session_version: 0,
  created_at: "",
  updated_at: ""};

let snapshotCalls = 0;
// P2 重试测试：前 N 次快照返回失败（0 = 全部成功，保持既有用例行为）
let snapshotFailureRounds = 0;

vi.mock("@/gateway/api", () => ({
  conversationApi: {
    create: vi.fn(async () => ({ ok: true, data: { conversation } })),
    snapshot: vi.fn(async () => {
      snapshotCalls += 1;
      // 前置失败轮次：验证初始快照失败后自动指数退避重试
      if (snapshotCalls <= snapshotFailureRounds) {
        return { ok: false, status: 500, error: { code: "snapshot_failed", message: "snapshot failed" } };
      }
      // 第一次成功快照：会话刚建，无 live turn；之后：带 t-w 的 live turn（修复后）
      if (snapshotCalls === snapshotFailureRounds + 1) {
        return {
          ok: true,
          data: {
            conversation, session_version: 0, queue: [], live_turn: null, turn_version: 0, nodes: [], queued_nodes: [],
            projections: [], pending_approvals: [], server_time: ""}};
      }
      return {
        ok: true,
        data: {
          conversation, session_version: 1, queue: [], live_turn: {
            turn_id: "t-w", conversation_id: "conv-w", status: "queued",
            turn_version: 1, started_at: "", finished_at: null,
            final_assistant_node_id: null, error_code: null,
            parent_conversation_id: null, parent_turn_id: null},
          turn_version: 1,
          nodes: [
            { node_id: "n-user", conversation_id: "conv-w", turn_id: "t-w", type: "user", position: 1, status: "dispatched", text: "q", metadata: {}, source_channel: null, source_message_id: null, sender_id: null, sender_name: null, created_at: "", updated_at: "" },
            { node_id: "n-tool", conversation_id: "conv-w", turn_id: "t-w", type: "tool", position: 2, status: "running", text: "bash", metadata: { call_id: "c1", tool: "bash" }, source_channel: null, source_message_id: null, sender_id: null, sender_name: null, created_at: "", updated_at: "" },
          ],
          queued_nodes: [], projections: [], pending_approvals: [], server_time: ""}};
    }),
    history: vi.fn(async () => ({ ok: true, data: { items: [], next_cursor: null } }))}}));

const sseEvent = (type: string, data: Record<string, unknown>) => ({
  type,
  data: {
    conversation_id: "conv-w",
    session_key: "workspace:w1:s1",
    origin: "webui",
    subtype: "workspace",
    workspace_id: "w1",
    turn_id: "t-w",
    scope: "turn",
    version: 2,
    ...data},
  event_id: 2,
  at: Date.now()});

describe("useConversation self-healing", () => {
  it("fetches a snapshot when a turn event arrives but the store lacks the turn", async () => {
    gatewayStore.reset();
    snapshotCalls = 0;
    snapshotFailureRounds = 0;
    const { result } = renderHook(() => useConversation({ sessionKey: "workspace:w1:s1" }));
    // 首次快照（无 live turn）完成
    await waitFor(() => expect(snapshotCalls).toBeGreaterThanOrEqual(1));

    // 事件到达但 store 尚无 t-w（turn 事件先于快照的竞态）
    act(() => {
      result.current.handleEvent(sseEvent("node.tool", { data: { node_id: "n-tool", call_id: "c1", status: "running" } }) as never);
    });

    // 自愈：自动拉取快照 → store 中出现 t-w 与其节点
    await waitFor(() => expect(snapshotCalls).toBeGreaterThanOrEqual(2));
    const state = gatewayStore.getState();
    expect(state.turnsById["t-w"]).toBeTruthy();
    expect(state.nodesById["n-tool"]?.type).toBe("tool");
  });

  it("retries the initial snapshot with backoff after failures (no silent blank UI)", async () => {
    gatewayStore.reset();
    snapshotCalls = 0;
    snapshotFailureRounds = 2; // 前两次快照失败：500ms → 1000ms 退避后第三次成功
    try {
      const { result } = renderHook(() => useConversation({ sessionKey: "workspace:w1:s1" }));
      // 旧实现失败后不再重试（永远停在 2 次调用、UI 永久空白）；
      // 修复后无需任何外部触发即自动重试至成功。
      await waitFor(() => expect(snapshotCalls).toBe(snapshotFailureRounds + 1), { timeout: 5000 });
      // 成功后 loadedRef 置位：后续 SSE 事件直接应用，不再进缓冲
      act(() => {
        result.current.handleEvent(sseEvent("node.tool", { data: { node_id: "n-tool", call_id: "c1", status: "running" } }) as never);
      });
      expect(gatewayStore.getState().nodesById["n-tool"]?.type).toBe("tool");
    } finally {
      snapshotFailureRounds = 0;
    }
  });
});

// ============================================================
// 多会话切换竞态回归（用户实测：两个会话同时运行时切换会话页面，
// 内容输出全部错乱）。根因：useConversation 的单布尔 loadedRef +
// applySnapshot 无会话归属校验——旧会话在途快照返回时置位 loaded 并
// 提前消费新会话的事件缓冲（未经新会话快照建立基线），恢复期遮罩也
// 被提前解除；store 水位随后被旧快照回退，旧版本事件被当作新事件重复
// 应用，页面内容呈现为"全部乱了"。
// ============================================================

interface DeferredSnapshot {
  resolve: (value: unknown) => void;
  promise: Promise<unknown>;
}

const deferred = <T,>(): { promise: Promise<T>; resolve: (v: T) => void } => {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
};

const convA: ConversationSession = { ...conversation, conversation_id: "conv-a", session_key: "webui:a" };
const convB: ConversationSession = { ...conversation, conversation_id: "conv-b", session_key: "webui:b" };

const snapshotFor = (conv: ConversationSession, version: number) => ({
  ok: true as const,
  data: {
    conversation: conv, session_version: version, queue: [], live_turn: null,
    turn_version: 0, nodes: [], queued_nodes: [], projections: [],
    pending_approvals: [], server_time: ""},
});

const eventFor = (convId: string, sessionKey: string, turnId: string, version: number) => ({
  type: "node.tool",
  data: {
    conversation_id: convId, session_key: sessionKey, turn_id: turnId,
    scope: "turn", version,
    data: { node_id: `n-${turnId}`, call_id: "c1", tool: "bash", status: "running" }},
  event_id: 99,
  at: Date.now()});

describe("useConversation multi-session switch race", () => {
  it("stale in-flight snapshot must not unlock the new session buffer", async () => {
    gatewayStore.reset();
    // 可控时序：两会话快照都挂起，按需 resolve
    const pending: Record<string, DeferredSnapshot[]> = { "conv-a": [], "conv-b": [] };
    const { conversationApi } = vi.mocked(await import("@/gateway/api"));
    (conversationApi.create as ReturnType<typeof vi.fn>).mockImplementation(
      async (sessionKey: string) => ({ ok: true, data: { conversation: sessionKey === "webui:a" ? convA : convB } }));
    (conversationApi.snapshot as ReturnType<typeof vi.fn>).mockImplementation(
      async (cid: string) => {
        const d = deferred<unknown>();
        pending[cid]!.push(d);
        return d.promise;
      });

    const { result, rerender } = renderHook(
      ({ sessionKey }: { sessionKey: string }) => useConversation({ sessionKey }),
      { initialProps: { sessionKey: "webui:a" } });

    // A 的快照在途（尚未 resolve）→ 切换到会话 B
    await act(async () => { await Promise.resolve(); });
    rerender({ sessionKey: "webui:b" });
    await act(async () => { await Promise.resolve(); });

    // B 的实时事件到达（快照 B 尚未返回）→ 必须进缓冲
    const evB = eventFor("conv-b", "webui:b", "t-b", 2);
    act(() => { result.current.handleEvent(evB as never); });
    // A 的迟到快照此时 resolve：修复前会置位 loaded 并提前消费 B 的缓冲
    await act(async () => {
      pending["conv-a"]![0]!.resolve(snapshotFor(convA, 1));
      await pending["conv-a"]![0]!.promise;
    });

    // 关键断言：A 快照 resolve 后、B 快照之前，B 事件不得直接灌入 store
    const evB2 = eventFor("conv-b", "webui:b", "t-b2", 2);
    act(() => { result.current.handleEvent(evB2 as never); });
    expect(gatewayStore.getState().nodesById["n-t-b2"]).toBeUndefined();

    // B 快照返回 → 缓冲重放，事件应用
    await act(async () => {
      pending["conv-b"]![0]!.resolve(snapshotFor(convB, 1));
      await pending["conv-b"]![0]!.promise;
    });
    // 重放后缓冲中的 evB 已应用；后续事件正常直灌
    const evB3 = eventFor("conv-b", "webui:b", "t-b3", 2);
    act(() => { result.current.handleEvent(evB3 as never); });
    expect(gatewayStore.getState().nodesById["n-t-b3"]).toBeTruthy();
  });

  it("stale snapshot must not clear recovering or regress the watermark", async () => {
    gatewayStore.reset();
    const pending: Record<string, DeferredSnapshot[]> = { "conv-a": [], "conv-b": [] };
    const { conversationApi } = vi.mocked(await import("@/gateway/api"));
    (conversationApi.create as ReturnType<typeof vi.fn>).mockImplementation(
      async (sessionKey: string) => ({ ok: true, data: { conversation: sessionKey === "webui:a" ? convA : convB } }));
    (conversationApi.snapshot as ReturnType<typeof vi.fn>).mockImplementation(
      async (cid: string) => {
        const d = deferred<unknown>();
        pending[cid]!.push(d);
        return d.promise;
      });

    const { result, rerender } = renderHook(
      ({ sessionKey }: { sessionKey: string }) => useConversation({ sessionKey }),
      { initialProps: { sessionKey: "webui:a" } });
    await act(async () => { await Promise.resolve(); });
    rerender({ sessionKey: "webui:b" });
    await act(async () => { await Promise.resolve(); });

    // B 快照先返回（版本 5，正常加载完成）
    await act(async () => {
      pending["conv-b"]![0]!.resolve(snapshotFor(convB, 5));
      await pending["conv-b"]![0]!.promise;
    });
    expect(result.current.recovering).toBe(false);
    expect(gatewayStore.getState().sessionVersions["conv-b"]).toBe(5);

    // A 的迟到快照随后返回：不得影响 B 的恢复状态与水位
    await act(async () => {
      pending["conv-a"]![0]!.resolve(snapshotFor(convA, 9));
      await pending["conv-a"]![0]!.promise;
    });
    expect(gatewayStore.getState().sessionVersions["conv-b"]).toBe(5);

    // 水位只进不退：本地已连续应用到 6 的会话收到更旧的快照（版本 5）→ 保留 6
    act(() => {
      gatewayStore.applyEvent({
        type: "queue.updated",
        data: { conversation_id: "conv-b", session_key: "webui:b", scope: "session", version: 6, data: { queue: [] } },
        event_id: 100, at: Date.now()} as never);
    });
    expect(gatewayStore.getState().sessionVersions["conv-b"]).toBe(6);
    act(() => {
      gatewayStore.applySnapshot(snapshotFor(convB, 5).data as never);
    });
    expect(gatewayStore.getState().sessionVersions["conv-b"]).toBe(6);
  });
});
