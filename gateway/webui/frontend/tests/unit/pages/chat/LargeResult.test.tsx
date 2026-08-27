import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LargeResult } from "@/pages/chat/LargeResult";

const LIMIT = 64 * 1024;

describe("LargeResult", () => {
  it("renders at most 64 KiB until explicitly expanded", () => {
    const value = "x".repeat(LIMIT + 100);
    render(<LargeResult cacheKey="s:tool" value={value} />);
    expect(screen.getByText(/仅显示前 64 KiB/)).toBeInTheDocument();
    expect(screen.getByTestId("large-result").textContent?.length).toBe(LIMIT);
    fireEvent.click(screen.getByRole("button", { name: "展开全部" }));
    expect(screen.getByTestId("large-result").textContent?.length).toBe(value.length);
  });

  it("keeps small values fully visible", () => {
    render(<LargeResult cacheKey="s:small" value="small" />);
    expect(screen.getByTestId("large-result")).toHaveTextContent("small");
    expect(screen.queryByRole("button", { name: "展开全部" })).not.toBeInTheDocument();
  });
});
