import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import type { QuestionPrompt } from "@/api/types";
import { QuestionHost } from "@/questions/QuestionHost";
import { useQuestionQueue } from "@/questions/useQuestionQueue";
import type { QuestionScope } from "@/questions/questionTypes";

const question = (id: string, patch: Partial<QuestionPrompt> = {}): QuestionPrompt => ({
  question_id: id,
  session_key: "s",
  question: `问题 ${id}`,
  options: [
    { id: "a", label: "选项A" },
    { id: "b", label: "选项B" },
  ],
  ...patch,
});

function HostHarness({ client, scope }: { client: ApiClient; scope: QuestionScope }) {
  const queue = useQuestionQueue(client, scope);
  return <QuestionHost queue={queue} />;
}

const clientOf = (get: unknown, post: unknown = vi.fn(() => Promise.resolve({ ok: true }))) =>
  ({ get, post }) as unknown as ApiClient;

describe("QuestionHost", () => {
  it("recovers pending questions via GET and shows the modal", async () => {
    const client = clientOf(vi.fn(() => Promise.resolve({ questions: [question("q1")] })));
    render(<HostHarness client={client} scope={{ sessionKey: "s" }} />);
    expect(await screen.findByText("问题 q1")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("shows only the head of the queue and reveals the next after answering", async () => {
    const post = vi.fn(() => Promise.resolve({ ok: true }));
    const client = clientOf(
      vi.fn(() => Promise.resolve({ questions: [question("q1"), question("q2")] })),
      post,
    );
    render(<HostHarness client={client} scope={{ sessionKey: "s" }} />);
    await screen.findByText("问题 q1");
    expect(screen.getByText("待回答 1/2")).toBeInTheDocument();
    expect(screen.queryByText("问题 q2")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: /选项A/ }));
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    expect(await screen.findByText("问题 q2")).toBeInTheDocument();
    // 答复请求体携带归属信息（session_key）——后端 fail-closed
    expect(post).toHaveBeenCalledWith("/api/questions/q1", { selected_option_ids: ["a"], session_key: "s" });
  });

  it("keeps the modal and the user input when submission fails", async () => {
    const post = vi.fn(() => Promise.reject(new Error("409 已答复")));
    const client = clientOf(
      vi.fn(() => Promise.resolve({ questions: [question("q1", { allow_custom: true })] })),
      post,
    );
    render(<HostHarness client={client} scope={{ sessionKey: "s" }} />);
    await screen.findByText("问题 q1");
    fireEvent.click(screen.getByRole("button", { name: /其他/ }));
    const input = screen.getByPlaceholderText("输入自定义答案…");
    fireEvent.change(input, { target: { value: "保留我的答案" } });
    fireEvent.click(screen.getByRole("button", { name: "提交" }));

    await screen.findByRole("alert");
    expect(screen.getByText("问题 q1")).toBeInTheDocument();
    expect(input).toHaveValue("保留我的答案");
  });

  it("is not dismissible when allow_cancel is false", async () => {
    const client = clientOf(vi.fn(() => Promise.resolve({ questions: [question("q1", { allow_cancel: false })] })));
    render(<HostHarness client={client} scope={{ sessionKey: "s" }} />);
    await screen.findByText("问题 q1");
    expect(screen.queryByRole("button", { name: "取消" })).not.toBeInTheDocument();
  });

  it("dismisses (cancels) a cancelable question locally", async () => {
    const client = clientOf(vi.fn(() => Promise.resolve({ questions: [question("q1")] })));
    render(<HostHarness client={client} scope={{ sessionKey: "s" }} />);
    await screen.findByText("问题 q1");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("renders nothing with an empty queue", async () => {
    const client = clientOf(vi.fn(() => Promise.resolve({ questions: [] })));
    const { container } = render(<HostHarness client={client} scope={{ sessionKey: "s" }} />);
    await waitFor(() => expect(client.get).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
