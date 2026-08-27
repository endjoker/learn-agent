import { QuestionModal } from "@/questions/QuestionModal";
import type { QuestionQueue } from "@/questions/useQuestionQueue";

/**
 * Renders the pending question queue for one scope as a modal. Only the head
 * of the queue is shown; answering (or resolving) it reveals the next one.
 * The modal is keyed by question_id so switching questions resets the input
 * state, while a failed submit keeps the same key (input retained).
 */
export function QuestionHost({ queue }: { queue: QuestionQueue }) {
  const { state } = queue;
  const active = state.items[0];
  if (!active) return null;
  const total = state.items.length;
  const submitting = state.submittingId === active.question_id;
  const error = state.submitErrorId === active.question_id ? state.submitError : undefined;
  const cancelable = active.allow_cancel !== false;

  return (
    <QuestionModal
      key={active.question_id}
      question={active}
      position={1}
      total={total}
      submitting={submitting}
      error={error}
      onSubmit={(answer) => void queue.answer(active, answer)}
      onCancel={cancelable ? () => queue.dismiss(active) : undefined}
      onChanged={queue.clearError}
    />
  );
}
