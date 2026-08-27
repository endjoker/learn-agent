import { useEffect, useState } from "react";

import type { Goal, Plan } from "@/api/types";
import { confirmDialog } from "@/components/confirm";
import { toast } from "@/components/toast";

export type PlanAction = "pause" | "resume" | "cancel" | "archive" | "approve" | "reject";
export type GoalAction = "pause" | "resume" | "cancel" | "archive";

const PLAN_STATUS_LABELS: Record<string, string> = {
  awaiting_approval: "待确认",
  approved: "已排队",
  active: "执行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  superseded: "已替代",
};

const GOAL_STATUS_LABELS: Record<string, string> = {
  active: "自主运行",
  paused: "已暂停",
  blocked: "需要关注",
  completed: "已完成",
  cancelled: "已取消",
  archived: "已归档",
};

function statusBadgeClass(status: string): string {
  if (["failed", "blocked", "cancelled"].includes(status)) return "high";
  if (["completed", "ok"].includes(status)) return "low";
  return "medium";
}

interface PlanActionButton {
  label: string;
  kind: string;
  action: PlanAction;
  /** 危险操作二次确认（pause/resume/cancel）；approve/reject 直接执行。 */
  confirm?: { message: string; okText: string; cancelText?: string };
}

/** Durable plan card mirroring the legacy HA.PlanCard. */
export function PlanCard({ plan, onAction }: { plan: Plan; onAction: (action: PlanAction, plan: Plan) => unknown }) {
  const status = String(plan.status || "pending");
  const tasks = Array.isArray(plan.tasks) ? plan.tasks : [];
  // 只有真正 COMPLETED 的步骤才算完成；cancelled/failed 等终止态不计入，
  // 避免已取消的 Plan 出现 “0% · 11/11 步” 的自相矛盾显示。
  const done = tasks.filter((t) => String(t.status) === "completed").length;
  const progress = Math.max(0, Math.min(100, Math.round(Number(plan.progress ?? (tasks.length ? done / tasks.length : 0)) * 100)));
  const title = plan.title || plan.objective || plan.plan_id || "执行方案";
  // 宿主折叠值语义（优化方案 #9，与 GoalBar 一致）：请求在途显示目标状态
  // 的否定（点了暂停立即按已暂停渲染），值来自后端投影折叠而非前端乐观
  // 猜测；失败（onAction 返回 false）回滚到真实投影并 toast。
  const [pending, setPending] = useState<PlanAction | null>(null);
  const [failed, setFailed] = useState(false);
  // 目标身份变更重置提交/失败态（#10 同源原则）
  useEffect(() => {
    setPending(null);
    setFailed(false);
  }, [plan.plan_id]);

  const displayStatus =
    pending === "pause" ? "paused"
      : pending === "resume" ? "active"
        : status;

  const actions: PlanActionButton[] = [];
  if (status === "awaiting_approval") {
    // AWAITING_APPROVAL：确认/拒绝（POST 既有 /api/plan/{id}/approve|reject）
    actions.push({ label: "确认", kind: "primary", action: "approve" });
    actions.push({ label: "拒绝", kind: "danger", action: "reject" });
  }
  if (displayStatus === "active") {
    actions.push({ label: "暂停", kind: "", action: "pause", confirm: { message: `暂停执行方案「${title}」？`, okText: "确认暂停" } });
  }
  if (displayStatus === "paused") {
    actions.push({ label: "继续", kind: "primary", action: "resume", confirm: { message: `继续执行方案「${title}」？`, okText: "确认继续" } });
  }
  if (["active", "paused", "approved"].includes(status)) {
    actions.push({ label: "取消", kind: "danger", action: "cancel", confirm: { message: `取消执行方案「${title}」？此操作不可恢复。`, okText: "确认取消", cancelText: "返回" } });
  }
  if (["completed", "failed", "cancelled", "superseded"].includes(status)) actions.push({ label: "隐藏", kind: "", action: "archive" });

  const runAction = async (action: PlanAction) => {
    if (pending !== null) return;
    setPending(action);
    setFailed(false);
    try {
      const ok = await onAction(action, plan);
      if (ok === false) {
        setFailed(true);
        toast("操作失败，已恢复当前状态显示", "err");
      }
    } finally {
      setPending(null);
    }
  };

  const click = async (button: PlanActionButton) => {
    if (button.confirm) {
      const ok = await confirmDialog(button.confirm.message, {
        okText: button.confirm.okText,
        cancelText: button.confirm.cancelText,
      });
      if (!ok) return;
    }
    await runAction(button.action);
  };

  const badgeStatus = failed ? status : displayStatus;

  return (
    <section className={`ws-plan-card runtime-card ${badgeStatus}${failed ? " failed" : ""}`}>
      <div className="runtime-card-head">
        <div className="runtime-card-kicker">PLAN · DURABLE TASK</div>
        <span className={`badge ${statusBadgeClass(badgeStatus)}`}>{PLAN_STATUS_LABELS[badgeStatus] ?? badgeStatus}</span>
      </div>
      <div className="runtime-card-title">{title}</div>
      <div className="runtime-card-meta">{status === "cancelled"
        ? `已取消 · ${tasks.length || 0} 步${plan.plan_id ? ` · ${String(plan.plan_id).slice(0, 12)}` : ""}`
        : `${progress}% · ${done}/${tasks.length || 0} 步${plan.plan_id ? ` · ${String(plan.plan_id).slice(0, 12)}` : ""}`}</div>
      <div className="runtime-progress" aria-label={`Plan progress ${progress}%`}><span style={{ width: `${progress}%` }} /></div>
      {tasks.length ? (
        <ol className="runtime-step-list">
          {tasks.map((task, index) => {
            const taskStatus = String(task.status || "pending");
            const reason = task.blocked_reason && typeof task.blocked_reason === "object"
              ? ((task.blocked_reason as { message?: string; type?: string }).message ?? (task.blocked_reason as { message?: string; type?: string }).type)
              : undefined;
            const detail = String(task.result_summary ?? "") || reason || "";
            return (
              <li key={task.task_id ?? index} className={`runtime-step ${taskStatus}`}>
                <span className="runtime-step-dot">{taskStatus === "completed" ? "✓" : ["failed", "blocked"].includes(taskStatus) ? "!" : "·"}</span>
                <span className="runtime-step-text">
                  <span>{String(task.description ?? "") || String(task.title ?? "") || taskStatus || "步骤"}</span>
                  {detail ? <small className="runtime-step-detail">{detail}</small> : null}
                </span>
              </li>
            );
          })}
        </ol>
      ) : null}
      {actions.length ? (
        <div className="plan-actions">
          {actions.map((button) => (
            <button
              key={button.action}
              type="button"
              className={`btn${button.kind ? ` ${button.kind}` : ""}`}
              disabled={pending !== null}
              onClick={() => void click(button)}
            >
              {button.label}
            </button>
          ))}
        </div>
      ) : null}
    </section>
  );
}

/** Durable goal card mirroring the legacy HA.GoalCard. */
export function GoalCard({ goal, onAction, onMaxRounds }: {
  goal: Goal;
  onAction: (action: GoalAction, goal: Goal) => unknown;
  onMaxRounds?: (goal: Goal, maxRounds: number) => void;
}) {
  const status = String(goal.status || "active");
  const activation = String(goal.activation || "disarmed");
  const rounds = Number(goal.rounds_started ?? 0);
  const max = Number(goal.max_rounds ?? 20);
  const terminal = ["completed", "cancelled", "archived"].includes(status);
  const [roundsDraft, setRoundsDraft] = useState<string>(String(max));
  // 目标身份变更重置本地编辑态（优化方案 #10，借鉴 dsh GoalBar）：goal_id 变化
  // （清除/完成/外部替换）时自动重置草稿为新目标的轮次上限——防止残留草稿的
  // 「更新轮次」提交写到新目标上。
  useEffect(() => {
    setRoundsDraft(String(Number(goal.max_rounds ?? 20)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [goal.goal_id]);
  // 宿主折叠值语义（优化方案 #9）：同 PlanCard——pending pause/resume 折叠
  // 显示目标态，失败回滚 + toast。
  const [pending, setPending] = useState<GoalAction | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setPending(null);
    setFailed(false);
  }, [goal.goal_id]);

  const base = status === "blocked" ? "blocked" : status === "paused" ? "paused" : "active";
  const phase = pending === "pause" ? "paused"
    : pending === "resume" ? "active"
      : base;

  const minRounds = Math.max(1, rounds);
  const actions: Array<{ label: string; kind: string; action: GoalAction }> = [];
  if (phase === "active" && activation === "armed") actions.push({ label: "暂停", kind: "", action: "pause" });
  if (phase !== "active") actions.push({ label: "恢复运行", kind: "primary", action: "resume" });
  if (!terminal) actions.push({ label: "结束 Goal", kind: "danger", action: "cancel" });
  // 任意状态都可直接删除（后端 archive 支持非终态，自动先取消）
  actions.push({ label: terminal ? "清除" : "删除", kind: "", action: "archive" });
  const reason = goal.blocked_reason && typeof goal.blocked_reason === "object"
    ? ((goal.blocked_reason as { message?: string; type?: string }).message ?? (goal.blocked_reason as { message?: string; type?: string }).type)
    : undefined;
  const reasonText = reason ? String(reason) : undefined;

  const runAction = async (action: GoalAction) => {
    if (pending !== null) return;
    setPending(action);
    setFailed(false);
    try {
      const ok = await onAction(action, goal);
      if (ok === false) {
        setFailed(true);
        toast("操作失败，已恢复当前状态显示", "err");
      }
    } finally {
      setPending(null);
    }
  };

  return (
    <section className={`goal-card runtime-card ${phase}${failed ? " failed" : ""}`}>
      <div className="runtime-card-head">
        <div className="runtime-card-kicker">GOAL · AUTONOMOUS LOOP</div>
        <span className={`badge ${statusBadgeClass(phase)}`}>{GOAL_STATUS_LABELS[phase] ?? phase}</span>
      </div>
      <div className="runtime-card-title">{goal.objective || goal.goal_id || "长期目标"}</div>
      <div className="runtime-card-meta">{`${activation === "armed" ? "● 已武装" : "○ 未运行"} · 第 ${rounds}/${max} 轮${goal.current_task_id ? " · 当前轮次执行中" : ""}`}</div>
      {!terminal && onMaxRounds ? (
        <div className="goal-rounds-edit">
          <label>轮次上限
            <input
              type="number"
              min={minRounds}
              max={1000}
              value={roundsDraft}
              onChange={(event) => setRoundsDraft(event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn"
            disabled={Number(roundsDraft) < minRounds || !Number.isFinite(Number(roundsDraft))}
            onClick={() => { const next = Math.floor(Number(roundsDraft)); if (next >= minRounds) onMaxRounds(goal, next); }}
          >更新轮次</button>
        </div>
      ) : null}
      <div className="runtime-progress" aria-label={`Goal rounds ${rounds} of ${max}`}>
        <span style={{ width: `${Math.min(100, Math.round((rounds / Math.max(1, max)) * 100))}%` }} />
      </div>
      {reasonText ? <div className="runtime-card-notice">{reasonText}</div> : null}
      {actions.length ? (
        <div className="plan-actions">
          {actions.map((button) => (
            <button
              key={button.action}
              type="button"
              className={`btn${button.kind ? ` ${button.kind}` : ""}`}
              disabled={pending !== null}
              onClick={() => void runAction(button.action)}
            >
              {button.label}
            </button>
          ))}
        </div>
      ) : null}
    </section>
  );
}
