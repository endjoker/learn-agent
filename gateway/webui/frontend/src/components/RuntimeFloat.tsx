import type { Goal, Plan } from "@/api/types";

export type FloatAction = "pause" | "resume" | "cancel" | "archive";

const PLAN_STATUS_LABELS: Record<string, string> = {
  awaiting_approval: "待确认", approved: "已排队", active: "执行中", paused: "已暂停",
  completed: "已完成", failed: "失败", cancelled: "已取消", superseded: "已替代",
};

const GOAL_STATUS_LABELS: Record<string, string> = {
  active: "自主运行", paused: "已暂停", blocked: "需要关注",
  completed: "已完成", cancelled: "已取消", archived: "已归档",
};

function badgeClass(status: string): string {
  if (["failed", "blocked", "cancelled"].includes(status)) return "err";
  if (["completed", "ok"].includes(status)) return "ok";
  return "warn";
}

const taskTitle = (task: Record<string, unknown>): string => {
  const value = String(task.description ?? task.title ?? task.task_id ?? "");
  return value || "步骤";
};

/** 正在执行的那一步：第一个尚未 completed 的任务。 */
const currentStepOf = (plan: Plan): string => {
  const tasks = Array.isArray(plan.tasks) ? plan.tasks : [];
  const running = tasks.find((task) => String(task.status) !== "completed")
    ?? tasks[tasks.length - 1];
  return running ? taskTitle(running) : "";
};

function PlanFloat({ plan, onAction }: { plan: Plan; onAction: (action: FloatAction, plan: Plan) => void }) {
  const status = String(plan.status || "pending");
  const tasks = Array.isArray(plan.tasks) ? plan.tasks : [];
  const done = tasks.filter((task) => String(task.status) === "completed").length;
  // progress > 1 视为百分比（后端可能直接给 0-100），否则按 0-1 比例换算
  const rawProgress = Number(plan.progress ?? (tasks.length ? done / tasks.length : 0));
  const progress = Math.max(0, Math.min(100, Math.round(rawProgress > 1 ? rawProgress : rawProgress * 100)));
  const current = currentStepOf(plan);
  const running = !["completed", "failed", "cancelled", "superseded"].includes(status);
  const actions: Array<{ label: string; kind: string; action: FloatAction }> = [];
  if (status === "active") actions.push({ label: "暂停", kind: "", action: "pause" });
  if (status === "paused") actions.push({ label: "继续", kind: "primary", action: "resume" });
  if (["active", "paused", "approved"].includes(status)) actions.push({ label: "取消", kind: "danger", action: "cancel" });
  if (["completed", "failed", "cancelled", "superseded"].includes(status)) actions.push({ label: "隐藏", kind: "", action: "archive" });

  return (
    <details className={`runtime-float-card plan${running ? " live" : ""}`} open={!running}>
      <summary className="runtime-float-head">
        <span className="runtime-float-ico">📋</span>
        <span className="runtime-float-title">{running && current ? `正在执行：${current}` : (plan.title || plan.objective || "执行方案")}</span>
        <span className={`badge ${badgeClass(status)}`}>{PLAN_STATUS_LABELS[status] ?? status}</span>
        <span className="runtime-float-meta">{progress}% · {done}/{tasks.length || 0} 步</span>
        <span className="runtime-float-caret">▸</span>
      </summary>
      <div className="runtime-float-body">
        <div className="runtime-float-progress" aria-label={`Plan progress ${progress}%`}><span style={{ width: `${progress}%` }} /></div>
        {tasks.length ? (
          <ol className="runtime-float-steps">
            {tasks.map((task, index) => {
              const taskStatus = String(task.status || "pending");
              const reason = task.blocked_reason && typeof task.blocked_reason === "object"
                ? String(((task.blocked_reason as { message?: string; type?: string }).message ?? (task.blocked_reason as { message?: string; type?: string }).type) ?? "")
                : "";
              const detail = String(task.result_summary ?? "") || reason;
              return (
                <li key={String(task.task_id ?? index)} className={`runtime-float-step ${taskStatus}`}>
                  <span className="runtime-float-dot">{taskStatus === "completed" ? "✓" : ["failed", "blocked"].includes(taskStatus) ? "!" : "·"}</span>
                  <span className="runtime-float-step-text">
                    <span>{taskTitle(task)}</span>
                    {detail ? <small>{detail}</small> : null}
                  </span>
                </li>
              );
            })}
          </ol>
        ) : null}
        {actions.length ? (
          <div className="runtime-float-actions">
            {actions.map((button) => (
              <button key={button.action} type="button" className={`btn${button.kind ? ` ${button.kind}` : ""}`} onClick={() => onAction(button.action, plan)}>{button.label}</button>
            ))}
          </div>
        ) : null}
      </div>
    </details>
  );
}

function GoalFloat({ goal, onAction }: { goal: Goal; onAction: (action: FloatAction, goal: Goal) => void }) {
  const status = String(goal.status || "active");
  const activation = String(goal.activation || "disarmed");
  const rounds = Number(goal.rounds_started ?? 0);
  const max = Number(goal.max_rounds ?? 20);
  const terminal = ["completed", "cancelled", "archived"].includes(status);
  const isLive = !terminal && status === "active";
  const actions: Array<{ label: string; kind: string; action: FloatAction }> = [];
  if (status === "active" && activation === "armed") actions.push({ label: "暂停", kind: "", action: "pause" });
  if (["paused", "blocked"].includes(status)) actions.push({ label: "恢复运行", kind: "primary", action: "resume" });
  if (!terminal) actions.push({ label: "结束 Goal", kind: "danger", action: "cancel" });
  if (terminal) actions.push({ label: "清除", kind: "", action: "archive" });
  const reason = goal.blocked_reason && typeof goal.blocked_reason === "object"
    ? String(((goal.blocked_reason as { message?: string; type?: string }).message ?? (goal.blocked_reason as { message?: string; type?: string }).type) ?? "")
    : "";

  return (
    <div className={`runtime-float-card goal${isLive ? " live" : ""}`}>
      <div className="runtime-float-head">
        <span className="runtime-float-ico">🎯</span>
        <span className="runtime-float-title">{goal.objective || goal.title || goal.goal_id || "长期目标"}</span>
        <span className={`badge ${badgeClass(status)}`}>{GOAL_STATUS_LABELS[status] ?? status}</span>
        <span className="runtime-float-meta">{activation === "armed" ? "已武装" : "未运行"} · 第 {rounds}/{max} 轮</span>
      </div>
      {reason ? <div className="runtime-float-reason">{reason}</div> : null}
      {actions.length ? (
        <div className="runtime-float-actions">
          {actions.map((button) => (
            <button key={button.action} type="button" className={`btn${button.kind ? ` ${button.kind}` : ""}`} onClick={() => onAction(button.action, goal)}>{button.label}</button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function RuntimeFloat({
  plan,
  goal,
  onPlanAction,
  onGoalAction,
}: {
  plan: Plan | null;
  goal: Goal | null;
  onPlanAction: (action: FloatAction, plan: Plan) => void;
  onGoalAction: (action: FloatAction, goal: Goal) => void;
}) {
  if (!plan && !goal) return null;
  return (
    <div className="runtime-float" role="region" aria-label="运行任务">
      {plan ? <PlanFloat plan={plan} onAction={onPlanAction} /> : null}
      {goal ? <GoalFloat goal={goal} onAction={onGoalAction} /> : null}
    </div>
  );
}
