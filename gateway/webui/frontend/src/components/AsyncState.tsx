import type { ReactNode } from "react";

export type AsyncViewStatus = "idle" | "loading" | "empty" | "error";

interface AsyncStateProps {
  status: AsyncViewStatus;
  error?: unknown;
  loadingMessage?: string;
  emptyMessage?: string;
  onRetry?: () => void;
  children?: ReactNode;
}

const errorText = (error: unknown): string => {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "请求失败";
};

export function AsyncState({
  status,
  error,
  loadingMessage = "加载中…",
  emptyMessage = "暂无数据",
  onRetry,
  children,
}: AsyncStateProps) {
  if (status === "loading") return <div className="empty" role="status">{loadingMessage}</div>;
  if (status === "empty") return <div className="empty">{emptyMessage}</div>;
  if (status === "error") {
    return (
      <div className="empty" role="alert">
        <div>{errorText(error)}</div>
        {onRetry ? <button type="button" className="btn" onClick={onRetry}>重试</button> : null}
      </div>
    );
  }
  return <>{children}</>;
}
