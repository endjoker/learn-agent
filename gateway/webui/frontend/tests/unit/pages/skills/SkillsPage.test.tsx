import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SkillsPage } from "@/pages/skills/SkillsPage";
import { createMockClient } from "../../../helpers/mockClient";

const skills = [
  { name: "code-review", version: 2, description: "代码评审", tags: ["review"], instruction_chars: 120 },
];

describe("SkillsPage", () => {
  it("renders meta line and skill cards", async () => {
    const client = createMockClient({
      get: (path) => {
        if (path === "/api/skills/meta") return Promise.resolve({ skills_dir: "/repo/SKILLS", exists: true, platform_note: "" });
        if (path === "/api/skills") return Promise.resolve({ skills });
        return Promise.reject(new Error(path));
      },
    });
    render(<SkillsPage client={client} />);
    await screen.findByText(/技能目录: \/repo\/SKILLS/);
    expect(screen.getByText("code-review")).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(screen.getByText("120 字符指令")).toBeInTheDocument();
  });

  it("opens the instruction modal and renders sanitized markdown", async () => {
    const client = createMockClient({
      get: (path) => {
        if (path === "/api/skills/meta") return Promise.resolve({ skills_dir: "/repo/SKILLS", exists: true, platform_note: "" });
        if (path === "/api/skills") return Promise.resolve({ skills });
        if (path === "/api/skills/code-review") return Promise.resolve({ name: "code-review", instruction: "# 标题\n**bold**\n\n<script>alert(1)</script>" });
        return Promise.reject(new Error(path));
      },
    });
    render(<SkillsPage client={client} />);
    fireEvent.click(await screen.findByRole("button", { name: "查看指令" }));
    await screen.findByRole("dialog");
    expect(screen.getByText(/code-review — instruction\.md/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "标题" })).toBeInTheDocument();
    expect(screen.queryByText("alert(1)")).not.toBeInTheDocument();
  });

  it("shows empty placeholder", async () => {
    const client = createMockClient({
      get: (path) => {
        if (path === "/api/skills/meta") return Promise.resolve({ skills_dir: "", exists: false, platform_note: "" });
        return Promise.resolve({ skills: [] });
      },
    });
    render(<SkillsPage client={client} />);
    await screen.findByText(/暂无技能/);
  });
});
