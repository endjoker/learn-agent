import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useAsyncAction } from "@/hooks/useAsyncAction";

describe("useAsyncAction", () => {
  it("runs an action and exposes its result", async () => {
    const action = vi.fn((_signal: AbortSignal, value: string) => Promise.resolve(value.toUpperCase()));
    const { result } = renderHook(() => useAsyncAction(action));

    await act(async () => {
      await expect(result.current.run("hello")).resolves.toBe("HELLO");
    });
    expect(result.current.data).toBe("HELLO");
    expect(result.current.pending).toBe(false);
  });

  it("aborts the previous action when a new one starts", async () => {
    const signals: AbortSignal[] = [];
    const action = vi.fn((signal: AbortSignal, value: string) => {
      signals.push(signal);
      return value === "second" ? Promise.resolve(value) : new Promise<string>(() => undefined);
    });
    const { result } = renderHook(() => useAsyncAction(action));

    act(() => { void result.current.run("first"); });
    await act(async () => { await result.current.run("second"); });

    expect(signals[0]?.aborted).toBe(true);
    await waitFor(() => expect(result.current.data).toBe("second"));
  });
});
