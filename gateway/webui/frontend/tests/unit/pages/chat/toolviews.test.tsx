import { render, screen } from "@testing-library/react";

import { describe, expect, it } from "vitest";

import { buildToolRowModel, parseToolInput, readLineBadge } from "@/pages/chat/toolviews/model";
import { toolRowViews, ToolRowView } from "@/pages/chat/toolviews";

describe("toolviews model layer (pure functions)", () => {
  it("parses JSON object input and rejects non-object payloads", () => {
    expect(parseToolInput("{\"command\":\"ls -la\"}")).toEqual({ command: "ls -la" });
    expect(parseToolInput("plain text")).toBeNull();
    expect(parseToolInput("[1,2]")).toBeNull();
    expect(parseToolInput("")).toBeNull();
  });

  it("bash: renders the command as mono primary", () => {
    const model = buildToolRowModel("bash", JSON.stringify({ command: "pnpm test" }));
    expect(model.kind).toBe("bash");
    expect(model.primary).toBe("pnpm test");
    expect(model.mono).toBe(true);
  });

  it("read: path tail primary with line-range badge", () => {
    const model = buildToolRowModel("read__a1b2c3", JSON.stringify({ file_path: "/home/test/project/learn-agent-main_extracted/learn-agent-main/src/app/main.tsx", offset: 12, limit: 40 }));
    expect(model.kind).toBe("read");
    // pathTail 默认保留最后 2 段
    expect(model.primary).toBe("…/app/main.tsx");
    expect(model.badge).toBe("L12-51");
  });

  it("read: badge omitted when offset missing/invalid", () => {
    expect(buildToolRowModel("read", JSON.stringify({ file_path: "/a/b.ts" })).badge).toBeUndefined();
    expect(readLineBadge({ offset: -1 })).toBeUndefined();
  });

  it("edit/write: path tail primary with edit badge", () => {
    const model = buildToolRowModel("edit", JSON.stringify({ file_path: "/w/x/y/component.tsx" }));
    expect(model.kind).toBe("edit");
    expect(model.primary).toBe("…/y/component.tsx");
  });

  it("glob/grep/search: pattern primary with @path badge", () => {
    const model = buildToolRowModel("grep", JSON.stringify({ pattern: "useGatewaySelector", path: "/repo/src" }));
    expect(model.kind).toBe("search");
    expect(model.primary).toBe("useGatewaySelector");
    // 徽章路径只保留最后 1 段
    expect(model.badge).toBe("@ …/src");
  });

  it("unknown tools fall back to generic with empty primary", () => {
    const model = buildToolRowModel("web_fetch", JSON.stringify({ url: "https://example.com" }));
    expect(model.kind).toBe("generic");
    expect(model.primary).toBe("");
  });
});

describe("toolviews registry dispatch", () => {
  it("registers specialized views for bash/read/edit/write/glob/grep/search", () => {
    const kinds = ["bash", "read", "edit", "write", "glob", "grep", "search"] as const;
    for (const kind of kinds) {
      expect(toolRowViews[kind]).toBeDefined();
    }
    expect("generic" in toolRowViews && toolRowViews.generic != null).toBe(false);
  });

  it("renders bash command row instead of plain summary", () => {
    render(
      <ToolRowView
        name="bash"
        input={JSON.stringify({ command: "cargo check" })}
        summary="cargo check"
      />,
    );
    expect(screen.getByText("$ cargo check")).toBeTruthy();
  });

  it("falls back to summary text for unregistered tools", () => {
    render(<ToolRowView name="web_fetch" input={JSON.stringify({ url: "https://example.com" })} summary="https://example.com" />);
    expect(screen.getByText("https://example.com")).toBeTruthy();
  });

  it("shows 执行中 placeholder when pending without any summary", () => {
    render(<ToolRowView name="unknown_tool" input="" summary="" pending />);
    expect(screen.getByText("执行中…")).toBeTruthy();
  });
});
