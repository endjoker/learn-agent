import { useEffect, useRef, useState } from "react";

// Turn 级统一加载指示（优化方案 #5，借鉴 dsh ChatView.TurnStatus）：
// 「任务运行中」的视觉信号收敛到列表尾部单一状态条，骑在整个运行中 turn 上
// ——首 token 等待、工具执行、流式输出期间都只显示这一条，不随过程节点逐个
// 闪烁。计时器锚定 turn 开始时间，运行 ≥15s 才出现（短任务不打扰）；
// 结束后展示总用时 5s 随即消失。

const TIMER_DELAY_MS = 15_000;
const DONE_FLASH_MS = 5000;

const fmtDuration = (ms: number): string => {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}分${s}秒` : `${s}秒`;
};

export function TurnStatus({ busy, turnId, turnStartedAt, resetKey }: {
  busy: boolean;
  /** 当前运行中 turn id（换 turn 重新锚定开始时间）。 */
  turnId?: string | null;
  /** 后端 turn.started_at；缺失时用本地首次观测时间兜底。 */
  turnStartedAt?: string | null;
  /** 会话切换时重置内部状态（避免上一会话的计时/终态闪现串台）。 */
  resetKey?: string | null;
}) {
  // 开始锚点：优先后端 started_at，否则本地首次观测到 busy 的时刻。
  // anchoredTurn 记录锚点归属的 turn——运行中换了新 turn（旧 turn 终态、
  // 新 turn 接棒）必须重新锚定，否则新 turn 继承上一 turn 的已过时长。
  const anchorRef = useRef<number | null>(null);
  const anchoredTurnRef = useRef<string>("");
  const [elapsedText, setElapsedText] = useState("");
  const [doneFlash, setDoneFlash] = useState("");

  // 会话切换：完全复位。
  useEffect(() => {
    anchorRef.current = null;
    anchoredTurnRef.current = "";
    setElapsedText("");
    setDoneFlash("");
  }, [resetKey]);

  // 锚定 + 运行计时：busy 开始（或换 turn）时锚定一次；≥15s 后每秒刷新显示。
  useEffect(() => {
    if (!busy) return undefined;
    const currentTurn = turnId ?? "";
    if (anchorRef.current === null || anchoredTurnRef.current !== currentTurn) {
      const parsed = turnStartedAt ? Date.parse(turnStartedAt) : NaN;
      anchorRef.current = Number.isFinite(parsed) ? parsed : Date.now();
      anchoredTurnRef.current = currentTurn;
      setElapsedText("");
    }
    const anchor = anchorRef.current;
    const tick = () => {
      const elapsed = Date.now() - anchor;
      setElapsedText(elapsed >= TIMER_DELAY_MS ? fmtDuration(elapsed) : "");
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [busy, turnId, turnStartedAt]);

  // 终态：展示总用时 5s（沿用原顶栏计时器的收尾体验，位置收敛到本条）。
  useEffect(() => {
    if (busy) return;
    const anchor = anchorRef.current;
    if (anchor === null) return;
    const total = fmtDuration(Date.now() - anchor);
    anchorRef.current = null;
    anchoredTurnRef.current = "";
    setElapsedText("");
    setDoneFlash(total);
    const id = window.setTimeout(() => setDoneFlash(""), DONE_FLASH_MS);
    return () => window.clearTimeout(id);
  }, [busy]);

  if (busy) {
    return (
      <div className="turn-status live" data-testid="turn-status" role="status">
        <span className="turn-status-dot" aria-hidden />
        <span>任务运行中… 再次点击发送按钮可停止</span>
        {elapsedText ? <span className="turn-status-timer">· {elapsedText}</span> : null}
      </div>
    );
  }
  if (doneFlash) {
    return (
      <div className="turn-status done" data-testid="turn-status" role="status">
        <span>⏱ 用时 {doneFlash}</span>
      </div>
    );
  }
  return null;
}
