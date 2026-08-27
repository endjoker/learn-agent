import { act, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "@/api/client";
import { useApprovalQueue } from "@/approvals/useApprovalQueue";

const approval = (id: string, sessionKey = "s") => ({ id, approval_id: id, session_key: sessionKey, tool: "write", params_preview: "{path: a}" });

const requested = (item: ReturnType<typeof approval>) => ({
  type: "approval.requested" as const, data: item, event_id: 1, at: 1,
});

const resolved = (id: string) => ({
  type: "approval.resolved" as const, data: { id }, event_id: 2, at: 2,
});

describe("useApprovalQueue", () => {
  it("recovers pending approvals and filters another session", async () => {
    const get = vi.fn(() => Promise.resolve({ approvals: [approval("a1", "s"), approval("a2", "other")] }));
    const client = { get, post: vi.fn() } as unknown as ApiClient;
    const { result } = renderHook(() => useApprovalQueue(client, { sessionKey: "s" }));
    await waitFor(() => expect(result.current.state.items).toHaveLength(1));
    expect(result.current.state.items[0]?.id).toBe("a1");
    // GET 恢复带当前 session_key 过滤（后端只返回本会话待办）
    expect(get).toHaveBeenCalledWith("/api/approvals", expect.objectContaining({ query: { session_key: "s" } }));
  });

  it("handles requested/resolved SSE without duplicates", async () => {
    const client = { get: vi.fn(() => Promise.resolve({ approvals: [] })), post: vi.fn() } as unknown as ApiClient;
    const { result } = renderHook(() => useApprovalQueue(client, { sessionKey: "s" }));
    await act(async () => { await Promise.resolve(); });
    act(() => { result.current.onSse(requested(approval("a1"))); result.current.onSse(requested(approval("a1"))); });
    expect(result.current.state.items).toHaveLength(1);
    act(() => result.current.onSse(resolved("a1")));
    expect(result.current.state.items).toEqual([]);
  });

  it("posts an answer and removes the approval", async () => {
    const post = vi.fn(() => Promise.resolve({ ok: true }));
    const client = { get: vi.fn(() => Promise.resolve({ approvals: [approval("a1")] })), post } as unknown as ApiClient;
    const { result } = renderHook(() => useApprovalQueue(client, { sessionKey: "s" }));
    await waitFor(() => expect(result.current.state.items).toHaveLength(1));
    await act(async () => { await result.current.answer(result.current.state.items[0]!, "y"); });
    // 答复请求体携带归属信息（session_key 取自 pending 记录）——后端 fail-closed
    expect(post).toHaveBeenCalledWith("/api/approvals/a1", { answer: "y", session_key: "s" });
    expect(result.current.state.items).toEqual([]);
  });

  it("removes the approval and toasts on 403 ownership failure", async () => {
    const post = vi.fn(() => Promise.reject(new ApiError("审批归属不匹配", { status: 403 })));
    const client = { get: vi.fn(() => Promise.resolve({ approvals: [approval("a1")] })), post } as unknown as ApiClient;
    const { result } = renderHook(() => useApprovalQueue(client, { sessionKey: "s" }));
    await waitFor(() => expect(result.current.state.items).toHaveLength(1));
    await act(async () => { await result.current.answer(result.current.state.items[0]!, "y"); });
    expect(result.current.state.items).toEqual([]);
    expect(await screen.findByText(/审批已失效/)).toBeInTheDocument();
  });

  it("keeps the approval when submission fails", async () => {
    const client = { get: vi.fn(() => Promise.resolve({ approvals: [approval("a1")] })), post: vi.fn(() => Promise.reject(new Error("404"))) } as unknown as ApiClient;
    const { result } = renderHook(() => useApprovalQueue(client, { sessionKey: "s" }));
    await waitFor(() => expect(result.current.state.items).toHaveLength(1));
    await act(async () => { await result.current.answer(result.current.state.items[0]!, "n"); });
    expect(result.current.state.items).toHaveLength(1);
    expect(result.current.state.error).toContain("404");
  });

  it("poll-recover keeps refreshing pending approvals (SSE fallback)", async () => {
    vi.useFakeTimers();
    try {
      const get = vi.fn(() => Promise.resolve({ approvals: [approval("a1", "s")] }));
      const client = { get, post: vi.fn() } as unknown as ApiClient;
      renderHook(() => useApprovalQueue(client, { sessionKey: "s" }));
      await act(async () => { await Promise.resolve(); });
      const initialCalls = get.mock.calls.length;
      expect(initialCalls).toBeGreaterThanOrEqual(1);
      // 周期性轮询（SSE 缺失时也能拉到 pending 审批，弹窗实时出现）
      await act(async () => { vi.advanceTimersByTime(3000); await Promise.resolve(); });
      expect(get.mock.calls.length).toBeGreaterThan(initialCalls);
    } finally {
      vi.useRealTimers();
    }
  });
});
