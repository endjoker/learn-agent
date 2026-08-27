// Markdown 核心渲染原语（从 Markdown.tsx 迁出，供全量渲染与增量渲染共用）。
// 安全契约与旧 HA.renderMd() 完全一致：marked 12.0.2 parse、解析失败纯文本兜底、
// DOM 白名单消毒（无 script/iframe/object/embed/link/style/meta/base/form/
// noscript/template，无 on* 属性，仅 http(s)/mailto/#/相对 URL，
// 无 javascript:/expression style 值）。
import { marked } from "marked";

const esc = (s: string) => String(s).replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[ch] as string));

const inlineMd = (s: string) => esc(s)
  .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
  .replace(/\*([^*]+)\*/g, "<em>$1</em>")
  .replace(/`([^`]+)`/g, "<code>$1</code>")
  .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
    '<a href="$2" rel="noopener" target="_blank">$1</a>');

/** Fallback renderer used when marked throws (mirrors HA.renderMdFallback). */
export function renderMdFallback(text: string): string {
  const lines = String(text).split("\n");
  let out = "";
  let list: "ul" | "ol" | null = null;
  let inCode = false;
  let fence = "";
  const closeList = () => { if (list) { out += `</${list}>`; list = null; } };
  const closeCode = () => { if (inCode) { out += "</code></pre>"; inCode = false; } };
  for (const line of lines) {
    if (inCode) {
      if (line.trim().startsWith(fence)) { fence = ""; closeCode(); }
      else out += esc(line) + "\n";
      continue;
    }
    const fm = line.trim().match(/^```+/);
    if (fm) { closeList(); fence = fm[0]; inCode = true; out += "<pre><code>"; continue; }
    const hm = line.match(/^(#{1,6})\s+(.*)$/);
    if (hm) { closeList(); closeCode(); const lv = hm[1]!.length; out += `<h${lv}>${inlineMd(hm[2]!)}</h${lv}>`; continue; }
    const lm = line.match(/^\s*[-*+]\s+(.*)$/);
    if (lm) { closeCode(); if (list !== "ul") { closeList(); out += "<ul>"; list = "ul"; } out += `<li>${inlineMd(lm[1]!)}</li>`; continue; }
    const om = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (om) { closeCode(); if (list !== "ol") { closeList(); out += "<ol>"; list = "ol"; } out += `<li>${inlineMd(om[1]!)}</li>`; continue; }
    if (!line.trim()) { closeList(); continue; }
    closeList(); closeCode();
    out += `<p>${inlineMd(line)}</p>`;
  }
  closeList(); closeCode();
  return out;
}

// 内联 style 允许的展示类属性白名单（其余一律剔除）。
const SAFE_STYLE_PROPS = new Set([
  "color", "background-color", "font-size", "font-weight", "font-style", "font-family",
  "text-align", "text-decoration", "text-transform", "line-height", "letter-spacing",
  "white-space", "word-break", "vertical-align", "margin", "margin-top", "margin-right",
  "margin-bottom", "margin-left", "padding", "padding-top", "padding-right",
  "padding-bottom", "padding-left", "border", "border-radius", "width", "height",
  "min-width", "min-height", "max-width", "max-height", "display", "position",
  "top", "left", "right", "bottom", "z-index", "float", "overflow", "box-sizing",
  "flex", "flex-direction", "flex-wrap", "gap", "align-items", "justify-content",
]);
// 属性值级拦截：url( / expression( / -moz-binding 等可脚本化向量直接剔除该声明。
const UNSAFE_STYLE_VALUE = /url\s*\(|expression\s*\(|-moz-binding|javascript:|behavior\s*:|@import/i;

/** 属性级 style 白名单清洗：仅保留安全展示属性，且值不含脚本向量。 */
const sanitizeStyle = (value: string): string => {
  const out: string[] = [];
  for (const decl of String(value).split(";")) {
    const trimmed = decl.trim();
    if (!trimmed) continue;
    const colon = trimmed.indexOf(":");
    if (colon <= 0) continue;
    const prop = trimmed.slice(0, colon).trim().toLowerCase();
    const body = trimmed.slice(colon + 1).trim();
    if (!prop || !body) continue;
    if (!SAFE_STYLE_PROPS.has(prop)) continue;
    if (UNSAFE_STYLE_VALUE.test(body)) continue;
    out.push(`${prop}: ${body}`);
  }
  return out.join("; ");
};

// 黑名单标签：命中即连同子树整体移除。其中两个特殊成员必须显式列出：
// - noscript：DOMParser 文档的 scripting 标志禁用，<noscript> 内容按普通
//   元素解析；而渲染产物经 dangerouslySetInnerHTML 写入活文档时按 RAWTEXT
//   解析到第一个 </noscript>。两套解析结果不一致（mXSS）：属性值里的
//   "</noscript><img src=x onerror=…>" 在活文档中会突破为真实元素。
// - template：querySelectorAll("*") 不下钻 template.content，内部节点永远
//   遍历不到，无法逐个清洗，只能整体移除 template 标签。
const BLOCKED_TAGS = new Set([
  "script", "iframe", "object", "embed", "link", "style", "meta", "base",
  "form", "noscript", "template",
]);

/** DOM whitelist sanitization (mirrors HA.sanitizeHtml). */
export function sanitizeHtml(html: string): string {
  const doc = new DOMParser().parseFromString(String(html), "text/html");
  for (const el of Array.from(doc.body.querySelectorAll("*"))) {
    const tag = el.tagName.toLowerCase();
    if (BLOCKED_TAGS.has(tag)) {
      el.remove();
      continue;
    }
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on")) { el.removeAttribute(attr.name); continue; }
      // xlink:href 走与 href 相同的 URL 校验（SVG 图片引用向量）。
      if (name === "href" || name === "src" || name === "xlink:href") {
        const v = (attr.value || "").trim().toLowerCase();
        if (!/^(https?:|mailto:|#|\/|\.\/|\.\.\/)/.test(v)) { el.removeAttribute(attr.name); continue; }
      }
      if (name === "style") {
        const cleaned = sanitizeStyle(attr.value || "");
        if (cleaned) el.setAttribute(attr.name, cleaned);
        else el.removeAttribute(attr.name);
      }
    }
  }
  // 属性值中的 < > 做 HTML 实体转义（防 RAWTEXT 解析不一致类 mXSS 的另一半
  // 入口）。必须在序列化结果上做字符串级处理：DOM API 写不进未解码的实体，
  // setAttribute("&lt;…") 会被序列化器再次转义成 "&amp;lt;"（双重转义失真）。
  return escapeRawAnglesInAttrValues(doc.body.innerHTML);
}

// ---- 序列化层收尾：属性值中的原始 < > 实体化 ---------------------------------
// HTML 序列化器只转义属性值中的 & 和引号，< 与 > 原样输出；而活文档按 RAWTEXT
// 解析 noscript 等容器时引号边界不被识别，属性值里的 "</noscript>" 会提前闭合
// 元素。统一实体化后，消毒输出在活文档重解析不会产生任何新节点。
// 序列化器保证带引号属性值内不含裸引号，因此引号必为属性定界符，可安全切分。

const escapeAngles = (value: string): string =>
  value.replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** 单个已序列化的开标签内：转义带引号属性值中的 < >（标签名/属性名不含 <>）。 */
const escapeAnglesInOpenTag = (tag: string): string =>
  tag.replace(/=("[^"]*"|'[^']*')/g, (match, value: string) =>
    /[<>]/.test(value)
      ? `=${value.charAt(0)}${escapeAngles(value.slice(1, -1))}${value.charAt(value.length - 1)}`
      : match);

/** 序列化 HTML 全文扫描：开标签走引号感知的属性值转义，注释原样保留，其余逐字复制。 */
const escapeRawAnglesInAttrValues = (html: string): string => {
  let out = "";
  let i = 0;
  while (i < html.length) {
    if (html.startsWith("<!--", i)) {
      const end = html.indexOf("-->", i + 4);
      const stop = end === -1 ? html.length : end + 3;
      out += html.slice(i, stop);
      i = stop;
      continue;
    }
    if (html.charAt(i) === "<" && /[a-zA-Z]/.test(html.charAt(i + 1))) {
      // 引号感知地找真正的标签结束 >（属性值内的 > 不结束标签）。
      let quote = "";
      let closed = -1;
      for (let j = i + 1; j < html.length; j += 1) {
        const c = html.charAt(j);
        if (quote !== "") {
          if (c === quote) quote = "";
        } else if (c === "\"" || c === "'") {
          quote = c;
        } else if (c === ">") {
          closed = j;
          break;
        }
      }
      if (closed !== -1) {
        out += escapeAnglesInOpenTag(html.slice(i, closed + 1));
        i = closed + 1;
        continue;
      }
    }
    out += html.charAt(i);
    i += 1;
  }
  return out;
};

/** 全量渲染：整段 marked.parse + 消毒（终态权威路径）。 */
export function renderMd(text: string): string {
  if (!text) return "";
  try {
    return sanitizeHtml(marked.parse(String(text)) as string);
  } catch {
    return renderMdFallback(String(text));
  }
}
