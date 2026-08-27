import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StatusPage } from "@/pages/status/StatusPage";
import { createMockClient } from "../../../helpers/mockClient";

const statusPayload = {
  executor: { workers: 4, pending: 2 },
  sessions: {
    active: 2,
    max: 50,
    busy: ["webui:default"],
    list: [
      { session_key: "webui:default", model: "deepseek-v4", message_count: 10, is_busy: true },
      { session_key: "webui:other", model: "gpt-4o", message_count: 3, is_busy: false },
    ],
  },
  channels: { webui: { status: "running" }, feishu: { status: "stopped" } },
  scheduler: { present: true, jobs: 5, running: ["cron-1"] },
  heartbeat: { present: true, paused: false, every: "30s", beats: 12 },
};

describe("StatusPage", () => {
  it("renders KPI cards, channels and session tables", async () => {
    const client = createMockClient({
      get: (path) => {
        if (path === "/api/status") return Promise.resolve(statusPayload);
        return Promise.reject(new Error(path));
      },
    });
    render(<StatusPage client={client} />);
    await screen.findByText("2/50");
    expect(screen.getByText("2/50")).toBeInTheDocument();
    expect(screen.getByText("4 槽 · 排队 2")).toBeInTheDocument();
    expect(screen.getByText("5 个 · 运行 1")).toBeInTheDocument();
    expect(screen.getByText("30s · 12 轮")).toBeInTheDocument();
    expect(screen.getByText("webui")).toBeInTheDocument();
    expect(screen.getByText("busy")).toBeInTheDocument();
    expect(screen.getByText("deepseek-v4")).toBeInTheDocument();
  });

  it("tolerates missing optional sections", async () => {
    const client = createMockClient({
      get: (path) => {
        if (path === "/api/status") return Promise.resolve({ executor: {}, sessions: {} });
        return Promise.reject(new Error(path));
      },
    });
    render(<StatusPage client={client} />);
    await screen.findByText("0/0");
    expect(screen.getByText("0/0")).toBeInTheDocument();
  });

  it("refreshes on channel.status SSE events", async () => {
    const client = createMockClient({
      get: vi.fn((path: string) => {
        if (path === "/api/status") return Promise.resolve(statusPayload);
        return Promise.reject(new Error(path));
      }),
    });
    render(<StatusPage client={client} />);
    await screen.findByText("2/50");
    expect(client.get).toHaveBeenCalledTimes(1);
    const eventSource = globalThis.EventSource as unknown as { instances: Array<{ onmessage: ((e: { data: string }) => void) | null }> };
    const source = eventSource.instances[eventSource.instances.length - 1];
    expect(source).toBeDefined();
    await act(async () => {
      source!.onmessage?.({ data: JSON.stringify({ type: "channel.status", data: {}, event_id: 1, at: Date.now() }) });
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(client.get).toHaveBeenCalledTimes(2));
  });
});
