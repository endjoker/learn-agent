import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

export interface ModalProps {
  title?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
  wide?: boolean;
  onClose?: () => void;
  /** Controlled visibility; defaults to true so callers may render conditionally. */
  open?: boolean;
  className?: string;
  ariaLabel?: string;
}

let openModalCount = 0;

/**
 * Shared modal shell mirroring the legacy .modal-mask / .modal markup.
 *
 * - Rendered in a portal on document.body so stacking contexts / overflow
 *   ancestors cannot clip or trap it.
 * - Closable via Escape or backdrop click only when `onClose` is provided
 *   (omit it for non-dismissible dialogs, e.g. a required question).
 * - Focus moves into the dialog while open and returns to the previously
 *   focused element on close; body scroll is locked while any modal is open.
 */
export function Modal({ title, children, actions, wide, onClose, open = true, className = "", ariaLabel }: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<Element | null>(null);
  // onClose 走 ref：调用方常传内联箭头（每次渲染新引用），若把它放进 effect
  // 依赖，SSE/轮询等高频重渲染会让"聚焦面板 + body 锁滚"effect 反复重跑——
  // 每次都把焦点从正在输入的输入框抢回弹窗面板（提问弹窗无法输入的根因）。
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const handleClose = () => onCloseRef.current?.();
    previousFocus.current = document.activeElement;
    openModalCount += 1;
    document.body.style.overflow = "hidden";
    panelRef.current?.focus({ preventScroll: true });
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && onCloseRef.current) {
        event.preventDefault();
        event.stopPropagation();
        handleClose();
        return;
      }
      // 基础焦点陷阱：Tab / Shift+Tab 在面板内可聚焦元素间循环
      if (event.key === "Tab") {
        const panel = panelRef.current;
        if (!panel) return;
        const focusables = Array.from(
          panel.querySelectorAll<HTMLElement>(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
          ),
        ).filter((el) =>
          !el.hasAttribute("disabled")
          && !el.hasAttribute("hidden")
          && el.getAttribute("aria-hidden") !== "true",
        );
        if (focusables.length === 0) return;
        const first = focusables[0]!;
        const last = focusables[focusables.length - 1]!;
        const current = document.activeElement;
        if (event.shiftKey) {
          if (current === first || !panel.contains(current)) {
            event.preventDefault();
            last.focus();
          }
        } else if (current === last || !panel.contains(current)) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown, true);
      openModalCount = Math.max(0, openModalCount - 1);
      if (openModalCount === 0) document.body.style.overflow = "";
      if (previousFocus.current instanceof HTMLElement) previousFocus.current.focus({ preventScroll: true });
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div className="modal-mask" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && onClose) onClose();
    }}>
      <div
        ref={panelRef}
        className={`modal${wide ? " wide" : ""}${className ? ` ${className}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        tabIndex={-1}
      >
        {title ? <h2>{title}</h2> : null}
        {children}
        {actions ? <div className="modal-actions">{actions}</div> : null}
      </div>
    </div>,
    document.body,
  );
}

export interface FormFieldProps {
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  style?: React.CSSProperties;
}

/** Legacy .form-label row: label + control (+ optional hint). */
export function FormField({ label, hint, children, style }: FormFieldProps) {
  return (
    <label className="form-label" style={style}>
      {label}
      {hint ? <div className="dim" style={{ fontSize: 11, margin: "3px 0 7px" }}>{hint}</div> : null}
      {children}
    </label>
  );
}
