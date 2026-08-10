// pages/settings.js —— 设置页（P3d，保守白名单）
// 大模型（列表 + 厂商向导 + 默认模型）/ 工作区 / Prompt 截断 / Gateway 会话
"use strict";

window.PageSettings = class {
  constructor() { this._offs = []; this._rev = 0; }

  async render(root) {
    this._root = root;
    root.appendChild(HA.el("h1", { text: "⚙️ 设置" }));
    this._note = HA.el("div", { class: "dim settings-note" },
      "保守白名单：仅 llm / gateway.sessions / prompt / workspace 可编辑；" +
      "mcp.servers 在 MCP 页管理；permission / sandbox / hooks / gateway 主段请手编 config.json。");
    root.appendChild(this._note);
    this._container = HA.el("div", { class: "settings-sections" });
    root.appendChild(this._container);
    this._offs.push(HA.onSSE("config.updated", () => this._loadConfig()));
    await this._loadConfig();
  }

  async _loadConfig() {
    let d;
    try {
      d = await HA.api("GET", "/api/config", undefined, { silent: true });
    } catch (e) { return; }
    this._cfg = d.config || {};
    this._rev = d.rev;
    this._renderSections();
  }

  _renderSections() {
    this._container.innerHTML = "";
    this._container.append(
      this._sectionLLM(),
      this._sectionWorkspace(),
      this._sectionPrompt(),
      this._sectionSessions());
  }

  _card(title, ...body) {
    return HA.el("div", { class: "settings-card" },
      HA.el("h2", { text: title }), ...body);
  }

  // ---------- 大模型 ----------
  _sectionLLM() {
    const llm = this._cfg.llm || {};
    const models = llm.models || {};

    const table = HA.el("table", {},
      HA.el("tr", {},
        HA.el("th", { text: "名称" }), HA.el("th", { text: "base_url / provider" }),
        HA.el("th", { text: "api_key" }), HA.el("th", { text: "操作" })));
    for (const [name, cfg] of Object.entries(models)) {
      const isDefault = llm.model_id === name;
      table.appendChild(HA.el("tr", {},
        HA.el("td", {}, HA.el("b", { text: name }),
          isDefault ? HA.badge("默认", "ok") : null),
        HA.el("td", { class: "mono",
          text: cfg.base_url || cfg.provider || "-" }),
        HA.el("td", { class: "mono", text: cfg.api_key || "-" }),
        HA.el("td", { class: "ops" },
          HA.el("button", { class: "btn", text: "编辑",
            onclick: () => this._editModel(name, cfg) }),
          isDefault ? null : HA.el("button", { class: "btn", text: "设为默认",
            onclick: () => this._setDefaultModel(name) }),
          HA.el("button", { class: "btn danger", text: "删除",
            onclick: () => this._deleteModel(name) }))));
    }

    const defaultSel = HA.el("select", {},
      ...Object.keys(models).map(n =>
        HA.el("option", { value: n, text: n,
          ...(n === llm.model_id ? { selected: "selected" } : {}) })));
    defaultSel.addEventListener("change", () => this._setDefaultModel(defaultSel.value));

    return this._card("大模型",
      HA.el("div", { class: "form-row" },
        HA.el("label", { class: "dim" }, "默认模型"), defaultSel),
      table,
      HA.el("div", { class: "form-row", style: "margin-top:10px" },
        HA.el("button", { class: "btn primary", text: "＋ 按厂商添加模型",
          onclick: () => this._addModelWizard() })));
  }

  async _setDefaultModel(name) {
    try {
      await HA.api("PUT", "/api/config/llm", { model_id: name });
      HA.toast(`默认模型: ${name}`, "ok");
      this._loadConfig();
    } catch (e) { }
  }

  async _deleteModel(name) {
    if (!confirm(`删除模型 ${name}？`)) return;
    try {
      await HA.api("DELETE", `/api/config/models/${encodeURIComponent(name)}`);
      HA.toast("已删除", "ok");
      this._loadConfig();
    } catch (e) { }
  }

  // 编辑模型（#4）：api_key 仅以掩码占位提示，输入框不明文回显；留空=保留原值
  _editModel(name, cfg) {
    const baseIn = HA.el("input", { value: cfg.base_url || "" });
    const ctxIn = HA.el("input", { type: "number", value: cfg.context_length || "" });
    const keyIn = HA.el("input", { type: "password", value: "",
      placeholder: `当前 ${cfg.api_key || "（无）"} — 留空保持不变` });
    const save = async () => {
      const body = { base_url: baseIn.value.trim() };
      if (ctxIn.value) body.context_length = parseInt(ctxIn.value, 10);
      if (keyIn.value) body.api_key = keyIn.value;  // 空→后端保留原值
      try {
        await HA.api("PUT", `/api/config/models/${encodeURIComponent(name)}`, body);
        HA.toast("已保存", "ok");
        modal.remove();
        this._loadConfig();
      } catch (e) { }
    };
    const modal = HA.el("div", { class: "modal-mask" },
      HA.el("div", { class: "modal" },
        HA.el("h2", { text: `编辑模型 ${name}` }),
        HA.el("label", { class: "form-label" }, "base_url", baseIn),
        HA.el("label", { class: "form-label" }, "context_length", ctxIn),
        HA.el("label", { class: "form-label" }, "api_key（不明文显示）", keyIn),
        HA.el("div", { class: "modal-actions" },
          HA.el("button", { class: "btn primary", text: "保存", onclick: save }),
          HA.el("button", { class: "btn", text: "取消", onclick: () => modal.remove() }))));
    document.body.appendChild(modal);
  }

  async _addModelWizard() {
    let prov;
    try {
      prov = await HA.api("GET", "/api/config/providers");
    } catch (e) { return; }

    // 第 1 步：选厂商
    const step1 = HA.el("div", { class: "modal" },
      HA.el("h2", { text: "选择厂商 / 协议" }));
    const grid = HA.el("div", { class: "prov-grid" });
    const pick = (spec) => { this._addModelForm(spec, prov); modal.remove(); };

    for (const [key, p] of Object.entries(prov.local || {})) {
      grid.appendChild(HA.el("button", { class: "prov-card",
        onclick: () => pick({ type: "local", provider: key,
          base_url: p.base_url, api_key: p.api_key, label: p.name }) },
        HA.el("b", { text: p.name }),
        HA.el("div", { class: "dim", text: p.desc || "" })));
    }
    for (const c of prov.cloud || []) {
      grid.appendChild(HA.el("button", { class: "prov-card",
        onclick: () => pick({ type: "cloud", protocol: c.protocol,
          base_url: c.default_url, api_key: "", label: c.label }) },
        HA.el("b", { text: c.label }),
        HA.el("div", { class: "dim",
          text: `env: ${(prov.env_hints || {})[c.protocol] || "—"}` })));
    }
    step1.appendChild(grid);
    const modal = HA.el("div", { class: "modal-mask" }, step1);
    document.body.appendChild(modal);
  }

  _addModelForm(spec, prov) {
    const nameIn = HA.el("input", { placeholder: "模型名称（唯一）" });
    const urlIn = HA.el("input", { value: spec.base_url || "" });
    const hint = (prov.env_hints || {})[spec.protocol];
    const keyIn = HA.el("input", { type: "password",
      placeholder: hint ? `留空 = 使用环境变量 ${hint}` : "api_key" });
    const ctxIn = HA.el("input", { type: "number",
      value: spec.type === "local" ? 131072 : 128000 });

    const save = async () => {
      const body = {
        name: nameIn.value.trim(),
        type: spec.type,
        base_url: urlIn.value.trim(),
        api_key: keyIn.value,
        context_length: parseInt(ctxIn.value || "0", 10),
      };
      if (spec.type === "local") body.provider = spec.provider;
      else body.protocol = spec.protocol;
      if (!body.name) return HA.toast("名称必填", "err");
      try {
        await HA.api("POST", "/api/config/models", body);
        HA.toast(`已添加模型 ${body.name}，可在会话页 /model 切换`, "ok");
        modal.remove();
        this._loadConfig();
      } catch (e) { }
    };

    const modal = HA.el("div", { class: "modal-mask" },
      HA.el("div", { class: "modal" },
        HA.el("h2", { text: `添加模型 — ${spec.label}` }),
        HA.el("label", { class: "form-label" }, "名称", nameIn),
        HA.el("label", { class: "form-label" }, "base_url", urlIn),
        HA.el("label", { class: "form-label" }, "api_key", keyIn),
        HA.el("label", { class: "form-label" }, "context_length", ctxIn),
        HA.el("div", { class: "modal-actions" },
          HA.el("button", { class: "btn primary", text: "保存", onclick: save }),
          HA.el("button", { class: "btn", text: "取消",
            onclick: () => modal.remove() }))));
    document.body.appendChild(modal);
  }

  // ---------- 工作区 ----------
  _sectionWorkspace() {
    const ws = this._cfg.workspace || {};
    const pathIn = HA.el("input", { value: ws.path || "./workspace" });
    const saveBtn = HA.el("button", { class: "btn primary", text: "保存",
      onclick: () => this._patch("workspace", { path: pathIn.value },
        "工作区已保存（重启后生效）") });
    return this._card("工作区",
      HA.el("label", { class: "form-label" }, "workspace.path", pathIn),
      HA.el("div", { class: "form-row" }, saveBtn));
  }

  // ---------- Prompt 截断 ----------
  _sectionPrompt() {
    const p = this._cfg.prompt || {};
    const perFile = HA.el("input", { type: "number",
      value: p.bootstrap_max_chars_per_file ?? 8000 });
    const total = HA.el("input", { type: "number",
      value: p.bootstrap_max_chars_total ?? 32000 });
    const warn = HA.el("select", {},
      ...["once", "always", "never"].map(w =>
        HA.el("option", { value: w, text: w,
          ...(p.truncation_warning === w ? { selected: "selected" } : {}) })));
    const saveBtn = HA.el("button", { class: "btn primary", text: "保存",
      onclick: () => this._patch("prompt", {
        bootstrap_max_chars_per_file: parseInt(perFile.value, 10),
        bootstrap_max_chars_total: parseInt(total.value, 10),
        truncation_warning: warn.value,
      }, "Prompt 截断已保存（新会话生效）") });
    return this._card("Prompt 截断",
      HA.el("label", { class: "form-label" }, "每文件上限 (bootstrap_max_chars_per_file)", perFile),
      HA.el("label", { class: "form-label" }, "总量上限 (bootstrap_max_chars_total)", total),
      HA.el("label", { class: "form-label" }, "截断告警 (truncation_warning)", warn),
      HA.el("div", { class: "form-row" }, saveBtn));
  }

  // ---------- Gateway 会话 ----------
  _sectionSessions() {
    const s = (this._cfg.gateway || {}).sessions || {};
    const mk = (key, label, def) => {
      const inp = HA.el("input", { type: "number", value: s[key] ?? def });
      return { key, label, inp };
    };
    const fields = [
      mk("max_sessions", "最大会话数", 50),
      mk("idle_timeout_minutes", "空闲超时（分钟）", 60),
      mk("soft_timeout_seconds", "软超时（秒）", 90),
      mk("hard_timeout_seconds", "硬超时（秒）", 600),
    ];
    const saveBtn = HA.el("button", { class: "btn primary", text: "保存",
      onclick: () => {
        const patch = {};
        for (const f of fields) patch[f.key] = parseInt(f.inp.value, 10);
        this._patch("gateway.sessions", patch, "会话配置已保存（重启后生效）");
      } });
    return this._card("Gateway 会话",
      ...fields.map(f => HA.el("label", { class: "form-label" }, f.label, f.inp)),
      HA.el("div", { class: "form-row" }, saveBtn));
  }

  async _patch(section, patch, okMsg) {
    try {
      await HA.api("PATCH", `/api/config/${encodeURIComponent(section)}`,
        { patch, base_rev: this._rev });
      HA.toast(okMsg, "ok");
      this._loadConfig();
    } catch (e) {
      if (e.status === 409) HA.toast("配置已被修改，已刷新，请重试", "err");
    }
  }

  destroy() { this._offs.forEach(f => f()); this._offs = []; }
};
