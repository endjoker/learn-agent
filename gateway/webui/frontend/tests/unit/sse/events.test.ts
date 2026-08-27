import { describe, expect, it, vi } from "vitest";

import { buildSseUrl, eventMatchesScope, getSseLastEventId, parseSseEvent, recordSseEventId } from "@/sse/events";

describe("SSE event infrastructure", () => {
  it("parses the backend envelope", () => {
    expect(parseSseEvent(JSON.stringify({
      type: "chat.text_delta",
      data: { session_key: "webui:default", delta: "hi" },
      event_id: 7,
      at: 123.5,
    }))).toEqual({
      type: "chat.text_delta",
      data: { session_key: "webui:default", delta: "hi" },
      event_id: 7,
      at: 123.5,
    });
  });

  it("rejects malformed envelopes", () => {
    expect(parseSseEvent("not-json")).toBeNull();
    expect(parseSseEvent(JSON.stringify({ data: {} }))).toBeNull();
  });

  it("tolerates missing event_id/at with defaults and a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      const parsed = parseSseEvent(JSON.stringify({ type: "chat.started", data: { session_key: "s" } }));
      expect(parsed).not.toBeNull();
      expect(parsed?.event_id).toBe(0);
      expect(typeof parsed?.at).toBe("number");
      expect(warn).toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });

  it("builds the exact backend scope query", () => {
    expect(buildSseUrl({
      lastEventId: 9,
      sessionKey: "workspace:a b",
      workspaceId: "ws-1",
      workspaceSessionId: "s-1",
    })).toBe("/api/events?last_event_id=9&session_key=workspace%3Aa+b&workspace_id=ws-1&workspace_session_id=s-1");
  });

  it("strictly isolates scoped events while keeping the global feed compatible", () => {
    const scope = { sessionKey: "s1", workspaceId: "w1", workspaceSessionId: "ws1" };
    expect(eventMatchesScope({ session_key: "s1", workspace_id: "w1", workspace_session_id: "ws1" }, scope)).toBe(true);
    expect(eventMatchesScope({}, scope)).toBe(false);
    expect(eventMatchesScope({ session_key: "s2", workspace_id: "w1", workspace_session_id: "ws1" }, scope)).toBe(false);
    expect(eventMatchesScope({ session_key: "s1" }, scope)).toBe(false);
    expect(eventMatchesScope({}, {})).toBe(true);
  });

  it("throttles sessionStorage writes to at least 1s and flushes the latest id (L5)", () => {
    vi.useFakeTimers();
    try {
      // jsdom 的 window.sessionStorage 每次访问返回不同实例，须挂在原型上拦截
      const setItem = vi.spyOn(Object.getPrototypeOf(window.sessionStorage), "setItem");
      // 首个水位：距上次写（0）≥1s → 立即落盘
      recordSseEventId(1);
      expect(setItem).toHaveBeenCalledWith("jkagent.sse.last_event_id", "1");
      setItem.mockClear();
      // 1s 窗口内高频事件：只合并为一次定时补写（写最新水位 3，不写中间值 2）
      recordSseEventId(2);
      recordSseEventId(3);
      expect(setItem).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1100);
      expect(setItem).toHaveBeenCalledWith("jkagent.sse.last_event_id", "3");
      setItem.mockClear();
      // 窗口内再写 4 → 合并；跨过 1s 节流窗口后补写最新水位 5
      recordSseEventId(4);
      recordSseEventId(5);
      expect(setItem).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1100);
      expect(setItem).toHaveBeenCalledWith("jkagent.sse.last_event_id", "5");
      // 读侧一致
      expect(getSseLastEventId()).toBe(5);
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });
});
