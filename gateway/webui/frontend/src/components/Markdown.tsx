import { useEffect, useMemo, useRef, useState } from "react";

import { renderMd, renderMdFallback, sanitizeHtml } from "@/components/markdownCore";
import { createMarkdownIncrementalCache, renderMdIncremental } from "@/components/markdownIncremental";

// 兼容导出：消毒/渲染原语已迁至 markdownCore.ts（增量渲染共用同一安全契约）。
export { renderMd, renderMdFallback, sanitizeHtml };

// 大文本兜底阈值。增量解析（#2）生效后单次刷新成本只与尾部不稳定块相关，
// 与总长无关，原 8KB <pre> 直出降级不再必要——长回复流式期间也能看到实时
// 排版；仅保留一个极端防护阈值，防止病理性超大文本拖垮 DOM。
const LARGE_TEXT_FALLBACK_BYTES = 512 * 1024;

/** React wrapper: renders sanitized markdown into a .md container.
 *  视觉节流：默认 ~80ms 合帧刷新（高频 delta 不逐次 parse+渲染），由 rAF 驱动
 *  （后台标签页自动暂停，与 gateway store 的 rAF 通知合帧节奏对齐）；
 *  reduced-motion 下全部即时；chat.done 前用节流值，终态（isFinal）立即渲染
 *  最新文本并走全量权威路径。
 *
 *  解析策略：非终态走块级增量渲染（markdownIncremental.renderMdIncremental）——
 *  除尾部 2 个顶层块外全部冻结缓存，每 tick 只重解析尾部切片，流式总成本从
 *  O(n²) 降到 O(n)；终态 isFinal 用全量 renderMd 渲染权威完整文本。 */
export function Markdown({ text, className = "md", isFinal = false, largeTextFallback = true }: {
  text: string;
  className?: string;
  isFinal?: boolean;
  /** 超大文本（非终态）时降级 <pre> 直出原始文本的极端防护开关（默认开启）。 */
  largeTextFallback?: boolean;
}) {
  const [displayText, setDisplayText] = useState(text);
  const latestRef = useRef(text);
  latestRef.current = text;
  // 已跳过节流（reduced-motion 等一次性同步最新文本）。
  const bypassRef = useRef(false);
  // 块级增量缓存（每个实例独立；终态后不再使用）。
  const incrementalCacheRef = useRef(createMarkdownIncrementalCache());

  // 首挂载：reduced-motion 直接同步最新文本并停表；否则启动 rAF 驱动的节流
  // 循环——最小间隔 80ms、后台标签页暂停。text 变化不在此依赖——latestRef
  // 始终持有最新值，节流循环负责按周期拉取。
  useEffect(() => {
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
    if (reduced) {
      setDisplayText(text);
      bypassRef.current = true;
      return undefined;
    }
    const VISUAL_INTERVAL_MS = 80;
    const pull = () => setDisplayText((prev) => (prev === latestRef.current ? prev : latestRef.current));
    if (typeof window.requestAnimationFrame !== "function") {
      const id = window.setInterval(() => { if (!document.hidden) pull(); }, VISUAL_INTERVAL_MS);
      return () => window.clearInterval(id);
    }
    let handle = 0;
    let last = 0;
    const tick = (now: number) => {
      handle = window.requestAnimationFrame(tick);
      if (document.hidden) return;
      if (now - last < VISUAL_INTERVAL_MS) return;
      last = now;
      pull();
    };
    handle = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // isFinal：终态到达时最终同步一次权威文本（节流周期外立即生效）。
  useEffect(() => {
    if (isFinal) setDisplayText(text);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isFinal]);

  // 终态直接用权威 text 全量渲染（isFinal 效果触发重渲染前也不显示过期内容）。
  // 注意：useMemo 必须在任何条件 return 之前调用（rules-of-hooks）。
  const html = useMemo(() => {
    if (isFinal || bypassRef.current) return renderMd(isFinal ? text : displayText);
    // 流式：块级增量渲染（冻结区缓存复用 + 尾部重解析），异常时退回全量。
    try {
      return renderMdIncremental(displayText, incrementalCacheRef.current);
    } catch {
      return renderMd(displayText);
    }
  }, [isFinal, text, displayText]);

  // 极端超大文本且非终态：跳过 markdown parse 直出原始文本（病理性防护）。
  if (largeTextFallback && !isFinal && !bypassRef.current && text.length > LARGE_TEXT_FALLBACK_BYTES) {
    return <pre className={`${className} large-text-fallback`}>{text}</pre>;
  }

  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />;
}
