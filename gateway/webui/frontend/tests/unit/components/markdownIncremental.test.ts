import { parse } from "parse5";
import { describe, expect, it } from "vitest";

import { renderMd } from "@/components/markdownCore";
import {
  createMarkdownIncrementalCache,
  renderMdIncremental,
  UNSTABLE_TAIL_BLOCKS,
} from "@/components/markdownIncremental";

// 活文档等价重解析（scripting 开启），收集危险残留：on* 属性元素 / noscript / template。
// （与 Markdown.test.ts 的 mXSS 回归同一套判定；属性值中被实体转义的载荷文本
// 如 &lt;img onerror=…&gt; 不构成标记，不计入残留。）
type P5Node = { tagName?: string; attrs?: Array<{ name: string }>; childNodes?: P5Node[] };
const dangerousResidue = (html: string): string[] => {
  const tree = parse(html, { scriptingEnabled: true }) as unknown as P5Node;
  const found: string[] = [];
  const walk = (node: P5Node): void => {
    for (const child of node.childNodes ?? []) {
      if (child.tagName != null) {
        const tag = child.tagName.toLowerCase();
        const onAttrs = (child.attrs ?? []).map((a) => a.name.toLowerCase()).filter((n) => /^on/i.test(n));
        if (onAttrs.length > 0) found.push(`${tag}[${onAttrs.join(",")}]`);
        if (tag === "noscript" || tag === "template") found.push(`<${tag}>`);
      }
      walk(child);
    }
  };
  walk(tree);
  return found;
};

// 增量 vs 全量的等价性语料：覆盖标题/段落/列表/有序列表/代码栅栏/引用/表格/
// 行内样式/链接/图片/行内 HTML/水平线等聊天回复常见块型。
const CORPUS_DOCS: string[] = [
  ["# 标题一", "", "段落 **加粗** `code` [链接](https://a.com)。", "", "- 列表项 1", "- 列表项 2", "", "## 结尾", "", "尾段文本。"].join("\n"),
  ["第一段。", "", "1. 有序一", "2. 有序二", "3. 有序三", "", "> 引用行 1", "> 引用行 2", "", "---", "", "结尾段。"].join("\n"),
  ["```js", "console.log('hi');", "const x = 1;", "```", "", "代码块后段落 *斜体* 与 ~~删除线~~。", "", "| a | b |", "|---|---|", "| 1 | 2 |"].join("\n"),
  ["段落含行内 HTML <b>加粗</b> 与 <img src=\"https://a.com/x.png\" alt=\"图\">。", "", "# 标题", "", "<div>原生 html 块</div>", "", "最终段 <script>alert(1)</script> 应被消毒。"].join("\n"),
];

describe("markdownIncremental equivalence (chunked stream == whole renderMd)", () => {
  for (const doc of CORPUS_DOCS) {
    it(`streaming accumulation equals whole parse (${doc.slice(0, 24)}…)`, () => {
      const whole = renderMd(doc);
      // 模拟流式：按多字节步进逐 chunk 追加，每步都做增量渲染。
      const cache = createMarkdownIncrementalCache();
      const step = 7;
      let acc = "";
      let lastOut = "";
      for (let i = 0; i < doc.length; i += step) {
        acc = doc.slice(0, i + step);
        lastOut = renderMdIncremental(acc, cache);
      }
      expect(acc).toBe(doc);
      // 最终一步（终态前最后一次流式输出）与整段全量渲染一致。
      expect(lastOut).toBe(whole);
    });

    it(`single-shot split equals whole parse (${doc.slice(0, 24)}…)`, () => {
      const cache = createMarkdownIncrementalCache();
      expect(renderMdIncremental(doc, cache)).toBe(renderMd(doc));
    });
  }

  it("repeated calls with unchanged text are stable and idempotent", () => {
    const doc = CORPUS_DOCS[0]!;
    const cache = createMarkdownIncrementalCache();
    const first = renderMdIncremental(doc, cache);
    const second = renderMdIncremental(doc, cache);
    expect(second).toBe(first);
  });
});

describe("markdownIncremental streaming behavior", () => {
  it("freezes nothing while top-level token count <= UNSTABLE_TAIL_BLOCKS", () => {
    expect(UNSTABLE_TAIL_BLOCKS).toBeGreaterThan(0);
    const cache = createMarkdownIncrementalCache();
    // 单一顶层块（1 个 token）：无稳定冻结区，全部按尾部处理（缓存保持为空）。
    renderMdIncremental("只有一个段落，没有尾随空行。", cache);
    expect(cache.frozenSrc).toBe("");
  });

  it("freezes everything before the last UNSTABLE_TAIL_BLOCKS tokens", () => {
    const cache = createMarkdownIncrementalCache();
    // 五个顶层 token（段/空行相间）：尾部 2 个不冻结，
    // 冻结区恰为前三个 token 的源码拼接，且渲染结果与整段一致。
    const doc = "首段内容。\n\n中段内容。\n\n尾段内容。";
    renderMdIncremental(doc, cache);
    expect(cache.frozenSrc).toBe("首段内容。\n\n中段内容。");
    expect(renderMdIncremental(doc, createMarkdownIncrementalCache())).toBe(renderMd(doc));
  });

  it("advances the frozen prefix monotonically as text appends", () => {
    const cache = createMarkdownIncrementalCache();
    const chunks = ["# 标题\n\n", "第一段完整内容。\n\n", "第二段完整内容。\n\n", "尾部生长中"];
    let acc = "";
    let prevFrozen = "";
    for (const chunk of chunks) {
      acc += chunk;
      renderMdIncremental(acc, cache);
      expect(cache.frozenSrc.startsWith(prevFrozen)).toBe(true);
      expect(acc.startsWith(cache.frozenSrc)).toBe(true);
      prevFrozen = cache.frozenSrc;
    }
  });

  it("rebuilds the frozen region on non-append rewrites (correctness over cost)", () => {
    const cache = createMarkdownIncrementalCache();
    renderMdIncremental("# A\n\n第一版内容。\n\n第二段。\n\n尾。", cache);
    const rewritten = "# B\n\n完全不同的开头。\n\n第二段。\n\n尾。";
    const out = renderMdIncremental(rewritten, cache);
    expect(out).toBe(renderMd(rewritten));
  });
});

describe("markdownIncremental safety parity with sanitizeHtml", () => {
  it("strips scripts/noscript/template vectors arriving across chunk boundaries", () => {
    // 载荷整体跨多个 chunk 到达（先到无害前缀，恶意载荷在后）。消毒结果必须
    // 与整段消毒同等安全：无 script 执行载体、无 noscript/template 残留。
    // 注：多个连续危险 HTML 块被整体移除后，整段与分片的块分组可能产生空段落
    // 级白噪音差异（marked 对相邻 html 块的聚合策略），因此这里不承诺字节相等，
    // 只承诺安全不变式 + 正文保留 + 自身一致性。
    const payload = [
      "# 安全标题",
      "",
      "普通段落。",
      "",
      '<noscript><p title="</noscript><img src=x onerror=alert(1)>"></p></noscript>',
      "",
      "<template><img src=x onerror=alert(1)></template>",
      "",
      "<script>alert(1)</script>",
      "",
      "结尾。",
    ].join("\n");
    const cache = createMarkdownIncrementalCache();
    let out = "";
    for (let i = 0; i < payload.length; i += 5) {
      out = renderMdIncremental(payload.slice(0, i + 5), cache);
    }
    const whole = renderMd(payload);
    // 活文档等价重解析：无 on* 元素属性、无 noscript/template 载体（与整段消毒同安全级）。
    expect(dangerousResidue(out)).toEqual([]);
    expect(dangerousResidue(whole)).toEqual([]);
    // 无未转义的标记级突破（img 只允许出现在实体化属性值内）。
    expect(out).not.toContain("<img");
    expect(out).toContain("结尾。");
    // 自身一致性：流式累积结果 == 全新缓存对同一全文的单次增量渲染。
    expect(out).toBe(renderMdIncremental(payload, createMarkdownIncrementalCache()));
  });

  it("blocks javascript: URLs rendered inside the tail region", () => {
    const doc = "# 头部\n\n中间段落。\n\n[坏](javascript:alert(1)) [好](https://example.com)";
    const cache = createMarkdownIncrementalCache();
    const out = renderMdIncremental(doc, cache);
    expect(out).not.toContain("javascript:");
    expect(out).toContain('href="https://example.com"');
  });
});
