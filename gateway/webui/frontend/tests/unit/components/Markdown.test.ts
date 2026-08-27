import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { parse } from "parse5";
import { describe, expect, it } from "vitest";

import { renderMd, renderMdFallback, sanitizeHtml } from "@/components/Markdown";

describe("Markdown rendering (legacy HA.renderMd parity)", () => {
  it("renders basic markdown", () => {
    expect(renderMd("**bold** and `code`")).toContain("<strong>bold</strong>");
    expect(renderMd("**bold** and `code`")).toContain("<code>code</code>");
  });

  it("strips scripts and event attributes", () => {
    const html = renderMd("hello <script>alert(1)</script> <img src=x onerror=alert(1)>");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("onerror");
  });

  it("blocks dangerous URLs and keeps safe ones", () => {
    const html = renderMd("[bad](javascript:alert(1)) [ok](https://example.com)");
    expect(html).not.toContain("javascript:");
    expect(html).toContain('href="https://example.com"');
  });

  it("falls back to plain rendering when marked throws", () => {
    const out = renderMdFallback("```\ncode <tag>\n```\n\n# 标题");
    expect(out).toContain("<h1>标题</h1>");
    expect(out).toContain("&lt;tag&gt;");
    expect(out).not.toContain("<script>");
  });

  it("keeps markdown table headers and body cells in the same column structure", () => {
    const html = renderMd([
      "| 检查 | 命令 | 结果 |",
      "| --- | --- | --- |",
      "| history 分页专项 | `.venv/bin/python -m pytest tests/test_history_pagination.py` | 13 passed |",
      "| 全量回归 | `.venv/bin/python -m pytest tests/ -q` | 103 passed |",
    ].join("\n"));
    const doc = new DOMParser().parseFromString(html, "text/html");

    expect(doc.querySelectorAll("thead th")).toHaveLength(3);
    expect(doc.querySelectorAll("tbody tr")).toHaveLength(2);
    for (const row of Array.from(doc.querySelectorAll("tbody tr"))) {
      expect(row.querySelectorAll("td")).toHaveLength(3);
    }
  });

  it("keeps markdown tables in the native table layout so headers align with body columns", () => {
    const css = readFileSync(resolve(process.cwd(), "public/style.css"), "utf8");
    expect(css).toMatch(/\.bubble-text\.md table\s*\{[^}]*display:\s*table;/s);
    expect(css).not.toMatch(/\.bubble-text\.md table\s*\{[^}]*display:\s*block;/s);
    expect(css).toMatch(/\.bubble-text\.md\s*\{[^}]*overflow-x:\s*auto;/s);
  });

  it("sanitizes a DOM string like the legacy sanitizeHtml", () => {
    const doc = '<p onclick="x()">ok</p><iframe src="x"></iframe><a href="javascript:x">y</a>';
    const out = sanitizeHtml(doc);
    expect(out).not.toContain("<iframe");
    expect(out).not.toContain("onclick");
    expect(out).not.toContain("javascript:");
    expect(out).toContain("ok");
  });
});

// ---------------------------------------------------------------------------
// mXSS 回归（P0）：DOMParser 的 scripting 标志禁用 → <noscript> 内容按普通元素
// 解析、querySelectorAll("*") 不下钻 template.content；而渲染产物经
// dangerouslySetInnerHTML 写入活文档时 <noscript> 按 RAWTEXT 解析到第一个
// </noscript>。两套解析不一致会让属性值里的 "</noscript><img onerror=…>" 在
// 活文档中突破为真实元素。这里用 parse5 以活文档等价配置
// （scriptingEnabled: true）重解析消毒输出，断言无 on* 元素、无
// noscript/template 标签。
// ---------------------------------------------------------------------------
describe("sanitizeHtml mXSS hardening (parse5 live-doc reparse)", () => {
  type P5Node = { tagName?: string; attrs?: Array<{ name: string }>; childNodes?: P5Node[] };

  /** 活文档等价重解析（scripting 开启），收集危险残留：on* 元素 / noscript / template。 */
  const dangerousResidue = (html: string): string[] => {
    const tree = parse(html, { scriptingEnabled: true }) as unknown as P5Node;
    const found: string[] = [];
    const walk = (node: P5Node): void => {
      for (const child of node.childNodes ?? []) {
        if (child.tagName != null) {
          const tag = child.tagName.toLowerCase();
          const attrNames = (child.attrs ?? []).map((a) => a.name.toLowerCase());
          // 断言一：不存在带 on* 事件属性的元素（注入的执行载荷）
          const onAttrs = attrNames.filter((name) => /^on/i.test(name));
          if (onAttrs.length > 0) found.push(`${tag}[${onAttrs.join(",")}]`);
          // 断言二：不存在 noscript/template 标签（解析不一致载体，整体移除）
          if (tag === "noscript" || tag === "template") found.push(`<${tag}>`);
        }
        walk(child);
      }
    };
    walk(tree);
    return found;
  };

  it("任务复现载荷：noscript 属性值突破向量消毒后无 on*/noscript 残留", () => {
    const payload = '<noscript><p title="</noscript><img src=x onerror=alert(1)>"></p></noscript>';
    expect(dangerousResidue(sanitizeHtml(payload))).toEqual([]);
    expect(dangerousResidue(renderMd(payload))).toEqual([]);
  });

  it("div 包裹的 noscript 嵌套变体（修复前可真实穿透出 img[onerror]）", () => {
    const payload = '<div><noscript><p title="</noscript><img src=x onerror=alert(1)>"></p></noscript></div>';
    expect(dangerousResidue(sanitizeHtml(payload))).toEqual([]);
    expect(dangerousResidue(renderMd(payload))).toEqual([]);
  });

  it("template.content 不被选择器下钻：整体移除 template 标签", () => {
    const payload = "<div><template><img src=x onerror=alert(1)></template>ok</div>";
    const out = sanitizeHtml(payload);
    expect(out).toContain("ok");
    expect(out.toLowerCase()).not.toContain("<template");
    expect(out.toLowerCase()).not.toContain("<img");
    expect(dangerousResidue(out)).toEqual([]);
  });

  it("属性值中的 < > 被实体转义，活文档重解析不会突破出新标签", () => {
    const out = sanitizeHtml('<p title="a</p><img src=x onerror=alert(1)>b">t</p>');
    expect(out).toContain('title="a&lt;/p&gt;&lt;img src=x onerror=alert(1)&gt;b"');
    expect(dangerousResidue(out)).toEqual([]);
  });
});
