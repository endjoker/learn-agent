// 运行中队列分派 hook —— 主会话 / 工作区会话共用（设计方案 8.3/8.4/9）。
//
// 职责：
// - 订阅当前会话的队列（selectQueue）与活动 Turn；
// - 记录上一 Turn 的终态类型（error/interrupted/空回复 → 暂停自动分派）；
// - Turn 终态后队首 5 秒倒计时，到点自动 sendNext（设计方案 8.4）；
// - 队列项 Steering 注入（prepare：打断输出，工具自然结束后注入续跑，9.1/9.2）。
import { useCallback, useEffect, useRef, useState } from "react";

import { conversationApi } from "@/gateway/api";
import { gatewayStore, selectLiveTurn, selectQueue, useGatewaySelector } from "@/gateway/store";
import type { QueueItem, Turn } from "@/gateway/types";
import { toast } from "@/components/toast";

const TERMINAL = new Set(["done", "stopped", "error", "interrupted"]);
export const SEND_COUNTDOWN_SECONDS = 5;

export interface QueueDispatch {
  queue: QueueItem[];
  /** 队首倒计时剩余秒数（0 = 未在倒计时）。 */
  countdown: number;
  /** 会话运行中且队列有待插项（输入框旁"插入提示"快捷键可用性）。 */
  steeringAvailable: boolean;
  /** 统一"插入 = 立即发送"（用户验收 2026-08-27）：运行中 → Steering 注入
   *  当前 Turn；空闲/暂停（无活动 Turn）→ 立即分派（非队首项先上移到队首，
   *  保证"点哪条发哪条"）。 */
  injectQueueItem: (queueItemId: string) => Promise<void>;
  /** 取队首等待项注入（输入框旁快捷键）。 */
  insertSteeringHint: () => Promise<void>;
  /** 自动分派被暂停的原因（error/interrupted/空回复；null = 未暂停）。 */
  pausedReason: string | null;
}

export function useQueueDispatch(convId: string | null): QueueDispatch {
  const liveTurn = useGatewaySelector(selectLiveTurn(convId ?? ""));
  const queue = useGatewaySelector(selectQueue(convId ?? ""));
  const [countdown, setCountdown] = useState(0);
  const countdownTimerRef = useRef<number | null>(null);

  // 终态分派依据：上一 Turn 的终态类型 + 是否空回复
  //（error/interrupted 或 done 空回复 → 暂停，不自动发送，设计方案 8.4）。
  const lastTerminalRef = useRef<string | null>(null);
  const lastTurnEmptyRef = useRef(false);
  const prevLiveTurnRef = useRef<Turn | null | undefined>(undefined);

  useEffect(() => {
    const prev = prevLiveTurnRef.current;
    prevLiveTurnRef.current = liveTurn;
    if (prev && !liveTurn && prev.conversation_id === convId) {
      lastTerminalRef.current = prev.status;
      const state = gatewayStore.getState();
      const turnNodes = Object.values(state.nodesById)
        .filter((n) => n.turn_id === prev.turn_id && n.type === "assistant");
      lastTurnEmptyRef.current = !(
        prev.final_assistant_node_id
        || turnNodes.some((n) => (n.text ?? "").trim().length > 0)
      );
    }
  }, [liveTurn, convId]);

  const dispatchNext = useCallback(async () => {
    if (!convId) return;
    const res = await conversationApi.sendNext(convId);
    if (!res.ok) toast(res.error?.message ?? "发送队列消息失败", "err");
  }, [convId]);

  // 队首 5 秒倒计时（Turn 终态后仍有等待项时自动分派，设计方案 8.4）
  const hasWaiting = queue.some((q) => q.status === "waiting");
  useEffect(() => {
    if (countdownTimerRef.current !== null) window.clearInterval(countdownTimerRef.current);
    setCountdown(0);
    const idle = liveTurn == null;
    if (!idle || !hasWaiting || !convId) return undefined;
    const last = lastTerminalRef.current;
    if (last === "error" || last === "interrupted") return undefined;
    if (last === "done" && lastTurnEmptyRef.current) return undefined;
    const startedAt = Date.now();
    countdownTimerRef.current = window.setInterval(() => {
      const remaining = Math.ceil(SEND_COUNTDOWN_SECONDS - (Date.now() - startedAt) / 1000);
      if (remaining <= 0) {
        if (countdownTimerRef.current !== null) window.clearInterval(countdownTimerRef.current);
        setCountdown(0);
        void dispatchNext();
      } else {
        setCountdown(remaining);
      }
    }, 200);
    return () => {
      if (countdownTimerRef.current !== null) window.clearInterval(countdownTimerRef.current);
    };
  }, [liveTurn, queue, convId, dispatchNext]);

  // 自动分派暂停原因（error/interrupted/空回复 → 8.4 暂停）。QueuePanel 据此
  // 提示；此时「插入」自动退化为立即分派（见 injectQueueItem）——否则空闲
  // 会话上 Steering 会被后端拒绝（"当前没有活动 Turn"），队列永久卡死。
  let pausedReason: string | null = null;
  if (liveTurn == null && hasWaiting) {
    const last = lastTerminalRef.current;
    if (last === "error" || last === "interrupted") {
      pausedReason = "上一轮出错/已中断，已暂停自动发送";
    } else if (last === "done" && lastTurnEmptyRef.current) {
      pausedReason = "上一轮为空回复，已暂停自动发送";
    }
  }

  const steeringAvailable = liveTurn != null && !TERMINAL.has(liveTurn.status)
    && queue.some((q) => q.status === "waiting");

  const injectQueueItem = useCallback(async (queueItemId: string) => {
    if (!convId) return;
    // 统一「插入 = 立即发送」语义（用户验收 2026-08-27）：
    // - 运行中：只做 prepare——后端打断模型输出，工具自然结束后自动注入
    //   并续跑（9.1/9.2）。控制租约已废弃：持有者校验由执行域并发上限/
    //   exec_lock 承担。
    // - 空闲/暂停（无活动 Turn）：Steering 会被后端拒绝（"当前没有活动
    //   Turn"）→ 退化为立即分派；点击非队首项时先上移到队首，保证
    //   "点哪条发哪条"。这也是暂停态的手动逃生口（原独立「立即发送」
    //   按钮已并入此路径）。
    if (!steeringAvailable) {
      const state = gatewayStore.getState();
      const waiting = selectQueue(convId)(state).filter((q) => q.status === "waiting");
      const idx = waiting.findIndex((q) => q.queue_item_id === queueItemId);
      for (let i = 0; idx > 0 && i < idx; i++) {
        try {
          await conversationApi.moveQueueItem(convId, queueItemId, "up");
        } catch { break; } // 移动失败不阻断分派（退化：发当前队首）
      }
      await dispatchNext();
      return;
    }
    const result = await conversationApi.prepareSteering(convId, [queueItemId]);
    if (result.ok) toast("已插入队列消息（Steering）", "ok");
    else toast(result.error?.message ?? "插入失败", "err");
  }, [convId, steeringAvailable, queue, dispatchNext]);

  const insertSteeringHint = useCallback(async () => {
    const waiting = queue.find((q) => q.status === "waiting");
    if (!waiting) { toast("队列中没有待插入的消息", "err"); return; }
    await injectQueueItem(waiting.queue_item_id);
  }, [queue, injectQueueItem]);

  return { queue, countdown, steeringAvailable, injectQueueItem, insertSteeringHint, pausedReason };
}
