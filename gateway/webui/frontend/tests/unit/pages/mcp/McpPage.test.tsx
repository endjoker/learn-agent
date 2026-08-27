import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { McpPage } from "@/pages/mcp/McpPage";
import { createMockClient } from "../../../helpers/mockClient";

const servers = [
  { name: "filesystem", transport: "stdio", command: "npx", args: ["-y", "mcp-server-fs"], env: { TOKEN: "…" }, enabled: true },
  { name: "remote", transport: "sse", url: "http://localhost:3000/sse", enabled: true },
];
const live = { filesystem: { sessions: 1, initialized: true, tools: 4 } };

describe("McpPage", () => {
  it("renders the server table with live status", async () => {
    const client = createMockClient({
      get: (path) => {
        if (path === "/api/mcp") return Promise.resolve({ servers, live });
        return Promise.reject(new Error(path));
      },
    });
    render(<McpPage client={client} />);
    await screen.findByText("filesystem");
    expect(screen.getByText("remote")).toBeInTheDocument();
    expect(screen.getByText("1 会话 · 4 工具")).toBeInTheDocument();
    expect(screen.getByText("npx -y mcp-server-fs")).toBeInTheDocument();
    expect(screen.getByText("http://localhost:3000/sse")).toBeInTheDocument();
  });

  it("creates a stdio server via the modal", async () => {
    const client = createMockClient({
      get: (path) => {
        if (path === "/api/mcp") return Promise.resolve({ servers: [], live: {} });
        return Promise.reject(new Error(path));
      },
      post: vi.fn(() => Promise.resolve({ ok: true })),
    });
    render(<McpPage client={client} />);
    await screen.findByText(/暂无 MCP 服务器/);
    fireEvent.click(screen.getByRole("button", { name: "＋ 添加" }));
    const nameInput = await screen.findByLabelText("名称");
    await userEvent.type(nameInput, "demo");
    await userEvent.type(screen.getByPlaceholderText("如 node"), "node");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      "/api/mcp/servers",
      expect.objectContaining({ name: "demo", transport: "stdio", command: "node", args: [] }),
    ));
  });

  it("deletes a server only after confirmation", async () => {
    const client = createMockClient({
      get: (path) => {
        if (path === "/api/mcp") return Promise.resolve({ servers, live: {} });
        return Promise.reject(new Error(path));
      },
      delete: vi.fn(() => Promise.resolve({ ok: true })),
    });
    render(<McpPage client={client} />);
    const deleteButtons = await screen.findAllByRole("button", { name: "删除" });
    fireEvent.click(deleteButtons[0]!);
    const confirm = await screen.findByRole("dialog");
    expect(confirm).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确定" }));
    await waitFor(() => expect(client.delete).toHaveBeenCalledWith("/api/mcp/servers/filesystem"));
  });
});
