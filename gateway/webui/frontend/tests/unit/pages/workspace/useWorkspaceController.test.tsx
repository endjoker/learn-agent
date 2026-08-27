import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { useWorkspaceController } from "@/pages/workspace/useWorkspaceController";

const workspace = (id: string) => ({ workspace_id: id, name: id, project_path: `/tmp/${id}` });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; };

describe("useWorkspaceController", () => {
  it("keeps an empty directory usable", async () => {
    const get = vi.fn((path: string) => {
      if (path === "/api/workspaces?limit=200") return Promise.resolve({ workspaces: [workspace("w1")], total: 1 });
      if (path.endsWith("/sessions")) return Promise.resolve({ sessions: [] });
      if (path.endsWith("/files")) return Promise.resolve({ workspace_id: "w1", path: "", entries: [], total: 0, truncated: false });
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const client = { get } as unknown as ApiClient;
    const { result } = renderHook(() => useWorkspaceController({ client }));
    await waitFor(() => expect(result.current.state.loading).toBe(false));
    await act(async () => { await result.current.selectWorkspace("w1"); });
    expect(result.current.state.files).toEqual([]);
    expect(result.current.state.fileError).toBeUndefined();
  });

  it("ignores stale workspace responses during rapid switching", async () => {
    const slow = deferred<{ sessions: never[] }>();
    const get = vi.fn((path: string) => {
      if (path === "/api/workspaces?limit=200") return Promise.resolve({ workspaces: [workspace("w1"), workspace("w2")], total: 2 });
      if (path.includes("/w1/sessions")) return slow.promise;
      if (path.includes("/w2/sessions")) return Promise.resolve({ sessions: [{ session_id: "s2", workspace_id: "w2", session_key: "workspace:w2:s2", name: "new" }] });
      if (path.endsWith("/files")) return Promise.resolve({ workspace_id: "x", path: "", entries: [], total: 0, truncated: false });
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const client = { get } as unknown as ApiClient;
    const { result } = renderHook(() => useWorkspaceController({ client }));
    await waitFor(() => expect(result.current.state.loading).toBe(false));
    act(() => { void result.current.selectWorkspace("w1"); });
    await act(async () => { await result.current.selectWorkspace("w2"); });
    slow.resolve({ sessions: [] });
    await act(async () => { await Promise.resolve(); });
    expect(result.current.state.selectedWorkspaceId).toBe("w2");
    expect(result.current.state.sessions[0]?.session_id).toBe("s2");
  });

  it("aborts an obsolete file request and keeps the newest content", async () => {
    const signals: AbortSignal[] = [];
    const slow = deferred<{ workspace_id: string; path: string; content: string; size: number; truncated: boolean }>();
    const get = vi.fn((path: string, options?: { signal?: AbortSignal; query?: Record<string, unknown> }) => {
      if (path === "/api/workspaces?limit=200") return Promise.resolve({ workspaces: [workspace("w1")], total: 1 });
      if (path.includes("/file") && options?.query && (options.query as { path?: string }).path === "old.txt") { if (options.signal) signals.push(options.signal); return slow.promise; }
      if (path.includes("/file")) return Promise.resolve({ workspace_id: "w1", path: "new.txt", content: "new", size: 3, truncated: false });
      return Promise.resolve({ sessions: [] });
    });
    const client = { get } as unknown as ApiClient;
    const { result } = renderHook(() => useWorkspaceController({ client }));
    await waitFor(() => expect(result.current.state.loading).toBe(false));
    await act(async () => { await result.current.selectWorkspace("w1"); });
    act(() => { void result.current.openFile("w1", "old.txt"); });
    await act(async () => { await result.current.openFile("w1", "new.txt"); });
    expect(signals[0]?.aborted).toBe(true);
    slow.resolve({ workspace_id: "w1", path: "old.txt", content: "old", size: 3, truncated: false });
    await act(async () => { await Promise.resolve(); });
    expect(result.current.state.openFile?.content).toBe("new");
  });
});
