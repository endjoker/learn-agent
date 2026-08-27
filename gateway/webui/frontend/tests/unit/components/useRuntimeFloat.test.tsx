import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { useRuntimeFloat } from "@/components/useRuntimeFloat";

describe("useRuntimeFloat", () => {
  it("ignores runtime events with a missing or wrong session scope", async () => {
    const client = {
      get: vi.fn().mockResolvedValue({ plans: [], goals: [] }),
      post: vi.fn(),
    } as unknown as ApiClient;
    const { result } = renderHook(() => useRuntimeFloat(client, { sessionKey: "s1" }));
    await waitFor(() => expect(client.get).toHaveBeenCalledTimes(2));

    act(() => {
      result.current.onSse({
        type: "plan.changed",
        data: { session_key: "s2", plan: { plan_id: "p2", status: "active" } },
        event_id: 1,
        at: 1,
      });
      result.current.onSse({
        type: "goal.changed",
        data: { goal: { goal_id: "g-unscoped", status: "active" } },
        event_id: 2,
        at: 2,
      });
    });

    expect(result.current.plan).toBeNull();
    expect(result.current.goal).toBeNull();
  });

  it("does not share state when the active session changes", async () => {
    const client = {
      get: vi.fn().mockResolvedValue({ plans: [], goals: [] }),
      post: vi.fn(),
    } as unknown as ApiClient;
    const { result, rerender } = renderHook(
      ({ sessionKey }) => useRuntimeFloat(client, { sessionKey }),
      { initialProps: { sessionKey: "s1" } },
    );
    await waitFor(() => expect(client.get).toHaveBeenCalledTimes(2));

    act(() => {
      result.current.onSse({
        type: "plan.changed",
        data: { session_key: "s1", plan: { plan_id: "p1", status: "active" } },
        event_id: 1,
        at: 1,
      });
    });
    expect(result.current.plan?.plan_id).toBe("p1");

    rerender({ sessionKey: "s2" });
    await waitFor(() => expect(client.get).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(result.current.plan).toBeNull());

    act(() => {
      result.current.onSse({
        type: "goal.changed",
        data: { session_key: "s1", goal: { goal_id: "g1", status: "active" } },
        event_id: 2,
        at: 2,
      });
    });
    expect(result.current.goal).toBeNull();
  });

  it("accepts runtime events from the active session", async () => {
    const client = {
      get: vi.fn().mockResolvedValue({ plans: [], goals: [] }),
      post: vi.fn(),
    } as unknown as ApiClient;
    const { result } = renderHook(() => useRuntimeFloat(client, { sessionKey: "s1" }));
    await waitFor(() => expect(client.get).toHaveBeenCalledTimes(2));

    act(() => {
      result.current.onSse({
        type: "plan.changed",
        data: { session_key: "s1", plan: { plan_id: "p1", status: "active" } },
        event_id: 1,
        at: 1,
      });
    });

    expect(result.current.plan?.plan_id).toBe("p1");

    act(() => {
      result.current.onSse({
        type: "chat.started",
        data: { session_key: "s1" },
        event_id: 2,
        at: 2,
      });
    });
    expect(result.current.plan?.plan_id).toBe("p1");
  });

  it("does not restore TERMINAL plan/goal after refresh (刷新后终态不恢复)", async () => {
    const client = {
      get: vi.fn().mockResolvedValue({
        plans: [{ plan_id: "p-done", status: "completed" }],
        goals: [{ goal_id: "g-done", status: "completed" }],
      }),
      post: vi.fn(),
    } as unknown as ApiClient;
    const { result } = renderHook(() => useRuntimeFloat(client, { sessionKey: "s1" }));
    await waitFor(() => expect(client.get).toHaveBeenCalledTimes(2));
    expect(result.current.plan).toBeNull();
    expect(result.current.goal).toBeNull();

    act(() => {
      result.current.onSse({
        type: "plan.changed",
        data: { session_key: "s1", plan: { plan_id: "p-done", status: "completed" } },
        event_id: 3,
        at: 3,
      });
      result.current.onSse({
        type: "goal.changed",
        data: { session_key: "s1", goal: { goal_id: "g-done", status: "completed" } },
        event_id: 4,
        at: 4,
      });
    });
    expect(result.current.plan?.plan_id).toBe("p-done");
    expect(result.current.goal?.goal_id).toBe("g-done");

    act(() => {
      result.current.dismissStale();
    });
    expect(result.current.plan).toBeNull();
    expect(result.current.goal).toBeNull();
  });

  it("dismissStale keeps a RUNNING plan (运行中的状态框保留)", async () => {
    const client = {
      get: vi.fn().mockResolvedValue({ plans: [{ plan_id: "p-run", status: "active" }], goals: [] }),
      post: vi.fn(),
    } as unknown as ApiClient;
    const { result } = renderHook(() => useRuntimeFloat(client, { sessionKey: "s1" }));
    await waitFor(() => expect(result.current.plan?.plan_id).toBe("p-run"));
    act(() => {
      result.current.dismissStale();
    });
    expect(result.current.plan?.plan_id).toBe("p-run");
  });
});
