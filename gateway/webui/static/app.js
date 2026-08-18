// app.js —— hash 路由 + 全局唯一 EventSource + 页面分发
"use strict";

(function () {
  const PAGES = {
    chat: window.PageChat,
    mcp: window.PageMcp,
    skills: window.PageSkills,
    prompt: window.PagePrompt,
    status: window.PageStatus,
    cron: window.PageCron,
    settings: window.PageSettings,
    agents: window.PageAgents,
    "agent-editor": window.PageAgents,
    workspace: window.PageWorkspace,
  };

  const main = document.getElementById("main");
  let current = null;      // 当前页面实例
  let currentName = null;
  const sseListeners = {}; // type -> Set<handler>（页面在 render 时注册，destroy 时注销）

  // ---------- 全局唯一 EventSource ----------
  const sseState = document.getElementById("sse-state");
  let es = null;
  let lastEventId = 0;
  function connectSSE() {
    // Phase 5：带 Last-Event-ID 重连，服务端先补 backlog 再进实时
    const url = "/api/events" + (lastEventId ? "?last_event_id=" + lastEventId : "");
    try { es = new EventSource(url); } catch (e) { return; }
    es.onopen = function () {
      sseState.textContent = "SSE: 已连接";
      sseState.className = "sse-state ok";
    };
    es.onerror = function () {
      sseState.textContent = "SSE: 断开，重连中…";
      sseState.className = "sse-state err";
      // EventSource 内置自动重连；重连成功后 onopen 触发
    };
    es.onmessage = function (ev) {
      let evt;
      try { evt = JSON.parse(ev.data); } catch (e) { return; }
      if (evt.event_id && evt.event_id > lastEventId) lastEventId = evt.event_id;
      const sets = [sseListeners[evt.type], sseListeners["*"]];
      for (const set of sets) {
        if (!set) continue;
        for (const h of Array.from(set)) {
          try { h(evt.data || {}, evt); } catch (e) { console.error(e); }
        }
      }
    };
  }
  connectSSE();
  // 连接状态与 last event id 暴露给页面（重连/resync）
  HA.getSSEState = function () {
    return { connected: es && es.readyState === 1, lastEventId };
  };

  // 页面订阅/注销 SSE 事件（type 支持 "*" 通配，接收全部事件）
  HA.onSSE = function (type, handler) {
    const key = type || "*";
    (sseListeners[key] = sseListeners[key] || new Set()).add(handler);
    return function () { (sseListeners[key] || new Set()).delete(handler); };
  };

  // ---------- 路由 ----------
  function route() {
    const hash = location.hash || "#/chat";
    const name = hash.replace(/^#\//, "").split("?")[0] || "chat";
    const knownPage = Object.prototype.hasOwnProperty.call(PAGES, name);
    const PageCls = knownPage ? PAGES[name] : PAGES.chat;

    if (current && typeof current.destroy === "function") {
      try { current.destroy(); } catch (e) { console.error(e); }
    }
    main.innerHTML = "";
    document.querySelectorAll(".nav-item").forEach(a => {
      a.classList.toggle("active", a.dataset.page === (PAGES[name] ? name : "chat"));
    });

    const container = HA.el("div", { class: "page" });
    main.appendChild(container);
    if (typeof PageCls !== "function") {
      current = null;
      currentName = name;
      container.appendChild(HA.el("div", { class: "page-load-error" },
        HA.el("div", { class: "page-load-error-title", text: "页面模块加载失败" }),
        HA.el("div", { text: `「${name}」页面脚本未正确加载，请刷新页面或查看浏览器控制台。` })));
      return;
    }
    current = new PageCls();
    currentName = name;
    Promise.resolve(current.render(container)).catch((error) => {
      console.error(error);
      container.innerHTML = "";
      container.appendChild(HA.el("div", { class: "page-load-error" },
        HA.el("div", { class: "page-load-error-title", text: "页面渲染失败" }),
        HA.el("div", { text: error && error.message ? error.message : "未知错误" })));
    });
  }

  window.addEventListener("hashchange", route);
  route();

  // ---------- UI 版本检测：代码热更新后提示刷新（避免旧 JS 渲染异常）----------
  // 页面打开期间不会自动重载脚本；后端静态资源更新后，已打开的页面仍跑旧代码。
  // 定期用 no-store 拉取 index.html，对比其中资源版本号签名；不一致时提示刷新。
  function uiVersionSignature() {
    const list = [...document.querySelectorAll('script[src*="?v="], link[href*="?v="]')]
      .map(el => (el.src || el.href).split("/").pop())
      .sort()
      .join("|");
    return list;
  }
  const loadedSignature = uiVersionSignature();
  let versionWarned = false;
  async function checkUiVersion() {
    try {
      const resp = await fetch("/ui/", { cache: "no-store" });
      if (!resp.ok) return;
      const html = await resp.text();
      const latest = [...html.matchAll(/(?:script[^>]*src|link[^>]*href)="([^"]*\?v=[0-9.]+)"/g)]
        .map(m => m[1].split("/").pop())
        .sort()
        .join("|");
      if (latest && latest !== loadedSignature && !versionWarned) {
        versionWarned = true;
        HA.toast("检测到界面已更新，请刷新页面以加载新版本（Ctrl+Shift+R）", "ok");
      }
    } catch (e) { /* 版本检测失败静默 */ }
  }
  setInterval(checkUiVersion, 30000);
  checkUiVersion();
})();
