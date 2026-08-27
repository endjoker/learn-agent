import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AsyncState } from "@/components/AsyncState";
import { PaginationControls } from "@/components/PaginationControls";
import { PageLayout } from "@/components/PageLayout";

describe("shared UI states", () => {
  it("renders loading, empty and retryable error states", () => {
    const retry = vi.fn();
    const { rerender } = render(<AsyncState status="loading" />);
    expect(screen.getByRole("status")).toHaveTextContent("加载中");

    rerender(<AsyncState status="empty" emptyMessage="没有记录" />);
    expect(screen.getByText("没有记录")).toBeInTheDocument();

    rerender(<AsyncState status="error" error="网络失败" onRetry={retry} />);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("provides a common page layout and pagination controls", () => {
    const previous = vi.fn();
    const next = vi.fn();
    render(
      <PageLayout title="会话" actions={<button>新建</button>}>
        <PaginationControls page={2} pageSize={20} total={45} onPrevious={previous} onNext={next} />
      </PageLayout>,
    );
    expect(screen.getByRole("heading", { name: "会话" })).toBeInTheDocument();
    expect(screen.getByText("21–40 / 45")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(previous).toHaveBeenCalledOnce();
    expect(next).toHaveBeenCalledOnce();
  });
});
