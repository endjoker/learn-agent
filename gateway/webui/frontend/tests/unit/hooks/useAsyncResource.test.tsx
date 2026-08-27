import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useAsyncResource } from "@/hooks/useAsyncResource";

describe("useAsyncResource", () => {
  it("loads and reloads data", async () => {
    const loader = vi.fn().mockResolvedValueOnce(["a"]).mockResolvedValueOnce(["b"]);
    const { result } = renderHook(() => useAsyncResource(loader, []));

    await waitFor(() => expect(result.current.status).toBe("success"));
    expect(result.current.data).toEqual(["a"]);

    await act(() => result.current.reload());
    await waitFor(() => expect(result.current.data).toEqual(["b"]));
  });

  it("aborts obsolete work on unmount", () => {
    let signal: AbortSignal | undefined;
    const loader = vi.fn((nextSignal: AbortSignal) => {
      signal = nextSignal;
      return new Promise<never>(() => undefined);
    });
    const { unmount } = renderHook(() => useAsyncResource(loader, []));
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});
