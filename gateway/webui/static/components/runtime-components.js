// runtime-components.js —— 工作区/智能体模块可复用组件（Phase 2 起步，Phase 5 扩展）
// 不依赖具体页面全局状态；数据经参数传入。
"use strict";

window.HA = window.HA || {};

// 搜索选择器：候选列表 + 已选集合
HA.SearchSelector = class {
  /**
   * @param opts {items:[{id,name,description,risk,available}], selected:Set,
   *              onToggle:(id,checked)=>void, placeholder:string}
   */
  constructor(opts) {
    this.items = opts.items || [];
    this.selected = opts.selected || new Set();
    this.onToggle = opts.onToggle || (() => {});
    this.placeholder = opts.placeholder || "搜索…";
    this._q = "";
  }
  render() {
    const root = HA.el("div", { class: "ws-selector" });
    const q = HA.el("input", { type: "text", placeholder: this.placeholder,
      oninput: (e) => { this._q = e.target.value.toLowerCase(); this._renderList(); } });
    this._list = HA.el("div", { class: "ws-check-grid" });
    root.appendChild(q);
    root.appendChild(this._list);
    this._renderList();
    return root;
  }
  _renderList() {
    this._list.innerHTML = "";
    const shown = this.items.filter(i => !this._q || (i.name || i.id).toLowerCase().includes(this._q));
    if (!shown.length) {
      this._list.appendChild(HA.el("div", { class: "ws-empty", text: "无匹配项" }));
      return;
    }
    for (const item of shown) {
      const id = item.id || item.name;
      const checked = this.selected.has(id);
      const riskLabels = {
        low: "\u4f4e\u98ce\u9669\uff08\u53ea\u8bfb/\u67e5\u8be2\uff09",
        medium: "\u4e2d\u98ce\u9669\uff08\u5199\u5165/\u7f51\u7edc\uff09",
        high: "\u9ad8\u98ce\u9669\uff08\u6267\u884c\u547d\u4ee4/\u4ee3\u7801\uff09",
      };
      const tag = item.risk ? HA.badge(riskLabels[item.risk] || item.risk, item.risk) : null;
      // 名称 + 可选描述（描述存在时在名称下方小字展示，便于配置时了解能力）
      const textWrap = HA.el("span", { class: "ws-check-text" },
        HA.el("span", { class: "ws-check-name", text: item.name || id }),
        item.description
          ? HA.el("span", { class: "ws-check-desc", text: item.description })
          : null);
      const row = HA.el("label", { class: "ws-check" },
        HA.el("input", { type: "checkbox", checked, disabled: item.available === false,
          onchange: (e) => { this.onToggle(id, e.target.checked); } }),
        textWrap,
        tag || null);
      if (item.available === false) {
        row.classList.add("dim");
        row.title = item.unavailable_reason || "\u5f53\u524d\u8fd0\u884c\u73af\u5883\u4e0d\u53ef\u7528";
      }
      this._list.appendChild(row);
    }
  }
};

// 能力标签组
HA.CapabilityTags = function (names, kind) {
  return HA.el("div", { class: "ws-tags" },
    ...(names || []).map(n => HA.el("span", { class: "ws-tag", text: n })));
};

// Prompt section 预览组件
HA.PromptPreview = class {
  constructor(data) {
    this.data = data || null;
  }
  render() {
    const root = HA.el("div", { class: "ws-preview" });
    if (!this.data) {
      root.appendChild(HA.el("div", { class: "ws-empty", text: "点击「预览 Prompt」生成分区预览" }));
      return root;
    }
    const head = HA.el("div", { class: "ws-field" },
      HA.el("div", { text: `总计 ${this.data.total_chars} 字符 · 约 ${this.data.estimated_tokens} tokens` }),
      HA.el("div", { text: `hash: ${(this.data.expected_prompt_hash || "").slice(0, 24)}…`,
        class: "dim", style: "font-size:11px" }));
    root.appendChild(head);
    for (const w of (this.data.warnings || [])) {
      root.appendChild(HA.el("div", { class: "ws-warn", text: `⚠️ ${w.message}` }));
    }
    for (const s of (this.data.sections || [])) {
      if (!s.content) continue;
      root.appendChild(HA.el("details", { class: "ws-preview-section" },
        HA.el("summary", { text: `${s.name} · ${s.chars} 字符 · ~${s.estimated_tokens} tokens` }),
        HA.el("pre", { text: s.content })));
    }
    return root;
  }
};

// 确认对话框
HA.confirm = function (message, okText) {
  return new Promise((resolve) => {
    const modal = HA.el("div", { class: "modal-mask" },
      HA.el("div", { class: "modal" },
        HA.el("div", { class: "md", text: message }),
        HA.el("div", { class: "modal-actions" },
          HA.el("button", { class: "btn", text: "取消",
            onclick: () => { modal.remove(); resolve(false); } }),
          HA.el("button", { class: "btn primary", text: okText || "确定",
            onclick: () => { modal.remove(); resolve(true); } }))));
    document.body.appendChild(modal);
  });
};

// ============================================================
// Phase 5：运行期展示组件（消息/工具/审批/Plan）
// 组件接收数据，不直接订阅全局 SSE。
// ============================================================

// 任务耗时格式化（deepseek-harness 风格 HH:MM:SS）
HA.fmtDuration = function (ms) {
  const total = Math.max(0, Math.floor((ms || 0) / 1000));
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
};

// 消息气泡：与主会话复用 .bubble / .bubble-text 视觉体系。
HA.MessageBubble = class {
  constructor(msg) {
    this.msg = msg || {};
  }
  render() {
    const role = this.msg.role || "assistant";
    const visualRole = role === "user" ? "user" : role === "tool" ? "system" : "assistant";
    const content = String(this.msg.content || "");
    const bubble = HA.el("div", { class: `bubble ${visualRole}` });
    if (visualRole === "user") {
      bubble.dataset.copyText = content;
      bubble.appendChild(HA.el("div", { class: "bubble-text", text: content }));
      let copyBtn;
      copyBtn = HA.el("button", {
        class: "answer-action",
        type: "button",
        text: "复制",
        title: "复制输入内容",
        onclick: () => this._copyAnswer(bubble, copyBtn),
      });
      bubble.appendChild(HA.el("div", { class: "answer-actions" }, copyBtn));
    } else if (visualRole === "assistant") {
      bubble.dataset.copyText = content;
      bubble.appendChild(HA.el("div", { class: "bubble-text md", html: HA.renderMd(content) }));
      let copyBtn;
      copyBtn = HA.el("button", {
        class: "answer-action",
        type: "button",
        text: "复制",
        title: "复制回答",
        onclick: () => this._copyAnswer(bubble, copyBtn),
      });
      const timer = (this.msg.duration != null)
        ? HA.el("span", { class: "task-timer", text: `⏱ ${HA.fmtDuration(this.msg.duration)}` })
        : null;
      bubble.appendChild(HA.el("div", { class: "answer-actions" }, copyBtn, timer || null));
    } else {
      bubble.appendChild(HA.el("pre", { class: "bubble-text", text: content }));
    }
    return bubble;
  }

  async _copyAnswer(bubble, button) {
    const text = bubble.dataset.copyText || bubble.querySelector(".bubble-text")?.innerText || "";
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.focus();
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    const original = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => { if (button.isConnected) button.textContent = original; }, 1400);
  }
};

// 工具调用卡片
HA.ToolCard = class {
  constructor(data) {
    this.data = data || {};
  }
  render() {
    const d = this.data;
    const status = d.status || "running";
    const card = HA.el("div", { class: "ws-tool-card " + status });
    card.appendChild(HA.el("div", { class: "ws-tool-head" },
      HA.el("span", { class: "ws-tool-name", text: d.tool || d.name || "tool" }),
      HA.badge(status, status === "ok" ? "low" : status === "error" ? "high" : "medium")));
    if (d.arguments) {
      card.appendChild(HA.el("pre", { class: "ws-tool-args", text: JSON.stringify(d.arguments, null, 2) }));
    }
    if (d.result !== undefined && d.result !== null) {
      const resultStr = String(d.result);
      const preview = resultStr.length > 2000 ? resultStr.slice(0, 2000) + "…（已截断）" : resultStr;
      card.appendChild(HA.el("details", { class: "ws-tool-result-wrap" },
        HA.el("summary", { text: "工具结果" }),
        HA.el("pre", { class: "ws-tool-result", text: preview })));
    }
    return card;
  }
};

// 审批卡片
HA.ApprovalCard = class {
  constructor(data, onAnswer) {
    this.data = data || {};
    this.onAnswer = onAnswer || (() => {});
  }
  render() {
    const d = this.data;
    const card = HA.el("div", { class: "ws-approval-card" },
      HA.el("div", { class: "ws-msg-head" },
        HA.el("span", { class: "ws-msg-role", text: "审批" }),
        HA.el("span", { class: "dim", style: "font-size:11px", text: d.tool })),
      HA.el("pre", { class: "ws-tool-args", text: d.params_preview || "" }),
      HA.el("div", { class: "ws-actions" },
        HA.el("button", { class: "btn primary", text: "允许一次",
          onclick: () => this.onAnswer("y") }),
        HA.el("button", { class: "btn", text: "拒绝",
          onclick: () => this.onAnswer("n") }),
        HA.el("button", { class: "btn", text: "全部允许",
          onclick: () => this.onAnswer("a") }),
        HA.el("button", { class: "btn", text: "停止",
          onclick: () => this.onAnswer("s") })));
    return card;
  }
};

// Plan 卡片（简化：显示状态）
HA.PlanCard = class {
  constructor(data) {
    this.data = data || {};
  }
  render() {
    const d = this.data;
    return HA.el("div", { class: "ws-plan-card" },
      HA.el("div", { class: "ws-msg-head" },
        HA.el("span", { class: "ws-msg-role", text: "Plan" }),
        HA.badge(d.status || "pending", "medium")),
      HA.el("div", { class: "ws-msg-body", text: d.title || d.plan_id || "" }));
  }
};
