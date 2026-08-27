import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WorkspaceFileList } from "@/pages/workspace/WorkspaceFileList";

const entries = Array.from({ length: 5000 }, (_, index) => ({
  name: `file-${index}.txt`, path: `file-${index}.txt`, kind: "file" as const, size: index,
}));

describe("WorkspaceFileList", () => {
  it("renders an empty directory state", () => {
    render(<WorkspaceFileList entries={[]} onOpen={() => undefined} onEnter={() => undefined} />);
    expect(screen.getByText("目录为空")).toBeInTheDocument();
  });

  it("virtualizes a large directory instead of mounting every entry", () => {
    const { container } = render(<WorkspaceFileList entries={entries} onOpen={() => undefined} onEnter={() => undefined} />);
    const rows = container.querySelectorAll("[data-file-index]");
    expect(rows.length).toBeLessThan(100);
    expect(rows.length).toBeGreaterThan(0);
  });

  it("opens a file and enters a directory", () => {
    const calls: string[] = [];
    render(<WorkspaceFileList entries={[
      { name: "src", path: "src", kind: "directory", size: 0 },
      { name: "README.md", path: "README.md", kind: "file", size: 12 },
    ]} onOpen={(path) => calls.push(`file:${path}`)} onEnter={(path) => calls.push(`dir:${path}`)} />);
    fireEvent.click(screen.getByText("src"));
    fireEvent.click(screen.getByText("README.md"));
    expect(calls).toEqual(["dir:src", "file:README.md"]);
  });
});
