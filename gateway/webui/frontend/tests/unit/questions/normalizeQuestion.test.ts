import { describe, expect, it } from "vitest";

import { normalizeQuestion } from "@/questions/questionTypes";

describe("normalizeQuestion", () => {
  it("reads top-level ownership fields (SSE / new GET payload)", () => {
    const q = normalizeQuestion({
      question_id: "q-1",
      session_key: "workspace:w1:s1",
      question: "选哪个?",
      options: [{ id: "a", label: "A" }],
      workspace_id: "w1",
      workspace_session_id: "s1",
      snapshot_id: "snap-1",
      message_id: "m-9",
    });
    expect(q).toMatchObject({
      workspace_id: "w1",
      workspace_session_id: "s1",
      snapshot_id: "snap-1",
      message_id: "m-9",
    });
  });

  it("falls back to nested context for legacy GET payloads (fix: answer 403)", () => {
    // 旧 GET /api/questions 只把归属放在嵌套 context；若不回退，答复 POST
    // 会因缺 message_id/snapshot_id 被判 context_mismatch → 403 "无法答复"。
    const q = normalizeQuestion({
      id: "q-2",
      question_id: "q-2",
      session_key: "workspace:w1:s1",
      question: "选哪个?",
      options: [{ id: "a", label: "A" }],
      context: {
        workspace_id: "w1",
        workspace_session_id: "s1",
        snapshot_id: "snap-2",
        message_id: "m-10",
      },
    });
    expect(q).toMatchObject({
      workspace_id: "w1",
      workspace_session_id: "s1",
      snapshot_id: "snap-2",
      message_id: "m-10",
    });
  });

  it("returns null for unusable payloads", () => {
    expect(normalizeQuestion(null)).toBeNull();
    expect(normalizeQuestion({ id: "x", question: "" })).toBeNull();
  });
});
