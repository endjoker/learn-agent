import { useEffect, useRef, useState } from "react";

import { conversationApi } from "@/gateway/api";
import type { Turn, TurnNode } from "@/gateway/types";
import { Markdown } from "@/components/Markdown";
import { stripMdSummary } from "@/pages/chat/toolSummary";

/** 系统任务（Plan/Goal/Subagent）运行投影卡：展示实时进度/最终回复，
 *  并可点开"查看工具调用详情"展示父会话同 turn 的工具/思考明细。
 *  明细优先取 timeline 传入的父会话节点（对齐 dsh 主会话累积），缺省时
 *  回退到旧 system 会话按需拉取（兼容存量数据）。 */
// A5 归档占位文本（goal 轮次终答被上限裁剪后的占位）识别：渲染链接而非占位原文。
const GOAL_ARCHIVED_RE = /^\[Goal\s+([A-Za-z0-9_-]+)\s+第\d+轮终答已归档，详见目标页\]$/;

export function ProjectionCard({ item }: {
  item: {
    kind: "projection";
    runtime_type: string;
    runtime_id: string;
    status: string;
    liveText?: string;
    finalText?: string;
    summary?: string;
    detailNodes?: TurnNode[];
    systemConversationId?: string;
    runtime_status?: string;
    errorCode?: string;
    message?: string;
    goalRound?: number;
  };
}) {
  // 默认折叠（参考工具卡：折叠态用头部摘要展示最终回复，点开看明细/正文）。
  const [open, setOpen] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [nodes, setNodes] = useState<TurnNode[]>([]);
  const typeLabel: Record<string, string> = { plan: "📋 Plan", goal: "🎯 Goal", subagent: "🔀 Subagent" };
  const running = item.status === "running";
  // 收官：failed / blocked 终态视觉（红/黄徽章 + error/message 行）
  const failed = item.status === "failed" || item.status === "error" || item.status === "cancelled";
  const blocked = item.status === "blocked" || item.status === "paused";
  const statusLabel = item.status === "failed" ? "失败"
    : item.status === "blocked" ? "需要关注"
      : item.status === "cancelled" ? "已取消"
        : item.status === "paused" ? "已暂停"
          : running ? "运行中"
            : item.status === "done" || item.status === "completed" ? "已完成"
              : item.status;
  const statusBadgeCls = failed ? "err" : blocked ? "warn" : running ? "warn" : item.status === "done" || item.status === "completed" ? "ok" : "dim";
  const errorText = item.message ?? item.errorCode ?? "";
  const body = item.finalText ?? (item.liveText ? (item.liveText || item.summary) : item.summary) ?? "";
  // 折叠态头部摘要：剥掉 Markdown 语法后压成一行（与工具卡摘要同密度，
  // 避免 **加粗**/反引号 等原始语法符号出现在卡片头）。
  const summaryText = stripMdSummary(item.finalText ?? item.liveText ?? item.summary ?? "");
  const hasDetail = (item.detailNodes?.length ?? 0) > 0 || Boolean(item.systemConversationId);
  // 运行中展开时自动跟随滚动到最新输出（对齐 dsh 流式：正文限高内滚动）。
  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open && running && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [open, running, body, bodyRef]);
  const loadDetail = async () => {
    if (detailOpen || loading) return;
    // 优先使用父会话已传入的明细节点；无则回退到旧 system 会话按需拉取。
    if (item.detailNodes && item.detailNodes.length > 0) {
      setNodes(item.detailNodes);
      setDetailOpen(true);
      return;
    }
    if (!item.systemConversationId) return;
    setDetailOpen(true);
    setLoading(true);
    try {
      const result = await conversationApi.history(item.systemConversationId, { limit: 50 });
      if (result.ok && result.data) {
        const turns = result.data.items as Array<{ turn: Turn; nodes: TurnNode[] }>;
        const all: TurnNode[] = [];
        for (const t of turns) all.push(...(t.nodes ?? []));
        setNodes(all);
      }
    } catch { /* silent */ }
    finally { setLoading(false); }
  };
  const toolStatus = (node: TurnNode): { label: string; cls: string } => {
    const meta = node.metadata ?? {};
    if (meta.error !== undefined) return { label: "失败", cls: "err" };
    if (node.status === "running") return { label: "执行中", cls: "warn" };
    if (node.status === "done") return { label: "完成", cls: "ok" };
    return { label: node.status ?? "", cls: "dim" };
  };
  // 归档占位：正文/终答文本命中 A5 占位格式 → 渲染"已归档，详见目标页"链接
  const archivedMatch = GOAL_ARCHIVED_RE.exec(body);
  const archivedGoalId = archivedMatch ? archivedMatch[1] : undefined;
  return (
    <details className={`ws-projection-card${running ? " live" : ""}${failed ? " failed" : ""}${blocked ? " blocked" : ""}`} open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary className="ws-projection-head">
        <span className="ws-projection-ico">{failed ? "❌" : blocked ? "⏸" : running ? "⚙️" : "✅"}</span>
        <span className="ws-projection-title">
          {typeLabel[item.runtime_type] ?? item.runtime_type}
          {running ? "（进行中）" : ""}
          {typeof item.goalRound === "number" && item.goalRound > 0 ? <span className="goal-round-badge">第 {item.goalRound} 轮</span> : null}
        </span>
        {summaryText ? <span className="ws-projection-summary">{summaryText}</span> : null}
        <span className={`badge ${statusBadgeCls}`}>{statusLabel}</span>
        <span className="ws-projection-caret">▸</span>
      </summary>
      {(failed || blocked) && errorText ? (
        <div className="ws-projection-error" role={failed ? "alert" : undefined}>
          {failed ? "❌ " : "⚠️ "}{errorText}
        </div>
      ) : null}
      {archivedGoalId ? (
        <div className="goal-archived-note" role="note">
          <span className="goal-archived-ico">🗂️</span>
          <span>该轮 Goal 终答已归档（归档内容见归档记录）。</span>
        </div>
      ) : body ? <div ref={bodyRef} className="ws-projection-body md"><Markdown text={body} isFinal={Boolean(item.finalText)} /></div> : null}
      {hasDetail ? (
        <div className="ws-projection-body">
          <button type="button" className="btn" onClick={() => void loadDetail()}>
            {loading ? "读取中…" : detailOpen ? "收起工具详情" : "查看工具调用详情"}
          </button>
          {detailOpen && nodes.length > 0 ? (
            <div className="ws-projection-detail">
              {nodes.filter((n) => n.type === "tool" || n.type === "reasoning").map((node) => {
                if (node.type === "reasoning") {
                  return <div key={node.node_id} className="ws-proj-reasoning">🧠 {node.text ?? ""}</div>;
                }
                const meta = node.metadata ?? {};
                const name = String(meta.tool ?? meta.call_id ?? "工具");
                const params = String(meta.params_summary ?? "");
                const result = String(meta.result_summary ?? "");
                const st = toolStatus(node);
                return (
                  <div key={node.node_id} className="ws-proj-tool">
                    <div className="ws-proj-tool-head">
                      <span className="ws-proj-tool-name">🔧 {name}</span>
                      <span className={`badge ${st.cls}`}>{st.label}</span>
                    </div>
                    {params ? <pre className="ws-proj-tool-io">输入：{params}</pre> : null}
                    {result ? <pre className="ws-proj-tool-io">返回：{result.slice(0, 500)}</pre> : null}
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      ) : null}
    </details>
  );
}
