import { describe, expect, it } from "vitest";

import type { QuestionPrompt } from "@/api/types";
import {
  createInitialQuestionQueueState,
  reduceQuestionQueue,
  type QuestionQueueAction,
} from "@/questions/questionQueue";

const question = (id: string, patch: Partial<QuestionPrompt> = {}): QuestionPrompt => ({
  question_id: id,
  session_key: "s",
  question: `问题 ${id}`,
  options: [{ id: "a", label: "A" }],
  ...patch,
});

const reduce = (state = createInitialQuestionQueueState(), ...actions: QuestionQueueAction[]) =>
  actions.reduce(reduceQuestionQueue, state);

describe("question queue reducer", () => {
  it("enqueues requested questions in arrival order, deduped", () => {
    let state = reduce(undefined, { type: "requested", question: question("q1"), scope: { sessionKey: "s" } });
    state = reduce(state, { type: "requested", question: question("q2"), scope: { sessionKey: "s" } });
    state = reduce(state, { type: "requested", question: question("q1"), scope: { sessionKey: "s" } });
    expect(state.items.map((item) => item.question_id)).toEqual(["q1", "q2"]);
  });

  it("isolates questions by session scope (no cross-session bleed)", () => {
    let state = createInitialQuestionQueueState();
    state = reduce(state, { type: "requested", question: question("q1", { session_key: "s1" }), scope: { sessionKey: "s2" } });
    expect(state.items).toHaveLength(0);
    state = reduce(state, { type: "requested", question: question("q2", { session_key: "s2" }), scope: { sessionKey: "s2" } });
    expect(state.items.map((item) => item.question_id)).toEqual(["q2"]);
  });

  it("isolates questions by workspace session scope", () => {
    const scope = { workspaceId: "w", workspaceSessionId: "ws" };
    let state = createInitialQuestionQueueState();
    state = reduce(state, { type: "requested", question: question("q1", { workspace_id: "w", workspace_session_id: "ws" }), scope });
    expect(state.items).toHaveLength(1);
    state = reduce(state, { type: "requested", question: question("q2", { workspace_id: "w", workspace_session_id: "other" }), scope });
    expect(state.items.map((item) => item.question_id)).toEqual(["q1"]);
  });

  it("accepts scope-less (global) questions in any scope", () => {
    const state = reduce(undefined, {
      type: "requested",
      question: question("g", { session_key: "" }),
      scope: { sessionKey: "s" },
    });
    expect(state.items.map((item) => item.question_id)).toEqual(["g"]);
  });

  it("removes a resolved question and its submit error", () => {
    let state = createInitialQuestionQueueState();
    state = reduce(state,
      { type: "requested", question: question("q1"), scope: { sessionKey: "s" } },
      { type: "answerError", questionId: "q1", message: "boom" });
    expect(state.submitError).toBe("boom");
    state = reduce(state, { type: "resolved", questionId: "q1" });
    expect(state.items).toHaveLength(0);
    expect(state.submitError).toBeUndefined();
  });

  it("keeps the question and records the error when answering fails", () => {
    const state = reduce(createInitialQuestionQueueState(),
      { type: "requested", question: question("q1"), scope: { sessionKey: "s" } },
      { type: "answerStart", questionId: "q1" },
      { type: "answerError", questionId: "q1", message: "网络错误" });
    expect(state.items.map((item) => item.question_id)).toEqual(["q1"]);
    expect(state.submittingId).toBeUndefined();
    expect(state.submitErrorId).toBe("q1");
    expect(state.submitError).toBe("网络错误");
  });

  it("removes the question on success and keeps the rest of the queue", () => {
    const state = reduce(createInitialQuestionQueueState(),
      { type: "requested", question: question("q1"), scope: { sessionKey: "s" } },
      { type: "requested", question: question("q2"), scope: { sessionKey: "s" } },
      { type: "answerStart", questionId: "q1" },
      { type: "answerOk", questionId: "q1" });
    expect(state.items.map((item) => item.question_id)).toEqual(["q2"]);
    expect(state.submittingId).toBeUndefined();
  });

  it("replaces the queue from GET recovery and clears transient state", () => {
    const state = reduce(createInitialQuestionQueueState(),
      { type: "requested", question: question("stale"), scope: { sessionKey: "s" } },
      { type: "recoverStart" },
      { type: "recoverDone", questions: [question("fresh")] });
    expect(state.items.map((item) => item.question_id)).toEqual(["fresh"]);
    expect(state.recovering).toBe(false);
    expect(state.recoveryDone).toBe(true);
  });

  it("dismisses only the requested question", () => {
    const state = reduce(createInitialQuestionQueueState(),
      { type: "requested", question: question("q1"), scope: { sessionKey: "s" } },
      { type: "requested", question: question("q2"), scope: { sessionKey: "s" } },
      { type: "dismiss", questionId: "q1" });
    expect(state.items.map((item) => item.question_id)).toEqual(["q2"]);
  });
});
