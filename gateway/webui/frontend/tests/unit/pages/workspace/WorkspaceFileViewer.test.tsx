import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WorkspaceFileViewer } from "@/pages/workspace/WorkspaceFileViewer";
import { ByteLruCache } from "@/pages/chat/byteLru";

describe("WorkspaceFileViewer", () => {
  it("shows only a 64 KiB preview for a large file until expanded", () => {
    const content = "x".repeat(1024 * 1024);
    render(<WorkspaceFileViewer file={{ workspace_id: "w", path: "huge.txt", content, size: content.length, truncated: false }} />);
    expect(screen.getByTestId("large-result").textContent?.length).toBe(64 * 1024);
    expect(screen.getByText(/仅显示前 64 KiB/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("展开全部"));
    expect(screen.getByTestId("large-result").textContent?.length).toBe(content.length);
  });

  it("keeps expanded file values within the shared byte LRU budget", () => {
    const cache = new ByteLruCache<string>(10);
    cache.set("workspace:w:a", "123456");
    cache.set("workspace:w:b", "abcdef");
    expect(cache.totalBytes).toBeLessThanOrEqual(10);
    expect(cache.get("workspace:w:a")).toBeUndefined();
    expect(cache.get("workspace:w:b")).toBe("abcdef");
  });
});
