import type { Approval } from "@/api/types";
import type { ApprovalAnswer } from "@/approvals/useApprovalQueue";

const text = (value: unknown): string => typeof value === "string" ? value : "";

export function ApprovalHost({
  approvals,
  submittingId,
  error,
  onAnswer,
}: {
  approvals: Approval[];
  submittingId?: string;
  error?: string;
  onAnswer: (approval: Approval, answer: ApprovalAnswer) => void;
}) {
  if (approvals.length === 0) return null;
  return (
    <div className="approval-host" aria-label="待审批操作">
      {approvals.map((approval) => {
        const id = text(approval.id) || text(approval.approval_id);
        const busy = submittingId === id;
        return (
          <section key={id} className="approval-card" data-aid={id} aria-label={`审批：${text(approval.tool) || text(approval.tool_name) || "工具调用"}`}>
            <div className="approval-title">❓ 需要确认: {text(approval.tool) || text(approval.tool_name) || "工具调用"}</div>
            <pre className="approval-params">{text(approval.params_preview) || text(approval.prompt)}</pre>
            {error && busy ? <div className="error-box" role="alert">{error}</div> : null}
            <div className="approval-actions">
              <button type="button" className="btn primary" disabled={busy} onClick={() => onAnswer(approval, "y")}>允许一次</button>
              <button type="button" className="btn danger" disabled={busy} onClick={() => onAnswer(approval, "n")}>拒绝</button>
              <button type="button" className="btn" disabled={busy} onClick={() => onAnswer(approval, "a")}>区内全放</button>
              <button type="button" className="btn" disabled={busy} onClick={() => onAnswer(approval, "s")}>跳过</button>
            </div>
          </section>
        );
      })}
    </div>
  );
}
