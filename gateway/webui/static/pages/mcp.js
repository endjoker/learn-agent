// pages/mcp.js —— MCP 页（P3c）
// 服务器列表（配置 + 实时状态）/ 添加·编辑·删除 / 重连 / 应用到运行中会话
"use strict";

window.PageMcp = class {
  constructor() { this._offs = []; }

  async render(root) {
    this._root = root;
    root.appendChild(HA.el("div", { class: "page-head" },
      HA.el("h1", { text: "🔌 MCP 服务器" }),
      HA.el("button", { class: "btn primary", text: "＋ 添加",
        onclick: () => this._editModal(null) }),
      HA.el("button", { class: "btn", text: "🔄 应用到运行中会话",
        onclick: () => this._apply() })));
    this._list = HA.el("div", { class: "mcp-list" });
    root.appendChild(this._list);
    this._offs.push(HA.onSSE("mcp.changed", () => this.refresh()));
    await this.refresh();
  }

  async refresh() {
    let d;
    try {
      d = await HA.api("GET", "/api/mcp", undefined, { silent: true });
    } catch (e) { return; }
    this._renderList(d.servers || [], d.live || {});
  }

  _renderList(servers, live) {
    this._list.innerHTML = "";
    if (!servers.length) {
      this._list.appendChild(HA.el("div",
        { class: "placeholder", text: "暂无 MCP 服务器，点右上 [＋ 添加]" }));
      return;
    }
    const table = HA.el("table", {},
      HA.el("tr", {},
        HA.el("th", { text: "名称" }), HA.el("th", { text: "传输" }),
        HA.el("th", { text: "command / url" }), HA.el("th", { text: "env" }),
        HA.el("th", { text: "实时状态" }), HA.el("th", { text: "操作" })));
    for (const s of servers) {
      const lv = live[s.name] || { sessions: 0, initialized: false, tools: 0 };
      const target = s.transport === "stdio"
        ? `${s.command || ""} ${(s.args || []).join(" ")}`.trim()
        : (s.url || "");
      const badge = lv.sessions === 0
        ? HA.badge("未连接", "dim")
        : (lv.initialized
            ? HA.badge(`${lv.sessions} 会话 · ${lv.tools} 工具`, "ok")
            : HA.badge(`${lv.sessions} 会话 · 连接中`, "warn"));
      const opsTd = HA.el("td", { class: "ops" },
        HA.el("button", { class: "btn", text: "重连", onclick: () => this._reconnect(s.name) }),
        HA.el("button", { class: "btn", text: "编辑", onclick: () => this._editModal(s) }),
        HA.el("button", { class: "btn danger", text: "删除", onclick: () => this._remove(s.name) }));
      const tr = HA.el("tr", {},
        HA.el("td", {}, HA.el("b", { text: s.name }),
          s.trust ? HA.el("span", { class: "dim", text: " · trust" }) : null),
        HA.el("td", { text: s.transport || "" }),
        HA.el("td", { class: "mono", text: target }),
        HA.el("td", { class: "mono", text: s.env ? JSON.stringify(s.env) : "-" }),
        HA.el("td", {}, badge),
        opsTd);
      table.appendChild(tr);
    }
    this._list.appendChild(table);
  }

  _editModal(existing) {
    const isNew = !existing;
    const s = existing || { name: "", transport: "stdio", command: "",
      args: [], url: "", env: {}, headers: {}, enabled: true, trust: false };

    const nameIn = HA.el("input", { value: s.name, ...(isNew ? {} : { disabled: "disabled" }) });
    const transSel = HA.el("select", {},
      ...["stdio", "streamable", "sse", "http"].map(t =>
        HA.el("option", { value: t, text: t,
          ...(s.transport === t ? { selected: "selected" } : {}) })));
    const cmdIn = HA.el("input", { value: s.command || "", placeholder: "如 node" });
    const argsIn = HA.el("input", { value: (s.args || []).join(" "), placeholder: "空格分隔参数" });
    const urlIn = HA.el("input", { value: s.url || "", placeholder: "http://…" });
    const envTa = HA.el("textarea", { rows: "3", placeholder: "KEY=value 每行一个" },
      Object.entries(s.env || {}).map(([k, v]) => `${k}=${v}`).join("\n"));
    const trustChk = HA.el("input", { type: "checkbox", ...(s.trust ? { checked: "checked" } : {}) });
    const enabledChk = HA.el("input", { type: "checkbox", ...(s.enabled !== false ? { checked: "checked" } : {}) });

    const isStdio = () => transSel.value === "stdio";
    const stdioRow = HA.el("div", { class: "form-row" }, cmdIn, argsIn);
    const urlRow = HA.el("div", { class: "form-row" }, urlIn);
    const sync = () => {
      stdioRow.style.display = isStdio() ? "flex" : "none";
      urlRow.style.display = isStdio() ? "none" : "flex";
    };
    transSel.addEventListener("change", sync);

    const parseKV = txt => {
      const out = {};
      for (const line of String(txt).split("\n")) {
        const i = line.indexOf("=");
        if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
      }
      return out;
    };

    const save = async () => {
      const body = {
        name: nameIn.value.trim(),
        transport: transSel.value,
        enabled: enabledChk.checked,
        trust: trustChk.checked,
      };
      if (isStdio()) {
        body.command = cmdIn.value.trim();
        body.args = argsIn.value.trim().split(/\s+/).filter(Boolean);
      } else {
        body.url = urlIn.value.trim();
      }
      body.env = parseKV(envTa.value);
      if (!body.name) return HA.toast("name 必填", "err");
      try {
        if (isNew) await HA.api("POST", "/api/mcp/servers", body);
        else await HA.api("PUT", `/api/mcp/servers/${encodeURIComponent(s.name)}`, body);
        HA.toast("已写入配置，点 [应用到运行中会话] 立即生效", "ok");
        modal.remove();
        this.refresh();
      } catch (e) { }
    };

    const modal = HA.el("div", { class: "modal-mask" },
      HA.el("div", { class: "modal" },
        HA.el("h2", { text: isNew ? "添加 MCP 服务器" : `编辑 ${s.name}` }),
        HA.el("label", { class: "form-label" }, "名称", nameIn),
        HA.el("label", { class: "form-label" }, "传输", transSel),
        stdioRow, urlRow,
        HA.el("label", { class: "form-label" }, "env", envTa),
        HA.el("label", { class: "form-label" }, trustChk, " 信任（工具调用免确认）"),
        HA.el("label", { class: "form-label" }, enabledChk, " 启用该服务器"),
        HA.el("div", { class: "modal-actions" },
          HA.el("button", { class: "btn primary", text: "保存", onclick: save }),
          HA.el("button", { class: "btn", text: "取消",
            onclick: () => modal.remove() }))));
    sync();
    document.body.appendChild(modal);
  }

  async _reconnect(name) {
    try {
      await HA.api("POST",
        `/api/mcp/servers/${encodeURIComponent(name)}/reconnect`);
      HA.toast(`已广播重连 ${name}`, "ok");
    } catch (e) { }
  }

  async _remove(name) {
    if (!confirm(`删除 MCP 服务器 ${name}？`)) return;
    try {
      await HA.api("DELETE", `/api/mcp/servers/${encodeURIComponent(name)}`);
      HA.toast("已删除，点 [应用] 生效", "ok");
      this.refresh();
    } catch (e) { }
  }

  async _apply() {
    try {
      const d = await HA.api("POST", "/api/mcp/apply");
      HA.toast(`已广播 /mcp reload 到 ${d.queued} 个会话`, "ok");
    } catch (e) { }
  }

  destroy() { this._offs.forEach(f => f()); this._offs = []; }
};
