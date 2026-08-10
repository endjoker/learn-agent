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
  };

  const main = document.getElementById("main");
  let current = null;      // 当前页面实例
  let currentName = null;
  const sseListeners = {}; // type -> Set<handler>（页面在 render 时注册，destroy 时注销）

  // ---------- 全局唯一 EventSource ----------
  const sseState = document.getElementById("sse-state");
  let es = null;
  function connectSSE() {
    try { es = new EventSource("/api/events"); } catch (e) { return; }
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
      const set = sseListeners[evt.type];
      if (!set) return;
      for (const h of Array.from(set)) {
        try { h(evt.data || {}); } catch (e) { console.error(e); }
      }
    };
  }
  connectSSE();

  // 页面订阅/注销 SSE 事件
  HA.onSSE = function (type, handler) {
    (sseListeners[type] = sseListeners[type] || new Set()).add(handler);
    return function () { (sseListeners[type] || new Set()).delete(handler); };
  };

  // ---------- 路由 ----------
  function route() {
    const hash = location.hash || "#/chat";
    const name = hash.replace(/^#\//, "").split("?")[0] || "chat";
    const PageCls = PAGES[name] || PAGES.chat;

    if (current && typeof current.destroy === "function") {
      try { current.destroy(); } catch (e) { console.error(e); }
    }
    main.innerHTML = "";
    document.querySelectorAll(".nav-item").forEach(a => {
      a.classList.toggle("active", a.dataset.page === (PAGES[name] ? name : "chat"));
    });

    current = new PageCls();
    currentName = name;
    const container = HA.el("div", { class: "page" });
    main.appendChild(container);
    current.render(container);
  }

  window.addEventListener("hashchange", route);
  route();
})();
