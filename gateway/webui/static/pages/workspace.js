// pages/workspace.js —— 工作区管理（Phase 3）
// 三栏：工作区列表/会话列表 · 会话占位 · 配置摘要 + 创建向导
"use strict";

window.PageWorkspace = class {
  constructor() {
    this.state = {
      workspaces: [],
      selectedWs: null,
      sessions: [],
      selectedSession: null,
      loading: false,
      creating: false,
      wizardStep: 0,
      wizard: this._blankWizard(),
      agents: null,
      agentsLoadError: "",
      catalogs: { tools: [], skills: [], mcp: [], models: [] },
      expandedWorkspaceId: null,
    };
    this._reqSeq = 0;
    this._off = [];
  }

  _blankWizard() {
    // Workspace owns project metadata only. Runtime configuration belongs to sessions.
    return {
      name: "", description: "", project_path: "", risk_confirmed: false,
      pathResult: null, create_first_session: false,
      first_session_name: "\u9996\u4e2a\u4f1a\u8bdd", agent_profile_id: "", model: "",
      permission_mode: "ask", chat_mode: "chat", reasoning_level: "inherit",
    };
  }

  async render(root) {
    this._root = root;
    root.classList.add("ws-page", "workspace-page");
    this._buildLayout();
    this._bindHash();
    await Promise.all([this._loadCatalogs(), this._loadAgents(), this.refresh()]);
    this._applyRoute();
  }

  destroy() {
    this._off.forEach(f => f());
    this._off = [];
  }

  // ---------- 路由 ----------
  _bindHash() {
    const h = () => this._applyRoute();
    window.addEventListener("hashchange", h);
    this._off.push(() => window.removeEventListener("hashchange", h));
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

  _applyRoute() {
    const q = this._parseQuery();
    // 方案 5.3：`#/workspace?id=ws_001&session=wss_001`（兼容旧 workspace 参数）
    const wid = q.id || q.workspace || "";
    const sid = q.session || "";
    if (wid && this.state.selectedWs !== wid) {
      this._selectWorkspace(wid, sid);
    } else if (sid && this.state.selectedSession !== sid) {
      this._selectSession(sid);
    }
  }

  _setRoute(wsId, sessId) {
    const params = new URLSearchParams();
    if (wsId) params.set("id", wsId);
    if (sessId) params.set("session", sessId);
    const target = "#/workspace" + (params.toString() ? "?" + params.toString() : "");
    if (location.hash !== target) location.hash = target;
  }

  // ---------- 布局 ----------
  _buildLayout() {
    const layout = HA.el("div", { class: "ws-layout" });
    this._left = HA.el("div", { class: "ws-panel ws-left" });
    this._leftHead = HA.el("div", { class: "ws-panel-head" });
    this._leftBody = HA.el("div", { class: "ws-panel-body" });
    this._left.append(this._leftHead, this._leftBody);
    this._center = HA.el("div", { class: "ws-panel" });
    this._centerHead = HA.el("div", { class: "ws-panel-head" });
    this._centerBody = HA.el("div", { class: "ws-panel-body" });
    this._center.append(this._centerHead, this._centerBody);
    this._right = HA.el("div", { class: "ws-panel ws-right" });
    this._rightHead = HA.el("div", { class: "ws-panel-head", text: "\u9879\u76ee\u4fe1\u606f" });
    this._rightBody = HA.el("div", { class: "ws-panel-body" });
    this._right.append(this._rightHead, this._rightBody);
    layout.append(this._left, this._center, this._right);
    this._root.appendChild(layout);
  }

  async _loadCatalogs() {
    try {
      const d = await HA.api("GET", "/api/agents/catalog", undefined, { silent: true });
      this.state.catalogs = d || this.state.catalogs;
    } catch (e) { /* 局部降级 */ }
  }

  // ---------- 数据 ----------
  // Agent profiles must be loaded before the wizard renders its selector.
  async _loadAgents({ force = false } = {}) {
    if (this._agentsPromise && !force) return this._agentsPromise;
    this.state.agentsLoadError = "";
    this._agentsPromise = HA.api(
      "GET", "/api/agents?status=active&limit=200", undefined, { silent: true })
      .then((data) => {
        this.state.agents = Array.isArray(data && data.agents) ? data.agents : [];
        return this.state.agents;
      })
      .catch((error) => {
        this.state.agents = [];
        this.state.agentsLoadError = error && error.message
          ? error.message : "Agent profiles failed to load";
        return [];
      })
      .finally(() => { this._agentsPromise = null; });
    return this._agentsPromise;
  }

  _selectDefaultAgent(draft, field = "agent_profile_id") {
    if (draft[field] || !this.state.agents || !this.state.agents.length) return;
    const preferred = this.state.agents.find((agent) => agent.profile_id === "agent_coder")
      || this.state.agents[0];
    draft[field] = preferred.profile_id;
  }

  async refresh() {
    const seq = ++this._reqSeq;
    let d;
    try {
      d = await HA.api("GET", "/api/workspaces?limit=200", undefined, { silent: true });
    } catch (e) { return; }
    if (seq !== this._reqSeq) return; // 迟到响应丢弃
    this.state.workspaces = d.workspaces || [];
    // 会话数徽标（并行、静默失败）
    this.state.wsSessionCounts = {};
    await Promise.all((this.state.workspaces || []).map(async (w) => {
      try {
        const sd = await HA.api("GET",
          `/api/workspaces/${encodeURIComponent(w.workspace_id)}/sessions`,
          undefined, { silent: true });
        this.state.wsSessionCounts[w.workspace_id] = (sd.sessions || []).length;
      } catch (e) { this.state.wsSessionCounts[w.workspace_id] = 0; }
    }));
    if (seq !== this._reqSeq) return;
    this._renderLeft();
  }

  async _selectWorkspace(id, preferSession) {
    const seq = ++this._reqSeq;
    this.state.selectedWs = id;
    this.state.expandedWorkspaceId = id;
    this.state.selectedWsData = null;
    this.state.sessions = [];
    this.state.selectedSession = null;
    this._setRoute(id, preferSession || "");
    this._renderLeft();
    this._renderCenter();
    this._renderRight();
    try {
      const [wsData, sessData] = await Promise.all([
        HA.api("GET", `/api/workspaces/${encodeURIComponent(id)}`, undefined, { silent: true }),
        HA.api("GET", `/api/workspaces/${encodeURIComponent(id)}/sessions`, undefined, { silent: true }),
      ]);
      if (seq !== this._reqSeq) return;
      this.state.selectedWsData = wsData;
      this.state.sessions = sessData.sessions || [];
      // 方案 5.2：工作区列表 → 工作区详情。选中工作区只展开其会话列表，
      // 不自动进入某个会话；仅当路由明确指定并验证归属后进入时间线。
      const requestedSession = preferSession && this.state.sessions.some(
        s => s.session_id === preferSession) ? preferSession : null;
      this.state.selectedSession = requestedSession;
      if (preferSession && !requestedSession) this._setRoute(id, "");
      this._renderLeft();
      this._renderCenter();
      this._renderRight();
      if (requestedSession) this._loadSessionHistory(id, requestedSession);
    } catch (e) { /* ignore */ }
  }

  async _selectSession(id) {
    const workspaceId = this.state.selectedWs;
    this.state.selectedSession = id;
    this._setRoute(workspaceId, id);
    this._renderLeft();
    this._renderCenter();
    this._renderRight();
    await this._loadSessionHistory(workspaceId, id);
  }

  // ---------- 左栏（方案 6.2：工作区/会话） ----------
  _renderLeft() {
    this._leftHead.innerHTML = "";
    this._leftHead.append(
      HA.el("span", { text: "\u5de5\u4f5c\u533a" }),
      HA.el("button", { class: "btn", text: "\uff0b \u65b0\u5efa", onclick: () => this._openWizard() }));
    this._leftBody.innerHTML = "";
    const search = HA.el("input", { type: "text", placeholder: "\u641c\u7d22\u9879\u76ee\u2026",
      class: "ws-nav-search",
      oninput: (e) => { this._wsFilter = e.target.value.toLowerCase(); this._renderLeft(); } });
    this._leftBody.appendChild(search);
    const shown = (this.state.workspaces || []).filter(w =>
      !this._wsFilter || (w.name || "").toLowerCase().includes(this._wsFilter)
      || (w.project_path || "").toLowerCase().includes(this._wsFilter));
    if (!shown.length) {
      this._leftBody.appendChild(HA.el("div", { class: "ws-empty",
        text: this._wsFilter ? "\u65e0\u5339\u914d\u5de5\u4f5c\u533a" : "\u6682\u65e0\u5de5\u4f5c\u533a" }));
      return;
    }
    for (const w of shown) {
      const active = w.workspace_id === this.state.selectedWs;
      const expanded = active && this.state.expandedWorkspaceId === w.workspace_id;
      const toggle = HA.el("button", {
        class: "ws-tree-toggle", text: expanded ? "\u2304" : "\u203a",
        title: expanded ? "\u6298\u53e0\u4f1a\u8bdd" : "\u5c55\u5f00\u4f1a\u8bdd",
        onclick: (event) => { event.stopPropagation(); this._toggleWorkspaceTree(w.workspace_id); },
      });
      const deleteButton = HA.el("button", {
        class: "ws-nav-action danger", text: "\u00d7", title: "\u5220\u9664\u5de5\u4f5c\u533a",
        onclick: (event) => { event.stopPropagation(); this._deleteWorkspace(w); },
      });
      const item = HA.el("div", {
        class: "ws-item ws-workspace-item" + (active ? " active" : ""),
        title: `${w.name || "\u5de5\u4f5c\u533a"}\n${w.project_path || ""}`,
        onclick: () => this._toggleWorkspaceTree(w.workspace_id),
      },
        HA.el("div", { class: "ws-item-title" }, toggle,
          HA.el("span", { class: "ws-nav-name", text: w.name || "\u672a\u547d\u540d\u5de5\u4f5c\u533a" }),
          deleteButton),
        HA.el("div", { class: "ws-item-sub ws-project-path", text: w.project_path || "\uff08\u672a\u914d\u7f6e\u76ee\u5f55\uff09" }));
      this._leftBody.appendChild(item);
      if (!expanded) continue;
      const sessBox = HA.el("div", { class: "ws-session-tree" },
        HA.el("button", { class: "ws-session-new", text: "\uff0b \u65b0\u4f1a\u8bdd",
          onclick: () => this._openSessionCreate() }));
      if (!this.state.sessions.length) {
        sessBox.appendChild(HA.el("div", { class: "ws-empty", text: "\u6682\u65e0\u4f1a\u8bdd" }));
      }
      for (const s of this.state.sessions) {
        const deleteSessionButton = HA.el("button", {
          class: "ws-nav-action danger", text: "\u00d7", title: "\u5220\u9664\u4f1a\u8bdd",
          onclick: (event) => { event.stopPropagation(); this._deleteSession(s); },
        });
        sessBox.appendChild(HA.el("div", {
          class: "ws-item ws-session-item" + (s.session_id === this.state.selectedSession ? " active" : ""),
          title: `${s.name || s.session_id}\n${this._agentName(s.agent_profile_id)}`,
          onclick: () => this._selectSession(s.session_id),
        },
          HA.el("div", { class: "ws-item-title" },
            HA.el("span", { class: "ws-nav-name", text: s.name || s.session_id }), deleteSessionButton),
          HA.el("div", { class: "ws-item-sub", text: this._agentName(s.agent_profile_id) })));
      }
      this._leftBody.appendChild(sessBox);
    }
  }

  _agentName(profileId) {
    if (!profileId) return "\u672a\u914d\u7f6e\u667a\u80fd\u4f53";
    const found = (this.state.agents || []).find(agent => agent.profile_id === profileId);
    return found ? found.name : profileId;
  }

  async _toggleWorkspaceTree(id) {
    if (this.state.selectedWs !== id) {
      await this._selectWorkspace(id);
      this.state.expandedWorkspaceId = id;
      this._renderLeft();
      return;
    }
    this.state.expandedWorkspaceId = this.state.expandedWorkspaceId === id ? null : id;
    this._renderLeft();
  }

  async _deleteWorkspace(workspace) {
    const ok = await HA.confirm(
      `${"\u5220\u9664\u5de5\u4f5c\u533a"}\u300c${workspace.name || workspace.workspace_id}\u300d\uff1f\n\n${"\u8be5\u64cd\u4f5c\u5c06\u5f7b\u5e95\u5220\u9664\u5de5\u4f5c\u533a\u53ca\u5176\u6240\u6709\u4f1a\u8bdd\u3001\u957f\u671f\u8bb0\u5fc6\uff0c\u4e0d\u53ef\u6062\u590d\uff1b\u9879\u76ee\u6587\u4ef6\u4e0d\u4f1a\u88ab\u5220\u9664\u3002"}\u3002`, "\u5f7b\u5e95\u5220\u9664");
    if (!ok) return;
    try {
      await HA.api("DELETE", `/api/workspaces/${encodeURIComponent(workspace.workspace_id)}`, {});
      if (this.state.selectedWs === workspace.workspace_id) {
        this.state.selectedWs = null;
        this.state.selectedSession = null;
        this.state.selectedWsData = null;
        this.state.sessions = [];
        this.state.expandedWorkspaceId = null;
        this._setRoute("", "");
        this._renderCenter();
        this._renderRight();
      }
      await this.refresh();
      HA.toast("\u5de5\u4f5c\u533a\u5df2\u5220\u9664", "ok");
    } catch (e) { HA.toast("\u5220\u9664\u5de5\u4f5c\u533a\u5931\u8d25: " + e.message, "err"); }
  }

  async _deleteSession(session) {
    const ok = await HA.confirm(
      `${"\u5220\u9664\u4f1a\u8bdd"}\u300c${session.name || session.session_id}\u300d\uff1f\n\n${"\u4f1a\u8bdd\u5386\u53f2\u5c06\u88ab\u79fb\u51fa\u6d3b\u52a8\u5217\u8868"}\u3002`, "\u5220\u9664");
    if (!ok) return;
    try {
      await HA.api("DELETE", `/api/workspaces/${encodeURIComponent(session.workspace_id)}/sessions/${encodeURIComponent(session.session_id)}`, {});
      if (this._chatStates) this._chatStates.delete(session.workspace_id + ":" + session.session_id);
      if (this.state.selectedSession === session.session_id) this.state.selectedSession = null;
      await this._selectWorkspace(session.workspace_id);
      this.state.expandedWorkspaceId = session.workspace_id;
      this._renderLeft();
      HA.toast("\u4f1a\u8bdd\u5df2\u5220\u9664", "ok");
    } catch (e) { HA.toast("\u5220\u9664\u4f1a\u8bdd\u5931\u8d25: " + e.message, "err"); }
  }

  _sessionConfigForm(draft, { heading = "\u4f1a\u8bdd\u914d\u7f6e", description = "\u9009\u62e9\u5df2\u914d\u7f6e\u597d\u7684\u667a\u80fd\u4f53\uff0c\u5e76\u8bbe\u7f6e\u672c\u4f1a\u8bdd\u7684\u8fd0\u884c\u504f\u597d\u3002" } = {}) {
    const box = HA.el("div", { class: "ws-session-config" },
      HA.el("div", { class: "ws-section-head" },
        HA.el("div", { class: "ws-section-title", text: heading }),
        HA.el("div", { class: "ws-section-desc", text: description })));
    const field = (label, node) => HA.el("div", { class: "ws-field" }, HA.el("label", { text: label }), node);
    const agents = this.state.agents || [];
    const loading = this.state.agents === null;
    const agentSelect = HA.el("select", { disabled: loading,
      onchange: (e) => { draft.agent_profile_id = e.target.value; } },
      HA.el("option", { value: "", text: loading ? "\u6b63\u5728\u52a0\u8f7d\u667a\u80fd\u4f53\u2026" : "\u8bf7\u9009\u62e9\u667a\u80fd\u4f53" }),
      ...agents.map(agent => HA.el("option", {
        value: agent.profile_id,
        text: agent.is_system ? `${agent.name}\uff08\u5185\u7f6e\uff09` : agent.name,
        selected: draft.agent_profile_id === agent.profile_id,
      })));
    const agentField = field("\u667a\u80fd\u4f53 *", agentSelect);
    if (this.state.agentsLoadError) agentField.appendChild(HA.el("div", { class: "ws-warn", text: this.state.agentsLoadError }));
    if (!loading && !agents.length) agentField.appendChild(HA.el("div", { class: "ws-warn", text: "\u6682\u65e0\u53ef\u7528\u667a\u80fd\u4f53\uff0c\u8bf7\u5148\u5230\u300c\u667a\u80fd\u4f53\u7f16\u8f91\u300d\u521b\u5efa\u3002" }));
    const nameValue = Object.prototype.hasOwnProperty.call(draft, "first_session_name") ? draft.first_session_name : draft.name;
    box.appendChild(field("\u4f1a\u8bdd\u540d\u79f0", HA.el("input", { type: "text", value: nameValue || "\u65b0\u4f1a\u8bdd",
      oninput: (e) => { if (Object.prototype.hasOwnProperty.call(draft, "first_session_name")) draft.first_session_name = e.target.value; else draft.name = e.target.value; } })));
    box.appendChild(agentField);
    const grid = HA.el("div", { class: "ws-editor-grid" });
    grid.appendChild(field("\u6a21\u578b", HA.el("select", { onchange: (e) => { draft.model = e.target.value; } },
      HA.el("option", { value: "", text: "\uff08\u7ee7\u627f Gateway \u9ed8\u8ba4\uff09" }),
      ...(this.state.catalogs.models || []).map(m => HA.el("option", { value: m.id, text: m.id, selected: draft.model === m.id })) )));
    grid.appendChild(field("\u601d\u8003\u6a21\u5f0f", HA.el("select", { onchange: (e) => { draft.reasoning_level = e.target.value; } },
      ...[["inherit", "\u7ee7\u627f\u6a21\u578b\u9ed8\u8ba4"], ["none", "\u5173\u95ed"], ["minimal", "\u6700\u5c11"], ["low", "\u4f4e"], ["medium", "\u4e2d"], ["high", "\u9ad8"], ["xhigh", "\u5f88\u9ad8"], ["max", "\u6700\u9ad8"]]
        .map(([value, label]) => HA.el("option", { value, text: label, selected: (draft.reasoning_level || "inherit") === value })) )));
    grid.appendChild(field("\u6743\u9650", HA.el("select", { onchange: (e) => { draft.permission_mode = e.target.value; } },
      ...[["readonly", "\u53ea\u8bfb\u4e0e\u641c\u7d22"], ["ask", "\u6bcf\u6b21\u8be2\u95ee"], ["allow", "\u5141\u8bb8\u5df2\u6388\u6743\u64cd\u4f5c"], ["unreviewed", "\u4e0d\u7ecf\u786e\u8ba4\u6267\u884c"]]
        .map(([value, label]) => HA.el("option", { value, text: label, selected: draft.permission_mode === value })) )));
    grid.appendChild(field("\u4f1a\u8bdd\u6a21\u5f0f", HA.el("select", { onchange: (e) => { draft.chat_mode = e.target.value; } },
      ...[["chat", "\u5bf9\u8bdd"], ["plan", "\u65b9\u6848"]]
        .map(([value, label]) => HA.el("option", { value, text: label, selected: draft.chat_mode === value })) )));
    box.appendChild(grid);
    box.appendChild(HA.el("div", { class: "dim", style: "font-size:12px", text: "\u5de5\u5177\u3001Skills \u548c MCP \u7531\u6240\u9009\u667a\u80fd\u4f53\u7edf\u4e00\u7ba1\u7406\uff0c\u65e0\u9700\u5728\u4f1a\u8bdd\u4e2d\u91cd\u590d\u914d\u7f6e\u3002" }));
    return box;
  }

  async _openSessionCreate() {
    if (!this.state.selectedWs) return;
    await this._loadAgents();
    const draft = { name: "\u65b0\u4f1a\u8bdd", agent_profile_id: "", model: "", permission_mode: "ask", chat_mode: "chat", reasoning_level: "inherit" };
    this._selectDefaultAgent(draft);
    const mask = HA.el("div", { class: "modal-mask" });
    const modal = HA.el("div", { class: "modal wide" },
      HA.el("h2", { text: "\u65b0\u5efa\u4f1a\u8bdd" }),
      this._sessionConfigForm(draft),
      HA.el("div", { class: "modal-actions" },
        HA.el("button", { class: "btn", text: "\u53d6\u6d88", onclick: () => mask.remove() }),
        HA.el("button", { class: "btn primary", text: "\u521b\u5efa\u4f1a\u8bdd", onclick: () => this._createSession(draft, mask) })));
    mask.appendChild(modal);
    document.body.appendChild(mask);
  }

  async _createSession(draft, mask = null) {
    if (!this.state.selectedWs) return;
    if (!draft.agent_profile_id) { HA.toast("\u8bf7\u9009\u62e9\u667a\u80fd\u4f53", "err"); return; }
    try {
      const s = await HA.api("POST", `/api/workspaces/${encodeURIComponent(this.state.selectedWs)}/sessions`, draft);
      if (mask) mask.remove();
      await this._selectWorkspace(this.state.selectedWs, s.session_id);
      this.state.expandedWorkspaceId = this.state.selectedWs;
      HA.toast("\u4f1a\u8bdd\u5df2\u521b\u5efa", "ok");
    } catch (e) { HA.toast("\u521b\u5efa\u4f1a\u8bdd\u5931\u8d25: " + e.message, "err"); }
  }

  _renderCenter() {
    this._centerHead.innerHTML = "";
    this._centerBody.innerHTML = "";
    this._centerBody.classList.remove("ws-chat-body");
    this._messageArea = null;
    if (!this.state.selectedWs) {
      // 方案 6.3：无工作区 → 居中大空状态
      const empty = HA.el("div", { class: "ws-hero-empty" },
        HA.el("div", { class: "ws-hero-icon", text: "▣" }),
        HA.el("div", { class: "ws-hero-title", text: "还没有创建工作区" }),
        HA.el("div", { class: "ws-hero-desc",
          text: "把本地项目与智能体、模型、工具和权限绑定，在项目上下文中完成分析、开发和规划任务。" }),
        HA.el("button", { class: "btn primary", text: "创建第一个工作区",
          onclick: () => this._openWizard() }));
      this._centerBody.appendChild(empty);
      return;
    }
    const sess = this.state.sessions.find(s => s.session_id === this.state.selectedSession);
    this._centerHead.appendChild(HA.el("span", {
      text: sess ? `会话：${sess.name || sess.session_id}` : "工作区详情" }));
    if (!sess) {
      // 方案 5.2：工作区详情（会话/配置/Prompt 预览）。未选会话时展示工作区信息与会话入口。
      const w = this.state.selectedWsData;
      if (w) {
        const rows = [
          ["\u9879\u76ee\u76ee\u5f55", w.project_path],
          ["\u63cf\u8ff0", w.description || "\uff08\u672a\u586b\u5199\uff09"],
          ["\u4f1a\u8bdd", `${this.state.sessions.length} \u4e2a`],
        ];
        const info = HA.el("div", { class: "ws-field" });
        for (const [k, v] of rows) {
          info.appendChild(HA.el("div", { class: "ws-field" },
            HA.el("label", { text: k }),
            HA.el("div", { text: v, style: "font-size:12px;word-break:break-all" })));
        }
        this._centerBody.appendChild(info);
        this._centerBody.appendChild(HA.el("hr", { class: "ws-divider" }));
      }
      const sessList = HA.el("div", {});
      sessList.appendChild(HA.el("div", { class: "ws-panel-head",
        style: "padding:6px 0;border:none" },
        HA.el("span", { text: "会话" }),
        HA.el("button", { class: "btn", text: "＋ 新会话",
          onclick: () => this._openSessionCreate() })));
      if (!this.state.sessions.length) {
        sessList.appendChild(HA.el("div", { class: "ws-empty",
          text: "暂无会话，点击「＋ 新会话」开始" }));
      }
      for (const s of this.state.sessions) {
        sessList.appendChild(HA.el("div", {
          class: "ws-item",
          onclick: () => this._selectSession(s.session_id),
        },
          HA.el("div", { class: "ws-item-title", text: s.name || s.session_id }),
          HA.el("div", { class: "ws-item-sub",
            text: `${this._agentName(s.agent_profile_id)} \u00b7 ${s.updated_at || ""}` })));
      }
      this._centerBody.appendChild(sessList);
      this._centerBody.appendChild(HA.el("div", { class: "ws-empty",
        style: "padding:12px 0 0",
        text: "选择左侧或上方会话，进入会话与工具时间线" }));
      return;
    }
    // Phase 5：聊天 UI
    this._centerBody.classList.add("ws-chat-body");
    this._chatInit();
    this._chatState = this._chatStateFor(this.state.selectedWs, this.state.selectedSession);
    const shell = HA.el("section", { class: "ws-chat-shell" });
    shell.appendChild(this._chatControls(sess));
    this._messageArea = HA.el("div", { class: "ws-chat-timeline" });
    shell.appendChild(this._messageArea);
    const composer = HA.el("div", { class: "ws-chat-composer" },
      this._wsAcBox = HA.el("div", { class: "ws-ac-box", style: "display:none" }),
      HA.el("textarea", { class: "ws-chat-input", rows: 2,
        placeholder: "\u8f93\u5165\u4efb\u52a1\u2026\u2026\uff08/ \u89e6\u53d1\u547d\u4ee4\u8865\u5168\uff1bEnter \u53d1\u9001\uff0cShift+Enter \u6362\u884c\uff09",
        oninput: () => this._maybeWorkspaceAutocomplete(),
        onkeydown: (e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            this._sendChat();
          }
        } }),
      HA.el("div", { class: "ws-chat-composer-actions" },
        HA.el("span", { class: "ws-chat-keyhint", text: "Enter \u53d1\u9001 \u00b7 Shift+Enter \u6362\u884c" }),
        this._wsCtxMeter = HA.el("div", { class: "ctx-meter ws-ctx-meter" },
          HA.el("span", { class: "ctx-icon", text: "\ud83d\udcca" }),
          HA.el("span", { class: "ctx-pct", text: "\u2013" }),
          HA.el("div", { class: "ctx-tip" })),
        HA.el("button", { class: "ws-chat-send", type: "button", title: "\u53d1\u9001\uff08Enter\uff09",
          onclick: () => this._sendChat() },
          HA.el("span", { class: "ws-chat-send-ico", text: "\u25b6" }))));
    this._composer = composer.querySelector("textarea");
    this._workspaceCommands = [];
    this._loadWorkspaceCommands();
    shell.appendChild(composer);
    this._centerBody.appendChild(shell);
    this._renderMessages();
    this._stopWsCtxPoll();
    this._refreshRuntimeStatus();
    this._startWsCtxPoll();
  }

  // ---------- 右栏：配置摘要 ----------
  _renderRight() {
    this._rightBody.innerHTML = "";
    if (!this.state.selectedWsData) {
      this._rightBody.appendChild(HA.el("div", { class: "ws-empty",
        text: "选择工作区查看配置" }));
      return;
    }
    const w = this.state.selectedWsData;
    const rows = [
      ["\u9879\u76ee\u540d\u79f0", w.name],
      ["\u9879\u76ee\u76ee\u5f55", w.project_path],
      ["\u63cf\u8ff0", w.description || "\uff08\u672a\u586b\u5199\uff09"],
      ["\u4f1a\u8bdd", `${this.state.sessions.length} \u4e2a`],
    ];
    const list = HA.el("div", {});
    for (const [k, v] of rows) {
      list.appendChild(HA.el("div", { class: "ws-field" },
        HA.el("label", { text: k }),
        HA.el("div", { text: v, style: "font-size:12px;word-break:break-all" })));
    }
    if ((w.path_warnings || []).length) {
      for (const warn of w.path_warnings) {
        list.appendChild(HA.el("div", { class: "ws-warn", text: `⚠️ ${warn}` }));
      }
    }

    this._rightBody.appendChild(list);
  }
  // ---------- 创建向导 ----------
  _openWizard() {
    this.state.wizard = this._blankWizard();
    this.state.wizardStep = 0;
    const mask = HA.el("div", { class: "modal-mask" });
    const modal = HA.el("div", { class: "modal wide" },
      HA.el("h2", { text: "\u521b\u5efa\u5de5\u4f5c\u533a" }),
      this._wizardProgress = HA.el("div", { class: "dim", style: "font-size:12px;margin-bottom:10px" }),
      this._wizardBody = HA.el("div", {}),
      HA.el("div", { class: "modal-actions" },
        HA.el("button", { class: "btn", text: "\u53d6\u6d88", onclick: () => mask.remove() }),
        this._wizardBack = HA.el("button", { class: "btn", text: "\u4e0a\u4e00\u6b65", onclick: () => { this.state.wizardStep = 0; this._renderWizard(); } }),
        this._wizardNextButton = HA.el("button", { class: "btn primary", text: "\u4e0b\u4e00\u6b65", onclick: () => this._wizardNext() })));
    this._wizardModal = mask;
    mask.appendChild(modal);
    document.body.appendChild(mask);
    this._renderWizard();
  }

  _renderWizard() {
    if (!this._wizardBody) return;
    const w = this.state.wizard;
    const step = this.state.wizardStep;
    const total = w.create_first_session ? 2 : 1;
    if (this._wizardProgress) this._wizardProgress.textContent = `\u6b65\u9aa4 ${step + 1} / ${total}`;
    if (this._wizardBack) this._wizardBack.style.visibility = step ? "visible" : "hidden";
    if (this._wizardNextButton) this._wizardNextButton.textContent = step ? (this.state.creating ? "\u521b\u5efa\u4e2d\u2026" : "\u521b\u5efa\u5de5\u4f5c\u533a") : (w.create_first_session ? "\u4e0b\u4e00\u6b65\uff1a\u914d\u7f6e\u9996\u4f1a\u8bdd" : "\u521b\u5efa\u5de5\u4f5c\u533a");
    if (this._wizardNextButton) this._wizardNextButton.disabled = Boolean(this.state.creating);
    this._wizardBody.innerHTML = "";
    if (step === 0) {
      const field = (label, node) => HA.el("div", { class: "ws-field" }, HA.el("label", { text: label }), node);
      this._wizardBody.appendChild(field("\u540d\u79f0 *", HA.el("input", { type: "text", value: w.name, oninput: (e) => { w.name = e.target.value; } })));
      this._wizardBody.appendChild(field("\u63cf\u8ff0", HA.el("input", { type: "text", value: w.description, oninput: (e) => { w.description = e.target.value; } })));
      this._wizardBody.appendChild(field("\u9879\u76ee\u76ee\u5f55 *", HA.el("input", { type: "text", value: w.project_path,
        oninput: (e) => { w.project_path = e.target.value; this._debounceValidatePath(); } })));
      this._wizardPathResult = HA.el("div", {});
      this._wizardBody.appendChild(this._wizardPathResult);
      this._wizardBody.appendChild(HA.el("label", { class: "ws-check" },
        HA.el("input", { type: "checkbox", checked: w.risk_confirmed, onchange: (e) => { w.risk_confirmed = e.target.checked; } }),
        HA.el("span", { text: "\u6211\u5df2\u4e86\u89e3\u8def\u5f84\u98ce\u9669\u5e76\u786e\u8ba4\uff08\u51fa\u73b0\u98ce\u9669\u63d0\u793a\u65f6\u9700\u8981\uff09" })));
      this._wizardBody.appendChild(HA.el("label", { class: "ws-check ws-first-session-option" },
        HA.el("input", { type: "checkbox", checked: w.create_first_session,
          onchange: (e) => { w.create_first_session = e.target.checked; this._renderWizard(); } }),
        HA.el("span", { text: "\u521b\u5efa\u540e\u7ee7\u7eed\u914d\u7f6e\u9996\u4e2a\u4f1a\u8bdd\uff08\u53ef\u9009\uff09" })));
      this._wizardBody.appendChild(HA.el("div", { class: "dim", style: "font-size:12px", text: "\u5de5\u4f5c\u533a\u53ea\u4fdd\u5b58\u9879\u76ee\u540d\u79f0\u3001\u63cf\u8ff0\u548c\u76ee\u5f55\u3002\u667a\u80fd\u4f53\u3001\u6a21\u578b\u3001\u601d\u8003\u6a21\u5f0f\u4e0e\u6743\u9650\u5728\u6bcf\u4e2a\u4f1a\u8bdd\u4e2d\u8bbe\u7f6e\u3002" }));
      return;
    }
    this._wizardBody.appendChild(this._sessionConfigForm(w, { heading: "\u914d\u7f6e\u9996\u4e2a\u4f1a\u8bdd\uff08\u53ef\u9009\uff09", description: "\u6b64\u914d\u7f6e\u4ec5\u4f5c\u7528\u4e8e\u9996\u4e2a\u4f1a\u8bdd\uff0c\u4e0d\u4f1a\u6210\u4e3a\u6574\u4e2a\u5de5\u4f5c\u533a\u7684\u9ed8\u8ba4\u667a\u80fd\u4f53\u6216\u6743\u9650\u3002" }));
  }

  _debounceValidatePath() {
    clearTimeout(this._pvTimer);
    this._pvTimer = setTimeout(() => this._validatePath(), 400);
  }

  async _validatePath() {
    const w = this.state.wizard;
    if (!w.project_path) return;
    try {
      const result = await HA.api("POST", "/api/workspaces/validate-path", { path: w.project_path, purpose: "project_root", risk_confirmed: w.risk_confirmed }, { silent: true });
      w.pathResult = result;
      if (this._wizardPathResult) {
        this._wizardPathResult.innerHTML = "";
        const lines = [`\u89c4\u8303\u5316\u8def\u5f84\uff1a${result.normalized || ""}`];
        if (result.exists) lines.push(`\u5b58\u5728\uff08${result.is_directory ? "\u76ee\u5f55" : "\u6587\u4ef6"}\uff09\u00b7 \u8bfb:${result.readable} \u5199:${result.writable}`);
        if (result.blocked) lines.push(`\u26d4 ${(result.reasons || []).join("\uff1b")}`);
        for (const warn of result.warnings || []) lines.push(`\u26a0\ufe0f ${warn}`);
        this._wizardPathResult.appendChild(HA.el("div", { class: result.blocked ? "ws-warn" : "dim", style: "font-size:12px", text: lines.join("\n") }));
      }
    } catch (e) { /* best effort until submit */ }
  }

  async _wizardNext() {
    const w = this.state.wizard;
    if (this.state.wizardStep === 0) {
      if (!w.name || !w.project_path) { HA.toast("\u540d\u79f0\u4e0e\u9879\u76ee\u76ee\u5f55\u4e3a\u5fc5\u586b", "err"); return; }
      if (!w.pathResult) await this._validatePath();
      if (w.pathResult && w.pathResult.blocked) { HA.toast("\u76ee\u5f55\u88ab\u62d2\u7edd", "err"); return; }
      if (w.pathResult && w.pathResult.status === "warning" && !w.risk_confirmed) { HA.toast("\u5b58\u5728\u98ce\u9669\u9879\uff0c\u8bf7\u52fe\u9009\u786e\u8ba4", "err"); return; }
      if (!w.create_first_session) { await this._submitWizard(); return; }
      await this._loadAgents();
      this._selectDefaultAgent(w);
      this.state.wizardStep = 1;
      this._renderWizard();
      return;
    }
    await this._submitWizard();
  }

  async _submitWizard() {
    const w = this.state.wizard;
    if (this.state.creating) return;
    if (w.create_first_session && !w.agent_profile_id) { HA.toast("\u8bf7\u9009\u62e9\u9996\u4e2a\u4f1a\u8bdd\u4f7f\u7528\u7684\u667a\u80fd\u4f53", "err"); return; }
    this.state.creating = true;
    this._renderWizard();
    try {
      const body = { name: w.name, description: w.description, project_path: w.project_path, risk_confirmed: w.risk_confirmed };
      if (w.create_first_session) body.first_session = { name: w.first_session_name || "\u9996\u4e2a\u4f1a\u8bdd", agent_profile_id: w.agent_profile_id, model: w.model, permission_mode: w.permission_mode, chat_mode: w.chat_mode, reasoning_level: w.reasoning_level };
      const d = await HA.api("POST", "/api/workspaces", body);
      this.state.creating = false;
      if (this._wizardModal) this._wizardModal.remove();
      HA.toast("\u5de5\u4f5c\u533a\u5df2\u521b\u5efa", "ok");
      await this.refresh();
      await this._selectWorkspace(d.workspace.workspace_id, d.first_session ? d.first_session.session_id : "");
      this.state.expandedWorkspaceId = d.workspace.workspace_id;
      this._renderLeft();
    } catch (e) {
      this.state.creating = false;
      this._renderWizard();
      HA.toast("\u521b\u5efa\u5931\u8d25: " + e.message, "err");
    }
  }

  _chatInit() {
    if (this._chatInited) return;
    this._chatInited = true;
    this._chatStates = new Map();
    this._off.push(HA.onSSE("*", (data, evt) => this._onSSE(data, evt)));
  }

  _chatStateFor(workspaceId, sessionId) {
    const key = workspaceId + ":" + sessionId;
    if (!this._chatStates.has(key)) {
      this._chatStates.set(key, {
        messages: [], toolCalls: {}, approvals: {}, plans: {},
        reasoningBlocks: {}, showTools: true, nextRenderOrder: 0,
      });
    }
    return this._chatStates.get(key);
  }

  _nextRenderOrder(state = this._chatState) {
    if (!state) return Date.now();
    state.nextRenderOrder = (state.nextRenderOrder || 0) + 1;
    return state.nextRenderOrder;
  }

  _historyContent(message) {
    const content = message && (message.content_text ?? message.content);
    if (typeof content === "string") return content;
    if (Array.isArray(content)) return content.map(part =>
      typeof part === "string" ? part : (part && (part.text || part.content) || "")).join("\n");
    return content == null ? "" : String(content);
  }

  _hydrateHistory(messages, state) {
    state.messages = [];
    state.toolCalls = {};
    state.approvals = {};
    state.plans = {};
    state.reasoningBlocks = {};
    state.nextRenderOrder = 0;
    for (const record of (messages || [])) {
      if (!record || record.internal === true || record.kind === "protocol_error"
        || record.kind === "history_summary") continue;
      const role = record.role || "";
      const content = this._historyContent(record);
      if (role === "system") continue;
      if (role === "user") {
        if (record.name === "format_hint" || record.name === "protocol_correction") continue;
        state.messages.push({ role: "user", content,
          render_order: this._nextRenderOrder(state) });
        continue;
      }
      if (role === "assistant") {
        state.messages.push({
          role: "assistant", content, message_id: record.message_id || "",
          render_order: this._nextRenderOrder(state),
          finish_reason: record.kind === "tool_calls" ? "tool_calls" : "stop",
          kind: record.kind === "tool_calls" ? "tool_calls" : "final",
        });
        continue;
      }
      if (role === "tool") {
        const callId = record.tool_call_id || record.message_id
          || `history-tool-${state.nextRenderOrder + 1}`;
        state.toolCalls[callId] = {
          tool: record.name || record.tool || "tool", result: content,
          status: record.is_error ? "error" : "ok",
          render_order: this._nextRenderOrder(state),
        };
      }
    }
  }

  async _loadSessionHistory(workspaceId, sessionId) {
    if (!workspaceId || !sessionId) return;
    const state = this._chatStateFor(workspaceId, sessionId);
    if (state.historyLoading || state.historyLoaded) return;
    state.historyLoading = true;
    const hadLiveMessages = state.messages.length > 0;
    try {
      const data = await HA.api("GET",
        `/api/workspaces/${encodeURIComponent(workspaceId)}/sessions/${encodeURIComponent(sessionId)}/history`,
        undefined, { silent: true });
      // Do not overwrite messages that arrived while the disk history was loading.
      if (!hadLiveMessages && !state.messages.length) this._hydrateHistory(data.messages || [], state);
      state.historyLoaded = true;
      if (this._isCurrentChat(workspaceId, sessionId)) {
        this._chatState = state;
        this._renderMessages();
      }
    } catch (e) {
      // A new session has no file yet; keep its empty timeline usable.
      state.historyLoaded = true;
    } finally {
      state.historyLoading = false;
    }
  }

  _isCurrentChat(workspaceId, sessionId) {
    return this.state.selectedWs === workspaceId
      && this.state.selectedSession === sessionId;
  }

  _identityMatch(data) {
    const wid = this.state.selectedWs;
    const sid = this.state.selectedSession;
    if (!wid || !sid) return false;
    const evWs = data.workspace_id || "";
    const evSess = data.workspace_session_id || "";
    if (evWs || evSess) return evWs === wid && evSess === sid;
    if (data.session_key) return data.session_key === "workspace:" + wid + ":" + sid;
    return false;
  }

  _isReasoningEvent(type, data = {}) {
    const normalized = String(type || "").toLowerCase();
    return normalized === "reasoning" || normalized === "reasoning_delta"
      || normalized === "analysis_delta" || normalized === "thinking"
      || normalized === "thinking_delta" || normalized.startsWith("reasoning.")
      || data.role === "reasoning" || data.kind === "reasoning"
      || data.channel === "reasoning";
  }

  _appendReasoning(data, { replace = false, key = "" } = {}) {
    const state = this._chatState;
    if (!state) return;
    const id = key || data.message_id || data.turn_id || "reasoning";
    const block = state.reasoningBlocks[id] || {
      id, content: "", sequence: data.sequence || Date.now(),
      render_order: this._nextRenderOrder(state), kind: replace ? "progress" : "reasoning",
    };
    const value = data.text ?? data.delta ?? data.content ?? "";
    if (replace) block.content = String(value);
    else block.content = (block.content || "") + String(value);
    block.sequence = Math.min(block.sequence || data.sequence || Date.now(), data.sequence || block.sequence || Date.now());
    state.reasoningBlocks[id] = block;
    this._renderMessages();
  }

  _onSSE(data, evt) {
    if (!this._identityMatch(data)) return; // \u8fdf\u5230\u4e8b\u4ef6\u4e0d\u4e32\u9875
    const type = (evt && evt.type || "").replace(/^chat\./, "");
    if (this._isReasoningEvent(type, data)) {
      this._appendReasoning(data);
      return;
    }
    if (type === "message_start") {
      // Tool result messages are represented by their own collapsed tool card;
      // they must never create an assistant bubble.
      if (data.role === "user" || data.role === "tool") return;
      if (data.role === "reasoning") {
        this._appendReasoning(data, { key: data.message_id });
        return;
      }
      this._chatState.messages.push({ role: "assistant", content: "", message_id: data.message_id,
        sequence: data.sequence || Date.now(), render_order: this._nextRenderOrder(),
        finish_reason: "", kind: "process" });
      this._renderMessages();
    } else if (type === "text_delta" || type === "text") {
      if (this._isReasoningEvent(type, data)) {
        this._appendReasoning(data);
        return;
      }
      const last = [...this._chatState.messages].reverse().find(message => message.role === "assistant");
      if (last) {
        last.content = (last.content || "") + (data.text || data.content || "");
        last.sequence = data.sequence || last.sequence;
        this._renderMessages();
      }
    } else if (type === "message_end") {
      if (data.role === "user" || data.role === "tool" || data.role === "reasoning") return;
      const last = [...this._chatState.messages].reverse().find(message => message.role === "assistant");
      if (last) {
        const finishReason = data.finish_reason || data.status || last.finish_reason || "";
        const isToolTurn = String(finishReason).toLowerCase() === "tool_calls" || data.kind === "tool_calls";
        last.finish_reason = finishReason;
        // `message_end.content` is the server-normalized final answer. Replace
        // the raw streamed text with it instead of rendering both variants.
        if (data.content && !isToolTurn) last.content = data.content;
        last.kind = isToolTurn ? "tool_calls" : (data.status === "error" ? "process" : "final");
        // 任务耗时：仅最终回答（final）结算，工具轮不结算
        if (!isToolTurn && this._chatState.taskStart) {
          last.duration = Date.now() - this._chatState.taskStart;
          this._chatState.taskStart = 0;
          this._chatState.taskDuration = last.duration;
          this._stopWsTaskTimer();
        }
      }
      this._renderMessages();
    } else if (type === "text_reset") {
      // Legacy protocol parsing emits text_reset before its normalized
      // FINAL_ANSWER. Clear the raw buffer so only the converted answer remains.
      const last = [...this._chatState.messages].reverse().find(message => message.role === "assistant");
      if (last) {
        last.content = "";
        last.kind = "final";
        last.finish_reason = "stop";
        this._renderMessages();
      }
    } else if (type === "progress") {
      this._appendReasoning(data, { replace: true, key: `progress:${data.message_id || "current"}` });
    } else if (type === "tool_start" || type === "tool_call_start" || type === "tool_execution_start") {
      const cid = data.call_id || data.tool_call_id || data.message_id || "tool-" + Date.now();
      const current = this._chatState.toolCalls[cid] || {};
      this._chatState.toolCalls[cid] = {
        ...current,
        tool: data.tool || data.name || current.tool || "",
        arguments: data.arguments ?? current.arguments ?? {}, status: "running",
        sequence: current.sequence || data.sequence || Date.now(),
        render_order: current.render_order || this._nextRenderOrder(),
        turn_id: data.turn_id || current.turn_id || "",
      };
      this._renderMessages();
    } else if (type === "tool_call_delta") {
      const cid = data.call_id || data.tool_call_id || data.message_id;
      const current = cid && this._chatState.toolCalls[cid];
      if (current && data.arguments_delta) {
        current.arguments_text = (current.arguments_text || "") + data.arguments_delta;
        this._renderMessages();
      }
    } else if (type === "tool_call_end") {
      // Native providers emit this when arguments are complete, not when the
      // local tool has finished. Keep the card in the collapsed running state.
      const cid = data.call_id || data.tool_call_id || data.message_id;
      if (cid && this._chatState.toolCalls[cid]) {
        if (data.arguments !== undefined) this._chatState.toolCalls[cid].arguments = data.arguments;
        this._renderMessages();
      }
    } else if (type === "tool_end" || type === "tool_execution_end" || type === "tool_result") {
      const cid = data.call_id || data.tool_call_id || data.message_id;
      if (cid && this._chatState.toolCalls[cid]) {
        const tool = this._chatState.toolCalls[cid];
        if (data.arguments !== undefined) tool.arguments = data.arguments;
        if (data.result !== undefined || data.content !== undefined) tool.result = data.result ?? data.content ?? "";
        tool.status = data.error || data.is_error ? "error" : "ok";
        tool.sequence = data.sequence || tool.sequence;
        this._renderMessages();
      }
    } else if (type === "approval.requested") {
      if (!data._render_order) data._render_order = this._nextRenderOrder();
      this._chatState.approvals[data.id] = data;
      this._renderMessages();
    } else if (type === "approval.resolved") {
      delete this._chatState.approvals[data.id];
      this._renderMessages();
    } else if (type === "plan.changed" && data.plan) {
      if (!data.plan._render_order) data.plan._render_order = this._nextRenderOrder();
      this._chatState.plans[data.plan.plan_id] = data.plan;
      this._renderMessages();
    } else if (type === "agent_end" || type === "chat.done") {
      this._chatBusy = false;
      if (this._chatState && this._chatState.taskStart) {
        const last = [...this._chatState.messages].reverse().find(message => message.role === "assistant");
        if (last && last.kind === "final" && last.duration == null) {
          last.duration = Date.now() - this._chatState.taskStart;
        }
        this._chatState.taskStart = 0;
      }
      this._stopWsTaskTimer();
      this._updateStatusBar();
    }
  }

  _splitInlineReasoning(content) {
    const source = String(content || "");
    const open = source.match(/^\s*<(think|thinking|analysis)\b[^>]*>/i);
    if (!open) return { reasoning: "", answer: source };
    const tag = open[1];
    const rest = source.slice(open[0].length);
    const close = rest.search(new RegExp(`</${tag}>`, "i"));
    if (close < 0) return { reasoning: rest, answer: "" };
    return {
      reasoning: rest.slice(0, close),
      answer: rest.slice(close + tag.length + 3).replace(/^\s+/, ""),
    };
  }

  _renderReasoningBlock(block) {
    const content = block.content || "";
    const title = block.kind === "progress" ? "\u8fd0\u884c\u8fdb\u5ea6" : "\u63a8\u7406";
    const extraClass = block.kind === "assistant" ? " ws-process-card" : "";
    return HA.el("details", { class: "ws-reasoning-card" + extraClass },
      HA.el("summary", { text: `${title} \u00b7 ${content.length} \u5b57\u7b26` }),
      HA.el("div", { class: "ws-reasoning-body md", html: HA.renderMd(content) }));
  }

  _renderToolBlock(tool) {
    const status = tool.status || "running";
    const name = tool.tool || tool.name || "tool";
    const label = status === "ok" ? "\u5df2\u5b8c\u6210" : status === "error" ? "\u5931\u8d25" : "\u6267\u884c\u4e2d";
    return HA.el("details", { class: "ws-tool-collapsible" },
      HA.el("summary", { class: "ws-tool-summary" },
        HA.el("span", { class: "ws-tool-summary-label", text: `\u5de5\u5177 \u00b7 ${name}` }),
        HA.badge(label, status === "ok" ? "low" : status === "error" ? "high" : "medium")),
      new HA.ToolCard(tool).render());
  }

  _renderMessages() {
    if (!this._messageArea) return;
    this._messageArea.innerHTML = "";
    const state = this._chatState;
    const messageList = state.messages || [];
    const reasoning = Object.values(state.reasoningBlocks || {}).filter(item => item.content);
    const tools = Object.values(state.toolCalls || {});
    const assistantMessages = messageList.filter(message => message.role === "assistant" && (message.content || ""));
    const inlineAnswers = assistantMessages.map(message => ({ message, parts: this._splitInlineReasoning(message.content) }));
    const inlineReasoning = inlineAnswers
      .filter(item => item.parts.reasoning)
      .map(item => ({ id: `inline:${item.message.message_id || item.message.render_order || Math.random()}`,
        content: item.parts.reasoning, render_order: item.message.render_order, kind: "reasoning" }));
    // A stream is process output until message_end proves it is the final answer.
    // Keep every record at its original event position so one LLM turn remains
    // directly followed by the tools it requested.
    const processReplies = inlineAnswers
      .filter(item => item.message.kind !== "final" && item.parts.answer)
      .map(item => ({ id: `process:${item.message.message_id || item.message.render_order || Math.random()}`,
        content: item.parts.answer, render_order: item.message.render_order, kind: "assistant" }));
    const answers = inlineAnswers
      .filter(item => item.message.kind === "final")
      .map(item => ({ ...item.message, content: item.parts.answer }));
    const hasContent = messageList.length || reasoning.length || inlineReasoning.length || processReplies.length || tools.length
      || Object.keys(state.approvals || {}).length || Object.keys(state.plans || {}).length;
    if (!hasContent) {
      const workspace = this.state.selectedWsData || {};
      const session = this._selectedSessionData() || {};
      this._messageArea.appendChild(HA.el("div", { class: "ws-chat-empty" },
        HA.el("div", { class: "ws-chat-empty-icon", text: "\u2726" }),
        HA.el("div", { class: "ws-chat-empty-title", text: "\u4ece\u9879\u76ee\u4e0a\u4e0b\u6587\u5f00\u59cb" }),
        HA.el("div", { class: "ws-chat-empty-desc",
          text: `${workspace.name || "\u5f53\u524d\u5de5\u4f5c\u533a"} \u00b7 ${session.name || "\u4f1a\u8bdd"}` }),
        HA.el("div", { class: "ws-chat-empty-tips",
          text: "\u53ef\u4ee5\u8ba9\u667a\u80fd\u4f53\u9605\u8bfb\u4ee3\u7801\u3001\u5b9a\u4f4d\u95ee\u9898\u3001\u89c4\u5212\u65b9\u6848\u6216\u6267\u884c\u5df2\u6388\u6743\u7684\u5de5\u5177\u3002" })));
      return;
    }
    const stack = HA.el("div", { class: "ws-turn-stack" });
    const timeline = [];
    const put = (kind, value, order, priority = 0) => timeline.push({
      kind, value, order: Number.isFinite(order) ? order : Number.MAX_SAFE_INTEGER, priority,
    });
    for (const message of messageList.filter(message => message.role === "user")) {
      put("message", message, message.render_order, 0);
    }
    for (const block of reasoning) put("reasoning", block, block.render_order, 10);
    for (const block of inlineReasoning) put("reasoning", block, block.render_order, 11);
    for (const block of processReplies) put("reasoning", block, block.render_order, 12);
    if (state.showTools !== false) {
      for (const tool of tools) put("tool", tool, tool.render_order, 20);
    }
    for (const plan of Object.values(state.plans || {})) put("plan", plan, plan._render_order, 30);
    for (const approval of Object.values(state.approvals || {})) put("approval", approval, approval._render_order, 31);
    for (const answer of answers) {
      if (answer.content) put("message", answer, answer.render_order, 40);
    }
    timeline.sort((a, b) => a.order - b.order || a.priority - b.priority);
    for (const item of timeline) {
      if (item.kind === "message") stack.appendChild(new HA.MessageBubble(item.value).render());
      else if (item.kind === "reasoning") stack.appendChild(this._renderReasoningBlock(item.value));
      else if (item.kind === "tool") stack.appendChild(this._renderToolBlock(item.value));
      else if (item.kind === "plan") stack.appendChild(new HA.PlanCard(item.value).render());
      else if (item.kind === "approval") stack.appendChild(new HA.ApprovalCard(item.value, (answer) =>
        this._answerApproval(item.value.id, answer)).render());
    }
    this._messageArea.appendChild(stack);
    this._messageArea.scrollTop = this._messageArea.scrollHeight;
  }

  _selectedSessionData() {
    return this.state.sessions.find(s => s.session_id === this.state.selectedSession) || null;
  }

  async _answerApproval(aid, answer) {
    try {
      await HA.api("POST", `/api/approvals/${encodeURIComponent(aid)}`, {
        answer, workspace_id: this.state.selectedWs,
        workspace_session_id: this.state.selectedSession,
      });
    } catch (e) { HA.toast("\u5ba1\u6279\u7b54\u590d\u5931\u8d25: " + e.message, "err"); }
  }

  async _loadWorkspaceCommands() {
    const wid = this.state.selectedWs, sid = this.state.selectedSession;
    if (!wid || !sid) return;
    try {
      const d = await HA.api("GET",
        `/api/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/commands`,
        undefined, { silent: true });
      if (this.state.selectedWs === wid && this.state.selectedSession === sid) {
        this._workspaceCommands = d.commands || [];
      }
    } catch (e) { this._workspaceCommands = []; }
  }

  // ---------- 任务运行时长（deepseek-harness 风格 HH:MM:SS）----------
  _startWsTaskTimer() {
    this._stopWsTaskTimer();
    this._wsTaskTimer = setInterval(() => {
      const state = this._chatState;
      if (!state || !state.taskStart) { this._stopWsTaskTimer(); this._updateStatusBar(); return; }
      if (this._statusBar) {
        this._statusBar.textContent = `\u23f3 ${HA.fmtDuration(Date.now() - state.taskStart)}`;
        this._statusBar.classList.add("busy");
      }
    }, 1000);
  }
  _stopWsTaskTimer() {
    if (this._wsTaskTimer) { clearInterval(this._wsTaskTimer); this._wsTaskTimer = null; }
  }

  _maybeWorkspaceAutocomplete() {
    const value = this._composer && this._composer.value || "";
    if (!value.startsWith("/") || value.includes(" ")) return this._hideWorkspaceAutocomplete();
    const hits = (this._workspaceCommands || []).filter(c => c.name.startsWith(value));
    if (!hits.length) return this._hideWorkspaceAutocomplete();
    this._wsAcBox.innerHTML = "";
    for (const command of hits.slice(0, 8)) {
      this._wsAcBox.appendChild(HA.el("div", {
        class: "ws-ac-item", title: command.help || "",
        onclick: () => {
          this._composer.value = command.insert_text || (command.name + " ");
          this._hideWorkspaceAutocomplete();
          this._composer.focus();
        },
      },
        HA.el("div", { class: "ws-ac-name", text: `${command.name} ${command.args || ""}`.trim() }),
        command.help ? HA.el("div", { class: "ws-ac-desc", text: command.help }) : null));
    }
    this._wsAcBox.style.display = "block";
  }

  _hideWorkspaceAutocomplete() {
    if (this._wsAcBox) this._wsAcBox.style.display = "none";
  }

  async _sendChat() {
    const text = (this._composer && this._composer.value || "").trim();
    if (!text) return;
    if (this._chatBusy) { HA.toast("\u4f1a\u8bdd\u6b63\u5728\u8fd0\u884c\uff0c\u8bf7\u7b49\u5f85", "err"); return; }
    const session = this._selectedSessionData();
    if (session && session.chat_mode === "plan") return this._sendWorkspacePlan(text);
    const wid = this.state.selectedWs, sid = this.state.selectedSession;
    if (!wid || !sid) return;
    const chatState = this._chatStateFor(wid, sid);
    chatState.messages.push({ role: "user", content: text,
      render_order: this._nextRenderOrder(chatState) });
    chatState.taskStart = Date.now();
    chatState.taskDuration = 0;
    this._chatBusy = true;
    this._startWsTaskTimer();
    this._updateStatusBar();
    if (this._composer) this._composer.value = "";
    this._hideWorkspaceAutocomplete();
    this._renderMessages();
    try {
      const d = await HA.api("POST",
        `/api/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/chat`,
        { message: text, timeout: 180 });
      if (d.ok && d.reply) {
        // The HTTP reply is authoritative and already normalized. Reuse the
        // live assistant turn when available; never append a second raw/final pair.
        const liveFinal = [...chatState.messages].reverse().find(message =>
          message.role === "assistant" && message.message_id === d.message_id
          && message.kind !== "tool_calls");
        if (liveFinal) {
          liveFinal.content = d.reply;
          liveFinal.kind = "final";
          liveFinal.finish_reason = "stop";
        } else if (!chatState.messages.some(message =>
          message.role === "assistant" && message.kind === "final" && message.content === d.reply)) {
          chatState.messages.push({ role: "assistant", content: d.reply, message_id: d.message_id,
            render_order: this._nextRenderOrder(chatState), kind: "final", finish_reason: "stop" });
        }
        if (this._isCurrentChat(wid, sid)) this._renderMessages();
      }
      if (!d.ok && d.error) HA.toast("\u8fd0\u884c\u9519\u8bef: " + d.error, "err");
    } catch (e) {
      HA.toast("\u53d1\u9001\u5931\u8d25: " + e.message, "err");
    } finally {
      this._chatBusy = false;
      if (this._isCurrentChat(wid, sid)) {
        this._updateStatusBar();
        this._refreshRuntimeStatus();
      }
    }
  }

  async _sendWorkspacePlan(text) {
    const wid = this.state.selectedWs, sid = this.state.selectedSession;
    if (!wid || !sid) return;
    const chatState = this._chatStateFor(wid, sid);
    chatState.messages.push({ role: "user", content: `[plan] ${text}`,
      render_order: this._nextRenderOrder(chatState) });
    this._chatBusy = true;
    this._updateStatusBar();
    if (this._composer) this._composer.value = "";
    this._renderMessages();
    try {
      const d = await HA.api("POST",
        `/api/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/plan`,
        { message: text, timeout: 300 });
      if (d.plan && d.plan.plan_id) {
        if (!d.plan._render_order) d.plan._render_order = this._nextRenderOrder(chatState);
        chatState.plans[d.plan.plan_id] = d.plan;
      }
      this._renderMessages();
      HA.toast("\u65b9\u6848\u5df2\u751f\u6210\uff0c\u8bf7\u5728\u65b9\u6848\u5361\u7247\u4e2d\u67e5\u770b\u548c\u786e\u8ba4", "ok");
    } catch (e) {
      HA.toast("\u751f\u6210\u65b9\u6848\u5931\u8d25: " + e.message, "err");
    } finally {
      this._chatBusy = false;
      this._updateStatusBar();
      this._refreshRuntimeStatus();
    }
  }

  // ---------- 上下文统计定期刷新（deepseek-harness 风格实时占用）----------
  _startWsCtxPoll() {
    this._stopWsCtxPoll();
    this._wsCtxPollTimer = setInterval(() => this._refreshRuntimeStatus(), 5000);
  }
  _stopWsCtxPoll() {
    if (this._wsCtxPollTimer) { clearInterval(this._wsCtxPollTimer); this._wsCtxPollTimer = null; }
  }

  async _refreshRuntimeStatus() {
    const wid = this.state.selectedWs, sid = this.state.selectedSession;
    if (!wid || !sid) return;
    try {
      this._runtimeStatus = await HA.api("GET",
        `/api/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/runtime-status`,
        undefined, { silent: true });
      this._renderWsCtxMeter(this._runtimeStatus);
      this._updateStatusBar();
    } catch (e) { /* status is advisory */ }
  }

  _wsCtxFmtTokens(n) {
    return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  }
  _renderWsCtxMeter(status) {
    if (!this._wsCtxMeter) return;
    const ctx = status && status.context;
    if (!this._wsCtxMeter._hoverBound) {
      this._wsCtxMeter.addEventListener("mouseenter", () => {
        const tip = this._wsCtxMeter.querySelector(".ctx-tip");
        if (tip) tip.style.display = "block";
      });
      this._wsCtxMeter.addEventListener("mouseleave", () => {
        const tip = this._wsCtxMeter.querySelector(".ctx-tip");
        if (tip) tip.style.display = "none";
      });
      this._wsCtxMeter._hoverBound = true;
    }
    const pctEl = this._wsCtxMeter.querySelector(".ctx-pct");
    const tipEl = this._wsCtxMeter.querySelector(".ctx-tip");
    if (!ctx || !ctx.total_tokens) {
      this._wsCtxMeter.classList.add("dim");
      this._wsCtxMeter.classList.remove("warn", "danger");
      if (pctEl) pctEl.textContent = "\u2013";
      if (tipEl) tipEl.innerHTML = '<div class="ctx-tip-row"><span>上下文</span><b>会话未加载</b></div>';
      return;
    }
    const pct = Math.round((ctx.usage_ratio || 0) * 100);
    this._wsCtxMeter.classList.remove("dim");
    this._wsCtxMeter.classList.toggle("warn", pct >= 70);
    this._wsCtxMeter.classList.toggle("danger", pct >= 90);
    if (pctEl) pctEl.textContent = pct + "%";
    // 与主会话一致：模型真实上下文窗口 与 历史预算（压缩阈值）分开展示
    const rows = [
      ["模型", status.model || "—"],
      ["消息数", String(ctx.total_messages ?? 0)],
      ["已用", `${this._wsCtxFmtTokens(ctx.total_tokens ?? 0)} tokens`],
      ["占用", `${pct}%`],
    ];
    const modelCtx = ctx.model_context_length || 0;
    const budget = ctx.max_tokens || 0;
    if (modelCtx > 0) rows.push(["模型上下文", `${this._wsCtxFmtTokens(modelCtx)} tokens`]);
    if (budget > 0) {
      rows.push(["历史预算", `${this._wsCtxFmtTokens(budget)} tokens`]);
      rows.push(["剩余", `${this._wsCtxFmtTokens(ctx.remaining_tokens ?? 0)} tokens`]);
    }
    if (ctx.anchored) rows.push(["锚定", `${this._wsCtxFmtTokens(ctx.anchored_tokens ?? 0)} tokens`]);
    let tipHtml = '<div class="ctx-tip-title">上下文占用</div>' + rows.map(([k, v]) =>
      `<div class="ctx-tip-row"><span>${k}</span><b>${v}</b></div>`).join("");
    if (modelCtx > 0 && budget > 0) {
      tipHtml += '<div class="ctx-tip-foot">历史预算 = 模型上下文 − 输出预留，达到阈值自动压缩</div>';
    }
    if (tipEl) tipEl.innerHTML = tipHtml;
  }

  _updateStatusBar() {
    if (!this._statusBar) return;
    // 任务计时中：状态栏显示实时耗时，避免被轮询/其他刷新覆盖
    const state = this._chatState;
    if (state && state.taskStart) {
      this._statusBar.textContent = `\u23f3 ${HA.fmtDuration(Date.now() - state.taskStart)}`;
      this._statusBar.classList.add("busy");
      return;
    }
    const parts = [];
    if (this._chatBusy || (this._runtimeStatus && this._runtimeStatus.is_busy)) parts.push("\u8fd0\u884c\u4e2d");
    if (this._runtimeStatus && this._runtimeStatus.snapshot_stale) parts.push("\u65b0\u914d\u7f6e\u5c06\u5728\u4e0b\u6761\u6d88\u606f\u5e94\u7528");
    const sse = HA.getSSEState ? HA.getSSEState() : null;
    if (sse && !sse.connected) parts.push("SSE \u91cd\u8fde\u4e2d");
    this._statusBar.textContent = parts.join(" \u00b7 ") || "\u5c31\u7eea";
    this._statusBar.classList.toggle("busy", !!(this._chatBusy || (this._runtimeStatus && this._runtimeStatus.is_busy)));
  }

  _workspaceSegmented(options, onPick) {
    const wrap = HA.el("div", { class: "segmented ws-chat-segmented" });
    const buttons = {};
    for (const option of options) {
      const button = HA.el("button", { class: "seg-btn", text: option.label, title: option.title || option.label,
        onclick: () => onPick(option.value) });
      buttons[option.value] = button;
      wrap.appendChild(button);
    }
    wrap._set = value => Object.entries(buttons).forEach(([key, button]) =>
      button.classList.toggle("on", key === value));
    return wrap;
  }

  _chatControls(session) {
    const workspace = this.state.selectedWsData || {};
    const models = (this.state.catalogs.models || []).map(model =>
      typeof model === "string" ? { id: model, name: model } : model).filter(model => model && model.id);
    const selectedModel = session.model || workspace.default_model || "";
    if (selectedModel && !models.some(model => model.id === selectedModel)) models.unshift({ id: selectedModel, name: selectedModel });
    this._modelSel = HA.el("select", { class: "ws-chat-model", title: "\u6a21\u578b", onchange: () =>
      this._updateSessionControl({ model: this._modelSel.value }, "\u6a21\u578b") },
      ...models.map(model => HA.el("option", { value: model.id, text: model.name || model.id, selected: model.id === selectedModel })));
    if (!models.length) this._modelSel.appendChild(HA.el("option", { value: "", text: "\u7ee7\u627f\u9ed8\u8ba4\u6a21\u578b" }));

    const reasoning = [
      ["inherit", "\u601d\u8003\uff1a\u7ee7\u627f\u6a21\u578b"], ["provider_default", "\u601d\u8003\uff1a\u670d\u52a1\u5546\u9ed8\u8ba4"],
      ["none", "\u601d\u8003\uff1a\u5173\u95ed"], ["minimal", "\u601d\u8003\uff1a\u6781\u4f4e"], ["low", "\u601d\u8003\uff1a\u4f4e"],
      ["medium", "\u601d\u8003\uff1a\u4e2d"], ["high", "\u601d\u8003\uff1a\u9ad8"], ["xhigh", "\u601d\u8003\uff1a\u6781\u9ad8"], ["max", "\u601d\u8003\uff1a\u6700\u5927"],
    ];
    this._reasoningSel = HA.el("select", { class: "ws-chat-reasoning", title: "\u601d\u8003\u6a21\u5f0f", onchange: () =>
      this._updateSessionControl({ reasoning_level: this._reasoningSel.value }, "\u601d\u8003\u6a21\u5f0f") },
      ...reasoning.map(([value, text]) => HA.el("option", { value, text, selected: value === (session.reasoning_level || "inherit") })));

    this._permSeg = this._workspaceSegmented([
      { value: "readonly", label: "\u53ea\u8bfb", title: "\u4ec5\u5141\u8bb8\u8bfb\u53d6\u3001\u68c0\u7d22\u4e0e\u641c\u7d22\uff0c\u62d2\u7edd\u4fee\u6539\u548c\u6267\u884c" },
      { value: "ask", label: "\u8be2\u95ee", title: "\u6bcf\u6b21\u5de5\u5177\u64cd\u4f5c\u8bf7\u6c42\u786e\u8ba4" },
      { value: "allow", label: "\u5141\u8bb8", title: "\u5728\u89c4\u5219\u5141\u8bb8\u8303\u56f4\u5185\u81ea\u52a8\u6267\u884c" },
      { value: "unreviewed", label: "\u514d\u5ba1", title: "\u8df3\u8fc7\u5ba1\u6279\uff0c\u8bf7\u8c28\u614e\u4f7f\u7528" },
    ], value => this._updateSessionControl({ permission_mode: value }, "\u6743\u9650"));
    this._permSeg._set(session.permission_mode || "ask");

    this._modeSeg = this._workspaceSegmented([
      { value: "chat", label: "\u4f1a\u8bdd", title: "\u76f4\u63a5\u6267\u884c\u4efb\u52a1" },
      { value: "plan", label: "\u65b9\u6848", title: "\u5148\u751f\u6210\u53ef\u786e\u8ba4\u7684\u6267\u884c\u65b9\u6848" },
    ], value => this._updateSessionControl({ chat_mode: value }, "\u6a21\u5f0f"));
    this._modeSeg._set(session.chat_mode || "chat");

    const showTools = this._chatState.showTools !== false;
    this._toolToggle = HA.el("label", { class: "chk ws-chat-tools" },
      HA.el("input", { type: "checkbox", checked: showTools, onchange: event => {
        this._chatState.showTools = event.target.checked;
        this._renderMessages();
      } }), " \u5de5\u5177\u8fc7\u7a0b");
    this._statusBar = HA.el("span", { class: "ws-chat-status" });

    const toolbar = HA.el("header", { class: "ws-chat-toolbar" },
      HA.el("div", { class: "ws-chat-context" },
        HA.el("div", { class: "ws-chat-context-title", text: session.name || "\u5de5\u4f5c\u533a\u4f1a\u8bdd" }),
        HA.el("div", { class: "ws-chat-context-sub", text: `${workspace.name || "\u5de5\u4f5c\u533a"} \u00b7 ${this._agentName(session.agent_profile_id)}` })),
      HA.el("div", { class: "ws-chat-config-row" },
        HA.el("span", { class: "ws-chat-label", text: "\u6a21\u578b" }), this._modelSel, this._reasoningSel,
        HA.el("span", { class: "ws-chat-label", text: "\u6743\u9650" }), this._permSeg,
        HA.el("span", { class: "ws-chat-label", text: "\u6a21\u5f0f" }), this._modeSeg,
        this._toolToggle),
      HA.el("div", { class: "ws-chat-actions" },
        this._statusBar,
        HA.el("button", { class: "btn", text: "\u505c\u6b62", title: "\u505c\u6b62\u5f53\u524d\u8fd0\u884c", onclick: () => this._stopChat() }),
        HA.el("button", { class: "btn", text: "\u6e05\u7a7a", onclick: () => this._clearChat() }),
        HA.el("button", { class: "btn danger", text: "\u5220\u9664", onclick: () => this._deleteChat() })));
    this._updateStatusBar();
    return toolbar;
  }

  async _updateSessionControl(patch, label) {
    const wid = this.state.selectedWs, sid = this.state.selectedSession;
    if (!wid || !sid) return;
    try {
      const updated = await HA.api("POST",
        `/api/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/switch`, patch);
      const index = this.state.sessions.findIndex(session => session.session_id === sid);
      if (index >= 0) this.state.sessions[index] = updated;
      this._syncChatControls(updated);
      this._renderLeft();
      this._renderRight();
      this._refreshRuntimeStatus();
      HA.toast(`${label}\u5df2\u66f4\u65b0\uff0c\u4e0b\u6761\u6d88\u606f\u751f\u6548`, "ok");
    } catch (e) {
      // Restore the persisted state on conflict/validation failure.
      this._renderCenter();
    }
  }

  _syncChatControls(session) {
    if (this._modelSel) this._modelSel.value = session.model || this._modelSel.value;
    if (this._reasoningSel) this._reasoningSel.value = session.reasoning_level || "inherit";
    if (this._permSeg) this._permSeg._set(session.permission_mode || "ask");
    if (this._modeSeg) this._modeSeg._set(session.chat_mode || "chat");
  }

  async _stopChat() {
    const wid = this.state.selectedWs, sid = this.state.selectedSession;
    if (!wid || !sid) return;
    try {
      await HA.api("POST", `/api/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/stop`, {});
      HA.toast("\u5df2\u8bf7\u6c42\u505c\u6b62", "ok");
    } catch (e) { /* HA.api emits the error */ }
  }

  async _clearChat() {
    const ok = await HA.confirm("\u6e05\u7a7a\u8be5\u4f1a\u8bdd\u7684\u6d88\u606f\u5386\u53f2\uff1f", "\u6e05\u7a7a");
    if (!ok) return;
    const wid = this.state.selectedWs, sid = this.state.selectedSession;
    if (!wid || !sid) return;
    try {
      await HA.api("POST", `/api/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/clear`, {});
      this._chatState.messages = [];
      this._chatState.toolCalls = {};
      this._chatState.approvals = {};
      this._chatState.plans = {};
      this._chatState.reasoningBlocks = {};
      this._chatState.nextRenderOrder = 0;
      this._chatState.historyLoaded = true;
      this._renderMessages();
    } catch (e) { /* HA.api emits the error */ }
  }

  async _deleteChat() {
    const ok = await HA.confirm("\u5220\u9664\u8be5\u4f1a\u8bdd\uff1f\u5386\u53f2\u4e0e\u8fd0\u884c\u8d44\u6e90\u5c06\u88ab\u91ca\u653e\uff0c\u4e0d\u4f1a\u5220\u9664\u9879\u76ee\u6587\u4ef6\u3002", "\u5220\u9664");
    if (!ok) return;
    const wid = this.state.selectedWs, sid = this.state.selectedSession;
    if (!wid || !sid) return;
    try {
      await HA.api("DELETE", `/api/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}`, {});
      HA.toast("\u4f1a\u8bdd\u5df2\u5220\u9664", "ok");
      if (this._chatStates) this._chatStates.delete(wid + ":" + sid);
      this.state.selectedSession = null;
      await this._selectWorkspace(wid);
    } catch (e) { /* HA.api emits the error */ }
  }


};
