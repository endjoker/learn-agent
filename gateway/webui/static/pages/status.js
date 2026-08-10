// pages/status.js —— 状态面板（P3a 实现）
"use strict";

window.PageStatus = class {
  constructor() {
    this._offs = [];
    this._timer = null;
  }

  render(root) {
    root.appendChild(HA.el("h1", { text: "📊 状态面板" }));
    this._kpi = HA.el("div", { class: "cards" });
    this._chan = HA.el("div");
    this._sess = HA.el("div");
    this._evt = HA.el("div", { id: "evt-log" });
    root.append(
      this._kpi,
      HA.el("h2", { text: "通道" }), this._chan,
      HA.el("h2", { text: "会话明细" }), this._sess,
      HA.el("h2", { text: "SSE 事件流（实时）" }), this._evt,
    );

    this.refresh();
    this._timer = setInterval(() => this.refresh(), 30000); // 轮询兜底
    // SSE 增量
    this._offs.push(HA.onSSE("channel.status", () => this.refresh()));
    ["chat.started", "chat.done", "chat.error", "chat.progress",
     "session.created", "session.evicted",
     "cron.fired", "cron.done", "cron.skipped", "heartbeat.done"]
      .forEach(t => this._offs.push(HA.onSSE(t, d => this.log(t, d))));
  }

  log(type, data) {
    const summary = JSON.stringify(data || {}).slice(0, 160);
    this._evt.prepend(HA.el("div", {
      text: `[${new Date().toLocaleTimeString()}] ${type}: ${summary}`,
    }));
    while (this._evt.children.length > 50) this._evt.lastChild.remove();
  }

  async refresh() {
    try {
      const s = await HA.api("GET", "/api/status", undefined, { silent: true });
      this.draw(s);
    } catch (e) { /* 静默：保留上次渲染 */ }
  }

  draw(s) {
    const ex = s.executor || {};
    const ses = s.sessions || {};
    const cards = [
      ["会话", `${ses.active ?? 0}/${ses.max ?? 0}`],
      ["线程池", `${ex.workers ?? 0} 槽 · 排队 ${ex.pending ?? 0}`],
    ];
    if (s.scheduler && s.scheduler.present) {
      cards.push(["定时任务", `${s.scheduler.jobs ?? 0} 个 · 运行 ${s.scheduler.running ? s.scheduler.running.length : 0}`]);
    }
    if (s.heartbeat && s.heartbeat.present) {
      const hb = s.heartbeat;
      cards.push(["心跳", hb.paused ? "已暂停" : `${hb.every || ""} · ${hb.beats || 0} 轮`]);
    }
    this._kpi.innerHTML = "";
    for (const [k, v] of cards) {
      this._kpi.appendChild(HA.el("div", { class: "card" },
        HA.el("div", { class: "k", text: k }),
        HA.el("div", { class: "v", text: String(v) })));
    }

    // 通道表
    const chanRows = Object.entries(s.channels || {}).map(([name, st]) =>
      HA.el("tr", {},
        HA.el("td", { text: name }),
        HA.el("td", {}, HA.badge(st.status || "?",
          st.status === "running" || st.status === "ok" ? "ok" : "dim")),
        HA.el("td", { text: JSON.stringify(st).slice(0, 80) })));
    this._chan.innerHTML = "";
    this._chan.appendChild(HA.el("table", {},
      HA.el("tr", {}, HA.el("th", { text: "通道" }), HA.el("th", { text: "状态" }), HA.el("th", { text: "详情" })),
      ...chanRows));

    // 会话明细表
    const busy = new Set(ses.busy || []);
    const sessRows = (ses.list || []).map(e =>
      HA.el("tr", {},
        HA.el("td", { text: e.session_key }),
        HA.el("td", { text: e.model || "-" }),
        HA.el("td", { text: String(e.message_count ?? 0) }),
        HA.el("td", {}, busy.has(e.session_key)
          ? HA.badge("busy", "warn") : HA.badge("idle", "dim"))));
    this._sess.innerHTML = "";
    this._sess.appendChild(HA.el("table", {},
      HA.el("tr", {}, HA.el("th", { text: "会话" }), HA.el("th", { text: "模型" }),
        HA.el("th", { text: "消息数" }), HA.el("th", { text: "状态" })),
      ...sessRows));
  }

  destroy() {
    this._offs.forEach(f => f());
    if (this._timer) clearInterval(this._timer);
  }
};
