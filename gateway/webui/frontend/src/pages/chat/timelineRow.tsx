import { memo, useEffect, useState } from "react";

import type { Message } from "@/api/types";
import { Markdown } from "@/components/Markdown";
import { ReasoningCard } from "@/components/ReasoningCard";
import { conversationApi } from "@/gateway/api";
import { LargeResult } from "@/pages/chat/LargeResult";
import { ProjectionCard } from "@/pages/chat/ProjectionCard";
import { isUiConfigFeedback } from "@/pages/chat/chatTimeline";
import type { TimelineItem } from "@/pages/chat/timeline";
import { ToolRowView } from "@/pages/chat/toolviews";

// 主会话与工作区会话共用的 TimelineRow（设计方案：卡片组件单一来源）。
// 之前工作区自绘一套 ws-tool-collapsible / ws-reasoning-card / ws-msg，
// 与主会话的 tool-card / reasoning-card / bubble 视觉不一致；这里统一。

const textOf = (message: Message) =>
  String(message.content_text ?? (typeof message.content === "string" ? message.content : ""));

// 中间输出条卡：无 final 标记的 assistant 消息（多轮中的过程性回复）。
// 不用大 Markdown 气泡，也不折叠——单行条卡：💬 回复 · 首行截断预览
// （对齐 Think 卡的视觉格式），过程性内容不抢占时间线注意力。
function AssistantFragment({ text }: { text: string }) {
  const firstLine = text.split("\n").find((line) => line.trim() !== "") ?? "";
  const preview = firstLine.trim().slice(0, 90) || "（中间输出）";
  return (
    <div className="assistant-strip">
      <span className="assistant-strip-ico">💬</span>
      <span className="assistant-strip-label">回复</span>
      <span className="assistant-strip-preview">{preview}</span>
    </div>
  );
}

export function MessageBubble({ item }: { item: TimelineItem & { kind: "message" } }) {
  const message = item.message;
  const role = message.role === "user" ? "user" : message.role === "assistant" ? "assistant" : "system";
  const text = textOf(message);
  const kind = (message as { kind?: string }).kind;
  const copyText = async (value: string, button: HTMLElement) => {
    try {
      await navigator.clipboard.writeText(value);
      const original = button.textContent ?? "";
      button.textContent = "已复制";
      window.setTimeout(() => { button.textContent = original; }, 1200);
    } catch { /* silent */ }
  };
  if (role === "assistant" && kind === "intermediate") {
    return <AssistantFragment text={text} />;
  }
  return (
    <article className={`bubble ${role}`}>
      <div className="bubble-role">{role === "user" ? "👤 我" : role === "assistant" ? "🤖 助手" : "ℹ️"}</div>
      {role === "user"
        ? <div className="bubble-text">{text}</div>
        : <Markdown text={text} className="bubble-text md" isFinal={kind === "final"} />}
      {role !== "system" ? (
        <div className="answer-actions">
          <button type="button" className="answer-action" onClick={(event) => void copyText(text, event.currentTarget)}>复制</button>
        </div>
      ) : null}
    </article>
  );
}

function ToolCard({ item, sessionKey, convId, turnId }: {
  item: TimelineItem & { kind: "tool" };
  sessionKey: string;
  convId: string;
  turnId: string;
}) {
  const icon = item.pending ? "⏳" : item.isError ? "❌" : "✅";
  const collapsedSummary = item.summary ?? (item.pending ? "执行中…" : "");
  // 设计方案 17：大结果 result_ref → 展开时按需读取完整内容
  const [fullResult, setFullResult] = useState<string | null>(null);
  const [loadingRef, setLoadingRef] = useState(false);
  const loadFullResult = async () => {
    if (!item.resultRef || fullResult !== null || loadingRef) return;
    setLoadingRef(true);
    try {
      const result = await conversationApi.result(convId, turnId, item.resultRef);
      if (result.ok && result.data) {
        const summary = (result.data.result as { summary?: Record<string, unknown> } | undefined)?.summary ?? {};
        const head = typeof summary.head === "string" ? summary.head : "";
        const tail = typeof summary.tail === "string" ? summary.tail : "";
        const size = typeof summary.size_bytes === "number" ? summary.size_bytes : 0;
        setFullResult(head + (tail ? `\n\n…（已截断，原始 ${size.toLocaleString()} bytes）\n\n${tail}` : ""));
      } else {
        setFullResult("（完整结果读取失败，仅显示摘要）");
      }
    } catch {
      setFullResult("（完整结果读取失败，仅显示摘要）");
    } finally {
      setLoadingRef(false);
    }
  };
  return (
    <details className={`tool-card${item.isError ? " error" : ""}${item.pending ? " running" : ""}`} open={item.pending}>
      <summary className="tool-card-head">
        <span className="tool-ico">{icon}</span>
        <span className="tool-name">{item.name}</span>
        {/* 专属行卡（#12）：bash/read/edit 等注册工具渲染结构化摘要，其余回退 summary */}
        <ToolRowView name={item.name} input={item.input} summary={collapsedSummary} pending={Boolean(item.pending)} />
        <span className="tool-caret">▸</span>
      </summary>
      <div className="tool-card-body">
        {item.orphaned ? <div className="dim">加载更早历史以查看完整调用上下文</div> : null}
        {item.isError && item.error ? <div className="tool-error" role="alert">{item.error}</div> : null}
        <div className="tool-io">
          <div className="k">输入</div>
          <LargeResult cacheKey={`${sessionKey}:${item.key}:input`} value={item.input} />
        </div>
        <div className="tool-io">
          <div className="k">返回</div>
          {item.pending
            ? <div className="dim">执行中…</div>
            : (
              <>
                <LargeResult cacheKey={`${sessionKey}:${item.key}:result`} value={fullResult ?? item.result} />
                {item.resultRef && (
                  <div className="tool-io-actions">
                    {fullResult === null
                      ? <button className="btn" type="button" onClick={() => void loadFullResult()} disabled={loadingRef}>
                          {loadingRef ? "读取中…" : "读取完整结果"}
                        </button>
                      : <button className="btn" type="button" onClick={() => setFullResult(null)}>显示摘要</button>}
                  </div>
                )}
              </>
            )}
        </div>
      </div>
    </details>
  );
}

// TTFT 占位卡（多会话审计问题 1）：发送成功到首个 node.delta 之间时间线零反馈，
// 与慢 TTFT 叠加产生"卡死感"。spinner + "正在思考…" + 逐秒计时；首个节点到达后
// 该行被 live 思考卡原子替换（displayed 派生层控制，见两页 useMemo）。
function ThinkingCard({ startedAt }: { startedAt: number }) {
  const [elapsed, setElapsed] = useState(() => Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
  useEffect(() => {
    const id = window.setInterval(() => {
      setElapsed(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    }, 1000);
    return () => window.clearInterval(id);
  }, [startedAt]);
  return (
    <div className="turn-thinking" data-testid="turn-thinking" role="status">
      <span className="turn-thinking-spinner" aria-hidden />
      <span>正在思考…</span>
      {elapsed > 0 ? <span className="turn-thinking-timer">· {elapsed}秒</span> : null}
    </div>
  );
}

// 节点级 memo（设计方案 20.3：流式 delta 仅重渲染变化节点）。
// 浅比较 props：item 引用稳定时跳过整行重渲染（store 选择器已保证引用稳定）。
export const TimelineRow = memo(function TimelineRow({ item, sessionKey, convId }: {
  item: TimelineItem;
  sessionKey: string;
  convId: string;
}) {
  if (item.kind === "thinking") return <ThinkingCard startedAt={item.startedAt} />;
  if (item.kind === "reasoning") return <ReasoningCard text={item.text} tokens={item.tokens} live={item.live} />;
  if (item.kind === "message" && isUiConfigFeedback(String(item.message.content_text ?? item.message.content ?? ""))) return null;
  if (item.kind === "tool") {
    // turn_id 从 key 提取：tool:{turn_id}:{node_id}
    const turnId = item.key.split(":").slice(1, -1).join(":");
    return <ToolCard item={item} sessionKey={sessionKey} convId={convId} turnId={turnId} />;
  }
  if (item.kind === "notice") return <div className="runtime-step-notice">{item.text}</div>;
  if (item.kind === "image") {
    // 用户随消息发送的图片（修正版方案 A）：归属校验端点取原图；
    // ref 内容不可变 → 浏览器长缓存。缩略图 + 点击新窗口看原图。
    return (
      <a className="image-card" href={item.src} target="_blank" rel="noreferrer">
        <img className="image-thumb" src={item.src} alt={item.name} loading="lazy" />
      </a>
    );
  }
  if (item.kind === "goalArchived") {
    return (
      <div className="goal-archived-note" role="note">
        <span className="goal-archived-ico">🗂️</span>
        <span>该轮 Goal 终答已归档（归档内容见归档记录）。</span>
      </div>
    );
  }
  if (item.kind === "projection") return <ProjectionCard item={item} />;
  return <MessageBubble item={item} />;
});
