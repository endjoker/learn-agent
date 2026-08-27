/**
 * Toast notifications mirroring the legacy HA.toast() helper: append a
 * .toast node to #toast-root (falling back to <body>) and auto-remove it
 * after 4 seconds.
 */
let toastRoot: HTMLElement | null = null;

export type ToastKind = "ok" | "err" | "";

export function toast(message: string, kind: ToastKind = "") {
  if (!toastRoot) toastRoot = document.getElementById("toast-root") ?? document.body;
  const el = document.createElement("div");
  el.className = `toast${kind ? ` ${kind}` : ""}`;
  el.textContent = message;
  toastRoot.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}
