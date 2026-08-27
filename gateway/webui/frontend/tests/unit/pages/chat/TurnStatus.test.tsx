import { act, render, screen } from "@testing-library/react";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TurnStatus } from "@/pages/chat/TurnStatus";

// Turn 级统一加载指示（优化方案 #5）：骑在整个运行中 turn 上，不随过程节点
// 闪烁；运行 ≥15s 才出现计时器；结束后展示总用时 5s；会话切换完全复位。

describe("TurnStatus (#5)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** 假计时器下推进时钟：必须包 act()，否则 setState 不刷新。 */
  const advanceSeconds = (s: number) => {
    act(() => {
      vi.advanceTimersByTime(s * 1000);
    });
  };

  it("renders nothing when idle without a recent completion", () => {
    const { container } = render(<TurnStatus busy={false} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows running strip immediately on busy, without timer before 15s", () => {
    render(<TurnStatus busy turnId="t1" turnStartedAt={new Date().toISOString()} />);
    expect(screen.getByText(/任务运行中/)).toBeTruthy();
    expect(screen.queryByText(/分|秒/)).toBeNull(); // 计时器未出现
  });

  it("reveals the timer only after 15 seconds of running", () => {
    render(<TurnStatus busy turnId="t1" turnStartedAt={new Date().toISOString()} />);
    advanceSeconds(14);
    expect(screen.queryByText(/· \d/)).toBeNull();
    advanceSeconds(2);
    expect(screen.getByText(/16秒/)).toBeTruthy();
  });

  it("uses local first-seen time when started_at is missing", () => {
    render(<TurnStatus busy turnId="t1" turnStartedAt={null} />);
    advanceSeconds(16);
    expect(screen.getByText(/16秒/)).toBeTruthy();
  });

  it("re-anchors when the running turn is replaced by another", () => {
    const { rerender } = render(<TurnStatus busy turnId="t1" turnStartedAt={new Date().toISOString()} />);
    advanceSeconds(20);
    // 换了新 turn（此刻接棒，started_at=现在）→ 计时从头开始（旧 20秒 不继承）
    rerender(<TurnStatus busy turnId="t2" turnStartedAt={new Date().toISOString()} />);
    expect(screen.queryByText(/· \d+分|\d+秒/)).toBeNull();
    // 新 turn 运行满 15s 后才再次出现计时
    advanceSeconds(15);
    expect(screen.getByText(/15秒/)).toBeTruthy();
  });

  it("flashes the total duration for 5s after completion, then hides", () => {
    const { rerender } = render(<TurnStatus busy turnId="t1" turnStartedAt={new Date().toISOString()} />);
    advanceSeconds(8);
    rerender(<TurnStatus busy={false} turnId="t1" />);
    expect(screen.getByText(/⏱ 用时 8秒/)).toBeTruthy();
    advanceSeconds(5);
    expect(screen.queryByText(/用时/)).toBeNull();
  });

  it("resets completely on session switch (resetKey)", () => {
    const { rerender } = render(<TurnStatus busy turnId="t1" resetKey="conv-1" />);
    advanceSeconds(20);
    rerender(<TurnStatus busy={false} turnId="t1" resetKey="conv-1" />);
    expect(screen.getByText(/用时/)).toBeTruthy();
    // 切换会话：上一会话的终态闪现不得串台
    rerender(<TurnStatus busy={false} turnId={null} resetKey="conv-2" />);
    expect(screen.queryByText(/用时/)).toBeNull();
    expect(screen.queryByText(/任务运行中/)).toBeNull();
  });
});
