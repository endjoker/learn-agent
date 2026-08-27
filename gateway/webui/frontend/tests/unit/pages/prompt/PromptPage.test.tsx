import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { PromptPage } from "@/pages/prompt/PromptPage";
import { createMockClient } from "../../../helpers/mockClient";

const files = [
  { name: "AGENT.md", exists: true, size: 10, mtime_ns: 100, injected: true },
  { name: "GUIDE.md", exists: true, size: 10, mtime_ns: 100, injected: false },
  { name: "TOOLS.md", exists: true, size: 10, mtime_ns: 100, injected: true },
];

const fileClient = () => createMockClient({
  get: (path) => {
    if (path === "/api/prompt/files") return Promise.resolve({ files });
    if (path === "/api/prompt/files/AGENT.md") {
      return Promise.resolve({ name: "AGENT.md", content: "# Agent 身份", size: 10, mtime_ns: 100, truncation_limit: 8000 });
    }
    return Promise.reject(new Error(path));
  },
});

describe("PromptPage files view", () => {
  it("renders file tabs, loads content and excludes GUIDE.md", async () => {
    const client = fileClient();
    render(<PromptPage client={client} />);
    await screen.findByText(/AGENT\.md/);
    expect(screen.getByText(/TOOLS\.md/)).toBeInTheDocument();
    expect(screen.queryByText(/GUIDE\.md/)).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("# Agent 身份")).toBeInTheDocument();
  });

  it("saves with base_mtime_ns and refreshes mtime", async () => {
    const client = fileClient();
    client.put = vi.fn(() => Promise.resolve({ ok: true, mtime_ns: 200 }));
    render(<PromptPage client={client} />);
    await screen.findByDisplayValue("# Agent 身份");
    fireEvent.click(screen.getByRole("button", { name: "💾 保存" }));
    await waitFor(() => expect(client.put).toHaveBeenCalledWith(
      "/api/prompt/files/AGENT.md",
      expect.objectContaining({ content: "# Agent 身份", base_mtime_ns: 100 }),
    ));
    expect(screen.getByText("已保存")).toBeInTheDocument();
  });

  it("offers overwrite on 409 conflict and retries with the new mtime", async () => {
    const client = fileClient();
    client.put = vi.fn()
      .mockRejectedValueOnce(new ApiError("文件已被修改", { status: 409, payload: { mtime_ns: 999 } }))
      .mockResolvedValueOnce({ ok: true, mtime_ns: 999 });
    render(<PromptPage client={client} />);
    await screen.findByDisplayValue("# Agent 身份");
    fireEvent.click(screen.getByRole("button", { name: "💾 保存" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/文件已被其他端修改/);
    fireEvent.click(screen.getByRole("button", { name: "确定" }));
    await waitFor(() => expect(client.put).toHaveBeenCalledTimes(2));
    expect(client.put).toHaveBeenLastCalledWith(
      "/api/prompt/files/AGENT.md",
      expect.objectContaining({ content: "# Agent 身份", base_mtime_ns: 999 }),
    );
    expect(await screen.findByText("已保存（覆盖冲突）")).toBeInTheDocument();
  });

  it("reloads the file when the conflict overwrite is cancelled", async () => {
    const client = fileClient();
    client.put = vi.fn().mockRejectedValueOnce(new ApiError("文件已被修改", { status: 409, payload: { mtime_ns: 999 } }));
    render(<PromptPage client={client} />);
    await screen.findByDisplayValue("# Agent 身份");
    fireEvent.click(screen.getByRole("button", { name: "💾 保存" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消" }));
    await waitFor(() => expect(client.get).toHaveBeenCalledWith("/api/prompt/files/AGENT.md", expect.anything()));
  });
});

describe("PromptPage caps view", () => {
  const capsClient = () => createMockClient({
    get: (path) => {
      if (path === "/api/prompt/main-session") {
        return Promise.resolve({
          session_key: "gateway:non-workspace",
          config: { tools: null, skills: null, mcp_servers: null },
          catalog: {
            tools: [{ name: "shell", risk: "high", available: true }],
            skills: [{ id: "code-review", name: "code-review" }],
            mcp: { servers: [{ name: "filesystem", available: false }] },
            models: [],
          },
        });
      }
      return Promise.reject(new Error(path));
    },
  });

  it("defaults to inherit-all selection and saves arrays", async () => {
    window.location.hash = "#/prompt?view=caps";
    const client = capsClient();
    render(<PromptPage client={client} />);
    await screen.findByText("能力选择");
    expect(screen.getByText("工具（1）")).toBeInTheDocument();
    expect(screen.getByText("技能（1）")).toBeInTheDocument();
    expect(screen.getByText("MCP 服务（1）")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "💾 保存配置" }));
    await waitFor(() => expect(client.put).toHaveBeenCalledWith(
      "/api/prompt/main-session",
      expect.objectContaining({ tools: ["shell"], skills: ["code-review"], mcp_servers: ["filesystem"] }),
    ));
  });

  it("previews the current selection", async () => {
    window.location.hash = "#/prompt?view=caps";
    const client = capsClient();
    client.post = vi.fn(() => Promise.resolve({
      sections: [{ name: "agent", chars: 10, estimated_tokens: 2, content: "preview-body" }],
      total_chars: 10,
      estimated_tokens: 2,
      expected_prompt_hash: "sha256:abc",
      warnings: [],
    }));
    render(<PromptPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "🔍 预览 Prompt" }));
    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      "/api/prompt/main-session/preview",
      expect.objectContaining({ tools: ["shell"] }),
    ));
    expect(await screen.findByText(/完整 Prompt：10 字符/)).toBeInTheDocument();
  });
});
