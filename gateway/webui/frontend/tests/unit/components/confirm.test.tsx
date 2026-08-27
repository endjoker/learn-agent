import { act, fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { confirmDialog } from "@/components/confirm";

describe("confirmDialog", () => {
  it("queues concurrent calls and resolves each promise in order", async () => {
    let first!: Promise<boolean>;
    let second!: Promise<boolean>;
    act(() => {
      first = confirmDialog("第一个");
      second = confirmDialog("第二个");
    });
    // 并发调用只展示第一个，第二个排队
    expect(screen.getByText("第一个")).toBeInTheDocument();
    expect(screen.queryByText("第二个")).not.toBeInTheDocument();

    // 确认第一个 → 展示第二个
    fireEvent.click(screen.getByRole("button", { name: "确定" }));
    expect(await screen.findByText("第二个")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await act(async () => {
      expect(await first).toBe(true);
      expect(await second).toBe(false);
    });
  });
});
