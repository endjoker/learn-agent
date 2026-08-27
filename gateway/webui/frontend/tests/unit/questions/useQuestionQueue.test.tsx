import { act, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "@/api/client";
import type { QuestionPrompt } from "@/api/types";
import { useQuestionQueue } from "@/questions/useQuestionQueue";

const question = (id: string, patch: Partial<QuestionPrompt> = {}): QuestionPrompt => ({
  question_id: id,
  session_key: "s",
  question: `问题 ${id}`,
  options: [{ id: "a", label: "A" }],
  ...patch,
});

const sseRequested = (question: QuestionPrompt) => ({
  type: "question.requested" as const,
  data: { session_key: question.session_key, question },
  event_id: 1,
  at: 1,
});

const sseResolved = (questionId: string) => ({
  type: "question.resolved" as const,
  data: { question_id: questionId },
  event_id: 2,
  at: 2,
});

const flush = () => act(async () => { await Promise.resolve(); });

describe("useQuestionQueue", () => {
  it("recovers pending questions from GET /api/questions on mount, scoped", async () => {
    const get = vi.fn(() => Promise.resolve({
      questions: [question("q1", { session_key: "s1" }), question("q2", { session_key: "other" })],
    }));
    const client = { get, post: vi.fn() } as unknown as ApiClient;
    const { result } = renderHook(() => useQuestionQueue(client, { sessionKey: "s1" }));
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    expect(get).toHaveBeenCalledWith("/api/questions", expect.anything());
    // GET 恢复带当前 session_key 过滤（后端只返回本会话待办）
    expect(get).toHaveBeenCalledWith("/api/questions", expect.objectContaining({ query: { session_key: "s1" } }));
    expect(result.current.state.items.map((item) => item.question_id)).toEqual(["q1"]);
  });

  it("tolerates GET failure and keeps the queue usable", async () => {
    const client = { get: vi.fn(() => Promise.reject(new Error("404"))), post: vi.fn() } as unknown as ApiClient;
    const { result } = renderHook(() => useQuestionQueue(client, { sessionKey: "s" }));
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    expect(result.current.state.items).toEqual([]);
    act(() => result.current.onSse(sseRequested(question("q1"))));
    expect(result.current.state.items.map((item) => item.question_id)).toEqual(["q1"]);
  });

  it("enqueues on question.requested and removes on question.resolved", async () => {
    const client = { get: vi.fn(() => Promise.resolve({ questions: [] })), post: vi.fn() } as unknown as ApiClient;
    const { result } = renderHook(() => useQuestionQueue(client, { sessionKey: "s" }));
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    act(() => {
      result.current.onSse(sseRequested(question("q1")));
      result.current.onSse(sseRequested(question("q2")));
    });
    expect(result.current.state.items.map((item) => item.question_id)).toEqual(["q1", "q2"]);
    act(() => result.current.onSse(sseResolved("q1")));
    expect(result.current.state.items.map((item) => item.question_id)).toEqual(["q2"]);
  });

  it("ignores SSE questions from other sessions", async () => {
    const client = { get: vi.fn(() => Promise.resolve({ questions: [] })), post: vi.fn() } as unknown as ApiClient;
    const { result } = renderHook(() => useQuestionQueue(client, { sessionKey: "s1" }));
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    act(() => result.current.onSse(sseRequested(question("q1", { session_key: "s2" }))));
    expect(result.current.state.items).toEqual([]);
  });

  it("posts the answer and removes the question on success", async () => {
    const post = vi.fn(() => Promise.resolve({ ok: true }));
    const client = { get: vi.fn(() => Promise.resolve({ questions: [question("q1")] })), post } as unknown as ApiClient;
    const { result } = renderHook(() => useQuestionQueue(client, { sessionKey: "s" }));
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    act(() => void result.current.answer(question("q1"), { selected_option_ids: ["a"] }));
    await waitFor(() => expect(result.current.state.items).toEqual([]));
    // 答复请求体携带归属信息（session_key 取自 pending 记录）——后端 fail-closed
    expect(post).toHaveBeenCalledWith("/api/questions/q1", { selected_option_ids: ["a"], session_key: "s" });
  });

  it("falls back to the queue scope session_key when the record lacks one", async () => {
    const post = vi.fn(() => Promise.resolve({ ok: true }));
    const client = { get: vi.fn(() => Promise.resolve({ questions: [] })), post } as unknown as ApiClient;
    const { result } = renderHook(() => useQuestionQueue(client, { sessionKey: "scope-s" }));
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    act(() => void result.current.answer(question("q1", { session_key: "" }), { selected_option_ids: ["a"] }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith("/api/questions/q1", { selected_option_ids: ["a"], session_key: "scope-s" });
  });

  it("removes the question and toasts on 403 ownership failure", async () => {
    const post = vi.fn(() => Promise.reject(new ApiError("问题归属不匹配", { status: 403 })));
    const client = { get: vi.fn(() => Promise.resolve({ questions: [question("q1")] })), post } as unknown as ApiClient;
    const { result } = renderHook(() => useQuestionQueue(client, { sessionKey: "s" }));
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    act(() => void result.current.answer(question("q1"), { selected_option_ids: ["a"] }));
    await waitFor(() => expect(result.current.state.items).toEqual([]));
    expect(await screen.findByText(/问题已失效/)).toBeInTheDocument();
  });

  it("keeps the question and surfaces the error when the POST fails", async () => {
    const post = vi.fn(() => Promise.reject(new Error("409 已答复")));
    const client = { get: vi.fn(() => Promise.resolve({ questions: [question("q1")] })), post } as unknown as ApiClient;
    const { result } = renderHook(() => useQuestionQueue(client, { sessionKey: "s" }));
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    act(() => void result.current.answer(question("q1"), { selected_option_ids: ["a"] }));
    await waitFor(() => expect(result.current.state.submitErrorId).toBe("q1"));
    expect(result.current.state.items.map((item) => item.question_id)).toEqual(["q1"]);
    expect(result.current.state.submitError).toContain("409");
  });

  it("resets the queue and re-recovers when the session scope changes", async () => {
    const get = vi.fn((path: string) => Promise.resolve({
      questions: path === "/api/questions" ? [] : [],
    }));
    const client = { get, post: vi.fn() } as unknown as ApiClient;
    const { result, rerender } = renderHook(
      ({ sessionKey }) => useQuestionQueue(client, { sessionKey }),
      { initialProps: { sessionKey: "s1" } },
    );
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    act(() => result.current.onSse(sseRequested(question("q1", { session_key: "s1" }))));
    expect(result.current.state.items.map((item) => item.question_id)).toEqual(["q1"]);

    rerender({ sessionKey: "s2" });
    await flush();
    await waitFor(() => expect(result.current.state.recoveryDone).toBe(true));
    expect(result.current.state.items).toEqual([]);
    expect(get).toHaveBeenCalledTimes(2);

    act(() => result.current.onSse(sseRequested(question("q2", { session_key: "s2" }))));
    expect(result.current.state.items.map((item) => item.question_id)).toEqual(["q2"]);
  });

  it("poll-recover keeps refreshing pending questions (SSE fallback)", async () => {
    vi.useFakeTimers();
    try {
      const get = vi.fn(() => Promise.resolve({ questions: [question("q1", { session_key: "s" })] }));
      const client = { get, post: vi.fn() } as unknown as ApiClient;
      renderHook(() => useQuestionQueue(client, { sessionKey: "s" }));
      await act(async () => { await Promise.resolve(); });
      const initialCalls = get.mock.calls.length;
      expect(initialCalls).toBeGreaterThanOrEqual(1);
      await act(async () => { vi.advanceTimersByTime(3000); await Promise.resolve(); });
      expect(get.mock.calls.length).toBeGreaterThan(initialCalls);
    } finally {
      vi.useRealTimers();
    }
  });
});
