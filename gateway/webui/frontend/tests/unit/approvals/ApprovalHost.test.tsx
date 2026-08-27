import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApprovalHost } from "@/approvals/ApprovalHost";

describe("ApprovalHost", () => {
  it("renders the four legacy approval actions", () => {
    const onAnswer = vi.fn();
    const item = { id: "a1", session_key: "s", tool: "write", params_preview: "{path: a}" };
    render(<ApprovalHost approvals={[item]} onAnswer={onAnswer} />);
    expect(screen.getByLabelText("审批：write")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "允许一次" }));
    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    fireEvent.click(screen.getByRole("button", { name: "区内全放" }));
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    expect(onAnswer.mock.calls.map((call) => call[1])).toEqual(["y", "n", "a", "s"]);
  });
});
