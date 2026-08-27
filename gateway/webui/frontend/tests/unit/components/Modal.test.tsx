import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Modal } from "@/components/Modal";

describe("Modal", () => {
  it("renders title, children and actions in a dialog", () => {
    render(
      <Modal title="标题" ariaLabel="标题" actions={<button type="button">确定</button>}>
        <p>内容</p>
      </Modal>,
    );
    expect(screen.getByRole("dialog", { name: "标题" })).toBeInTheDocument();
    expect(screen.getByText("内容")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确定" })).toBeInTheDocument();
  });

  it("returns null while closed", () => {
    const { container } = render(
      <Modal open={false} title="标题" ariaLabel="标题"><p>内容</p></Modal>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });

  it("closes via Escape when onClose is provided", () => {
    const onClose = vi.fn();
    render(<Modal title="标题" ariaLabel="标题" onClose={onClose}><p>内容</p></Modal>);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not close via Escape when onClose is missing (non-dismissible)", () => {
    const onClose = vi.fn();
    render(<Modal title="标题" ariaLabel="标题"><p>内容</p></Modal>);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("closes via backdrop click only on the mask itself", () => {
    const onClose = vi.fn();
    render(<Modal title="标题" ariaLabel="标题" onClose={onClose}><p>内容</p></Modal>);
    fireEvent.mouseDown(screen.getByRole("presentation"));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("moves focus into the dialog while open", () => {
    const { rerender } = render(<Modal open={false} title="标题" ariaLabel="标题"><p>内容</p></Modal>);
    rerender(<Modal open title="标题" ariaLabel="标题"><p>内容</p></Modal>);
    expect(screen.getByRole("dialog")).toHaveFocus();
  });

  it("traps Tab focus within the dialog (wraps around)", () => {
    render(
      <Modal title="标题" ariaLabel="标题" actions={<button type="button">确定</button>}>
        <input aria-label="输入框" />
        <button type="button">中间</button>
      </Modal>,
    );
    const input = screen.getByLabelText("输入框");
    const ok = screen.getByRole("button", { name: "确定" });
    // Tab on the last focusable wraps to the first
    ok.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(input).toHaveFocus();
    // Shift+Tab on the first focusable wraps to the last
    input.focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(ok).toHaveFocus();
    // 普通 Tab 在中间元素间顺序移动，不打断
    ok.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(input).toHaveFocus();
  });

  it("does not steal focus back to the panel on re-render with a new onClose", () => {
    const { rerender } = render(
      <Modal title="t" ariaLabel="t" open onClose={() => undefined}>
        <input aria-label="弹窗输入" />
      </Modal>,
    );
    const input = screen.getByLabelText("弹窗输入");
    input.focus();
    // 父组件高频重渲染传入新 onClose 内联函数；修复前聚焦 effect 反复重跑，
    // 把焦点从正在输入的输入框抢回弹窗面板。断言重渲染后焦点仍在输入框。
    rerender(
      <Modal title="t" ariaLabel="t" open onClose={() => undefined}>
        <input aria-label="弹窗输入" />
      </Modal>,
    );
    expect(input).toHaveFocus();
  });
});
