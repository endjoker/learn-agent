import { useEffect, useRef, useState, type ReactNode } from "react";

import type { QuestionAnswer, QuestionPrompt } from "@/api/types";
import { Modal } from "@/components/Modal";

export interface QuestionModalProps {
  question: QuestionPrompt;
  /** 1-based position in the pending queue (shown when >1 pending). */
  position?: number;
  total?: number;
  submitting?: boolean;
  error?: string;
  onSubmit: (answer: QuestionAnswer) => void;
  /** Provided only when the question may be cancelled (allow_cancel). */
  onCancel?: () => void;
  /** Notify the parent that the user edited the answer (clears server error). */
  onChanged?: () => void;
}

const optionDescription = (option: { description?: string }): ReactNode =>
  option.description ? <span className="q-opt-desc">{option.description}</span> : null;

/**
 * Structured question dialog: single/multi candidate options, optional
 * custom free-text answer, required validation, recommended badges, keyboard
 * navigation (arrows / Enter / Esc) and submit-error retention. The answer is
 * composed of selected option ids plus optional custom text.
 */
export function QuestionModal({
  question,
  position = 1,
  total = 1,
  submitting = false,
  error,
  onSubmit,
  onCancel,
  onChanged,
}: QuestionModalProps) {
  const [selected, setSelected] = useState<string[]>([]);
  const [customActive, setCustomActive] = useState(false);
  const [customText, setCustomText] = useState("");
  const panelRef = useRef<HTMLDivElement>(null);

  const multiple = question.multiple === true;
  const allowCustom = question.allow_custom === true;
  const required = question.required === true;
  const cancelable = onCancel !== undefined;
  const customValue = customActive ? customText.trim() : "";
  const hasSelection = selected.length > 0 || customValue !== "";
  // Safety valve: if the question offers no answerable input at all, do not
  // trap the user behind a forever-disabled submit.
  const answerable = question.options.length > 0 || allowCustom;
  const canSubmit = !submitting && (!required || !answerable || hasSelection);

  const markChanged = () => onChanged?.();

  const toggleOption = (optionId: string) => {
    setSelected((current) => {
      if (multiple) {
        return current.includes(optionId)
          ? current.filter((id) => id !== optionId)
          : [...current, optionId];
      }
      return current.includes(optionId) ? [] : [optionId];
    });
    markChanged();
  };

  const toggleCustom = () => {
    setCustomActive((active) => !active);
    markChanged();
  };

  const submit = () => {
    if (!canSubmit) return;
    onSubmit({
      selected_option_ids: selected,
      ...(customValue ? { custom_text: customValue } : {}),
    });
  };

  // Roving focus over option buttons + custom toggle + submit button. Listens
  // at the document level so arrow keys work from any focus position inside
  // the dialog (including the dialog itself right after it opens), while
  // leaving text inputs free for caret movement.
  const focusables = () =>
    Array.from(panelRef.current?.querySelectorAll<HTMLElement>("[data-q-focus]") ?? []);

  useEffect(() => {
    const handleArrowKeys = (event: KeyboardEvent) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const target = event.target as HTMLElement;
      if (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement || target instanceof HTMLSelectElement) return;
      event.preventDefault();
      const list = focusables();
      if (list.length === 0) return;
      const current = document.activeElement;
      const index = list.indexOf(current as HTMLElement);
      const delta = event.key === "ArrowDown" ? 1 : -1;
      const next = index === -1
        ? (delta === 1 ? list[0] : list[list.length - 1])
        : list[(index + delta + list.length) % list.length];
      next?.focus();
    };
    document.addEventListener("keydown", handleArrowKeys);
    return () => document.removeEventListener("keydown", handleArrowKeys);
  }, []);

  const handlePanelKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Enter" && !event.shiftKey && !(event.target instanceof HTMLTextAreaElement)) {
      const target = event.target as HTMLElement;
      // Enter on an option toggles it natively (button); Enter elsewhere
      // submits when allowed.
      if (!target.closest("button")) {
        event.preventDefault();
        submit();
      }
    }
  };

  const title = (
    <>
      <span className="q-title">问题</span>
      {total > 1 ? <span className="q-pos">待回答 {position}/{total}</span> : null}
    </>
  );

  const body = (
    <div className="question-modal" ref={panelRef} onKeyDown={handlePanelKeyDown}>
      <div className="q-text">{question.question}</div>
      {question.description ? <div className="q-desc">{question.description}</div> : null}
      {required ? <div className="q-required-hint">* 必答</div> : null}

      {question.options.length > 0 ? (
        <div className="q-options" role={multiple ? "group" : "radiogroup"} aria-label="候选答案">
          {question.options.map((option) => {
            const active = selected.includes(option.id);
            return (
              <button
                key={option.id}
                type="button"
                data-q-focus
                role={multiple ? "checkbox" : "radio"}
                aria-checked={active}
                className={`q-opt${active ? " selected" : ""}`}
                onClick={() => toggleOption(option.id)}
              >
                <span className="q-opt-mark">{multiple ? (active ? "☑" : "☐") : (active ? "◉" : "○")}</span>
                <span className="q-opt-label">{option.label}</span>
                {option.recommended ? <span className="q-opt-rec">推荐</span> : null}
                {optionDescription(option)}
              </button>
            );
          })}
        </div>
      ) : null}

      {allowCustom ? (
        <div className="q-custom">
          <button
            type="button"
            data-q-focus
            className={`q-custom-toggle${customActive ? " active" : ""}`}
            onClick={toggleCustom}
            aria-expanded={customActive}
          >
            <span className="q-opt-mark">{customActive ? "☑" : "☐"}</span>
            <span className="q-opt-label">其他（自定义答案）</span>
          </button>
          {customActive ? (
            <textarea
              className="q-custom-input"
              value={customText}
              placeholder={question.custom_placeholder ?? "输入自定义答案…"}
              rows={3}
              onChange={(event) => { setCustomText(event.target.value); markChanged(); }}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  submit();
                }
              }}
            />
          ) : null}
        </div>
      ) : null}

      {error ? (
        <div className="q-err" role="alert">
          <span>提交失败：{error}</span>
          <span className="dim">（已保留你的输入，可修改后重试）</span>
        </div>
      ) : null}
      {submitting ? <div className="q-submitting" role="status">提交中…</div> : null}
    </div>
  );

  const actions = (
    <>
      {cancelable ? (
        <button type="button" className="btn" disabled={submitting} onClick={onCancel}>取消</button>
      ) : null}
      <button
        type="button"
        data-q-focus
        className="btn primary q-submit"
        disabled={!canSubmit}
        onClick={submit}
      >
        {submitting ? "提交中…" : "提交"}
      </button>
    </>
  );

  return (
    <Modal
      title={title}
      onClose={cancelable ? onCancel : undefined}
      ariaLabel={`问题：${question.question}`}
      className="question-dialog"
    >
      {body}
      {actions}
    </Modal>
  );
}
