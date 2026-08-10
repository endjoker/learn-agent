// pages/skills.js —— Skills 页（P3c，只读展示）
"use strict";

window.PageSkills = class {
  constructor() { this._offs = []; }

  async render(root) {
    this._root = root;
    root.appendChild(HA.el("h1", { text: "🧩 Skills" }));
    this._meta = HA.el("div", { class: "skills-meta dim" });
    root.appendChild(this._meta);
    this._grid = HA.el("div", { class: "skills-grid" });
    root.appendChild(this._grid);
    await Promise.all([this._loadMeta(), this.refresh()]);
  }

  async _loadMeta() {
    try {
      const m = await HA.api("GET", "/api/skills/meta", undefined, { silent: true });
      this._meta.textContent =
        `技能目录: ${m.skills_dir}${m.exists ? "" : "（不存在）"}`
        + (m.platform_note ? `  ⚠️ ${m.platform_note}` : "");
    } catch (e) { this._meta.textContent = ""; }
  }

  async refresh() {
    let d;
    try {
      d = await HA.api("GET", "/api/skills", undefined, { silent: true });
    } catch (e) { return; }
    this._renderGrid(d.skills || []);
  }

  _renderGrid(skills) {
    this._grid.innerHTML = "";
    if (!skills.length) {
      this._grid.appendChild(HA.el("div",
        { class: "placeholder", text: "暂无技能（SKILLS 目录为空）" }));
      return;
    }
    for (const s of skills) {
      const card = HA.el("div", { class: "skill-card" },
        HA.el("div", { class: "skill-head" },
          HA.el("b", { text: s.name }),
          HA.el("span", { class: "dim", text: `v${s.version ?? 1}` })),
        HA.el("div", { class: "skill-desc", text: s.description || "" }),
        (s.tags || []).length
          ? HA.el("div", { class: "skill-tags" },
              ...s.tags.map(t => HA.badge(t, "dim")))
          : null,
        HA.el("div", { class: "dim", style: "margin-top:6px" },
          `${s.instruction_chars ?? 0} 字符指令`),
        HA.el("button", { class: "btn", text: "查看指令",
          onclick: () => this._showInstruction(s.name) }));
      this._grid.appendChild(card);
    }
  }

  async _showInstruction(name) {
    let d;
    try {
      d = await HA.api("GET", `/api/skills/${encodeURIComponent(name)}`);
    } catch (e) { return; }
    const modal = HA.el("div", { class: "modal-mask" },
      HA.el("div", { class: "modal wide" },
        HA.el("h2", { text: `🧩 ${name} — instruction.md` }),
        HA.el("div", { class: "md skill-instr",
          html: HA.renderMd(d.instruction || "") }),
        HA.el("div", { class: "modal-actions" },
          HA.el("button", { class: "btn", text: "关闭",
            onclick: () => modal.remove() }))));
    document.body.appendChild(modal);
  }

  destroy() { this._offs.forEach(f => f()); this._offs = []; }
};
