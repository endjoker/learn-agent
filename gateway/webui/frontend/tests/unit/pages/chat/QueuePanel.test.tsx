import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { QueuePanel } from "@/pages/chat/QueuePanel";
import type { QueueItem } from "@/gateway/types";

const makeItem = (overrides: Partial<QueueItem>): QueueItem => ({
  queue_item_id: "q1",
  conversation_id: "c1",
  position: 1,
  revision: 1,
  status: "waiting",
  text: "帮我跑一下测试",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

describe("QueuePanel", () => {
  it("renders waiting rows with steering/move/delete ops and countdown", () => {
    const onInject = vi.fn();
    render(
      <QueuePanel
        countdown={3}
        onInject={onInject}
        queue={[
          makeItem({ queue_item_id: "q1", position: 1, text: "第一条" }),
          makeItem({ queue_item_id: "q2", position: 2, text: "第二条" }),
        ]}
      />,
    );
    expect(screen.getByTestId("queue-panel")).toHaveTextContent("队列（2）");
    expect(screen.getByText("3 秒后发送…")).toBeInTheDocument();
    const rows = screen.getAllByTestId("queue-row");
    expect(rows).toHaveLength(2);
    // 等待项带全部操作按钮（每行一组）；"插入"统一立即发送语义
    //（运行中 Steering 注入 / 空闲立即分派）
    expect(screen.getAllByTitle("插入：运行中注入当前 Turn（Steering）；空闲立即发送")).toHaveLength(2);
    expect(screen.getAllByTitle("上移")).toHaveLength(2);
    expect(screen.getAllByTitle("下移")).toHaveLength(2);
    expect(screen.getAllByTitle("删除")).toHaveLength(2);
    // 点击"插入"回调对应队列项
    fireEvent.click(screen.getAllByTitle("插入：运行中注入当前 Turn（Steering）；空闲立即发送")[0] as HTMLElement);
    expect(onInject).toHaveBeenCalledWith("q1");
  });

  it("hides ops and countdown for non-waiting items / idle state", () => {
    render(
      <QueuePanel
        countdown={0}
        onInject={vi.fn()}
        queue={[makeItem({ status: "sending", text: "发送中" })]}
      />,
    );
    expect(screen.queryByText(/秒后发送/)).not.toBeInTheDocument();
    expect(screen.queryByTitle("插入：运行中注入当前 Turn（Steering）；空闲立即发送")).not.toBeInTheDocument();
  });

  it("filters terminal (sent/deleted) items out of the window", () => {
    render(
      <QueuePanel
        countdown={0}
        onInject={vi.fn()}
        queue={[
          makeItem({ queue_item_id: "q0", status: "sent", text: "已发送" }),
          makeItem({ queue_item_id: "q1", status: "waiting", text: "等待中" }),
        ]}
      />,
    );
    expect(screen.getByTestId("queue-panel")).toHaveTextContent("队列（1）");
    expect(screen.queryByText("已发送")).not.toBeInTheDocument();
    expect(screen.getByText("等待中")).toBeInTheDocument();
  });

  it("renders nothing for an empty queue", () => {
    const { container } = render(<QueuePanel queue={[]} countdown={0} onInject={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });
});
