import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SettingsPage } from "@/pages/settings/SettingsPage";
import { createMockClient } from "../../../helpers/mockClient";

const configPayload = {
  config: {
    llm: {
      model_id: "deepseek-v4",
      timeout: 120,
      models: {
        "deepseek-v4": { base_url: "https://api.deepseek.com", api_key: "sk-…", context_length: 131072, protocol: "openai" },
        "gpt-4o": { provider: "cloud", api_key: "…", context_length: 128000 },
      },
      reasoning: { level: "medium" },
    },
    prompt: { bootstrap_max_chars_per_file: 8000, bootstrap_max_chars_total: 32000, truncation_warning: "once" },
    gateway: { sessions: { max_sessions: 200, idle_timeout_minutes: 60, soft_timeout_seconds: 90, hard_timeout_seconds: 6000, persist: true } },
  },
  rev: 7,
};

const makeClient = () => createMockClient({
  get: (path) => {
    if (path === "/api/config") return Promise.resolve(configPayload);
    return Promise.reject(new Error(path));
  },
});

describe("SettingsPage", () => {
  it("renders sections and the models table", async () => {
    const client = makeClient();
    render(<SettingsPage client={client} />);
    await screen.findByText("大模型");
    expect(screen.getByRole("cell", { name: /deepseek-v4/ })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "gpt-4o" })).toBeInTheDocument();
    expect(screen.getByText("默认", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getByText("Prompt 截断")).toBeInTheDocument();
    expect(screen.getByText("会话")).toBeInTheDocument();
    // 显示用户配置的真实值（max_sessions=200 / hard=6000，非默认值）
    await screen.findByDisplayValue("200");
    expect(screen.getByDisplayValue("6000")).toBeInTheDocument();
    // 死配置段已移除：无消费者（agent 工作区来自 permission.workspace）
    expect(screen.queryByText("工作区")).not.toBeInTheDocument();
  });

  it("uses one extended width style for the three primary settings selects", async () => {
    const client = makeClient();
    render(<SettingsPage client={client} />);
    await screen.findByText("大模型");

    const selects = [
      screen.getByLabelText("主题"),
      screen.getByLabelText("默认模型"),
      screen.getByLabelText("全局推理等级"),
    ];
    for (const select of selects) expect(select).toHaveClass("settings-primary-select");
  });

  it("patches gateway.sessions (GatewayServer config is the gateway sub-config)", async () => {
    const client = makeClient();
    render(<SettingsPage client={client} />);
    await screen.findByText("会话");
    const maxInput = screen.getByLabelText(/会话上限/);
    fireEvent.change(maxInput, { target: { value: "80" } });
    const sessionCard = maxInput.closest(".settings-card");
    if (!sessionCard) throw new Error("session settings card not found");
    fireEvent.click(sessionCard.querySelector("button") as HTMLButtonElement);
    await waitFor(() => expect(client.patch).toHaveBeenCalledWith(
      "/api/config/gateway.sessions",
      expect.objectContaining({
        patch: expect.objectContaining({ max_sessions: 80, persist: true }) as unknown,
      }),
    ));
  });

  it("blocks saving sessions when soft timeout exceeds hard timeout", async () => {
    const client = makeClient();
    render(<SettingsPage client={client} />);
    await screen.findByText("会话");
    fireEvent.change(screen.getByLabelText(/软超时（秒）/), { target: { value: "9999" } });
    const softInput = screen.getByLabelText(/软超时（秒）/);
    const sessionCard = softInput.closest(".settings-card") as HTMLElement;
    const cardSave = Array.from(sessionCard.querySelectorAll("button")).find((b) => b.textContent === "保存") as HTMLButtonElement;
    expect(cardSave.disabled).toBe(true);
    fireEvent.click(cardSave);
    await waitFor(() => expect(client.patch).not.toHaveBeenCalled());
  });

  it("toggles session persistence and includes it in the patch", async () => {
    const client = makeClient();
    render(<SettingsPage client={client} />);
    await screen.findByText("会话");
    fireEvent.change(screen.getByLabelText(/会话持久化/), { target: { value: "false" } });
    const persistSelect = screen.getByLabelText(/会话持久化/);
    const sessionCard = persistSelect.closest(".settings-card") as HTMLElement;
    const cardSave = Array.from(sessionCard.querySelectorAll("button")).find((b) => b.textContent === "保存") as HTMLButtonElement;
    fireEvent.click(cardSave);
    await waitFor(() => expect(client.patch).toHaveBeenCalledWith(
      "/api/config/gateway.sessions",
      expect.objectContaining({ patch: expect.objectContaining({ persist: false }) as unknown }),
    ));
  });

  it("saves the LLM request timeout via PUT /api/config/llm", async () => {
    const client = makeClient();
    render(<SettingsPage client={client} />);
    await screen.findByText("大模型");
    const timeoutInput = screen.getByLabelText(/请求超时（秒/);
    fireEvent.change(timeoutInput, { target: { value: "300" } });
    const saveTimeout = screen.getByRole("button", { name: "保存超时" });
    fireEvent.click(saveTimeout);
    await waitFor(() => expect(client.put).toHaveBeenCalledWith(
      "/api/config/llm",
      expect.objectContaining({ timeout: 300 }),
    ));
  });

  it("switches theme and persists to localStorage", async () => {
    const client = makeClient();
    render(<SettingsPage client={client} />);
    await screen.findByText("界面");
    const themeSelect = screen.getByLabelText("主题");
    fireEvent.change(themeSelect, { target: { value: "dark" } });
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("jkagent.theme")).toBe("dark");
    expect(screen.getByText("已切换为夜间主题")).toBeInTheDocument();
  });

  it("sets the default model via PUT /api/config/llm", async () => {
    const client = makeClient();
    render(<SettingsPage client={client} />);
    await screen.findByText("大模型");
    const defaultSelect = screen.getByLabelText("默认模型");
    fireEvent.change(defaultSelect, { target: { value: "gpt-4o" } });
    await waitFor(() => expect(client.put).toHaveBeenCalledWith(
      "/api/config/llm",
      expect.objectContaining({ model_id: "gpt-4o" }),
    ));
  });
});
