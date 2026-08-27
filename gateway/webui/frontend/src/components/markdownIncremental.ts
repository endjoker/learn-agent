// 块级增量 Markdown 渲染（优化方案 #2，借鉴 dsh ui-primitives incremental.ts
// 的「冻结-尾部」增量解析）。
//
// 现状问题：Markdown 组件每 80ms 对整个累积文本 marked.parse + sanitizeHtml，
// 单条回复内总成本 O(n²)；>8KB 非终态只能降级 <pre> 直出，流式期间丢失排版。
//
// 原理：CommonMark 块解析是行级的，追加文本最多重塑最后一个顶层块 —— 用
// marked lexer 把累积文本切 top-level tokens 后，除尾部 UNSTABLE_TAIL_BLOCKS
// 个块外的全部块都是「冻结区」：其源码不再变化，消毒后的 HTML 片段可以缓存。
// 每 tick 只对尾部切片重新 lexer+parse+sanitize，拼接 frozenHtml + tailHtml。
// 流式追加是纯 append，冻结边界只前移不回退，因此每个源码区域整条流只解析
// O(1) 次（新增冻结部分按增量 slice 追加消毒），单次刷新成本与总长无关。
//
// 安全契约保持不变：
// - 每个片段（冻结增量 / 尾部）都单独走现有 sanitizeHtml 白名单消毒；
//   顶层块边界处切分时逐块消毒与整段消毒等价（marked 各顶层 token 的 raw
//   拼合恰为原文，块级 renderer 输出上下文无关、标签配平——由等价性测试
//   覆盖标题/列表/表格/代码栅栏/引用/行内 HTML/mXSS 载荷）；
// - 不放弃 noscript/template/属性值角度实体化等 mXSS 防护。
// 已知取舍（与 harness 同）：
// 1. 跨块的引用式链接定义（[ref]: url 定义在前文、使用在后文）在分片解析下
//    不解析，聊天场景几乎不使用该语法，接受此分歧；
// 2. 多个连续危险 HTML 块被消毒整体移除后，整段与分片的块分组可能留下空段落
//    级白噪音差异（安全不变式不受影响）。

import { Lexer, marked } from "marked";

import { renderMdFallback, sanitizeHtml } from "@/components/markdownCore";

/** 尾部不稳定块数：最后 N 个顶层块每次刷新都重解析（正在生长的区域）。 */
export const UNSTABLE_TAIL_BLOCKS = 2;

export interface MarkdownIncrementalCache {
  /** 上次已冻结的完整源码前缀。 */
  frozenSrc: string;
  /** 冻结源码对应的消毒 HTML（frozenSrc 的渲染产物）。 */
  frozenHtml: string;
}

export function createMarkdownIncrementalCache(): MarkdownIncrementalCache {
  return { frozenSrc: "", frozenHtml: "" };
}

/** 计算尾部不稳定块的起始偏移；tokens 为空或不足时返回原文起点。 */
const tailStartOffset = (doc: string): number => {
  let tokens;
  try {
    tokens = new Lexer().lex(doc);
  } catch {
    return -1; // lexer 异常 → 走整段兜底
  }
  if (tokens.length <= UNSTABLE_TAIL_BLOCKS) return 0;
  let offset = 0;
  for (const token of tokens.slice(0, -UNSTABLE_TAIL_BLOCKS)) offset += token.raw.length;
  // 防御：raw 拼合必须无损覆盖原文，否则放弃切分（走整段路径）。
  if (offset === 0 || offset >= doc.length) return offset >= doc.length ? -1 : 0;
  return offset;
};

/** 单片段渲染：parse + 消毒，异常时纯文本兜底（与 renderMd 同契约）。 */
const renderFragment = (src: string): string => {
  try {
    return sanitizeHtml(marked.parse(src) as string);
  } catch {
    return renderMdFallback(src);
  }
};

/**
 * 增量渲染：返回 text 全文的消毒 HTML，成本集中在尾部不稳定块。
 * cache 由调用方持有（每个 Markdown 实例一份），跨 tick 累积冻结区。
 */
export function renderMdIncremental(text: string, cache: MarkdownIncrementalCache): string {
  if (!text) return "";
  const tailStart = tailStartOffset(text);
  if (tailStart < 0) {
    // lexer 异常/结构异常：退回全量渲染（正确性优先）。
    cache.frozenSrc = "";
    cache.frozenHtml = "";
    return renderFragment(text);
  }
  if (tailStart === 0) {
    // 还没有可冻结的稳定前缀：全部按尾部处理，清空冻结缓存。
    cache.frozenSrc = "";
    cache.frozenHtml = "";
    return renderFragment(text);
  }
  const frozenSrc = text.slice(0, tailStart);
  if (cache.frozenSrc.length > frozenSrc.length || !frozenSrc.startsWith(cache.frozenSrc)) {
    // 非追加式变化（替换/收缩，正常流式不会发生）：重建整个冻结区。
    cache.frozenSrc = frozenSrc;
    cache.frozenHtml = renderFragment(frozenSrc);
  } else if (frozenSrc.length > cache.frozenSrc.length) {
    // 追加推进：只消毒新冻结的中间切片并拼接（每段源码整条流只 parse O(1) 次）。
    cache.frozenHtml += renderFragment(text.slice(cache.frozenSrc.length, tailStart));
    cache.frozenSrc = frozenSrc;
  }
  // 尾部不稳定区每次重解析。
  return cache.frozenHtml + renderFragment(text.slice(tailStart));
}
