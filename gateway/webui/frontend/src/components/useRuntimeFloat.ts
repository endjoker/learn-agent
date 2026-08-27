import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import type { Goal, Plan } from "@/api/types";
import { eventMatchesScope, type ParsedSseEvent } from "@/sse/events";

// 已完成/失败的计划在当前页面收到终态事件后保留展示，直到用户发送新消息；
// 刷新或切换页面时只恢复仍在运行的 Plan。
const CLEAR_PLAN = new Set(["cancelled", "superseded", "archived"]);
const TERMINAL_PLAN = new Set(["completed", "failed", "cancelled", "superseded", "archived"]);
// Goal 同理：当前页面保留终态展示，刷新或切换页面时不恢复已完成/已取消的 Goal。
const CLEAR_GOAL = new Set(["archived"]);
const TERMINAL_GOAL = new Set(["completed", "cancelled", "archived"]);

export interface RuntimeFloatState {
  plan: Plan | null;
  goal: Goal | null;
  onSse: (event: ParsedSseEvent) => void;
  /** 提交运行时动作；返回是否成功（失败由调用方回滚 UI 并提示）。 */
  action: (kind: "plan" | "goal", actionName: string, id: string) => Promise<boolean>;
  /** 需求：输入新消息后，旧的终态 plan/goal 状态框应消失。发送新消息时调用。 */
  dismissStale: () => void;
}

/** 输入框上方的 Plan/Goal 浮动卡片状态，仅由当前会话的运行时生命周期事件更新。 */
export function useRuntimeFloat(
  client: ApiClient = defaultApi,
  scope?: { sessionKey?: string },
): RuntimeFloatState {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [goal, setGoal] = useState<Goal | null>(null);
  const sessionKey = scope?.sessionKey ?? "";
  const loadGeneration = useRef(0);
  const eventGeneration = useRef(0);

  // 初始加载：刷新/切换页面后立即恢复当前会话的活跃 plan/goal，
  // 不再干等下一次 plan.changed / goal.changed 事件。
  useEffect(() => {
    if (!sessionKey) {
      setPlan(null);
      setGoal(null);
      return;
    }
    let active = true;
    const generation = ++loadGeneration.current;
    const eventGenerationAtStart = eventGeneration.current;
    void (async () => {
      try {
        const [plansData, goalsData] = await Promise.all([
          client.get<{ plans?: Plan[] }>(`/api/plans?session_key=${encodeURIComponent(sessionKey)}`, { silent: true }),
          client.get<{ goals?: Goal[] }>(`/api/goals?session_key=${encodeURIComponent(sessionKey)}`, { silent: true }),
        ]);
        if (!active || generation !== loadGeneration.current || eventGenerationAtStart !== eventGeneration.current) return;
        // 只恢复仍处于运行生命周期的 Plan；已完成/失败等终态不恢复。
        const plans = plansData.plans ?? [];
        const activePlan = plans.find((p) => !TERMINAL_PLAN.has(String(p.status))) ?? null;
        const goals = goalsData.goals ?? [];
        // Goal 同理：刷新或切换页面只恢复 active/paused/blocked，终态不恢复。
        const activeGoal = goals.find((g) => !TERMINAL_GOAL.has(String(g.status))) ?? null;
        setPlan(activePlan);
        setGoal(activeGoal);
      } catch {
        if (active && generation === loadGeneration.current && eventGenerationAtStart === eventGeneration.current) {
          setPlan(null);
          setGoal(null);
        }
      }
    })();
    return () => { active = false; };
  }, [client, sessionKey]);

  const onSse = useCallback((event: ParsedSseEvent) => {
    if (!eventMatchesScope(event.data, { sessionKey })) return;
    eventGeneration.current += 1;
    const actionName = String((event.data as { action?: string }).action ?? "");
    // archive 动作删除后端记录，卡片应直接移除（status 不会变成 "archived"）。
    if (actionName === "archived") {
      if (event.type === "plan.changed") setPlan(null);
      else if (event.type === "goal.changed") setGoal(null);
      return;
    }
    if (event.type === "plan.changed") {
      const next = (event.data as { plan?: Plan }).plan;
      if (!next) return;
      // 已完成/失败保留展示终态；仅被取消/取代/归档时清除。
      setPlan(CLEAR_PLAN.has(String(next.status)) ? null : next);
      return;
    }
    if (event.type === "goal.changed") {
      const next = (event.data as { goal?: Goal }).goal;
      if (!next) return;
      // 已完成/取消的 Goal 保留展示终态；仅归档时清除。
      setGoal(CLEAR_GOAL.has(String(next.status)) ? null : next);
      return;
    }
  }, [sessionKey]);

  const action = useCallback(async (kind: "plan" | "goal", actionName: string, id: string): Promise<boolean> => {
    try {
      const data = await client.post<{ plan?: Plan; goal?: Goal }>(
        `/api/${kind === "plan" ? "plans" : "goals"}/${encodeURIComponent(id)}/${actionName}`,
      );
      // 「隐藏/清除」(archive) 会删除后端记录但返回的 status 仍是删除前的
      // 终态（completed/cancelled），并非 "archived"，因此不能靠 status 判断。
      // archive 动作直接移除卡片。
      if (actionName === "archive") {
        if (kind === "plan") setPlan(null);
        else setGoal(null);
        return true;
      }
      if (kind === "plan") {
        if (data.plan) setPlan(CLEAR_PLAN.has(String(data.plan.status)) ? null : data.plan);
      } else if (data.goal) {
        setGoal(CLEAR_GOAL.has(String(data.goal.status)) ? null : data.goal);
      }
      return true;
    } catch {
      /* 静默：由调用方按返回值回滚 UI（宿主折叠值语义 #9） */
      return false;
    }
  }, [client]);

  const dismissStale = useCallback(() => {
    // 只清终态（已完成/失败等）；运行中的 plan/goal 保留。
    setPlan((p) => (p && TERMINAL_PLAN.has(String(p.status)) ? null : p));
    setGoal((g) => (g && TERMINAL_GOAL.has(String(g.status)) ? null : g));
  }, []);

  return { plan, goal, onSse, action, dismissStale };
}
