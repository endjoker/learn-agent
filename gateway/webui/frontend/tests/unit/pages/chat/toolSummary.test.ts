import { describe, expect, it } from "vitest";

import { summarizeToolParams } from "@/pages/chat/toolSummary";

describe("summarizeToolParams — Plan/Goal 结构化能力工具", () => {
  it("create_goal/create_plan 突出 objective", () => {
    expect(summarizeToolParams("create_goal", '{"objective":"归档 2026 年度审计"}')).toBe("归档 2026 年度审计");
    expect(summarizeToolParams("create_plan", '{"objective":"生成月报"}')).toBe("生成月报");
  });

  it("get_goal/get_plan 缺省时给出可读占位", () => {
    expect(summarizeToolParams("get_goal", "{}")).toBe("查询当前执行状态");
    expect(summarizeToolParams("get_plan", "{}")).toBe("查询当前执行状态");
  });

  it("get_goal/get_plan 带 id 时突出 id 尾段", () => {
    expect(summarizeToolParams("get_plan", '{"plan_id":"plan_abcdef0123456789"}')).toContain("plan_abcdef0123456789");
  });

  it("update_goal/update_plan 突出动作 + id", () => {
    const s = summarizeToolParams("update_goal", '{"goal_id":"goal_abc","action":"resume"}');
    expect(s).toContain("resume");
    expect(s).toContain("goal_abc");
  });

  it("控制类动作用 id 尾段", () => {
    expect(summarizeToolParams("cancel_goal", '{"goal_id":"goal_xyz"}')).toBe("goal_xyz");
  });

  it("未知结构化工具不抛错，兜底第一个字段", () => {
    expect(summarizeToolParams("list_goals", "{}")).toBe("查询当前执行状态");
  });
});
