import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Goal } from "@/api/types";
import { GoalBar } from "@/components/GoalBar";

const makeGoal = (overrides: Partial<Goal> = {}): Goal => ({
  goal_id: "g1",
  objective: "每天整理一次学习笔记并归档",
  status: "active",
  activation: "armed",
  rounds_started: 3,
  max_rounds: 20,
  ...overrides,
} as Goal);

// toast 是挂到 document.body 的持久元素，测试间清场防泄漏
beforeEach(() => {
  document.body.innerHTML = "";
});

describe("GoalBar visibility (#8)", () => {
  it("renders nothing without a goal", () => {
    const { container } = render(<GoalBar goal={null} onAction={vi.fn(async () => true)} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for terminal goals (completed/cancelled/archived)", () => {
    for (const status of ["completed", "cancelled", "archived"] as const) {
      const { container } = render(
        <GoalBar goal={makeGoal({ status })} onAction={vi.fn(async () => true)} />,
      );
      expect(container.querySelector(".goal-bar")).toBeNull();
    }
  });

  it("shows phase label + truncated objective + round meta for an active goal", () => {
    render(<GoalBar goal={makeGoal()} onAction={vi.fn(async () => true)} />);
    expect(screen.getByText("● 自主运行")).toBeTruthy();
    expect(screen.getByText("每天整理一次学习笔记并归档")).toBeTruthy();
    expect(screen.getByText(/第 3\/20 轮/)).toBeTruthy();
    expect(screen.queryByText(/恢复运行/)).toBeNull();
  });
});

describe("GoalBar host-folded value semantics (#9)", () => {
  it("pause click renders the target state immediately while request is in flight", async () => {
    let resolveAction: ((ok: boolean) => void) | undefined;
    const onAction = vi.fn(() => new Promise<boolean>((resolve) => { resolveAction = resolve; }));
    render(<GoalBar goal={makeGoal()} onAction={onAction} />);
    fireEvent.click(screen.getByText("暂停"));
    // 请求在途：立即按目标态（paused）渲染；对面动作按钮出现并禁用
    expect(screen.getByText("⏸ 已暂停")).toBeTruthy();
    expect(screen.getByRole("button", { name: "恢复运行" })).toBeDisabled();
    // 成功解析：折叠值解除，回到后端投影的真实状态（active → 暂停按钮回归）
    resolveAction!(true);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "暂停" })).toBeEnabled();
      expect(screen.queryByText("⏸ 已暂停")).toBeNull();
    });
  });

  it("rolls back to the backend projection and toasts on failure", async () => {
    const onAction = vi.fn(async () => false);
    render(<GoalBar goal={makeGoal()} onAction={onAction} />);
    fireEvent.click(screen.getByText("暂停"));
    await waitFor(() => expect(screen.getByText("● 自主运行")).toBeTruthy());
    // 失败态视觉（红边）由 .failed 类承载
    expect(document.querySelector(".goal-bar.failed")).not.toBeNull();
  });

  it("resume on a paused goal folds to active display during flight", async () => {
    let resolveAction!: (ok: boolean) => void;
    const onAction = vi.fn(() => new Promise<boolean>((r) => { resolveAction = r; }));
    render(<GoalBar goal={makeGoal({ status: "paused" })} onAction={onAction} />);
    expect(screen.getByText("⏸ 已暂停")).toBeTruthy();
    fireEvent.click(screen.getByText("恢复运行"));
    // 在途即折叠为目标态 active（后端投影 paused 被在途动作覆盖）
    expect(screen.getByText("● 自主运行")).toBeTruthy();
    // 成功解析：折叠值解除，回到后端投影（测试内投影仍为 paused）
    resolveAction(true);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "恢复运行" })).toBeEnabled();
      expect(screen.getByText("⏸ 已暂停")).toBeTruthy();
    });
  });
});

describe("GoalBar identity change resets transient state (#10)", () => {
  it("pending/error state does not leak onto a replaced goal", async () => {
    let resolveAction!: (ok: boolean) => void;
    const onAction = vi.fn(() => new Promise<boolean>((r) => { resolveAction = r; }));
    const { rerender } = render(<GoalBar goal={makeGoal()} onAction={onAction} />);
    fireEvent.click(screen.getByText("暂停"));
    // 目标在请求在途时被外部替换：新目标的 pending 必须重置
    rerender(<GoalBar goal={makeGoal({ goal_id: "g2" })} onAction={onAction} />);
    expect(screen.queryByText("暂停中…")).toBeNull();
    resolveAction(true);
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(1));
  });
});
