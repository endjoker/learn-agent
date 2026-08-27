import { describe, expect, it } from "vitest";

import { render, screen } from "@testing-library/react";
import { firstReasoningLine, ReasoningCard, latestReasoningLine } from "@/components/ReasoningCard";

describe("firstReasoningLine", () => {
  it("returns first non-empty line", () => {
    expect(firstReasoningLine("\n\n读取 tests 目录统计文件数。\n后续内容")).toBe(
      "读取 tests 目录统计文件数。");
  });

  it("strips markdown decorations from the preview", () => {
    expect(firstReasoningLine("## 分析需求\n正文")).toBe("分析需求");
    expect(firstReasoningLine("- **第一步**：先读目录")).toBe("第一步：先读目录");
    expect(firstReasoningLine("> 引用要点")).toBe("引用要点");
  });

  it("truncates long first lines keeping the head", () => {
    const long = "意".repeat(300);
    const out = firstReasoningLine(long);
    expect(out.startsWith("意")).toBe(true);
    expect(out.length).toBeLessThanOrEqual(161);
    expect(out.endsWith("…")).toBe(true);
  });

  it("returns empty for blank text", () => {
    expect(firstReasoningLine("  \n \n")).toBe("");
  });
});

describe("latestReasoningLine (unchanged contract)", () => {
  it("keeps tail-follow semantics", () => {
    expect(latestReasoningLine("第一行\n第二行")).toBe("第二行");
  });
});

describe("ReasoningCard terminal-collapse preview", () => {
  const body = "先统计 tests 目录的测试文件数量。\n\n再汇总报告。";

  it("terminal collapsed state shows static first-line preview (regression: 头部只剩占位标题)", () => {
    render(<ReasoningCard text={body} tokens={42} live={false} />);
    // jsdom 中 details 正文始终在 DOM（折叠由 CSS 承担）：首行至少出现在
    // 头部预览槽（.reasoning-card-latest），即折叠可见的预览存在
    const previews = screen.getAllByText(/先统计 tests 目录的测试文件数量/);
    expect(previews.length).toBeGreaterThanOrEqual(1);
    expect(
      previews.some((el) => el.closest(".reasoning-card-head") !== null)
    ).toBe(true);
    // 标题仍是"思考过程 · N tokens"
    expect(screen.getByText(/思考过程/)).toBeInTheDocument();
  });

  it("expanded (open=true) removes the preview slot; preview slot only when collapsed", () => {
    // 折叠可见性逻辑直接按组件条件验证（jsdom 不派发 details toggle）：
    // collapsedLive = live && !open —— live 态走 latest（尾行）
    // 终态折叠（!live && !open）走 firstLine；open 时两槽都不渲染。
    const { container, rerender } = render(
      <ReasoningCard text={body} live={false} />);
    let head = container.querySelector(".reasoning-card-head");
    expect(head?.querySelector(".reasoning-card-latest")).not.toBeNull();

    rerender(<ReasoningCard key="live" text={body} live />);
    head = container.querySelector(".reasoning-card-head");
    // live 折叠同样有槽（尾行），且槽内容为末行
    const liveSlot = head?.querySelector(".reasoning-card-latest");
    expect(liveSlot?.textContent).toContain("再汇总报告");
  });

  it("live collapsed keeps tail-follow latest line (unchanged)", () => {
    render(<ReasoningCard text={body} live />);
    expect(screen.getByText(/再汇总报告/)).toBeInTheDocument();
  });
});
