// components.js —— 共享组件
// renderMd / toast / 元素构造助手。页面组件随 P3b-P3d 逐步扩充。

"use strict";

window.HA = window.HA || {};

// markdown 渲染（vendored marked；Phase 5 增加基础 XSS 净化：
// 剥掉 script/iframe/事件属性，危险 URL 仅允许 http/https/mailto）
HA.renderMd = function (text) {
  if (!text) return "";
  let html;
  try {
    html = marked.parse(String(text));
  } catch (e) {
    // marked 异常时降级为基础 Markdown 渲染（同样支持粗体/标题/列表/代码块），
    // 绝不把 Markdown 源码直接输出成未格式化内容。
    return HA.renderMdFallback(String(text));
  }
  return HA.sanitizeHtml(html);
};

// marked 不可用/抛错时的降级渲染：转义 HTML + 渲染基础 Markdown 语法
// （标题 / 粗体 / 斜体 / 行内代码 / 链接 / 列表 / 代码块），避免显示源码。
HA.renderMdFallback = function (text) {
  const esc = (s) => String(s).replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
  // 行内标记：先转义 HTML，再替换 Markdown 语法（顺序保证安全）
  const inline = (s) => esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" rel="noopener" target="_blank">$1</a>');
  const lines = String(text).split("\n");
  let out = "", list = null, inCode = false, fence = "";
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
    if (hm) { closeList(); closeCode(); const lv = hm[1].length; out += `<h${lv}>${inline(hm[2])}</h${lv}>`; continue; }
    const lm = line.match(/^\s*[-*+]\s+(.*)$/);
    if (lm) { closeCode(); if (list !== "ul") { closeList(); out += "<ul>"; list = "ul"; } out += `<li>${inline(lm[1])}</li>`; continue; }
    const om = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (om) { closeCode(); if (list !== "ol") { closeList(); out += "<ol>"; list = "ol"; } out += `<li>${inline(om[1])}</li>`; continue; }
    if (!line.trim()) { closeList(); continue; }
    closeList(); closeCode();
    out += `<p>${inline(line)}</p>`;
  }
  closeList(); closeCode();
  return out;
};

// 基础 HTML 净化（DOM 白名单过滤）
HA.sanitizeHtml = function (html) {
  const doc = new DOMParser().parseFromString(String(html), "text/html");
  const walker = (root) => {
    for (const el of Array.from(root.querySelectorAll("*"))) {
      const tag = el.tagName.toLowerCase();
      if (["script", "iframe", "object", "embed", "link", "style", "meta", "base", "form"].includes(tag)) {
        el.remove();
        continue;
      }
      for (const attr of Array.from(el.attributes)) {
        const name = attr.name.toLowerCase();
        if (name.startsWith("on")) { el.removeAttribute(attr.name); continue; }
        if (name === "href" || name === "src") {
          const v = (attr.value || "").trim().toLowerCase();
          if (!/^(https?:|mailto:|#|\/|\.\/|\.\.\/)/.test(v)) {
            el.removeAttribute(attr.name);
          }
        }
        if (name === "style") {
          const v = (attr.value || "").toLowerCase();
          if (v.includes("expression") || v.includes("javascript:")) {
            el.removeAttribute(attr.name);
          }
        }
      }
    }
  };
  walker(doc.body);
  return doc.body.innerHTML;
};

// 便捷元素构造：el("div", {class:"x", onclick:fn}, child1, child2)
HA.el = function (tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2), v);
      } else if (k === "text") {
        node.textContent = v;
      } else if (k === "html") {
        node.innerHTML = v;
      } else if (["checked", "selected", "disabled", "multiple", "readonly", "required"].includes(k)) {
        // Boolean HTML attributes are true by presence.  Setting
        // disabled="false" would still disable a control, so assign the DOM
        // property and only emit the attribute for a true value.
        node[k] = Boolean(v);
        if (v) node.setAttribute(k, "");
      } else if (v !== undefined && v !== null) {
        node.setAttribute(k, v);
      }
    }
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
};

// toast 通知
HA.toast = function (msg, kind) {
  const root = document.getElementById("toast-root");
  const t = HA.el("div", { class: "toast" + (kind ? " " + kind : "") }, msg);
  root.appendChild(t);
  setTimeout(() => t.remove(), 4000);
};

// fetch 封装：统一 JSON、4xx/5xx toast、409 特殊回调
HA.api = async function (method, url, body, opts) {
  opts = opts || {};
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(url, init);
  } catch (e) {
    if (!opts.silent) HA.toast("网络错误: " + e.message, "err");
    throw e;
  }
  let data = null;
  const text = await resp.text();
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!resp.ok) {
    if (resp.status === 409 && opts.onConflict) { opts.onConflict(data); return data; }
    const msg = (data && data.error) ? data.error : (resp.status + " " + resp.statusText);
    if (!opts.silent) HA.toast(msg, "err");
    const err = new Error(msg);
    err.status = resp.status; err.data = data;
    throw err;
  }
  return data;
};

// 状态徽章
HA.badge = function (text, kind) {
  return HA.el("span", { class: "badge " + (kind || "dim"), text });
};
