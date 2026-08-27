import { useEffect, useState } from "react";
import { createRoot, type Root } from "react-dom/client";

/**
 * Promise-based confirmation dialog replacing the legacy HA.confirm() modal.
 * The same dangerous-operation semantics are preserved: cancel resolves
 * false, the labelled primary button resolves true.
 *
 * Concurrent calls are queued: only one dialog is shown at a time; closing
 * it resolves the current promise and reveals the next request.
 */

interface ConfirmRequest {
  message: string;
  okText: string;
  cancelText: string;
  resolve: (value: boolean) => void;
}

const queue: ConfirmRequest[] = [];
let active: ConfirmRequest | null = null;
const listeners = new Set<() => void>();
let container: HTMLDivElement | null = null;
let root: Root | null = null;

function ConfirmDialog() {
  const [, force] = useState(0);
  useEffect(() => {
    const listener = () => force((n) => n + 1);
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  }, []);
  if (!active) return null;
  const request = active;
  const close = (value: boolean) => {
    queue.shift();
    active = queue[0] ?? null;
    listeners.forEach((l) => l());
    request.resolve(value);
  };
  return (
    <div className="modal-mask">
      <div className="modal" role="dialog" aria-modal="true">
        <div className="md">{request.message}</div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={() => close(false)}>{request.cancelText}</button>
          <button type="button" className="btn primary" onClick={() => close(true)}>{request.okText}</button>
        </div>
      </div>
    </div>
  );
}

function ensureHost() {
  if (!container) {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    root.render(<ConfirmDialog />);
  }
}

export interface ConfirmOptions {
  okText?: string;
  cancelText?: string;
}

export const confirmDialog = (message: string, options: ConfirmOptions = {}): Promise<boolean> =>
  new Promise((resolve) => {
    queue.push({
      message,
      okText: options.okText ?? "确定",
      cancelText: options.cancelText ?? "取消",
      resolve,
    });
    if (active === null) active = queue[0] ?? null;
    listeners.forEach((l) => l());
    ensureHost();
  });
