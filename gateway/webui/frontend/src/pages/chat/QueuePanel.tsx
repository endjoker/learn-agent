// 运行中队列等待面板 —— 主会话 / 工作区会话共用（设计方案 8）。
//
// 展示运行中入队的消息（等待窗口）：位置、文本预览与操作
// （上移/下移/插入当前 Turn/删除）；倒计时仅在 Turn 终态后出现。
// 本地缓存会保留终态队列项（sent/injected/...），这里过滤只展示活动项；
// 活动项为空时整体不渲染（空闲且无排队 → 无面板）。
import type { QueueItem } from "@/gateway/types";
import { TERMINAL_QUEUE_STATUS } from "@/gateway/store";
import { conversationApi } from "@/gateway/api";

export interface QueuePanelProps {
  queue: QueueItem[];
  /** 队首倒计时剩余秒数（0 = 未在倒计时）。 */
  countdown: number;
  /** 对指定等待项插入（统一"插入 = 立即发送"：运行中 Steering 注入当前
   *  Turn；空闲/暂停立即分派，非队首项先上移到队首）。 */
  onInject: (queueItemId: string) => void;
  /** 自动分派被暂停的原因提示（如"上一轮出错，已暂停自动发送"）。 */
  pausedReason?: string | null;
}

export function QueuePanel({ queue, countdown, onInject, pausedReason }: QueuePanelProps) {
  const active = queue.filter((item) => !TERMINAL_QUEUE_STATUS.has(item.status));
  if (active.length === 0) return null;
  return (
    <div className="chat-queue-panel" data-testid="queue-panel">
      <div className="chat-queue-title">
        <span>队列（{active.length}）</span>
        {countdown > 0 && <span className="chat-queue-countdown">{countdown} 秒后发送…</span>}
        {countdown === 0 && pausedReason && <span className="chat-queue-paused">{pausedReason}</span>}
      </div>
      <div className="chat-queue-list">
        {active.map((item) => (
          <div key={item.queue_item_id} className="chat-queue-row" data-testid="queue-row">
            <span className="chat-queue-pos">{item.position}</span>
            <span className="chat-queue-text" title={item.text}>{item.text}</span>
            {item.status === "waiting" && (
              <span className="chat-queue-ops">
                <button title="上移" onClick={() => void conversationApi.moveQueueItem(item.conversation_id, item.queue_item_id, "up")}>↑</button>
                <button title="下移" onClick={() => void conversationApi.moveQueueItem(item.conversation_id, item.queue_item_id, "down")}>↓</button>
                <button title="插入：运行中注入当前 Turn（Steering）；空闲立即发送" onClick={() => onInject(item.queue_item_id)}>插入</button>
                <button title="删除" onClick={() => void conversationApi.deleteQueueItem(item.conversation_id, item.queue_item_id, item.revision)}>删除</button>
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
