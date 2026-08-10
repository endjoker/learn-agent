// components.js —— 共享组件
// renderMd / toast / 元素构造助手。页面组件随 P3b-P3d 逐步扩充。

"use strict";

window.HA = window.HA || {};

// markdown 渲染（vendored marked；内容为 agent 自产，内网半可信，未做 XSS 净化）
HA.renderMd = function (text) {
  if (!text) return "";
  try {
    return marked.parse(String(text));
  } catch (e) {
    const d = document.createElement("div");
    d.textContent = String(text);
    return d.innerHTML;
  }
};

// 便捷元素构造：el("div", {class:"x", onclick:fn}, child1, child2)
HA.el = function (tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (v !== undefined && v !== null) node.setAttribute(k, v);
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
