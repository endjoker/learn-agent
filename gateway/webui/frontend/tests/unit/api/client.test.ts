import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, createApiClient } from "@/api/client";

const jsonResponse = (body: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });

const abortReason = (signal?: AbortSignal | null): Error => {
  const reason: unknown = signal?.reason;
  if (reason instanceof DOMException) return reason;
  if (reason instanceof Error) return reason;
  return new DOMException("Aborted", "AbortError");
};

describe("ApiClient", () => {
  afterEach(() => vi.restoreAllMocks());

  it("serializes JSON and parses typed responses", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ok: true }));
    const client = createApiClient({ fetcher });

    await expect(client.post<{ ok: boolean }>("/api/chat", { text: "hello" })).resolves.toEqual({ ok: true });
    const call = fetcher.mock.calls[0];
    expect(call?.[0]).toBe("/api/chat");
    expect(call?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ text: "hello" }),
      headers: { "Content-Type": "application/json" },
    });
  });

  it("surfaces backend error payloads", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ error: "bad request", code: "INVALID_PAGINATION" }, { status: 400 }),
    );
    const client = createApiClient({ fetcher });

    const promise = client.get("/api/sessions/x/history");
    await expect(promise).rejects.toMatchObject({
      name: "ApiError",
      message: "bad request",
      status: 400,
      code: "INVALID_PAGINATION",
    });
    await promise.catch((error: unknown) => expect(error).toBeInstanceOf(ApiError));
  });

  it("cancels a request when the caller aborts", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(abortReason(init.signal)), { once: true });
    }));
    const controller = new AbortController();
    const client = createApiClient({ fetcher });
    const pending = client.get("/api/status", { signal: controller.signal });

    controller.abort(new DOMException("cancelled", "AbortError"));
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("aborts requests after the configured timeout", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(abortReason(init.signal)), { once: true });
    }));
    const client = createApiClient({ fetcher, timeoutMs: 25 });
    const pending = client.get("/api/status");
    const assertion = expect(pending).rejects.toMatchObject({ name: "TimeoutError" });

    await vi.advanceTimersByTimeAsync(25);
    await assertion;
    vi.useRealTimers();
  });

  it("appends query parameters and supports silent error reporting", async () => {
    const onError = vi.fn();
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ value: 1 }))
      .mockResolvedValueOnce(jsonResponse({ error: "hidden" }, { status: 500 }));
    const client = createApiClient({ fetcher, onError });

    await client.get("/api/items?existing=yes", { query: { limit: 20, offset: 0, ignored: undefined } });
    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/items?existing=yes&limit=20&offset=0");

    await expect(client.get("/api/fail", { silent: true })).rejects.toBeInstanceOf(ApiError);
    expect(onError).not.toHaveBeenCalled();
  });

  it("returns text for non-JSON successful responses", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response("plain", { status: 200 }));
    const client = createApiClient({ fetcher });
    await expect(client.get<string>("/api/plain")).resolves.toBe("plain");
  });

  it("reports network (TypeError) failures to onError", async () => {
    const onError = vi.fn();
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("Failed to fetch"));
    const client = createApiClient({ fetcher, onError });
    await expect(client.get("/api/status")).rejects.toThrow(TypeError);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0]?.[0]).toMatchObject({ name: "ApiError", status: 0 });
  });

  it("reports timeouts to onError", async () => {
    vi.useFakeTimers();
    try {
      const onError = vi.fn();
      const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, init) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          // 直通 abort reason（DOMException 在 Node 下不继承 Error，勿用 instanceof 过滤）
          const reason: unknown = init.signal?.reason;
          // eslint-disable-next-line @typescript-eslint/prefer-promise-reject-errors -- mock 需忠实转发真实 abort reason
          reject(reason ?? new DOMException("Aborted", "AbortError"));
        }, { once: true });
      }));
      const client = createApiClient({ fetcher, timeoutMs: 25, onError });
      const pending = client.get("/api/status");
      // 先挂断言再推进计时器，避免拒绝发生在挂载处理器之前产生 unhandled rejection
      const assertion = expect(pending).rejects.toMatchObject({ name: "TimeoutError" });
      await vi.advanceTimersByTimeAsync(25);
      await assertion;
      expect(onError).toHaveBeenCalledTimes(1);
      expect(onError.mock.calls[0]?.[0]).toMatchObject({ name: "ApiError", status: 0 });
    } finally {
      vi.useRealTimers();
    }
  });

  it("handles multiple '?' in paths and preserves the existing query when nothing is added", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ok: true }));
    const client = createApiClient({ fetcher });

    await client.get("/api/items?a=1?b=2", { query: { limit: 20 } });
    // 第二个 "?" 属于已有查询值的一部分（a=1?b=2 → a="1?b=2"，编码为 %3F；"=" 一并编码）
    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/items?a=1%3Fb%3D2&limit=20");

    fetcher.mockClear();
    await client.get("/api/items?existing=yes", { query: { ignored: undefined } });
    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/items?existing=yes");

    fetcher.mockClear();
    await client.get("/api/items", { query: {} });
    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/items");
  });
});
