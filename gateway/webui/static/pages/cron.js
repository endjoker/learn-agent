// pages/cron.js —— 定时任务页（修 #6）
// 查看图/创建+编辑/启用暂停/手动触发/历史，支持飞书 announce 投递
"use strict";

window.PageCron = class {
  constructor() { this._offs = []; this._channels = []; }

  async render(root) {
    this._root = root;
    root.appendChild(HA.el("div", { class: "page-head" },
      HA.el("h1", { text: "⏰ 定时任务" }),
      HA.el("button", { class: "btn primary", text: "＋ 添加任务",
        onclick: () => this._editModal(null) })));
    this._list = HA.el("div");
    root.appendChild(this._list);
    this._hist = HA.el("div");
    root.appendChild(this._hist);
    this._offs.push(HA.onSSE("cron.changed", () => this.refresh()));
    await Promise.all([this._loadChannels(), this.refresh()]);
  }

  async _loadChannels() {
    try {
      const d = await HA.api("GET", "/api/scheduler/channels", undefined,
        { silent: true });
      this._channels = d.channels || [];
      this._webhooks = d.webhooks || [];
      this._targets = d.targets || {};
    } catch (e) { this._channels = []; this._webhooks = []; this._targets = {}; }
  }

  async refresh() {
    let d;
    try {
      d = await HA.api("GET", "/api/scheduler/jobs", undefined, { silent: true });
    } catch (e) { return; }
    this._renderList(d.jobs || []);
    try {
      d = await HA.api("GET", "/api/scheduler/history", undefined, { silent: true });
    } catch (e) { return; }
    this._renderHistory(d.history || []);
  }

  _renderList(jobs) {
    this._list.innerHTML = "";
    if (!jobs.length) {
      this._list.appendChild(HA.el("div",
        { class: "placeholder", text: "暂无定时任务，点 [＋ 添加任务]" }));
      return;
    }
    const table = HA.el("table", {},
      HA.el("tr", {},
        HA.el("th", { text: "名称" }), HA.el("th", { text: "schedule" }),
        HA.el("th", { text: "投递" }), HA.el("th", { text: "会话" }),
        HA.el("th", { text: "最近状态" }), HA.el("th", { text: "操作" })));
    for (const j of jobs) {
      const deliver = j.deliver ? `${j.deliver.mode || "none"}${j.deliver.channel ? "→" + j.deliver.channel : ""}` : "none";
      table.appendChild(HA.el("tr", {},
        HA.el("td", {}, HA.el("b", { text: j.name }),
          j.running ? HA.badge("进行中", "warn") : null,
          j.paused ? HA.badge("暂停", "dim") : null,
          !j.enabled ? HA.badge("禁用", "dim") : null),
        HA.el("td", { class: "mono", text: j.schedule || "" }),
        HA.el("td", { text: deliver }),
        HA.el("td", { text: j.session || "isolated" }),
        HA.el("td", {}, HA.el("span", {
          text: `${j.last_status || "-"} | ${j.runs || 0}次` + (j.failures ? `/fail ${j.failures}` : ""),
        })),
        HA.el("td", { class: "ops" },
          HA.el("button", { class: "btn", text: j.paused ? "恢复" : "暂停",
            onclick: () => this._action(j.name, j.paused ? "resume" : "pause") }),
          HA.el("button", { class: "btn primary", text: "▶ 手动触发",
            onclick: () => this._action(j.name, "run") }),
          HA.el("button", { class: "btn", text: "编辑",
            onclick: () => this._editModal(j) }),
          HA.el("button", { class: "btn danger", text: "删除",
            onclick: () => this._del(j.name) }))));
    }
    this._list.appendChild(table);
  }

  _renderHistory(hist) {
    this._hist.innerHTML = "";
    if (!hist.length) return;
    const t = HA.el("table", {},
      HA.el("tr", {},
        HA.el("th", { text: "时间" }), HA.el("th", { text: "任务" }),
        HA.el("th", { text: "状态" }), HA.el("th", { text: "耗时" }),
        HA.el("th", { text: "触发方式" })));
    for (const h of hist.slice(-10).reverse()) {
      t.appendChild(HA.el("tr", {},
        HA.el("td", { class: "mono", text: h.at || "" }),
        HA.el("td", { text: h.job }),
        HA.el("td", {}, HA.badge(h.status || "", h.status === "ok" ? "ok" : (h.status === "error" ? "err" : "dim"))),
        HA.el("td", { text: `${h.duration_s || 0}s` }),
        HA.el("td", { text: h.trigger || "" })));
    }
    this._hist.appendChild(HA.el("h2", { text: "最近执行历史" }));
    this._hist.appendChild(t);
  }

  async _action(name, action) {
    try {
      const d = await HA.api("POST", `/api/scheduler/jobs/${encodeURIComponent(name)}/${action}`);
      HA.toast(d.reply || "ok", "ok");
      this.refresh();
    } catch (e) { }
  }

  async _del(name) {
    if (!confirm(`删除定时任务 ${name}？`)) return;
    try {
      await HA.api("DELETE", `/api/scheduler/jobs/${encodeURIComponent(name)}`);
      HA.toast("已删除", "ok");
      this.refresh();
    } catch (e) { }
  }

  _editModal(existing) {
    const isNew = !existing;
    const j = existing || { name: "", schedule: "0 9 * * 1-5", prompt: "",
      session: "isolated", deliver: { mode: "none" }, timeout: 600, enabled: true };

    const nameIn = HA.el("input", { value: j.name, ...(isNew ? {} : { disabled: "disabled" }) });
    const schedIn = HA.el("input", { value: j.schedule || "" });
    const promptTa = HA.el("textarea", { rows: "4" }, j.prompt || "");
    const selOf = (pairs, cur) => HA.el("select", {},
      ...pairs.map(p => {
        const v = p.value !== undefined ? p.value : p;
        const l = p.label !== undefined ? p.label : p;
        return HA.el("option", { value: v, text: l,
          ...(cur === v ? { selected: "selected" } : {}) });
      }));
    const sessSel = selOf([
      { value: "isolated", label: "isolated（每次全新上下文）" },
      { value: "persist", label: "persist（固定会话累积）" },
    ], j.session);
    const timeoutIn = HA.el("input", { type: "number", value: j.timeout || 600 });

    // 投递模式（#3 中文标签）
    const modeSel = selOf([
      { value: "none", label: "none（仅日志）" },
      { value: "announce", label: "announce（推送到通道）" },
      { value: "webhook", label: "webhook（HTTP POST）" },
    ], (j.deliver || {}).mode);
    const chOpts = this._channels.length ? this._channels
      : [{ channel: "", hint: "无已启用通道" }];
    const chSel = HA.el("select", {},
      ...chOpts.map(c => HA.el("option",
        { value: c.channel, text: (c.channel || "(无)") + (c.hint ? `（${c.hint}）` : ""),
          ...((j.deliver || {}).channel === c.channel ? { selected: "selected" } : {}) })));
    // announce 目标下拉（#4：可选）
    const annSel = HA.el("select", {});
    const rebuildAnn = () => {
      const ch = chSel.value;
      const list = [...(((this._targets || {})[ch]) || [])];
      const cur = (j.deliver || {}).target || "";
      if (cur && (j.deliver || {}).channel === ch && !list.includes(cur)) list.push(cur);
      annSel.innerHTML = "";
      annSel.appendChild(HA.el("option",
        { value: "", text: list.length ? "（选择已有目标）" : "（无已有目标）" }));
      list.forEach(u => annSel.appendChild(HA.el("option", { value: u, text: u })));
      annSel.appendChild(HA.el("option", { value: "__custom__", text: "自定义…" }));
    };
    rebuildAnn();
    chSel.addEventListener("change", rebuildAnn);
    // webhook 目标下拉（#3）
    const whOpts = [...(this._webhooks || [])];
    const curWh = (j.deliver || {}).mode === "webhook" ? ((j.deliver || {}).target || "") : "";
    if (curWh && !whOpts.includes(curWh)) whOpts.push(curWh);
    const whSel = HA.el("select", {},
      HA.el("option", { value: "", text: whOpts.length ? "（选择已有 webhook）" : "（无已有 webhook）" }),
      ...whOpts.map(u => HA.el("option", { value: u, text: u,
        ...(curWh === u ? { selected: "selected" } : {}) })),
      HA.el("option", { value: "__custom__", text: "自定义…" }));
    const targetIn = HA.el("input", { value: (j.deliver || {}).target || "",
      placeholder: "announce 填 chat_id（oc_ 开头）；webhook 填 URL" });

    const syncDeliver = () => {
      const m = modeSel.value;
      chSel.parentElement.style.display = m === "announce" ? "" : "none";
      annSel.parentElement.style.display = m === "announce" ? "" : "none";
      whSel.parentElement.style.display = m === "webhook" ? "" : "none";
      const showTarget =
        (m === "announce" && annSel.value === "__custom__") ||
        (m === "webhook" && whSel.value === "__custom__");
      targetIn.parentElement.style.display = showTarget ? "" : "none";
    };
    modeSel.addEventListener("change", syncDeliver);
    whSel.addEventListener("change", syncDeliver);
    annSel.addEventListener("change", syncDeliver);

    const save = async () => {
      const m = modeSel.value;
      let target;
      if (m === "announce") {
        target = (annSel.value === "__custom__" || annSel.value === "")
          ? targetIn.value.trim() : annSel.value;
      } else if (m === "webhook") {
        target = (whSel.value === "__custom__" || whSel.value === "")
          ? targetIn.value.trim() : whSel.value;
      }
      const body = {
        name: nameIn.value.trim(),
        schedule: schedIn.value.trim(),
        prompt: promptTa.value.trim(),
        session: sessSel.value,
        deliver: { mode: m,
          channel: m === "announce" ? chSel.value : undefined,
          target: target || undefined },
        timeout: parseInt(timeoutIn.value, 10) || 600,
        enabled: true,
      };
      if (!body.name || !body.schedule || !body.prompt) return HA.toast("name/schedule/prompt 必填", "err");
      if (m === "announce" && !(body.deliver.channel && body.deliver.target))
        return HA.toast("announce 需选择通道和目标", "err");
      try {
        await HA.api("POST", "/api/scheduler/jobs", body);
        HA.toast(isNew ? "已创建" : "已更新", "ok");
        modal.remove();
        this.refresh();
      } catch (e) { }
    };

    const modal = HA.el("div", { class: "modal-mask" },
      HA.el("div", { class: "modal" },
        HA.el("h2", { text: isNew ? "添加定时任务" : `编辑 ${j.name}` }),
        HA.el("label", { class: "form-label" }, "名称", nameIn),
        HA.el("label", { class: "form-label" }, "cron 表达式", schedIn),
        HA.el("label", { class: "form-label" }, "prompt", promptTa),
        HA.el("label", { class: "form-label" }, "会话模式", sessSel),
        HA.el("label", { class: "form-label" }, "投递模式", modeSel),
        HA.el("label", { class: "form-label", style: "display:none" }, "通道", chSel),
        HA.el("label", { class: "form-label", style: "display:none" }, "announce 目标", annSel),
        HA.el("label", { class: "form-label", style: "display:none" }, "webhook 目标", whSel),
        HA.el("label", { class: "form-label" }, "自定义目标", targetIn),
        HA.el("label", { class: "form-label" }, "超时（秒）", timeoutIn),
        HA.el("div", { class: "modal-actions" },
          HA.el("button", { class: "btn primary", text: "保存", onclick: save }),
          HA.el("button", { class: "btn", text: "取消",
            onclick: () => modal.remove() }))));
    syncDeliver();
    document.body.appendChild(modal);
  }

  destroy() { this._offs.forEach(f => f()); this._offs = []; }
};
