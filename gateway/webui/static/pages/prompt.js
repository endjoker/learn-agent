// pages/prompt.js —— Prompt 页
// 两个视图：Prompt 文件编辑 / 主会话能力配置（tools / skills / MCP）
"use strict";

window.PagePrompt = class {
  constructor() {
    this._offs = [];
    this._files = [];
    this._current = null;     // 当前选中文件名
    this._mtime = 0;
    this._limit = 8000;
    this._view = "files";
    this._capSelectors = [];
    this._caps = {
      loading: false,
      saving: false,
      previewing: false,
      previewData: null,
      dirty: false,
      loaded: false,
      error: "",
      config: null,
      catalog: null,
      selected: {
        tools: new Set(),
        skills: new Set(),
        mcp_servers: new Set(),
      },
    };
  }

  async render(root) {
    this._root = root;
    root.appendChild(HA.el("div", { class: "page-head" },
      HA.el("h1", { text: "📝 Prompt" })));

    this._viewBtns = {};
    this._viewTabs = HA.el("div", { class: "prompt-tabs" },
      this._viewBtns.files = HA.el("button", {
        class: "prompt-tab on", text: "📄 Prompt 文件",
        onclick: () => this._setView("files"),
      }),
      this._viewBtns.caps = HA.el("button", {
        class: "prompt-tab", text: "🔵 非工作区会话能力",
        onclick: () => this._setView("caps"),
      }));
    root.appendChild(this._viewTabs);

    this._filesView = HA.el("div", { class: "prompt-view" });
    this._capsView = HA.el("div", { class: "prompt-view", style: "display:none" });
    root.appendChild(this._filesView);
    root.appendChild(this._capsView);

    this._buildFilesView();
    this._buildCapsView();
    this._offs.push(HA.onSSE("prompt.updated", () => this._loadFiles()));

    await this._loadFiles();
    const initial = this._parseQuery();
    this._setView(initial.view === "caps" ? "caps" : "files");
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

  _setView(view) {
    this._view = view;
    this._viewBtns.files.classList.toggle("on", view === "files");
    this._viewBtns.caps.classList.toggle("on", view === "caps");
    this._filesView.style.display = view === "files" ? "" : "none";
    this._capsView.style.display = view === "caps" ? "" : "none";
    if (view === "caps" && !this._caps.loaded && !this._caps.loading) {
      this._loadCapabilities();
    }
  }

  // ---------- Prompt 文件 ----------
  _buildFilesView() {
    this._filesView.appendChild(HA.el("div", { class: "prompt-toolbar" },
      HA.el("button", { class: "btn", text: "🔄 应用到运行中会话",
        onclick: () => this._apply() })));
    this._tabs = HA.el("div", { class: "prompt-tabs" });
    this._editorWrap = HA.el("div", { class: "prompt-editor" });
    this._filesView.appendChild(this._tabs);
    this._filesView.appendChild(this._editorWrap);
  }

  async _loadFiles() {
    let d;
    try {
      d = await HA.api("GET", "/api/prompt/files", undefined, { silent: true });
    } catch (e) { return; }
    this._files = (d.files || []).filter(f => f.name !== "GUIDE.md");
    this._renderTabs();
    if (!this._current && this._files.length) {
      const inj = this._files.find(f => f.injected && f.exists) || this._files[0];
      this._select(inj.name);
    }
  }

  _renderTabs() {
    this._tabs.innerHTML = "";
    for (const f of this._files) {
      const b = HA.el("button", {
        class: "prompt-tab" + (f.name === this._current ? " on" : ""),
        onclick: () => this._select(f.name),
      },
        HA.el("span", { class: f.injected ? "dot-inj" : "dot-no", text: "●" }),
        ` ${f.name}`,
        f.injected ? null : HA.el("span", { class: "dim", text: " (不注入)" }));
      this._tabs.appendChild(b);
    }
  }

  async _select(name) {
    this._current = name;
    this._renderTabs();
    let d;
    try {
      d = await HA.api("GET",
        `/api/prompt/files/${encodeURIComponent(name)}`, undefined,
        { silent: true });
    } catch (e) {
      this._renderEditor("", 0, this._limit);
      return;
    }
    this._mtime = d.mtime_ns;
    this._limit = d.truncation_limit || 8000;
    this._renderEditor(d.content, d.mtime_ns, d.truncation_limit);
  }

  _renderEditor(content, mtime, limit) {
    this._editorWrap.innerHTML = "";
    this._ta = HA.el("textarea", { class: "prompt-ta", spellcheck: "false" }, content);
    this._counter = HA.el("span", { class: "dim" });
    const updateCount = () => {
      const n = this._ta.value.length;
      this._counter.textContent = `${n} 字符` + (n > limit ? "（超截断阈值）" : "");
      this._counter.style.color = n > limit ? "var(--warn)" : "";
    };
    this._ta.addEventListener("input", updateCount);
    updateCount();

    const saveBtn = HA.el("button", { class: "btn primary", text: "💾 保存",
      onclick: () => this._save() });
    this._editorWrap.append(
      HA.el("div", { class: "prompt-toolbar" }, this._counter, saveBtn),
      this._ta);
  }

  async _save() {
    const content = this._ta.value;
    const payload = { content, base_mtime_ns: this._mtime };
    try {
      const r = await fetch(`/api/prompt/files/${encodeURIComponent(this._current)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await r.json();
      if (r.status === 409) {
        const overwrite = confirm(
          "文件已被其他端修改。\n\n点【确定】用当前编辑内容覆盖，点【取消】放弃本次保存。");
        if (overwrite) {
          payload.base_mtime_ns = d.mtime_ns;
          const r2 = await fetch(`/api/prompt/files/${encodeURIComponent(this._current)}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const d2 = await r2.json();
          if (r2.ok) { this._mtime = d2.mtime_ns; HA.toast("已保存（覆盖冲突）", "ok"); }
          else HA.toast(d2.error || "保存失败", "err");
        } else {
          this._select(this._current);
        }
        return;
      }
      if (!r.ok) { HA.toast(d.error || "保存失败", "err"); return; }
      this._mtime = d.mtime_ns;
      HA.toast(d.warning ? `已保存：${d.warning}` : "已保存", d.warning ? "err" : "ok");
    } catch (e) { HA.toast("保存失败: " + e.message, "err"); }
  }

  async _apply() {
    try {
      const d = await HA.api("POST", "/api/prompt/apply");
      HA.toast(`已广播 /reload-prompt 到 ${d.queued} 个会话`, "ok");
    } catch (e) { }
  }

  // ---------- 主会话能力配置 ----------
  _buildCapsView() {
    this._capsHead = HA.el("div", { class: "ws-section-head" },
      HA.el("div", { class: "ws-section-title", text: "主会话默认能力" }),
      HA.el("div", { class: "ws-section-desc",
        text: "自定义所有非工作区会话（WebUI 主会话、新建 WebUI 会话、飞书等）默认使用的工具、技能和 MCP 服务。保存后对新建/重新加载的非工作区会话生效；已加载的会话不会热切换。" }));
    this._capsBody = HA.el("div", { class: "ws-editor-body", style: "padding:18px" });
    this._capsView.appendChild(this._capsHead);
    this._capsView.appendChild(this._capsBody);
  }

  async _loadCapabilities() {
    this._caps.loading = true;
    this._caps.error = "";
    this._renderCapabilities();
    try {
      const d = await HA.api("GET", "/api/prompt/main-session", undefined,
        { silent: true });
      this._caps.catalog = d.catalog || { tools: [], skills: [], mcp: { servers: [] } };
      this._caps.config = d.config || {};
      this._initSelectedSets();
      this._caps.loaded = true;
    } catch (e) {
      this._caps.error = e.message || "加载主会话能力配置失败";
    } finally {
      this._caps.loading = false;
      this._renderCapabilities();
    }
  }

  _initSelectedSets() {
    const cfg = this._caps.config || {};
    const cat = this._caps.catalog || { tools: [], skills: [], mcp: { servers: [] } };
    const allTools = (cat.tools || []).map(t => t.name).filter(Boolean);
    const allSkills = (cat.skills || []).map(s => s.id || s.name).filter(Boolean);
    const allMcp = ((cat.mcp && cat.mcp.servers) || []).map(s => s.name).filter(Boolean);

    this._caps.selected.tools = new Set(
      cfg.tools == null ? allTools : (cfg.tools || []));
    this._caps.selected.skills = new Set(
      cfg.skills == null ? allSkills : (cfg.skills || []));
    this._caps.selected.mcp_servers = new Set(
      cfg.mcp_servers == null ? allMcp : (cfg.mcp_servers || []));
    this._caps.dirty = false;
  }

  _toggleCap(field, id, checked) {
    const set = this._caps.selected[field];
    if (!set) return;
    if (checked) set.add(id); else set.delete(id);
    this._caps.dirty = true;
    this._updateCapsDirty();
  }

  _updateCapsDirty() {
    if (this._capDirtySpan) {
      this._capDirtySpan.textContent = this._caps.dirty ? "● 未保存" : "";
    }
  }

  _capField(label, items, field, extraHint) {
    const set = this._caps.selected[field] || new Set();
    const labelEl = HA.el("label", {
      text: `${label}（${set.size}）`,
    });
    const selector = new HA.SearchSelector({
      items,
      selected: set,
      placeholder: `搜索 ${label}…`,
      onToggle: (id, checked) => {
        this._toggleCap(field, id, checked);
        labelEl.textContent = `${label}（${set.size}）`;
      },
    });
    this._capSelectors.push(selector);
    const fieldEl = HA.el("div", { class: "ws-field" }, labelEl);
    if (extraHint) {
      fieldEl.appendChild(HA.el("div", {
        class: "dim", style: "font-size:11px;margin:3px 0 7px", text: extraHint }));
    }
    fieldEl.appendChild(selector.render());
    return fieldEl;
  }

  _renderCapabilities() {
    if (!this._capsBody) return;
    this._capsBody.innerHTML = "";
    this._capSelectors = [];

    if (this._caps.loading) {
      this._capsBody.appendChild(HA.el("div", { class: "ws-empty", text: "加载中…" }));
      return;
    }
    if (this._caps.error && !this._caps.loaded) {
      this._capsBody.appendChild(HA.el("div", { class: "ws-empty", text: this._caps.error }));
      this._capsBody.appendChild(HA.el("div", { class: "ws-actions" },
        HA.el("button", { class: "btn", text: "重试", onclick: () => this._loadCapabilities() })));
      return;
    }

    const cat = this._caps.catalog || { tools: [], skills: [], mcp: { servers: [] } };
    const panel = HA.el("section", { class: "ws-editor-section" },
      HA.el("div", { class: "ws-section-head" },
        HA.el("div", { class: "ws-section-title", text: "能力选择" }),
        HA.el("div", { class: "ws-section-desc",
          text: "缺省值（null）表示继承全部；在这里保存后，未勾选的项将不会出现在非工作区会话默认工具列表中。" })));

    const tools = (cat.tools || []).map(t => ({
      id: t.name, name: t.name, risk: t.risk, available: t.available,
    }));
    panel.appendChild(this._capField(
      "工具", tools, "tools",
      "风险说明：低=只读/查询；中=写入/网络；高=执行命令或代码。实际执行仍受权限与审批控制。"));

    panel.appendChild(this._capField(
      "技能", (cat.skills || []).map(s => ({ id: s.id || s.name, name: s.name })),
      "skills", null));

    panel.appendChild(this._capField(
      "MCP 服务",
      ((cat.mcp && cat.mcp.servers) || []).map(s => ({
        id: s.name,
        name: s.available ? s.name : `${s.name}（未连接；运行时会尝试连接）`,
        available: true,
        unavailable_reason: s.available ? "" : "当前未连接；运行时按配置建立连接",
      })),
      "mcp_servers", null));

    this._capsBody.appendChild(panel);

    this._previewBox = HA.el("div", {});
    this._capsBody.appendChild(this._previewBox);
    if (this._caps.previewData) {
      this._previewBox.appendChild(new HA.PromptPreview(this._caps.previewData).render());
    }

    const saveBtn = HA.el("button", {
      class: "btn primary", text: this._caps.saving ? "保存中…" : "💾 保存配置",
      disabled: this._caps.saving,
      onclick: () => this._saveCapabilities(),
    });
    this._capDirtySpan = HA.el("span", { class: "ws-dirty", text: "" });
    this._capsBody.appendChild(HA.el("div", { class: "ws-savebar", style: "margin:18px 0 0" },
      saveBtn,
      HA.el("button", {
        class: "btn", text: this._caps.previewing ? "预览中…" : "🔍 预览 Prompt",
        disabled: this._caps.previewing,
        onclick: () => this._previewCaps(),
      }),
      HA.el("button", { class: "btn", text: "恢复继承全部",
        onclick: () => this._resetCapsToInherit() }),
      this._capDirtySpan,
      HA.el("span", { class: "dim", style: "font-size:12px",
        text: "保存后仅影响后续新建的非工作区会话 Agent；已加载的会话不会热切换。" })));
    this._updateCapsDirty();
  }

  async _resetCapsToInherit() {
    if (!(await HA.confirm("将非工作区会话工具、技能和 MCP 恢复为继承全部（null）？", "恢复"))) return;
    if (this._caps.saving) return;
    this._caps.saving = true;
    this._renderCapabilities();
    try {
      const d = await HA.api("PUT", "/api/prompt/main-session",
        { tools: null, skills: null, mcp_servers: null });
      this._caps.config = JSON.parse(JSON.stringify(d.config || {}));
      this._initSelectedSets();
      this._caps.dirty = false;
      HA.toast("已恢复为继承全部", "ok");
    } catch (e) {
      HA.toast("恢复失败: " + e.message, "err");
    } finally {
      this._caps.saving = false;
      this._renderCapabilities();
    }
  }

  async _previewCaps() {
    if (this._caps.previewing) return;
    this._caps.previewing = true;
    this._renderCapabilities();
    try {
      const data = await HA.api("POST", "/api/prompt/main-session/preview", {
        tools: Array.from(this._caps.selected.tools),
        skills: Array.from(this._caps.selected.skills),
        mcp_servers: Array.from(this._caps.selected.mcp_servers),
      });
      this._caps.previewData = data;
    } catch (e) {
      HA.toast("预览失败: " + e.message, "err");
    } finally {
      this._caps.previewing = false;
      this._renderCapabilities();
    }
  }

  async _saveCapabilities() {
    if (this._caps.saving) return;
    this._caps.saving = true;
    this._renderCapabilities();
    const payload = {
      tools: Array.from(this._caps.selected.tools),
      skills: Array.from(this._caps.selected.skills),
      mcp_servers: Array.from(this._caps.selected.mcp_servers),
    };
    try {
      const d = await HA.api("PUT", "/api/prompt/main-session", payload);
      this._caps.config = JSON.parse(JSON.stringify(d.config || payload));
      this._caps.dirty = false;
      HA.toast("非工作区会话能力配置已保存", "ok");
    } catch (e) {
      HA.toast("保存失败: " + e.message, "err");
    } finally {
      this._caps.saving = false;
      this._renderCapabilities();
    }
  }

  destroy() { this._offs.forEach(f => f()); this._offs = []; }
};
