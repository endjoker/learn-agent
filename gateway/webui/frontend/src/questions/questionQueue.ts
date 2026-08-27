import type { QuestionPrompt } from "@/api/types";
import { dedupeQuestions, matchesScope, type QuestionScope } from "@/questions/questionTypes";

/**
 * Pending structured questions for one scope, in arrival order. Only the
 * first item is presented; the rest stay queued. A failed answer keeps the
 * question at the head (user input is retained by the mounted modal).
 */
export interface QuestionQueueState {
  items: QuestionPrompt[];
  /** question_id currently being submitted (POST in flight). */
  submittingId?: string;
  /** question_id the last submit error belongs to. */
  submitErrorId?: string;
  submitError?: string;
  /** GET /api/questions recovery state (refresh-safe pending restore). */
  recovering: boolean;
  recoveryDone: boolean;
}

export const createInitialQuestionQueueState = (): QuestionQueueState => ({
  items: [],
  recovering: false,
  recoveryDone: false,
});

export type QuestionQueueAction =
  | { type: "requested"; question: QuestionPrompt; scope: QuestionScope }
  | { type: "resolved"; questionId: string }
  | { type: "reset" }
  | { type: "recoverStart" }
  | { type: "recoverDone"; questions: QuestionPrompt[] }
  | { type: "answerStart"; questionId: string }
  | { type: "answerOk"; questionId: string }
  | { type: "answerError"; questionId: string; message: string }
  | { type: "clearError" }
  | { type: "dismiss"; questionId: string };

const removeQuestion = (items: QuestionPrompt[], questionId: string): QuestionPrompt[] =>
  items.filter((item) => item.question_id !== questionId);

export const reduceQuestionQueue = (
  state: QuestionQueueState,
  action: QuestionQueueAction,
): QuestionQueueState => {
  switch (action.type) {
    case "requested": {
      if (!matchesScope(action.question, action.scope)) return state;
      if (state.items.some((item) => item.question_id === action.question.question_id)) return state;
      return { ...state, items: [...state.items, action.question] };
    }
    case "resolved":
      return {
        ...state,
        items: removeQuestion(state.items, action.questionId),
        ...(state.submittingId === action.questionId ? { submittingId: undefined } : {}),
        ...(state.submitErrorId === action.questionId ? { submitErrorId: undefined, submitError: undefined } : {}),
      };
    case "reset":
      return createInitialQuestionQueueState();
    case "recoverStart":
      return { ...state, recovering: true };
    case "recoverDone":
      return {
        items: dedupeQuestions(action.questions),
        recovering: false,
        recoveryDone: true,
        submittingId: undefined,
        submitErrorId: undefined,
        submitError: undefined,
      };
    case "answerStart":
      return { ...state, submittingId: action.questionId, submitErrorId: undefined, submitError: undefined };
    case "answerOk":
      return {
        ...state,
        items: removeQuestion(state.items, action.questionId),
        ...(state.submittingId === action.questionId ? { submittingId: undefined } : {}),
      };
    case "answerError":
      return {
        ...state,
        submittingId: undefined,
        submitErrorId: action.questionId,
        submitError: action.message,
      };
    case "clearError":
      return { ...state, submitErrorId: undefined, submitError: undefined };
    case "dismiss":
      return {
        ...state,
        items: removeQuestion(state.items, action.questionId),
        ...(state.submittingId === action.questionId ? { submittingId: undefined } : {}),
      };
    default:
      return state;
  }
};
