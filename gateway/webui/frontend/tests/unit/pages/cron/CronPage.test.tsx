import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CronPage } from "@/pages/cron/CronPage";
import { createMockClient } from "../../../helpers/mockClient";

const jobs = [
  {
    name: "daily-report",
    schedule: "0 9 * * 1-5",
    prompt: "生成日报",
    session: "isolated",
    deliver: { mode: "announce", channel: "feishu" },
    running: false,
    paused: true,
    last_status: "ok",
    runs: 3,
  },
  {
    name: "backup",
    schedule: "0 2 * * *",
    prompt: "备份数据",
    session: "persist",
    deliver: { mode: "none" },
    running: true,
    paused: false,
    last_status: "error",
    runs: 1,
    failures: 1,
  },
];

const channelsClient = () => createMockClient({
  get: (path) => {
    if (path === "/api/scheduler/channels") {
      return Promise.resolve({ channels: [{ channel: "feishu", hint: "飞书填 chat_id" }], webhooks: ["https://hooks.example.com/x"], targets: { feishu: ["oc_123"] } });
    }
    if (path === "/api/scheduler/jobs") return Promise.resolve({ jobs });
    if (path === "/api/scheduler/history") {
      return Promise.resolve({ history: [
        { at: "10:00", job: "daily-report", status: "ok", duration_s: 5, trigger: "cron" },
      ] });
    }
    return Promise.reject(new Error(path));
  },
});

describe("CronPage", () => {
  it("renders the jobs table with badges and history", async () => {
    const client = channelsClient();
    render(<CronPage client={client} />);
    await screen.findByText("daily-report", { selector: "b" });
    expect(screen.getByText("暂停", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getByText("进行中")).toBeInTheDocument();
    expect(screen.getByText("0 9 * * 1-5")).toBeInTheDocument();
    expect(screen.getByText("announce→feishu")).toBeInTheDocument();
    expect(screen.getByText("最近执行历史")).toBeInTheDocument();
    expect(screen.getByText("10:00")).toBeInTheDocument();
  });

  it("resumes a paused job", async () => {
    const client = channelsClient();
    render(<CronPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "恢复" }));
    await waitFor(() => expect(client.post).toHaveBeenCalledWith("/api/scheduler/jobs/daily-report/resume"));
  });

  it("creates a job via the modal", async () => {
    const client = channelsClient();
    render(<CronPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "＋ 添加任务" }));
    const nameInput = await screen.findByLabelText("名称");
    fireEvent.change(nameInput, { target: { value: "nightly" } });
    fireEvent.change(screen.getByLabelText("cron 表达式"), { target: { value: "0 0 * * *" } });
    fireEvent.change(screen.getByLabelText("prompt"), { target: { value: "夜间任务" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      "/api/scheduler/jobs",
      expect.objectContaining({ name: "nightly", schedule: "0 0 * * *", prompt: "夜间任务", session: "isolated" }),
    ));
  });

  it("deletes only after confirmation", async () => {
    const client = channelsClient();
    client.delete = vi.fn(() => Promise.resolve({ ok: true }));
    render(<CronPage client={client} />);
    const deleteButtons = await screen.findAllByRole("button", { name: "删除" });
    fireEvent.click(deleteButtons[0]!);
    fireEvent.click(await screen.findByRole("button", { name: "确定" }));
    await waitFor(() => expect(client.delete).toHaveBeenCalledWith("/api/scheduler/jobs/daily-report"));
  });
});
