import { useEffect, useMemo, useRef, useState } from "react";

import { Markdown } from "@/components/Markdown";

/** 运行态摘要行：只取最后一行非空内容（借鉴 dsh ReasoningRow 的 latestLine——
 *  折叠态 running 摘要恒为一行，长思考流式期间 DOM 文本量与重排成本 O(1)）。
 *  行超长时截尾保留末段（思考流最新的内容在行尾）。 */
export const latestReasoningLine = (text: string, max = 160): string => {
  const lines = String(text).split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i]!.trim();
    if (line) return line.length > max ? `…${line.slice(-max)}` : line;
  }
  return "";
};

/** 终态摘要行：取第一行非空内容（首行是"这一步要干什么"的意图陈述，与工具卡
 *  显示命令/路径的语义对等），头段截断保留开头。复用 stripMdSummary 剥掉
 *  Markdown 符号（标题/加粗/列表符），让纯文本预览在头部一行可扫描。 */
export const firstReasoningLine = (text: string, max = 160): string => {
  const lines = String(text).split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = stripMdSummary(lines[i]!.trim());
    if (line) return line.length > max ? `${line.slice(0, max)}…` : line;
  }
  return "";
};

/** 轻量 Markdown 符号剥离（仅用于折叠态单行预览，非正文渲染）。 */
const stripMdSummary = (line: string): string =>
  line
    .replace(/^#{1,6}\s+/, "")       // 标题
    .replace(/^>\s?/, "")            // 引用
    .replace(/^[-*+]\s+(\[[ xX]?\]\s+)?/, "") // 列表符/任务框
    .replace(/^\|/, "").replace(/\|$/, "")    // 表格行首尾
    .replace(/\*\*([^*]+)\*\*/g, "$1")        // 加粗
    .replace(/`([^`]+)`/g, "$1")              // 行内代码
    .trim();

export function ReasoningCard({ text, tokens, live = false }: { text: string; tokens?: number; live?: boolean }) {
  const [open, setOpen] = useState(false);
  const latest = useMemo(() => latestReasoningLine(text), [text]);
  // 终态/展开态的折叠预览：静态首行（意图陈述），节点文本不可变 → memo O(1)。
  const firstLine = useMemo(() => firstReasoningLine(text), [text]);
  // 运行态单行的横向滚动跟随：scrollLeft 贴右端（最新内容可见），3 帧节流
  // （对齐 dsh use-throttled-visual-update 的视觉对齐节奏）。
  const latestRef = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!live || open) return undefined;
    if (typeof window.requestAnimationFrame !== "function") return undefined;
    let handle = 0;
    let frames = 0;
    const follow = () => {
      handle = window.requestAnimationFrame(follow);
      frames = (frames + 1) % 3;
      if (frames !== 0) return;
      const el = latestRef.current;
      if (el) el.scrollLeft = el.scrollWidth;
    };
    handle = window.requestAnimationFrame(follow);
    return () => window.cancelAnimationFrame(handle);
  }, [live, open]);
  if (!text.trim()) return null;
  const tokenLabel = tokens != null ? ` · ${tokens.toLocaleString()} tokens` : "";
  // 运行中且折叠：正文不再渲染全量 Markdown（此前每 80ms 对整段累积文本
  // parse+消毒是流式期间的最大热点），只在头部展示最新一行 + 「思考中」标记；
  // 展开（用户主动查看）或终态才渲染完整富文本。
  const collapsedLive = live && !open;
  return (
    <details className={`reasoning-card${live ? " live" : ""}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="reasoning-card-head">
        <span className="reasoning-card-ico">🧠</span>
        <span className="reasoning-card-title">{live ? "思考中" : "思考过程"}{tokenLabel}</span>
        {collapsedLive && latest ? <span ref={latestRef} className="reasoning-card-latest">{latest}</span> : null}
        {/* 终态折叠：静态首行预览（此前被 collapsedLive 门控吞掉，折叠头部
            只剩占位标题；文本已不可变，静态渲染零性能成本） */}
        {!collapsedLive && !open && firstLine ? <span className="reasoning-card-latest">{firstLine}</span> : null}
        <span className="reasoning-card-caret">▸</span>
      </summary>
      {collapsedLive ? null : <Markdown text={text} className="reasoning-card-body md" isFinal={!live} />}
    </details>
  );
}
