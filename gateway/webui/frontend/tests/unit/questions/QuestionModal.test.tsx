import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { QuestionPrompt } from "@/api/types";
import { QuestionModal } from "@/questions/QuestionModal";

const base = (patch: Partial<QuestionPrompt> = {}): QuestionPrompt => ({
  question_id: "q1",
  session_key: "s",
  question: "选择部署环境",
  description: "请选择目标环境",
  options: [
    { id: "dev", label: "开发", description: "本地开发环境" },
    { id: "prod", label: "生产", recommended: true },
  ],
  ...patch,
});

const renderModal = (patch: Partial<QuestionPrompt> = {}, props: Partial<Parameters<typeof QuestionModal>[0]> = {}) => {
  const onSubmit = vi.fn();
  const onCancel = vi.fn();
  const onChanged = vi.fn();
  render(
    <QuestionModal
      question={base(patch)}
      onSubmit={onSubmit}
      onCancel={onCancel}
      onChanged={onChanged}
      {...props}
    />,
  );
  return { onSubmit, onCancel, onChanged };
};

const submitButton = () => screen.getByRole("button", { name: "提交" });

describe("QuestionModal", () => {
  it("shows question, description, recommended badge and queue position", () => {
    renderModal({}, { position: 1, total: 3 });
    expect(screen.getByText("选择部署环境")).toBeInTheDocument();
    expect(screen.getByText("请选择目标环境")).toBeInTheDocument();
    expect(screen.getByText("推荐")).toBeInTheDocument();
    expect(screen.getByText("待回答 1/3")).toBeInTheDocument();
    expect(screen.getByText("开发")).toBeInTheDocument();
    expect(screen.getByText("本地开发环境")).toBeInTheDocument();
  });

  it("recommended is displayed but never auto-selected", () => {
    const { onSubmit } = renderModal();
    fireEvent.click(submitButton());
    expect(onSubmit).toHaveBeenCalledWith({ selected_option_ids: [] });
  });

  it("submits a single selection; selecting another replaces it", () => {
    const { onSubmit } = renderModal();
    fireEvent.click(screen.getByRole("radio", { name: /开发/ }));
    fireEvent.click(screen.getByRole("radio", { name: /生产/ }));
    fireEvent.click(submitButton());
    expect(onSubmit).toHaveBeenLastCalledWith({ selected_option_ids: ["prod"] });
  });

  it("supports multi-select toggling", () => {
    const { onSubmit } = renderModal({ multiple: true });
    const dev = screen.getByRole("checkbox", { name: /开发/ });
    const prod = screen.getByRole("checkbox", { name: /生产/ });
    fireEvent.click(dev);
    fireEvent.click(prod);
    fireEvent.click(dev);
    fireEvent.click(submitButton());
    expect(onSubmit).toHaveBeenLastCalledWith({ selected_option_ids: ["prod"] });
  });

  it("combines candidates with a custom answer when allow_custom", () => {
    const { onSubmit } = renderModal({ allow_custom: true, custom_placeholder: "其他环境" });
    fireEvent.click(screen.getByRole("radio", { name: /开发/ }));
    fireEvent.click(screen.getByRole("button", { name: /其他/ }));
    fireEvent.change(screen.getByPlaceholderText("其他环境"), { target: { value: "预发" } });
    fireEvent.click(submitButton());
    expect(onSubmit).toHaveBeenLastCalledWith({ selected_option_ids: ["dev"], custom_text: "预发" });
  });

  it("disables submit for a required question until answered", () => {
    const { onSubmit } = renderModal({ required: true });
    expect(submitButton()).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: /开发/ }));
    expect(submitButton()).toBeEnabled();
    fireEvent.click(submitButton());
    expect(onSubmit).toHaveBeenCalledWith({ selected_option_ids: ["dev"] });
  });

  it("accepts a custom-only answer for required questions", () => {
    renderModal({ required: true, allow_custom: true });
    expect(submitButton()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /其他/ }));
    fireEvent.change(screen.getByPlaceholderText("输入自定义答案…"), { target: { value: "自定义" } });
    expect(submitButton()).toBeEnabled();
  });

  it("shows the submit error and keeps the user input intact", () => {
    renderModal({ allow_custom: true }, { error: "409 已答复" });
    fireEvent.click(screen.getByRole("button", { name: /其他/ }));
    const input = screen.getByPlaceholderText("输入自定义答案…");
    fireEvent.change(input, { target: { value: "保留我" } });
    expect(screen.getByRole("alert")).toHaveTextContent("409 已答复");
    expect(input).toHaveValue("保留我");
  });

  it("notifies onChanged when the user edits the answer", () => {
    const { onChanged } = renderModal({ allow_custom: true });
    fireEvent.click(screen.getByRole("button", { name: /其他/ }));
    fireEvent.change(screen.getByPlaceholderText("输入自定义答案…"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("radio", { name: /开发/ }));
    expect(onChanged).toHaveBeenCalled();
  });

  it("moves focus with arrow keys and submits with Enter", () => {
    const { onSubmit } = renderModal();
    const panel = screen.getByRole("dialog");
    const dev = screen.getByRole("radio", { name: /开发/ });
    const prod = screen.getByRole("radio", { name: /生产/ });
    dev.focus();
    fireEvent.keyDown(panel, { key: "ArrowDown" });
    expect(prod).toHaveFocus();
    fireEvent.keyDown(panel, { key: "ArrowUp" });
    expect(dev).toHaveFocus();
    // Enter on a non-button element inside the dialog submits.
    fireEvent.keyDown(screen.getByText("选择部署环境"), { key: "Enter" });
    expect(onSubmit).toHaveBeenCalled();
  });

  it("submits via Enter inside the custom textarea", () => {
    const { onSubmit } = renderModal({ allow_custom: true });
    fireEvent.click(screen.getByRole("button", { name: /其他/ }));
    const input = screen.getByPlaceholderText("输入自定义答案…");
    fireEvent.change(input, { target: { value: "直接回车" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith({ selected_option_ids: [], custom_text: "直接回车" });
  });

  it("renders a cancel action only when cancellation is allowed", () => {
    const { onCancel } = renderModal({ allow_cancel: true });
    expect(screen.getByRole("button", { name: "取消" })).toBeInTheDocument();
    render(<QuestionModal question={base({ allow_cancel: false })} onSubmit={vi.fn()} />);
    // Only the first (cancelable) modal exposes a cancel button.
    expect(screen.getAllByRole("button", { name: "取消" })).toHaveLength(1);
    expect(onCancel).not.toHaveBeenCalled();
  });
});
