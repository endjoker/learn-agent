// pages/prompt.js —— Prompt 页（P3d）
// 文件 Tab / 等宽编辑 / mtime 乐观并发（409 冲突）/ 应用到运行中会话
"use strict";

window.PagePrompt = class {
  constructor() {
    this._offs = [];
    this._files = [];
    this._current = null;     // 当前选中文件名
    this._mtime = 0;
    this._limit = 8000;
  }

  async render(root) {
    this._root = root;
    root.appendChild(HA.el("div", { class: "page-head" },
      HA.el("h1", { text: "📝 Prompt" }),
      HA.el("button", { class: "btn", text: "🔄 应用到运行中会话",
        onclick: () => this._apply() })));
    this._tabs = HA.el("div", { class: "prompt-tabs" });
    root.appendChild(this._tabs);
    this._editorWrap = HA.el("div", { class: "prompt-editor" });
    root.appendChild(this._editorWrap);
    this._offs.push(HA.onSSE("prompt.updated", () => this._loadFiles()));
    await this._loadFiles();
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
      // 文件不存在 → 空编辑器
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
        // 冲突：弹 diff 选择（覆盖 / 放弃）
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
          this._select(this._current);  // 重新加载最新
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

  destroy() { this._offs.forEach(f => f()); this._offs = []; }
};
