import { useEffect, useState } from "react";

import type { Goal } from "@/api/types";
import { toast } from "@/components/toast";

// 常驻目标条（优化方案 #8，借鉴 dsh ui-goal/GoalBar）：目标不是只活在聊天流
// 里的卡片，而是停靠在输入框上方的常驻条——phase 标签 + 截断目标文本 + 行内
// 快捷操作；完成/无目标渲染为空不占空间。目标创建仍走 /goal 命令，条内只做
// 状态展示与快捷操作，详情跳 GoalPage。

export type GoalBarAction = "pause" | "resume" | "archive";

const PHASE_LABELS: Record<string, string> = {
  active: "● 自主运行",
  paused: "⏸ 已暂停",
  blocked: "⚠ 需要关注",
};

/** 终态目标不在条内展示（RuntimeFloat 卡片保留终态详情）。 */
const TERMINAL = new Set(["completed", "cancelled", "archived"]);

export function GoalBar({ goal, onAction }: {
  goal: Goal | null;
  onAction: (action: GoalBarAction, goal: Goal) => Promise<boolean>;
}) {
  const [pending, setPending] = useState<GoalBarAction | null>(null);
  const [failed, setFailed] = useState(false);

  // 目标身份变更重置提交/错误态（优化方案 #10 同源原则）：清除/完成/外部替换
  // 后残留的 pending 不应作用到新目标。
  const goalId = goal?.goal_id ?? null;
  useEffect(() => {
    setPending(null);
    setFailed(false);
  }, [goalId]);

  if (!goal) return null;
  const status = String(goal.status || "active");
  if (TERMINAL.has(status)) return null;

  // 宿主折叠值语义（优化方案 #9）：请求进行中显示**目标状态**的否定——
  // 点了暂停立即按已暂停渲染，但该值是后端投影 (status) 与在途动作的折叠，
  // 而非前端本地翻转的乐观猜测；失败时回滚到真实投影并 toast。
  // 注意：在途动作必须覆盖后端投影（否则 resume 在途仍显示 paused，折叠失效）。
  const base = status === "blocked" ? "blocked" : status === "paused" ? "paused" : "active";
  const phase = pending === "pause" ? "paused"
    : pending === "resume" ? "active"
      : base;

  const run = async (action: GoalBarAction) => {
    if (pending !== null) return;
    setPending(action);
    setFailed(false);
    try {
      const ok = await onAction(action, goal);
      if (!ok) {
        setFailed(true);
        toast("目标操作失败，已恢复当前状态显示", "err");
      }
    } finally {
      setPending(null);
    }
  };

  const rounds = Number(goal.rounds_started ?? 0);
  const max = Number(goal.max_rounds ?? 20);
  const objective = String(goal.objective || goal.goal_id || "");
  const blockedReason = phase === "blocked" && goal.blocked_reason && typeof goal.blocked_reason === "object"
    ? String((goal.blocked_reason as { message?: string; type?: string }).message ?? (goal.blocked_reason as { message?: string; type?: string }).type ?? "")
    : "";

  return (
    <div className={`goal-bar ${phase}${failed ? " failed" : ""}`} data-testid="goal-bar" role="status">
      <span className="goal-bar-phase">{PHASE_LABELS[phase] ?? phase}</span>
      <span className="goal-bar-text" title={objective}>{objective}</span>
      {blockedReason ? <span className="goal-bar-reason" title={blockedReason}>{blockedReason}</span> : null}
      <span className="goal-bar-meta">第 {rounds}/{max} 轮</span>
      <span className="goal-bar-actions">
        {/* 在途时按钮整体禁用；pause/resume 在途即按目标态渲染（对面按钮出现并禁用） */}
        {phase === "active"
          ? <button type="button" className="btn" disabled={pending !== null} onClick={() => void run("pause")}>暂停</button>
          : <button type="button" className="btn primary" disabled={pending !== null} onClick={() => void run("resume")}>恢复运行</button>}
        <button
          type="button"
          className="btn"
          disabled={pending !== null}
          onClick={() => void run("archive")}
        >
          {pending === "archive" ? "清除中…" : "清除"}
        </button>
      </span>
    </div>
  );
}
