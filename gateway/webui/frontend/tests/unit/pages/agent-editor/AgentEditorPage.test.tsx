import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import { AgentEditorPage } from "@/pages/agent-editor/AgentEditorPage";
import { createMockClient } from "../../../helpers/mockClient";

const profile = {
  profile_id: "agent-coder",
  name: "代码助手",
  description: "写代码",
  system_prompt: "你是代码助手",
  tools: ["shell"],
  skills: [],
  mcp_servers: [],
  default_model: "",
  permission_mode: "ask",
  chat_mode: "chat",
  max_steps: 100,
  include_tools: [],
  exclude_tools: [],
  status: "active",
  version: 3,
  is_system: false,
};

const baseClient = () => createMockClient({
  get: (path) => {
    if (path === "/api/agents/catalog") {
      return Promise.resolve({
        tools: [{ name: "shell", risk: "high", available: true }, { name: "read_file", risk: "low", available: true }],
        skills: [{ id: "code-review", name: "code-review" }],
        mcp: { servers: [{ name: "filesystem", available: false }] },
        models: [{ id: "deepseek-v4", provider: "cloud" }],
      });
    }
    if (path === "/api/agents?status=active&limit=200") return Promise.resolve({ agents: [profile], total: 1, limit: 200, offset: 0 });
    if (path === "/api/agents?status=archived&limit=200") return Promise.resolve({ agents: [], total: 0, limit: 200, offset: 0 });
    if (path === "/api/agents/agent-coder") return Promise.resolve(profile);
    if (path === "/api/agents/agent-coder/references") return Promise.resolve({ references: [{ workspace_id: "w1", name: "Demo", status: "active" }] });
    return Promise.reject(new Error(path));
  },
});

describe("AgentEditorPage", () => {
  beforeEach(() => {
    window.location.hash = "#/agent-editor";
  });
  it("loads the agent list and selects the first profile into the editor", async () => {
    const client = baseClient();
    render(<AgentEditorPage client={client} />);
    await screen.findByText("代码助手");
    expect(screen.getByText("编辑：代码助手")).toBeInTheDocument();
    expect(screen.getByText("你是代码助手")).toBeInTheDocument();
    await screen.findByText("Demo（active）");
    expect(screen.queryByText("● 未保存")).not.toBeInTheDocument();
  });

  it("marks dirty on edit and saves with the server version", async () => {
    const client = baseClient();
    client.put = vi.fn(() => Promise.resolve({ ...profile, version: 4 }));
    render(<AgentEditorPage client={client} />);
    await screen.findByText("编辑：代码助手");
    const nameInput = screen.getByLabelText("名称 *");
    fireEvent.change(nameInput, { target: { value: "代码助手 v2" } });
    expect(screen.getByText("● 未保存")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(client.put).toHaveBeenCalledWith(
      "/api/agents/agent-coder",
      expect.objectContaining({ name: "代码助手 v2", version: 3 }),
      expect.anything(),
    ));
  });

  it("creates a new profile via POST", async () => {
    const client = baseClient();
    client.post = vi.fn(() => Promise.resolve({ ...profile, profile_id: "agent-new", name: "新智能体", version: 1 }));
    render(<AgentEditorPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "＋ 新建" }));
    expect(screen.getByText("新建智能体")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("名称 *"), { target: { value: "新智能体" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      "/api/agents",
      expect.objectContaining({ name: "新智能体" }),
    ));
  });

  it("deletes after confirmation with the version in the body", async () => {
    const client = baseClient();
    client.request = vi.fn(() => Promise.resolve(profile));
    render(<AgentEditorPage client={client} />);
    await screen.findByRole("button", { name: "删除" });
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(client.request).toHaveBeenCalledWith(
      "DELETE",
      "/api/agents/agent-coder",
      { version: 3 },
    ));
  });

  it("blocks switching when dirty until confirmed", async () => {
    const client = baseClient();
    render(<AgentEditorPage client={client} />);
    await screen.findByText("编辑：代码助手");
    fireEvent.change(screen.getByLabelText("名称 *"), { target: { value: "改名" } });
    // dirty switch shows the confirm dialog instead of switching
    const items = screen.getAllByText("代码助手");
    fireEvent.click(items[0]!);
    expect(await screen.findByText(/当前有未保存的修改/)).toBeInTheDocument();
  });
});
