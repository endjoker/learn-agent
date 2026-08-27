import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { VirtualMessageList } from "@/pages/chat/VirtualMessageList";

const original = HTMLElement.prototype.getBoundingClientRect;

const flushFrames = async () => {
  // rAF + MutationObserver 回调都在 timer/macrotask 里，多轮等待确保排空
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 60));
  });
};

describe("VirtualMessageList", () => {
  it("keeps DOM nodes bounded for a 1000-item timeline", () => {
    HTMLElement.prototype.getBoundingClientRect = vi.fn(() => ({
      width: 800, height: 600, top: 0, left: 0, right: 800, bottom: 600, x: 0, y: 0, toJSON: () => ({}),
    }));
    const items = Array.from({ length: 1000 }, (_, index) => ({ key: String(index), text: `message ${index}` }));
    const { container, unmount } = render(
      <VirtualMessageList items={items} estimateSize={() => 80} renderItem={(item) => <div>{item.text}</div>} />,
    );
    expect(container.querySelectorAll("[data-virtual-index]").length).toBeLessThan(50);
    unmount();
    HTMLElement.prototype.getBoundingClientRect = original;
  });

  it("shows a jump-to-latest button after scrolling away from the bottom", () => {
    const items = Array.from({ length: 20 }, (_, index) => ({ key: String(index), text: `message ${index}` }));
    const { container } = render(
      <VirtualMessageList items={items} renderItem={(item) => <div>{item.text}</div>} />,
    );
    const scrollArea = container.querySelector(".msg-area") as HTMLDivElement;
    Object.defineProperties(scrollArea, {
      scrollTop: { configurable: true, value: 100 },
      clientHeight: { configurable: true, value: 300 },
      scrollHeight: { configurable: true, value: 1000 },
    });
    fireEvent.scroll(scrollArea);
    expect(screen.getByRole("button", { name: "跳转到最新回复" })).toBeInTheDocument();
  });

  it("remeasures a row's real height when an embedded <details> expands", async () => {
    // 回归：展开卡片（details open 属性变化）后，virtualizer 必须按真实
    // 高度布局。旧实现的兜底 remeasureAll 调 virtualizer.measureElement(node)
    // ——virtual-core 手动调用路径是 cache-first（命中 itemSizeCache 直接返回
    // 旧尺寸），导致重测空转、后续行按折叠旧高度叠压。
    const protoDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 600 });
    try {
      const items = Array.from({ length: 5 }, (_, index) => ({ key: `k${index}`, text: `row ${index}` }));
      const { container } = render(
        <VirtualMessageList
          items={items}
          estimateSize={() => 80}
          renderItem={(item) => (
            <details data-testid={`d-${item.key}`}>
              <summary>{item.text}</summary>
              <div>expanded body</div>
            </details>
          )}
        />,
      );
      const sizedInner = container.querySelector(".msg-area > div") as HTMLDivElement;
      // 初始：5 行 × 600（jsdom 原型 mock）全部经 ref 测量入缓存
      expect(sizedInner.style.height).toBe("3000px");

      // 展开：row0 用实例属性覆盖为 300（遮蔽原型 600），改 open 属性触发 MO
      const row0 = container.querySelector('[data-virtual-index="0"]') as HTMLElement;
      const details = row0.querySelector("details") as HTMLDetailsElement;
      Object.defineProperty(row0, "offsetHeight", { configurable: true, value: 300 });
      details.setAttribute("open", "");

      await flushFrames();

      // 修复后：row0 按真实 300 写回 → 总高 300 + 4×600 = 2700
      // （旧代码 cache-first 返回 600 旧值，总高保持 3000 不变）
      expect(sizedInner.style.height).toBe("2700px");
    } finally {
      if (protoDesc) Object.defineProperty(HTMLElement.prototype, "offsetHeight", protoDesc);
    }
  });

  it("defers first reveal until the first real measurement pass lands", async () => {
    // 回归：估算滚底与揭示不能同帧——估算布局的"底部"在真实布局里对应
    // 偏顶部历史区域，先揭示会看到历史顶部再跳到输入框位置。
    const protoDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 600 });
    try {
      const items = Array.from({ length: 5 }, (_, index) => ({ key: `r${index}`, text: `row ${index}` }));
      const { container } = render(
        <VirtualMessageList items={items} estimateSize={() => 80} renderItem={(item) => <div>{item.text}</div>} />,
      );
      const scrollArea = container.querySelector(".msg-area") as HTMLDivElement;
      // 首帧：内容仍隐藏（揭示推迟到首轮真实测量之后）
      expect(scrollArea.getAttribute("style") ?? "").toContain("visibility");
      await flushFrames();
      // 首轮真实测量写回并重新钉底后才揭示
      expect(scrollArea.getAttribute("style") ?? "").not.toContain("visibility");
    } finally {
      if (protoDesc) Object.defineProperty(HTMLElement.prototype, "offsetHeight", protoDesc);
    }
  });

  it("clears measurement cache when items reset (session switch)", async () => {
    const protoDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 600 });
    try {
      const first = Array.from({ length: 5 }, (_, index) => ({ key: `a${index}`, text: `a${index}` }));
      const second = Array.from({ length: 3 }, (_, index) => ({ key: `b${index}`, text: `b${index}` }));
      const { container, rerender } = render(
        <VirtualMessageList items={first} estimateSize={() => 80} renderItem={(item) => <div>{item.text}</div>} />,
      );
      expect(container.querySelector(".msg-area > div")?.getAttribute("style")).toContain("3000px");

      // 会话切换：items 清空 → measure() 清缓存 → 新会话 items 进入
      rerender(<VirtualMessageList items={[] as typeof first} estimateSize={() => 80} renderItem={(item) => <div>{item.text}</div>} />);
      await flushFrames();
      rerender(<VirtualMessageList items={second} estimateSize={() => 80} renderItem={(item) => <div>{item.text}</div>} />);
      await flushFrames();

      // 新会话：3 行 × 600，无上一会话陈旧尺寸泄漏（若 3000 残留即为失败）
      expect(container.querySelector(".msg-area > div")?.getAttribute("style")).toContain("1800px");
      expect(container.textContent).toContain("b2");
      expect(container.textContent).not.toContain("a4");
    } finally {
      if (protoDesc) Object.defineProperty(HTMLElement.prototype, "offsetHeight", protoDesc);
    }
  });
});

