// pages/agent-editor.js —— 智能体编辑（方案 7.1：两栏）
// 左：Agent 列表；右：编辑区（基础信息 / 系统提示词 / 能力 / 预览 / 保存栏）
// 路由：`#/agent-editor?id=agent-coder`
"use strict";

window.PageAgents = class {
  constructor() {
    this.state = {
      selectedId: null,
      serverProfile: null,
      draftProfile: null,
      dirty: false,
      loading: false,
      saving: false,
      previewing: false,
      catalogs: { tools: [], skills: [], mcp: [], models: [] },
      catalogErrors: {},
      versionConflict: false,
      references: [],
    };
    this._off = [];
    this._filter = "";
  }

  async render(root) {
    this._root = root;
    root.classList.add("ws-page", "agent-editor-page");
    this._buildLayout();
    this._bindLeaveProtection();
    await Promise.all([this._loadCatalogs(), this.refreshList()]);
    this._applyRoute();
  }

  destroy() {
    this._off.forEach(f => f());
    this._off = [];
  }

  _parseQuery() {
    const out = {};
    const q = (location.hash.split("?")[1] || "");
    for (const pair of q.split("&")) {
      if (!pair) continue;
      const [k, v] = pair.split("=");
      out[decodeURIComponent(k)] = decodeURIComponent(v || "");
    }
    return out;
  }

  async _applyRoute() {
    const id = this._parseQuery().id || "";
    if (id && this.state.selectedId !== id) {
      await this._select(id);
    }
  }

  // ---------- 布局（两栏） ----------
  _buildLayout() {
    const layout = HA.el("div", { class: "ws-layout ws-layout-2col" });
    // 左：Agent 列表
    this._left = HA.el("div", { class: "ws-panel ws-left" });
    const leftHead = HA.el("div", { class: "ws-panel-head" },
      HA.el("span", { text: "智能体" }),
      HA.el("button", { class: "btn", text: "＋ 新建",
        onclick: () => this._newProfile() }));
    this._listBox = HA.el("div", { class: "ws-panel-body" });
    this._left.append(leftHead, this._listBox);
    // 右：编辑区
    this._editor = HA.el("div", { class: "ws-panel" });
    this._editorHead = HA.el("div", { class: "ws-panel-head" });
    this._editorBody = HA.el("div", { class: "ws-panel-body ws-editor-body" });
    this._editor.append(this._editorHead, this._editorBody);
    layout.append(this._left, this._editor);
    this._root.appendChild(layout);
  }

  _bindLeaveProtection() {
    const before = (e) => {
      if (this.state.dirty) { e.preventDefault(); e.returnValue = ""; }
    };
    window.addEventListener("beforeunload", before);
    this._off.push(() => window.removeEventListener("beforeunload", before));
  }

  // ---------- 数据加载 ----------
  async _loadCatalogs() {
    try {
      const d = await HA.api("GET", "/api/agents/catalog", undefined, { silent: true });
      this.state.catalogs = d || this.state.catalogs;
    } catch (e) {
      this.state.catalogErrors.tools = "Catalog 加载失败，工具/技能选择可能不可用";
      HA.toast("Catalog 加载失败（局部降级）", "err");
    }
  }

  async refreshList() {
    try {
      const [active, archived] = await Promise.all([
        HA.api("GET", "/api/agents?status=active&limit=200", undefined, { silent: true }),
        HA.api("GET", "/api/agents?status=archived&limit=200", undefined, { silent: true }),
      ]);
      this._agents = [...(active.agents || []), ...(archived.agents || [])];
    } catch (e) { return; }
    this._renderList();
    if (!this.state.selectedId && this._agents.length) {
      await this._select(this._agents[0].profile_id);
    } else if (this.state.selectedId) {
      await this._select(this.state.selectedId);
    } else {
      this._showEmpty();
    }
  }

  _renderList() {
    this._listBox.innerHTML = "";
    if (!this._filter) {
      this._listBox.appendChild(HA.el("input", { type: "text", placeholder: "搜索智能体…",
        style: "width:100%;box-sizing:border-box;margin-bottom:8px",
        oninput: (e) => { this._filter = e.target.value.toLowerCase(); this._renderList(); } }));
    }
    const shown = (this._agents || []).filter(a =>
      !this._filter || (a.name || "").toLowerCase().includes(this._filter)
      || (a.description || "").toLowerCase().includes(this._filter));
    if (!shown.length) {
      this._listBox.appendChild(HA.el("div", { class: "ws-empty",
        text: this._filter ? "无匹配智能体" : "暂无智能体，点击「新建」创建第一个" }));
      return;
    }
    for (const p of shown) {
      const active = p.profile_id === this.state.selectedId;
      const systemTag = p.is_system
        ? HA.el("span", { class: "ws-tag system", text: "\u5185\u7f6e" })
        : null;
      const capabilities = `${p.tools.length} \u5de5\u5177 \u00b7 ${p.skills.length} \u6280\u80fd \u00b7 ${p.mcp_servers.length} MCP`;
      const statusTag = p.status === "archived"
        ? HA.el("span", { class: "ws-tag disabled", text: "\u5df2\u505c\u7528" })
        : HA.el("span", { class: "ws-tag enabled", text: "\u5df2\u542f\u7528" });
      this._listBox.appendChild(HA.el("div", {
        class: "ws-item ws-agent-nav-item" + (active ? " active" : ""),
        title: `${p.name}\n${p.description || ""}\n${capabilities}`,
        onclick: () => this._select(p.profile_id),
      },
        HA.el("div", { class: "ws-item-title" }, HA.el("span", { text: p.name }), systemTag || null, statusTag),
        HA.el("div", { class: "ws-item-sub", text: capabilities })));
    }

  }
  _showEmpty() {
    this._editorHead.innerHTML = "";
    this._editorBody.innerHTML = "";
    this._editorBody.appendChild(HA.el("div", { class: "ws-empty",
      text: "选择或创建一个智能体开始编辑" }));
  }

  async _select(id) {
    if (this.state.dirty) {
      const ok = await HA.confirm("当前有未保存的修改，切换将丢失。继续？", "继续");
      if (!ok) return;
    }
    this.state.selectedId = id;
    this.state.dirty = false;
    this.state.versionConflict = false;
    await this._loadProfile(id);
    this._renderList();
  }

  async _loadProfile(id) {
    try {
      const p = await HA.api("GET", `/api/agents/${encodeURIComponent(id)}`, undefined, { silent: true });
      this.state.serverProfile = p;
      this.state.draftProfile = JSON.parse(JSON.stringify(p));
      this._renderEditor();
    } catch (e) { /* list handles */ }
  }

  // ---------- 新建 / 保存 / 复制 / 归档 ----------
  _newProfile() {
    if (this.state.dirty) {
      HA.toast("请先保存或放弃当前修改", "err");
      return;
    }
    const blank = {
      profile_id: "", name: "", description: "", system_prompt: "",
      tools: [], skills: [], mcp_servers: [], default_model: "",
      permission_mode: "ask", chat_mode: "chat", max_steps: 100,
      include_tools: [], exclude_tools: [], version: 1,
    };
    this.state.serverProfile = null;
    this.state.draftProfile = blank;
    this.state.selectedId = null;
    this.state.dirty = true;
    this._renderEditor();
    this._renderList();
  }

  _markDirty() { this.state.dirty = true; this._updateDirtyBar(); }

  async _save() {
    const d = this.state.draftProfile;
    if (this.state.saving) return;
    if (!d.name || !d.name.trim()) { HA.toast("\u540d\u79f0\u4e0d\u80fd\u4e3a\u7a7a", "err"); return; }
    this.state.saving = true;
    this._renderEditor();
    try {
      let saved;
      if (d.profile_id) {
        saved = await HA.api("PUT", `/api/agents/${encodeURIComponent(d.profile_id)}`,
          { ...d, version: this.state.serverProfile ? this.state.serverProfile.version : d.version },
          { silent: true, onConflict: () => {
            this.state.versionConflict = true;
            HA.toast("\u670d\u52a1\u5668\u5df2\u6709\u65b0\u7248\u672c\uff0c\u8bf7\u91cd\u65b0\u52a0\u8f7d\u540e\u7f16\u8f91", "err");
          }});
        if (!saved || saved.error) return;
      } else {
        saved = await HA.api("POST", "/api/agents", d);
      }
      this.state.serverProfile = saved;
      this.state.draftProfile = JSON.parse(JSON.stringify(saved));
      this.state.selectedId = saved.profile_id;
      this.state.dirty = false;
      this.state.versionConflict = false;
      HA.toast("\u5df2\u4fdd\u5b58", "ok");
      await this.refreshList();
    } catch (e) {
      if (e.status !== 409) HA.toast("\u4fdd\u5b58\u5931\u8d25: " + e.message, "err");
    } finally {
      this.state.saving = false;
      if (this.state.draftProfile) this._renderEditor();
    }
  }

  async _delete() {
    const d = this.state.draftProfile;
    if (!d || !d.profile_id || d.is_system) return;
    const ok = await HA.confirm("\u5220\u9664\u8be5\u667a\u80fd\u4f53\uff1f\u5df2\u4fdd\u5b58\u7684\u8fd0\u884c\u5feb\u7167\u4ecd\u4f1a\u4fdd\u7559\uff1b\u88ab\u5de5\u4f5c\u533a\u5f15\u7528\u65f6\u65e0\u6cd5\u5220\u9664\u3002", "\u5220\u9664");
    if (!ok) return;
    try {
      await HA.api("DELETE", `/api/agents/${encodeURIComponent(d.profile_id)}`,
        { version: this.state.serverProfile ? this.state.serverProfile.version : d.version });
      HA.toast("\u5df2\u5220\u9664", "ok");
      this.state.selectedId = null;
      this.state.serverProfile = null;
      this.state.draftProfile = null;
      this.state.dirty = false;
      await this.refreshList();
    } catch (e) {
      HA.toast("\u5220\u9664\u5931\u8d25: " + e.message, "err");
    }
  }

  async _duplicate() {
    if (!this.state.draftProfile || !this.state.draftProfile.profile_id) return;
    try {
      const dup = await HA.api("POST",
        `/api/agents/${encodeURIComponent(this.state.draftProfile.profile_id)}/duplicate`, {});
      this.state.dirty = false;
      HA.toast(`已复制为「${dup.name}」`, "ok");
      await this.refreshList();
      await this._select(dup.profile_id);
    } catch (e) { HA.toast("复制失败: " + e.message, "err"); }
  }

  async _toggleEnabled() {
    const d = this.state.draftProfile;
    if (!d || !d.profile_id || d.is_system) return;
    const enabling = d.status === "archived";
    const action = enabling ? "\u542f\u7528" : "\u505c\u7528";
    const message = enabling
      ? "\u542f\u7528\u8be5\u667a\u80fd\u4f53\uff1f"
      : "\u505c\u7528\u8be5\u667a\u80fd\u4f53\uff1f\u505c\u7528\u540e\u4e0d\u80fd\u88ab\u65b0\u5de5\u4f5c\u533a\u6216\u4f1a\u8bdd\u9009\u62e9\uff0c\u4f46\u4fdd\u7559\u914d\u7f6e\u4e0e\u7248\u672c\u8bb0\u5f55\u3002";
    const ok = await HA.confirm(message, action);
    if (!ok) return;
    try {
      const updated = await HA.api("POST",
        `/api/agents/${encodeURIComponent(d.profile_id)}/${enabling ? "activate" : "archive"}`,
        { version: this.state.serverProfile ? this.state.serverProfile.version : d.version });
      this.state.serverProfile = updated;
      this.state.draftProfile = JSON.parse(JSON.stringify(updated));
      this.state.dirty = false;
      HA.toast(enabling ? "\u5df2\u542f\u7528" : "\u5df2\u505c\u7528", "ok");
      await this.refreshList();
    } catch (e) {
      HA.toast((enabling ? "\u542f\u7528" : "\u505c\u7528") + "\u5931\u8d25: " + e.message, "err");
    }
  }

  async _loadReferences() {
    if (!this.state.draftProfile || !this.state.draftProfile.profile_id) return;
    try {
      const d = await HA.api("GET",
        `/api/agents/${encodeURIComponent(this.state.draftProfile.profile_id)}/references`,
        undefined, { silent: true });
      this.state.references = d.references || [];
      this._renderReferences();
    } catch (e) { /* ignore */ }
  }

  // ---------- 编辑渲染 ----------
  _renderEditor() {
    const d = this.state.draftProfile;
    if (!d) { this._showEmpty(); return; }
    this._editorHead.innerHTML = "";
    this._editorHead.appendChild(HA.el("span",
      { text: d.profile_id ? `编辑：${d.name}` : "新建智能体" }));
    if (d.is_system) {
      this._editorHead.appendChild(HA.el("span",
        { class: "ws-tag system", text: "\u5185\u7f6e" }));
      this._editorHead.appendChild(HA.el("span",
        { class: "dim", style: "font-size:11px",
          text: "\u5185\u7f6e\u9ed8\u8ba4\u667a\u80fd\u4f53\u53ef\u4ee5\u7f16\u8f91\u4e0e\u4fdd\u5b58\uff1b\u4e0d\u5141\u8bb8\u5220\u9664\u6216\u5f52\u6863" }));
    }
    this._editorBody.innerHTML = "";
    this._editorBody.appendChild(this._buildBasicForm(d));
    this._editorBody.appendChild(HA.el("hr", { class: "ws-divider" }));
    this._editorBody.appendChild(this._buildCapabilityPanel(d));
    // 保存栏
    const isBuiltin = Boolean(d.is_system);
    const bar = HA.el("div", { class: "ws-savebar" },
      HA.el("button", { class: "btn primary",
        text: this.state.saving ? "\u4fdd\u5b58\u4e2d\u2026" : "\u4fdd\u5b58",
        disabled: this.state.saving,
        onclick: () => this._save() }),
      HA.el("button", { class: "btn", text: "\u590d\u5236", onclick: () => this._duplicate() }),
      d.profile_id && !isBuiltin ? HA.el("button", { class: "btn",
        text: d.status === "archived" ? "\u542f\u7528" : "\u505c\u7528",
        onclick: () => this._toggleEnabled() }) : null,
      d.profile_id && !isBuiltin ? HA.el("button", { class: "btn danger", text: "\u5220\u9664",
        onclick: () => this._delete() }) : null,
      this._dirtySpan = HA.el("span", { class: "ws-dirty", text: "" }),
      this.state.versionConflict ? HA.el("span", { class: "ws-warn",
        text: "\u26a0\ufe0f \u670d\u52a1\u5668\u5df2\u6709\u65b0\u7248\u672c" }) : null);
    this._editorBody.appendChild(bar);
    this._loadReferences();
    this._updateDirtyBar();
  }

  _buildBasicForm(d) {
    const form = HA.el("section", { class: "ws-editor-section" },
      HA.el("div", { class: "ws-section-head" },
        HA.el("div", { class: "ws-section-title", text: "基础信息与提示词" }),
        HA.el("div", { class: "ws-section-desc", text: "定义智能体身份、职责和运行时默认设置" })));
    const f = (label, node) => HA.el("div", { class: "ws-field" },
      HA.el("label", { text: label }), node);
    const row = HA.el("div", { class: "ws-editor-grid" });
    row.appendChild(f("名称 *", HA.el("input", { type: "text", value: d.name || "",
      oninput: (e) => { d.name = e.target.value; this._markDirty(); } })));
    row.appendChild(f("描述", HA.el("input", { type: "text", value: d.description || "",
      oninput: (e) => { d.description = e.target.value; this._markDirty(); } })));
    form.appendChild(row);
    // 系统提示词 编辑器（方案 7.4：带字符计数）
    const promptLabel = HA.el("label", { text: "系统提示词" });
    const counter = HA.el("span", { class: "dim", style: "font-size:11px;margin-left:8px" });
    const promptTa = HA.el("textarea", {
      class: "ws-prompt-input", text: d.system_prompt || "",
      oninput: (e) => { d.system_prompt = e.target.value; counter.textContent = `字符 ${d.system_prompt.length} / 16,000`; this._markDirty(); } });
    counter.textContent = `字符 ${(d.system_prompt || "").length} / 16,000`;
    form.appendChild(HA.el("div", { class: "ws-field" }, promptLabel, counter, promptTa));
    const prefs = HA.el("div", { class: "ws-editor-grid" });
    prefs.appendChild(f("默认模型", HA.el("select",
      { onchange: (e) => { d.default_model = e.target.value; this._markDirty(); } },
      HA.el("option", { value: "", text: "（继承 Gateway 默认）" }),
      ...(this.state.catalogs.models || []).map(m =>
        HA.el("option", { value: m.id, text: m.id, selected: d.default_model === m.id })))));
    prefs.appendChild(f("\u6743\u9650", HA.el("select",
      { onchange: (e) => { d.permission_mode = e.target.value; this._markDirty(); } },
      [
        ["readonly", "\u53ea\u8bfb \u00b7 \u4ec5\u9605\u8bfb\u4e0e\u641c\u7d22"],
        ["ask", "\u9010\u6b21\u786e\u8ba4 \u00b7 \u6bcf\u6b21\u64cd\u4f5c\u524d\u786e\u8ba4"],
        ["allow", "\u5141\u8bb8 \u00b7 \u6267\u884c\u5df2\u6388\u6743\u64cd\u4f5c"],
        ["unreviewed", "\u514d\u5ba1\u67e5 \u00b7 \u8df3\u8fc7\u5ba1\u6279"],
      ].map(([value, text]) =>
        HA.el("option", { value, text, selected: d.permission_mode === value })
      )
    )));
    prefs.appendChild(f("会话模式", HA.el("select",
      { onchange: (e) => { d.chat_mode = e.target.value; this._markDirty(); } },
      ["chat", "plan"].map(m =>
        HA.el("option", { value: m, text: m, selected: d.chat_mode === m })))));
    prefs.appendChild(f("最大步数", HA.el("input", { type: "number", value: d.max_steps ?? 100, min: 1,
      oninput: (e) => { d.max_steps = parseInt(e.target.value, 10) || 100; this._markDirty(); } })));
    form.appendChild(prefs);
    // 引用信息
    this._refBox = HA.el("div", { class: "ws-field" });
    form.appendChild(this._refBox);
    return form;
  }

  _renderReferences() {
    if (!this._refBox) return;
    this._refBox.innerHTML = "";
    const refs = this.state.references;
    if (!refs.length) return;
    this._refBox.appendChild(HA.el("label", { text: "被以下工作区引用" }));
    this._refBox.appendChild(HA.el("div", { class: "ws-tags" },
      ...refs.map(r => HA.el("span", { class: "ws-tag", text: `${r.name}（${r.status}）` }))));
  }

  _buildCapabilityPanel(d) {
    const panel = HA.el("section", { class: "ws-editor-section" },
      HA.el("div", { class: "ws-section-head" },
        HA.el("div", { class: "ws-section-title", text: "能力配置与最终提示词" }),
        HA.el("div", { class: "ws-section-desc", text: "选择工具、技能和 MCP，并在保存前预览最终提示词" })));
    const toggle = (listKey) => (id, checked) => {
      const list = d[listKey] || [];
      d[listKey] = checked
        ? (list.includes(id) ? list : [...list, id])
        : list.filter(x => x !== id);
      this._markDirty();
    };
    const tools = (this.state.catalogs.tools || []).map(t => ({
      id: t.name, name: t.name, risk: t.risk, available: t.available,
    }));
    panel.appendChild(HA.el("div", { class: "ws-field" },
      // RISK_EXPLANATION: low=read/query, medium=write/network, high=command/code execution.
      HA.el("label", { text: `\u5de5\u5177\uff08${(d.tools || []).length}\uff09` }),
      HA.el("div", { class: "dim", style: "font-size:11px;margin:3px 0 7px",
        text: "\u98ce\u9669\u8bf4\u660e\uff1a\u4f4e\u98ce\u9669=\u53ea\u8bfb/\u67e5\u8be2\uff1b\u4e2d\u98ce\u9669=\u5199\u5165\u6587\u4ef6\u6216\u7f51\u7edc\u8bf7\u6c42\uff1b\u9ad8\u98ce\u9669=\u6267\u884c\u547d\u4ee4\u6216\u4ee3\u7801\u3002\u5b9e\u9645\u6267\u884c\u4ecd\u53d7\u6743\u9650\u4e0e\u5ba1\u6279\u63a7\u5236\u3002" }),
      new HA.SearchSelector({
        items: tools, selected: new Set(d.tools || []),
        onToggle: toggle("tools"), placeholder: "\u641c\u7d22\u5de5\u5177\u2026",
      }).render()));
    panel.appendChild(HA.el("div", { class: "ws-field" },
      HA.el("label", { text: `技能（${(d.skills || []).length}）` }),
      new HA.SearchSelector({
        items: (this.state.catalogs.skills || []).map(s => ({ id: s.id, name: s.name })),
        selected: new Set(d.skills || []),
        onToggle: toggle("skills"), placeholder: "搜索 技能…",
      }).render()));
    panel.appendChild(HA.el("div", { class: "ws-field" },
      HA.el("label", { text: `MCP 服务（${(d.mcp_servers || []).length}）` }),
      new HA.SearchSelector({
        items: (this.state.catalogs.mcp && this.state.catalogs.mcp.servers || [])
          .map(s => ({
            id: s.name,
            name: s.available ? s.name : `${s.name}\uff08\u672a\u8fde\u63a5\uff1b\u8fd0\u884c时会尝试连接\uff09`,
            // A configured MCP server remains selectable even before a live agent
            // has established its connection. The catalog status is informational.
            available: true,
            unavailable_reason: s.available ? "" : "\u5f53\u524d\u672a\u8fde\u63a5\uff1b\u4f1a\u5728\u8fd0\u884c\u65f6\u6309\u914d\u7f6e\u5efa\u7acb\u8fde\u63a5",
          })),
        selected: new Set(d.mcp_servers || []),
        onToggle: toggle("mcp_servers"), placeholder: "搜索 MCP 服务…",
      }).render()));
    const previewHead = HA.el("div", { class: "ws-actions" },
      HA.el("button", { class: "btn", text: this.state.previewing ? "预览中…" : "🔍 预览 提示词",
        onclick: () => this._preview() }));
    this._previewBox = HA.el("div", {});
    panel.appendChild(previewHead);
    panel.appendChild(this._previewBox);
    if (this.state.previewData) {
      this._previewBox.appendChild(new HA.PromptPreview(this.state.previewData).render());
    }
    return panel;
  }

  async _preview() {
    const d = this.state.draftProfile;
    if (!d) return;
    this.state.previewing = true;
    try {
      const data = await HA.api("POST", "/api/agents/preview", { profile: d });
      this.state.previewData = data;
      this._previewBox.innerHTML = "";
      this._previewBox.appendChild(new HA.PromptPreview(data).render());
    } catch (e) {
      HA.toast("预览失败: " + e.message, "err");
    } finally {
      this.state.previewing = false;
    }
  }

  _updateDirtyBar() {
    if (this._dirtySpan) {
      this._dirtySpan.textContent = this.state.dirty ? "● 未保存" : "";
    }
  }
};
