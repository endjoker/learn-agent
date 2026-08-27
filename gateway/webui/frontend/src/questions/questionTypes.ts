import type { QuestionAnswer, QuestionOption, QuestionPrompt } from "@/api/types";

/** Scope a question queue to a chat session and/or workspace session. */
export interface QuestionScope {
  sessionKey?: string;
  workspaceId?: string;
  workspaceSessionId?: string;
}

export interface QuestionsResponse {
  questions: QuestionPrompt[];
}

export const questionIdOf = (question: QuestionPrompt | QuestionInput): string =>
  typeof question.question_id === "string" && question.question_id
    ? question.question_id
    : typeof (question as QuestionInput).id === "string"
      ? ((question as QuestionInput).id as string)
      : "";

/** Loose shape tolerated from SSE payloads / the GET endpoint. */
export type QuestionInput = Partial<QuestionPrompt> & {
  id?: unknown;
  [key: string]: unknown;
};

const isOption = (value: unknown): value is QuestionOption => {
  if (!value || typeof value !== "object") return false;
  const option = value as Record<string, unknown>;
  return typeof option.id === "string" && typeof option.label === "string";
};

const str = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined);

/**
 * Normalize a raw question payload (SSE `question.requested` data or a GET
 * `/api/questions` item) into the canonical `QuestionPrompt` shape. Accepts
 * both `question_id` and `id` field names. Returns null when the payload is
 * unusable.
 *
 * 归属字段（workspace_id / workspace_session_id / snapshot_id / message_id）
 * 优先取顶层，缺失时回退嵌套 `context`（旧版 GET /api/questions 只带嵌套
 * context）——缺失会导致答复 POST 归属校验 403（"刷新后无法答复"）。
 */
export const normalizeQuestion = (raw: unknown): QuestionPrompt | null => {
  if (!raw || typeof raw !== "object") return null;
  const q = raw as QuestionInput;
  const questionId = questionIdOf(q as QuestionPrompt);
  const question = str(q.question);
  if (!questionId || !question) return null;
  const options = Array.isArray(q.options) ? q.options.filter(isOption) : [];
  const ctx = (q.context && typeof q.context === "object" ? q.context : {}) as Record<string, unknown>;
  const ownership = (key: string): string | undefined =>
    str(q[key]) ?? str(ctx[key]);
  return {
    question_id: questionId,
    session_key: str(q.session_key) ?? "",
    ...(ownership("workspace_id") !== undefined ? { workspace_id: ownership("workspace_id") } : {}),
    ...(ownership("workspace_session_id") !== undefined ? { workspace_session_id: ownership("workspace_session_id") } : {}),
    ...(ownership("message_id") !== undefined ? { message_id: ownership("message_id") } : {}),
    ...(ownership("snapshot_id") !== undefined ? { snapshot_id: ownership("snapshot_id") } : {}),
    question,
    ...(str(q.description) !== undefined ? { description: str(q.description) } : {}),
    options,
    ...(typeof q.multiple === "boolean" ? { multiple: q.multiple } : {}),
    ...(typeof q.required === "boolean" ? { required: q.required } : {}),
    ...(typeof q.allow_custom === "boolean" ? { allow_custom: q.allow_custom } : {}),
    ...(str(q.custom_placeholder) !== undefined ? { custom_placeholder: str(q.custom_placeholder) } : {}),
    ...(typeof q.allow_cancel === "boolean" ? { allow_cancel: q.allow_cancel } : {}),
  };
};

/**
 * Whether a question belongs to the given scope. Questions carrying no scope
 * marker are treated as global and match any scope; a marker that contradicts
 * the scope excludes the question (no cross-session bleed).
 */
export const matchesScope = (question: QuestionPrompt, scope: QuestionScope): boolean => {
  if (scope.sessionKey && question.session_key && question.session_key !== scope.sessionKey) return false;
  if (scope.workspaceId && question.workspace_id && question.workspace_id !== scope.workspaceId) return false;
  if (scope.workspaceSessionId && question.workspace_session_id && question.workspace_session_id !== scope.workspaceSessionId) return false;
  return true;
};

export const scopeKeyOf = (scope: QuestionScope): string =>
  [scope.sessionKey ?? "", scope.workspaceId ?? "", scope.workspaceSessionId ?? ""].join("|");

export const sameQuestion = (a: QuestionPrompt, b: QuestionPrompt): boolean =>
  a.question_id === b.question_id && a.question_id !== "";

export const dedupeQuestions = (questions: QuestionPrompt[]): QuestionPrompt[] => {
  const seen = new Set<string>();
  const out: QuestionPrompt[] = [];
  for (const question of questions) {
    if (!question.question_id || seen.has(question.question_id)) continue;
    seen.add(question.question_id);
    out.push(question);
  }
  return out;
};

export const emptyAnswer = (): QuestionAnswer => ({ selected_option_ids: [] });
