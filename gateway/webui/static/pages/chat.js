// pages/chat.js —— 会话页（P3b）
// 会话选择 / 模型切换 / 权限三档 / 模式切换 / 工具调用卡片 / plan 两阶段 / ask 审批
"use strict";

const CHAT_REASONING_LEVEL_LABELS = {
  inherit: "继承模型配置",
  provider_default: "服务商默认",
  none: "关闭推理",
  minimal: "极低",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "极高",
  max: "最大",
};

window.PageChat = class {
  constructor() {
    this._offs = [];
    // 跨页面实例保持状态（#1：切页不丢）
    this._state = HA.chatState = HA.chatState || {
      sessionKey: "webui:default",
      mode: "chat",
      showTools: true,
    };
    this.mode = this._state.mode;
    this.showTools = this._state.showTools;
    this.models = [];
    this.commands = [];
    this._commandsLoaded = false;
    this._busy = false;
    this._streamBubble = null;
    this._streamText = "";
    this._streamMessageId = "";
    this._closedStreamIds = new Set();
    // SSE tool events and the HTTP final reply travel independently.  Keep
    // the completed answer node by inbound message ID so late tool events
    // can still be inserted before their answer instead of at the bottom.
    this._finalBubbles = new Map();
    this._modeAvailability = { chat: true, plan: true };
    this._planCards = new Map();
    this._taskStart = 0;
    this._taskTimer = null;
  }

  // ---------- 任务运行时长（顶部栏常驻显示，无需 hover）----------
  _startTaskTimer() {
    this._stopTaskTimer();
    if (!this._taskStart) this._taskStart = Date.now();
    if (this._topTimer) {
      this._topTimer.style.display = "inline-flex";
      this._topTimer.classList.add("live");
    }
    this._taskTimer = setInterval(() => {
      if (this._topTimer) {
        this._topTimer.textContent = `\u23f3 ${HA.fmtDuration(Date.now() - this._taskStart)}`;
      }
    }, 1000);
  }
  _stopTaskTimer() {
    if (this._taskTimer) { clearInterval(this._taskTimer); this._taskTimer = null; }
  }
  _settleTaskTimer(bubble) {
    if (!this._taskStart) return;
    const elapsed = Date.now() - this._taskStart;
    this._taskStart = 0;
    this._stopTaskTimer();
    if (this._topTimer) {
      this._topTimer.textContent = `\u23f1 ${HA.fmtDuration(elapsed)}`;
      this._topTimer.classList.remove("live");
      // 结束后保留 5 秒展示总耗时，然后隐藏
      clearTimeout(this._topTimer._hideT);
      this._topTimer._hideT = setTimeout(() => {
        if (this._topTimer) this._topTimer.style.display = "none";
      }, 5000);
    }
  }

  get sessionKey() { return this._state.sessionKey; }
  set sessionKey(v) { this._state.sessionKey = v; }

  async render(root) {
    this._root = root;
    root.appendChild(this._buildTopbar());
    this._msgArea = HA.el("div", { class: "msg-area" });
    root.appendChild(this._msgArea);
    root.appendChild(this._buildInputbar());
    root.appendChild(this._buildApprovalHost());

    await Promise.all([
      this._loadModels(), this._loadCommands(), this._loadModes(),
    ]);
    await this._loadSessions();
    await this._refreshWorkMode();
    this._bindSSE();
    this._applyToolFilter();
    this._loadContext();
    if (!this._ctxPollTimer) {
      this._ctxPollTimer = setInterval(() => this._loadContext(), 5000);
    }
  }

  // ---------- 顶部栏 ----------
  _buildTopbar() {
    const bar = HA.el("div", { class: "chat-topbar" });

    this._sessionSel = HA.el("select", {
      onchange: async () => { this.sessionKey = this._sessionSel.value; await this._loadCommands(); await this.loadHistory(); await this._refreshWorkMode(); },
    });
    this._newSessionBtn = HA.el("button", { class: "btn", text: "＋ 新会话",
      onclick: () => this._newSession() });

    this._modelSel = HA.el("select", { onchange: () => this._switchModel() });
    this._reasoningSel = HA.el("select", { onchange: () => this._switchReasoning() },
      ...[
        ["inherit", "推理：继承模型"],
        ["provider_default", "推理：服务商默认"],
        ["none", "推理：关闭"],
        ["minimal", "推理：极低"],
        ["low", "推理：低"],
        ["medium", "推理：中"],
        ["high", "推理：高"],
        ["xhigh", "推理：极高"],
        ["max", "推理：最大"],
      ].map(([value, text]) => HA.el("option", { value, text })));
    this._permSeg = this._segmented([
      { value: "readonly", label: "\u53ea\u8bfb" },
      { value: "ask", label: "询问" },
      { value: "allow", label: "允许" },
      { value: "unreviewed", label: "免审" },
    ], m => this._setPermission(m));
    this._modeSeg = this._segmented([
      { value: "chat", label: "会话" },
      { value: "plan", label: "方案" },
    ], m => this._setMode(m));

    this._toolToggle = HA.el("label", { class: "chk" },
      HA.el("input", { type: "checkbox", checked: "checked",
        onchange: e => { this.showTools = e.target.checked; this._applyToolFilter(); } }),
      " 工具");

    this._clearBtn = HA.el("button", { class: "btn", text: "清空聊天",
      onclick: () => this._clear() });
    const delBtn = HA.el("button", { class: "btn danger", text: "删除",
      onclick: () => this._deleteSession() });

    this._modeHint = HA.el("div", { class: "mode-hint" });
    this._topTimer = HA.el("span", { class: "topbar-timer", style: "display:none" });

    bar.append(
      HA.el("div", { class: "tb-row" },
        HA.el("span", { class: "tb-label", text: "会话" }),
        this._sessionSel, this._newSessionBtn,
        HA.el("span", { class: "tb-label", text: "模型" }), this._modelSel,
        this._reasoningSel,
        HA.el("span", { class: "tb-label", text: "权限" }), this._permSeg,
        HA.el("span", { class: "tb-label", text: "模式" }), this._modeSeg,
        this._toolToggle, this._clearBtn, delBtn),
      this._modeHint,
      this._topTimer,
    );
    return bar;
  }

  _segmented(options, onPick) {
    // options 元素可为字符串或 {value,label}（label 中文展示，value 传回调）
    const wrap = HA.el("div", { class: "segmented" });
    const btns = {};
    for (const raw of options) {
      const o = typeof raw === "string" ? { value: raw, label: raw } : raw;
      const b = HA.el("button", {
        class: "seg-btn", text: o.label,
        title: o.value,
        disabled: o.disabled ? "disabled" : undefined,
        onclick: () => {
          Object.values(btns).forEach(x => x.classList.remove("on"));
          b.classList.add("on");
          onPick(o.value);
        },
      });
      btns[o.value] = b;
      wrap.appendChild(b);
    }
    wrap._set = v => {
      Object.entries(btns).forEach(([k, b]) =>
        b.classList.toggle("on", k === v));
    };
    wrap._setAvailable = values => {
      Object.entries(btns).forEach(([k, b]) => { b.disabled = values[k] === false; });
    };
    return wrap;
  }

  _renderModeHint() {
    const hints = {
      plan: "方案模式：发送将先生成方案，确认后执行（两阶段）",
    };
    this._modeHint.textContent = hints[this.mode] || "";
    if (this._clearBtn) {
      const labels = {
        plan: ["清空 Plan", "仅清除已结束的 Plan 卡片；运行中的 Plan 不受影响"],
      };
      const [text, title] = labels[this.mode] || ["清空聊天", "清除当前会话的聊天记录"];
      this._clearBtn.textContent = text;
      this._clearBtn.title = title;
    }
    if (this._input) {
      this._input.placeholder = "输入消息…（/ 触发命令补全；Ctrl+V 粘贴图片）";
    }
  }

  async _loadModes() {
    try {
      const d = await HA.api("GET", "/api/modes", undefined, { silent: true });
      for (const mode of d.modes || []) this._modeAvailability[mode.id] = !!mode.available;
    } catch (e) { }
    if (!this._modeAvailability[this.mode]) this.mode = this._state.mode = "chat";
    if (this._modeSeg) this._modeSeg._setAvailable(this._modeAvailability);
  }

  async _setMode(mode) {
    if (!this._modeAvailability[mode]) return;
    this.mode = this._state.mode = mode;
    this._modeSeg._set(mode);
    this._renderModeHint();
    await this._refreshWorkMode();
  }

  async _refreshWorkMode() {
    this._renderModeHint();
    if (this._msgArea) {
      this._msgArea.style.display = "";
      if (!this._msgArea.children.length) await this.loadHistory();
    }
  }

  // ---------- 输入栏（卡片式，deepseek-harness 风格）----------
  _buildInputbar() {
    const bar = HA.el("div", { class: "chat-inputbar" });
    this._acBox = HA.el("div", { class: "ac-box", style: "display:none" });

    // 图片缩略图预览区（卡片内顶部）
    this._imgPrev = HA.el("div", { class: "img-preview" });
    this._imgList = [];  // [{media_type, data}] base64

    this._input = HA.el("textarea", {
      class: "chat-input", rows: "1",
      placeholder: "输入消息…（/ 触发命令补全；Ctrl+V 粘贴图片）",
    });
    this._input.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this._send(); }
    });
    this._input.addEventListener("input", () => this._maybeAutocomplete());
    this._input.addEventListener("input", () => this._autoResizeInput());
    this._input.addEventListener("paste", e => this._onPaste(e));

    // 文件导入按钮（圆形 + 图标）
    this._fileInp = HA.el("input", { type: "file", accept: "image/*", multiple: "multiple",
      style: "display:none", onchange: e => this._onFiles(e) });
    const attachBtn = HA.el("button", { class: "chat-attach", type: "button",
      title: "添加图片（或 Ctrl+V 粘贴）", onclick: () => this._fileInp.click() },
      HA.el("span", { class: "chat-attach-ico", text: "＋" }));

    this._ctxMeter = HA.el("div", { class: "ctx-meter" },
      HA.el("span", { class: "ctx-icon", text: "📊" }),
      HA.el("span", { class: "ctx-pct", text: "–" }),
      HA.el("div", { class: "ctx-tip" }));
    this._ctxMeter.addEventListener("mouseenter", () => {
      const tip = this._ctxMeter.querySelector(".ctx-tip");
      if (tip) tip.style.display = "block";
    });
    this._ctxMeter.addEventListener("mouseleave", () => {
      const tip = this._ctxMeter.querySelector(".ctx-tip");
      if (tip) tip.style.display = "none";
    });

    this._sendBtn = HA.el("button", { class: "chat-send", type: "button",
      title: "发送（Enter）", onclick: () => this._send() },
      HA.el("span", { class: "chat-send-ico", text: "➤" }));

    // 卡片式容器：预览区 + textarea + 底部操作栏
    const composer = HA.el("div", { class: "chat-composer" },
      this._imgPrev, this._input,
      HA.el("div", { class: "chat-composer-actions" },
        attachBtn, this._fileInp,
        HA.el("span", { class: "chat-keyhint", text: "Enter 发送 · Shift+Enter 换行" }),
        this._ctxMeter, this._sendBtn));
    bar.append(this._acBox, composer);
    return bar;
  }

  _autoResizeInput() {
    const el = this._input;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }

  _onPaste(e) {
    const items = [...(e.clipboardData || {}).items || []];
    for (const item of items) {
      if (item.type.startsWith("image/")) {
        e.preventDefault();
        const blob = item.getAsFile();
        this._addImageFile(blob);
      }
    }
  }

  _onFiles(e) {
    for (const f of e.target.files) this._addImageFile(f);
    e.target.value = "";
  }

  _addImageFile(file) {
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = reader.result.split(",", 2)[1] || "";
      const mt = file.type || "image/png";
      this._imgList.push({ media_type: mt, data: b64 });
      const thumb = HA.el("div", { class: "img-thumb", style: `background-image:url(${reader.result})`,
        title: `${file.name || "粘贴"} — 点击删除`, onclick: () => { thumb.remove(); this._imgList = this._imgList.filter(x => x.data !== b64); }});
      this._imgPrev.appendChild(thumb);
    };
    reader.readAsDataURL(file);
  }

  _resetImages() { this._imgList = []; this._imgPrev.innerHTML = ""; }

  _buildApprovalHost() {
    this._approvalHost = HA.el("div", { class: "approval-host" });
    return this._approvalHost;
  }

  // ---------- 数据加载 ----------
  async _loadModels() {
    try {
      const d = await HA.api("GET", "/api/config/models", undefined, { silent: true });
      this.models = (d && d.models) ? d.models : [];
    } catch (e) { this.models = []; }  // P3d 端点未就绪时回退
    this._renderModelSel();
  }

  _renderModelSel() {
    this._modelSel.innerHTML = "";
    if (this.models.length) {
      for (const m of this.models) {
        const name = typeof m === "string" ? m : (m.name || m);
        this._modelSel.appendChild(HA.el("option", { value: name, text: name }));
      }
    } else {
      this._modelSel.appendChild(HA.el("option", { value: "", text: "(模型)" }));
    }
  }

  async _loadCommands() {
    try {
      const query = this.sessionKey ? `?session_key=${encodeURIComponent(this.sessionKey)}` : "";
      const d = await HA.api("GET", "/api/commands" + query, undefined, { silent: true });
      this.commands = d.commands || [];
      this._commandsLoaded = true;
    } catch (e) { this.commands = []; this._commandsLoaded = true; }
  }

  // ---------- 上下文占用指示器（deepseek-harness 风格）----------
  _ctxFmtTokens(n) {
    return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  }
  _ctxTipHtml(d) {
    const pct = Math.round((d.usage_ratio || 0) * 100);
    const rows = [
      ["模型", d.model || "—"],
      ["消息数", String(d.total_messages ?? 0)],
      ["已用", `${this._ctxFmtTokens(d.total_tokens ?? 0)} tokens`],
      ["占用", `${pct}%`],
    ];
    // 模型真实上下文窗口 与 历史预算（压缩阈值）分开展示，避免"上限=一半"的误解
    const ctx = d.model_context_length || 0;
    const budget = d.max_tokens || 0;
    if (ctx > 0) rows.push(["模型上下文", `${this._ctxFmtTokens(ctx)} tokens`]);
    if (budget > 0) {
      rows.push(["历史预算", `${this._ctxFmtTokens(budget)} tokens`]);
      rows.push(["剩余", `${this._ctxFmtTokens(d.remaining_tokens ?? 0)} tokens`]);
    }
    if (d.anchored) rows.push(["锚定", `${this._ctxFmtTokens(d.anchored_tokens ?? 0)} tokens`]);
    const title = `<div class="ctx-tip-title">上下文占用</div>`;
    const foot = ctx > 0 && budget > 0
      ? `<div class="ctx-tip-foot">历史预算 = 模型上下文 − 输出预留，达到阈值自动压缩</div>`
      : "";
    return title + rows.map(([k, v]) =>
      `<div class="ctx-tip-row"><span>${k}</span><b>${v}</b></div>`).join("") + foot;
  }
  _renderCtxMeter(d) {
    if (!this._ctxMeter) return;
    const pctEl = this._ctxMeter.querySelector(".ctx-pct");
    const tipEl = this._ctxMeter.querySelector(".ctx-tip");
    if (!d || !d.available) {
      this._ctxMeter.classList.add("dim");
      this._ctxMeter.classList.remove("warn", "danger");
      if (pctEl) pctEl.textContent = "–";
      if (tipEl) tipEl.innerHTML = '<div class="ctx-tip-row"><span>上下文</span><b>会话未加载</b></div>';
      return;
    }
    const pct = Math.round((d.usage_ratio || 0) * 100);
    this._ctxMeter.classList.remove("dim");
    this._ctxMeter.classList.toggle("warn", pct >= 70);
    this._ctxMeter.classList.toggle("danger", pct >= 90);
    if (pctEl) pctEl.textContent = pct + "%";
    if (tipEl) tipEl.innerHTML = this._ctxTipHtml(d);
  }
  async _loadContext() {
    try {
      const d = await HA.api("GET",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/context`,
        undefined, { silent: true });
      this._renderCtxMeter(d);
    } catch (e) { /* 会话未加载时静默 */ }
  }

  async _loadSessions() {
    try {
      const d = await HA.api("GET", "/api/sessions", undefined, { silent: true });
      this._renderSessionSel(d.sessions || []);
      // #1：回显当前会话的模型
      const cur = (d.sessions || []).find(s => s.session_key === this.sessionKey);
      if (cur && cur.model && this._modelSel) {
        for (const o of this._modelSel.options) { if (o.value === cur.model) o.selected = true; }
      }
    } catch (e) { /* ignore */ }
    if (![...this._sessionSel.options].some(o => o.value === this.sessionKey)) {
      this._sessionSel.appendChild(HA.el("option",
        { value: this.sessionKey, text: this.sessionKey }));
    }
    this._sessionSel.value = this.sessionKey;
    this._modeSeg._set(this.mode);  // #2
    this._renderModeHint();
    await this.loadHistory();
  }

  _renderSessionSel(sessions) {
    this._sessionSel.innerHTML = "";
    const mem = sessions.filter(s => s.source === "memory" || s.loaded);
    const disk = sessions.filter(s => s.source === "disk" && !s.loaded);
    if (mem.length) {
      const g = HA.el("optgroup", { label: "内存中" });
      mem.forEach(s => g.appendChild(HA.el("option",
        { value: s.session_key, text: s.session_key })));
      this._sessionSel.appendChild(g);
    }
    if (disk.length) {
      const g = HA.el("optgroup", { label: "仅磁盘" });
      disk.forEach(s => g.appendChild(HA.el("option",
        { value: s.session_key, text: s.session_key })));
      this._sessionSel.appendChild(g);
    }
  }

  async loadHistory() {
    this._msgArea.innerHTML = "";
    this._planCards.clear();
    try {
      const d = await HA.api("GET",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/history`,
        undefined, { silent: true });
      this._renderMessages(d.messages || []);
    } catch (e) {
      this._msgArea.appendChild(HA.el("div",
        { class: "empty-hint", text: "（无历史消息，发送第一条开始对话）" }));
    }
    await this._loadPlans();
    this._loadPermission();
    this._loadReasoning();
    this._loadContext();
  }

  async _loadReasoning() {
    try {
      const d = await HA.api("GET",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/reasoning`,
        undefined, { silent: true });
      if (d && this._reasoningSel) {
        this._reasoningSel.value = d.selected || "inherit";
        this._reasoningSel.disabled = !!d.loaded && d.protocol !== "openai";
        this._reasoningSel.title = d.loaded
          ? `当前生效：${CHAT_REASONING_LEVEL_LABELS[d.effective] || d.effective || "继承模型配置"}`
          : "将在会话加载后生效";
      }
    } catch (e) { }
  }

  async _loadPermission() {
    try {
      const d = await HA.api("GET",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/permission`,
        undefined, { silent: true });
      if (d && d.mode) this._permSeg._set(d.mode);
    } catch (e) { /* ignore */ }
  }

  // ---------- 消息渲染 ----------
  _renderMessages(messages) {
    let i = 0;
    while (i < messages.length) {
      const m = messages[i];
      // Internal recovery traffic must never be rendered as conversation.
      // Older sessions may still contain the raw failed envelope, so this
      // filters both the correction instruction and its paired assistant turn.
      if (m.kind === "protocol_error" || m.name === "protocol_correction" ||
          m.kind === "history_summary" || m.internal === true) {
        i++; continue;
      }
      // 内部格式纠正提示（format_hint）不在 UI 展示（name 标记 + 内容兜底兼容旧数据）
      const c0 = typeof m.content === "string" ? m.content : "";
      if (m.name === "format_hint" ||
          (m.role === "user" && (c0.startsWith("你的回复中包含代码但没有使用工具调用") ||
                                 c0.startsWith("【历史对话摘要】")))) {
        i++; continue;
      }
      if (m.role === "assistant" && m.kind === "tool_calls") {
        const group = HA.el("div", { class: "tool-group" });
        let j = i + 1;
        while (j < messages.length && messages[j].role === "tool") {
          const toolResult = messages[j];
          group.appendChild(this._toolCard({
            tool: toolResult.name || "tool",
            input: "",
            result: toolResult.content_text ?? toolResult.content ?? "",
            ok: !toolResult.is_error,
          }, false));
          j++;
        }
        if (j > i + 1) {
          this._msgArea.appendChild(group);
          i = j;
          continue;
        }
      }
      // Tool-invocation records often have assistant role but intentionally
      // carry no visible text.  Rendering them as normal messages leaves an
      // empty ???? card between tool calls and the final reply.
      const visibleText = String(m.content_text ?? (typeof m.content === "string" ? m.content : "")).trim();
      if (m.role === "assistant" && !visibleText) {
        i++;
        continue;
      }
      this._msgArea.appendChild(this._bubble(m));
      i++;
    }
    this._scrollBottom();
  }

  _bubble(m) {
    const role = (m.role === "user" && m.name !== "tool_result") ? "user"
      : (m.role === "assistant" ? "assistant" : "system");
    const text = m.content_text ?? (typeof m.content === "string" ? m.content : "");
    const bubble = HA.el("div", { class: `bubble ${role}` },
      HA.el("div", { class: "bubble-role",
        text: role === "user" ? "👤 我" : (role === "assistant" ? "🤖 助手" : "ℹ️") }));
    if (role === "user" || role === "assistant") {
      const body = role === "user"
        ? HA.el("div", { class: "bubble-text", text })
        : HA.el("div", { class: "bubble-text md", html: HA.renderMd(text) });
      bubble.appendChild(body);
      // 复制操作栏：用户消息与助手回答都提供（悬停显示）
      bubble.dataset.copyText = text;
      let copyBtn;
      copyBtn = HA.el("button", {
        class: "answer-action",
        type: "button",
        text: "复制",
        title: role === "user" ? "复制输入内容" : "复制回答",
        onclick: () => this._copyAnswer(bubble, copyBtn),
      });
      bubble.appendChild(HA.el("div", { class: "answer-actions" },
        copyBtn));
    } else {
      bubble.appendChild(HA.el("div", { class: "bubble-text md", html: HA.renderMd(text) }));
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

  _toolCard(c, pending) {
    const head = HA.el("div", { class: "tool-card-head" },
      HA.el("span", { class: "tool-ico", text: pending ? "⏳" : (c.ok ? "✅" : "❌") }),
      HA.el("span", { class: "tool-name", text: c.tool || "tool" }),
      HA.el("span", { class: "tool-caret", text: "▸" }));
    const body = HA.el("div", { class: "tool-card-body", style: "display:none" },
      HA.el("div", { class: "tool-io" },
        HA.el("div", { class: "k", text: "输入" }),
        HA.el("pre", { text: c.input || "" })),
      HA.el("div", { class: "tool-io" },
        HA.el("div", { class: "k", text: "返回" }),
        HA.el("pre", { text: c.result || "" })));
    const card = HA.el("div", { class: "tool-card" }, head, body);
    head.addEventListener("click", () => {
      const open = body.style.display !== "none";
      body.style.display = open ? "none" : "block";
      head.querySelector(".tool-caret").textContent = open ? "▸" : "▾";
    });
    return card;
  }

  _applyToolFilter() {
    if (this._root) this._root.classList.toggle("hide-tools", !this.showTools);
  }

  _scrollBottom() {
    requestAnimationFrame(() => { this._msgArea.scrollTop = this._msgArea.scrollHeight; });
  }

  // ---------- 发送 ----------
  _updateSendBtn() {
    const ico = this._sendBtn && this._sendBtn.querySelector(".chat-send-ico");
    if (ico) ico.textContent = this._busy ? "⏹" : "➤";
    if (this._sendBtn) {
      this._sendBtn.classList.toggle("busy", this._busy);
      this._sendBtn.title = this._busy ? "停止运行" : "发送（Enter）";
    }
  }

  async _send() {
    if (this._busy) { await this._stopSession(); return; }
    const text = this._input.value.trim();
    const hasImages = this._imgList.length > 0;
    if (!text && !hasImages) return;
    this._input.value = "";
    this._hideAutocomplete();
    const images = this._imgList.length ? [...this._imgList] : null;
    this._resetImages();

    if (this.mode === "plan") return this._sendPlan(text);

    const cmd = this.commands.find(c => c.name === "/plan");
    if (text.startsWith("/plan") && cmd && cmd.client_hint === "plan-flow") {
      return this._sendPlan(text.replace(/^\/plan\s*/, ""));
    }

    this._appendUser(text, images);
    this._setBusy(true);
    this._startTaskTimer();
    try {
      const r = await HA.api("POST", "/api/chat",
        { session_key: this.sessionKey, text, images: images || undefined, timeout: 120 });
      if (r && r.ok) this._finishStream(r.reply || "", r.message_id);
      else if (r && r.error) this._appendSystem(`⏳ ${r.error}（回复将稍后经事件到达）`);
    } catch (e) { /* toast 已弹 */ }
    this._setBusy(false);
    this._loadContext();
    // 命令回复不写入会话历史，不能用 loadHistory 重渲染（会清掉刚追加的回复）。
    // 仅对改写历史的命令重载历史。
    if (/^\/(clear|compact)\b/.test(text)) this.loadHistory();
  }

  async _stopSession() {
    if (!this._busy) return;
    try {
      await HA.api("POST",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/stop`);
    } catch (e) { /* ignore */ }
  }

  _setBusy(v) { this._busy = v; this._updateSendBtn(); }

  async _sendPlan(text) {
    this._appendUser(`[plan] ${text}`);
    try {
      const p = await HA.api("POST", "/api/plan",
        { session_key: this.sessionKey, text });
      this._appendPlanCard(p);
    } catch (e) { /* toast 已弹 */ }
  }

  async _loadPlans() {
    try {
      const d = await HA.api("GET", `/api/plans?session_key=${encodeURIComponent(this.sessionKey)}`,
        undefined, { silent: true });
      (d.plans || []).reverse().forEach(plan => this._appendPlanCard(plan));
    } catch (e) { /* plan persistence is optional for an empty/new session */ }
  }

  _appendPlanCard(payload) {
    const plan = payload && (payload.plan || payload);
    if (!plan || !plan.plan_id) return;
    let card = this._planCards.get(plan.plan_id);
    if (!card) {
      card = HA.el("div", { class: "plan-card", "data-plan-id": plan.plan_id });
      this._planCards.set(plan.plan_id, card);
      this._msgArea.appendChild(card);
    }
    this._renderPlanCard(card, plan);
    this._scrollBottom();
  }

  _renderPlanCard(card, plan) {
    card.dataset.planStatus = plan.status || "";
    const statusText = {
      awaiting_approval: "待确认", approved: "已批准", active: "执行中", paused: "已暂停",
      completed: "已完成", failed: "失败", cancelled: "已取消", superseded: "已替代",
    }[plan.status] || plan.status || "未知";
    const taskStatus = {
      pending: "○", ready: "◌", assigned: "◌", in_progress: "▶", waiting: "…",
      completed: "✅", failed: "❌", blocked: "⛔", cancelled: "⊘",
    };
    const list = HA.el("ol", { class: "plan-list" },
      ...(plan.tasks || []).map(task => HA.el("li", {
        text: `${taskStatus[task.status] || "•"} ${task.description}`,
        title: task.result_summary || task.blocked_reason?.message || task.status || "",
      })));
    const call = path => async () => {
      try {
        const response = await HA.api("POST", path);
        this._renderPlanCard(card, response.plan || plan);
      } catch (e) { /* toast is emitted by HA.api */ }
    };
    const archive = async () => {
      try {
        await HA.api("POST", `/api/plans/${encodeURIComponent(plan.plan_id)}/archive`);
        this._removePlanCard(plan.plan_id);
      } catch (e) { /* toast is emitted by HA.api */ }
    };
    const actions = [];
    if (plan.status === "awaiting_approval") {
      actions.push(HA.el("button", { class: "btn primary", text: "✅ 批准执行",
        onclick: call(`/api/plan/${encodeURIComponent(plan.plan_id)}/approve`) }));
      actions.push(HA.el("button", { class: "btn", text: "❌ 拒绝",
        onclick: call(`/api/plan/${encodeURIComponent(plan.plan_id)}/reject`) }));
    } else if (plan.status === "active") {
      actions.push(HA.el("button", { class: "btn", text: "⏸ 暂停",
        onclick: call(`/api/plans/${encodeURIComponent(plan.plan_id)}/pause`) }));
      actions.push(HA.el("button", { class: "btn danger", text: "⊘ 取消",
        onclick: call(`/api/plans/${encodeURIComponent(plan.plan_id)}/cancel`) }));
    } else if (plan.status === "paused") {
      actions.push(HA.el("button", { class: "btn primary", text: "▶ 继续",
        onclick: call(`/api/plans/${encodeURIComponent(plan.plan_id)}/resume`) }));
      actions.push(HA.el("button", { class: "btn danger", text: "⊘ 取消",
        onclick: call(`/api/plans/${encodeURIComponent(plan.plan_id)}/cancel`) }));
    } else if (plan.status === "approved") {
      actions.push(HA.el("button", { class: "btn danger", text: "⊘ 取消",
        onclick: call(`/api/plans/${encodeURIComponent(plan.plan_id)}/cancel`) }));
    }
    if (["completed", "failed", "cancelled", "superseded"].includes(plan.status)) {
      actions.push(HA.el("button", { class: "btn", text: "清除此 Plan",
        title: "仅隐藏已结束的 Plan 卡片，审计记录仍会保留",
        onclick: archive }));
    }
    const progress = Math.round((Number(plan.progress) || 0) * 100);
    card.replaceChildren(
      HA.el("div", { class: "plan-title", text: `📋 ${plan.title || "执行方案"} · ${statusText} · ${progress}%` }),
      list,
      HA.el("div", { class: "plan-actions" }, ...actions),
    );
  }
  _appendUser(text, images) {
    let content = text;
    if (images && images.length) {
      // 多模态：把图片渲染到气泡
      content = HA.el("div", {},
        HA.el("span", { text }),
        ...images.map(im => {
          const u = `data:${im.media_type || "image/png"};base64,${im.data}`;
          return HA.el("img", { src: u, class: "bubble-img" });
        }));
    }
    this._msgArea.appendChild(this._bubble({ role: "user", content }));
    this._scrollBottom();
  }
  _appendAssistant(text) { this._msgArea.appendChild(this._bubble({ role: "assistant", content: text })); this._scrollBottom(); }
  _appendSystem(text) { this._msgArea.appendChild(this._bubble({ role: "system", content: text })); this._scrollBottom(); }

  _startStream(messageId) {
    if (this._streamBubble && this._streamMessageId === messageId) return;
    this._streamText = "";
    this._streamMessageId = messageId || "";
    this._streamBubble = this._bubble({ role: "assistant", content: "" });
    this._msgArea.appendChild(this._streamBubble);
  }

  _appendStreamDelta(text, messageId) {
    if (messageId && this._closedStreamIds.has(messageId)) return;
    if (!text) return;
    this._startStream(messageId);
    this._streamText += text;
    this._streamBubble.dataset.copyText = this._streamText;
    const body = this._streamBubble.querySelector(".bubble-text");
    body.innerHTML = HA.renderMd(this._streamText);
    this._scrollBottom();
  }

  _resetStream(messageId) {
    if (messageId && this._closedStreamIds.has(messageId)) return;
    if (this._streamBubble) this._streamBubble.remove();
    this._streamBubble = null;
    this._streamText = "";
    this._streamMessageId = "";
  }

  _finishStream(answer, messageId) {
    if (messageId) {
      this._closedStreamIds.add(messageId);
      if (this._closedStreamIds.size > 64) {
        this._closedStreamIds.delete(this._closedStreamIds.values().next().value);
      }
    }
    let answerBubble = null;
    if (this._streamBubble) {
      this._streamText = answer;
      this._streamBubble.dataset.copyText = answer;
      const body = this._streamBubble.querySelector(".bubble-text");
      body.innerHTML = HA.renderMd(answer);
      answerBubble = this._streamBubble;
      this._streamBubble = null;
      this._streamText = "";
      this._streamMessageId = "";
      this._scrollBottom();
    } else if (answer) {
      answerBubble = this._bubble({ role: "assistant", content: answer });
      this._msgArea.appendChild(answerBubble);
      this._scrollBottom();
    }
    if (messageId && answerBubble) {
      this._finalBubbles.set(messageId, answerBubble);
      if (this._finalBubbles.size > 64) {
        this._finalBubbles.delete(this._finalBubbles.keys().next().value);
      }
    }
    this._settleTaskTimer(answerBubble);
  }

  _insertToolCard(card, messageId) {
    const answerBubble = this._finalBubbles.get(messageId) || this._streamBubble;
    if (answerBubble && answerBubble.parentNode === this._msgArea) {
      this._msgArea.insertBefore(card, answerBubble);
    } else {
      this._msgArea.appendChild(card);
    }
    this._scrollBottom();
  }

  // ---------- 运行期操作 ----------
  async _switchModel() {
    const model = this._modelSel.value;
    if (!model) return;
    try {
      await HA.api("POST",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/model`, { model });
      HA.toast(`已切换模型: ${model}`, "ok");
      await this._loadReasoning();
    } catch (e) { }
  }

  async _switchReasoning() {
    const level = this._reasoningSel.value;
    try {
      const d = await HA.api("POST",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/reasoning`, { level });
      this._reasoningSel.value = d.selected || level;
      const effective = d.effective || level;
      const label = CHAT_REASONING_LEVEL_LABELS[effective] || effective;
      this._reasoningSel.title = `当前生效：${label}`;
      HA.toast(`本会话推理等级：${label}`, "ok");
    } catch (e) { await this._loadReasoning(); }
  }

  async _setPermission(mode) {
    try {
      await HA.api("POST",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/permission`, { mode });
      HA.toast(`权限档位: ${mode}`, "ok");
    } catch (e) { }
  }

  async _clear() {
    try {
      if (this.mode === "plan") {
        const result = await HA.api("POST", "/api/plans/clear", { session_key: this.sessionKey });
        this._removeTerminalPlanCards();
        HA.toast(`已清除 ${result.archived || 0} 个已结束的 Plan`, "ok");
        return;
      }
      await HA.api("POST",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/clear`);
      await this.loadHistory();
      await this._refreshWorkMode();
    } catch (e) { }
  }

  _removePlanCard(planId) {
    const card = this._planCards.get(planId);
    if (card) card.remove();
    this._planCards.delete(planId);
  }

  _removeTerminalPlanCards() {
    const terminal = new Set(["completed", "failed", "cancelled", "superseded"]);
    for (const [planId, card] of this._planCards) {
      if (terminal.has(card.dataset.planStatus)) this._removePlanCard(planId);
    }
  }

  async _deleteSession() {
    if (!confirm(`删除会话 ${this.sessionKey}？（含磁盘文件）`)) return;
    try {
      await HA.api("DELETE",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}`);
      HA.toast("会话已删除", "ok");
      this.sessionKey = "webui:default";
      await this._loadSessions();
    } catch (e) { }
  }

  _newSession() {
    const name = prompt("新会话 key（自动加 webui: 前缀）", "s" + Date.now().toString(36));
    if (!name) return;
    this.sessionKey = name.startsWith("webui:") ? name : `webui:${name}`;
    this._loadSessions();
  }

  // ---------- / 命令补全 ----------
  _maybeAutocomplete() {
    const v = this._input.value;
    if (!this._commandsLoaded || !v.startsWith("/") || v.includes(" ")) {
      return this._hideAutocomplete();
    }
    const hits = this.commands.filter(c => c.name.startsWith(v));
    if (!hits.length) return this._hideAutocomplete();
    this._acBox.innerHTML = "";
    for (const c of hits.slice(0, 8)) {
      const item = HA.el("div", {
        class: "ac-item",
        title: c.help,
        onclick: () => {
          this._input.value = c.insert_text || (c.name + " ");
          this._hideAutocomplete();
          this._input.focus();
        },
      },
        HA.el("div", { class: "ac-name", text: `${c.name} ${c.args || ""}`.trim() }),
        c.help ? HA.el("div", { class: "ac-desc", text: c.help }) : null);
      this._acBox.appendChild(item);
    }
    this._acBox.style.display = "block";
  }
  _hideAutocomplete() { this._acBox.style.display = "none"; }

  // ---------- SSE ----------
  _bindSSE() {
    const sk = () => this.sessionKey;
    this._offs.push(HA.onSSE("chat.started", d => {
      if (d.session_key === sk()) {
        this._resetStream(d.message_id);
        this._setBusy(true);
      }
    }));
    this._offs.push(HA.onSSE("chat.text_delta", d => {
      if (d.session_key === sk()) this._appendStreamDelta(d.text || "", d.message_id);
    }));
    this._offs.push(HA.onSSE("chat.text_reset", d => {
      if (d.session_key === sk()) this._resetStream(d.message_id);
    }));
    this._offs.push(HA.onSSE("chat.error", d => {
      if (d.session_key === sk()) this._setBusy(false);
    }));
    this._offs.push(HA.onSSE("chat.stop_requested", d => {
      if (d.session_key === sk()) this._setBusy(false);
    }));
    this._offs.push(HA.onSSE("chat.tool_call_start", d => {
      if (d.session_key !== sk()) return;
      const card = this._toolCard({ tool: d.tool, input: "", result: "" }, true);
      card.dataset.toolCallId = d.tool_call_id;
      this._insertToolCard(card, d.message_id);
    }));
    this._offs.push(HA.onSSE("chat.tool_call_end", d => {
      if (d.session_key !== sk()) return;
      const card = this._msgArea.querySelector(`.tool-card[data-tool-call-id="${d.tool_call_id}"]`);
      if (card) {
        const pres = card.querySelectorAll(".tool-io pre");
        if (pres.length) pres[0].textContent = JSON.stringify(d.arguments || {}, null, 2);
      }
    }));
    this._offs.push(HA.onSSE("chat.tool_execution_end", d => {
      if (d.session_key !== sk()) return;
      const card = this._msgArea.querySelector(`.tool-card[data-tool-call-id="${d.tool_call_id}"]`);
      if (card) {
        card.querySelector(".tool-ico").textContent = d.is_error ? "❌" : "✅";
        const pres = card.querySelectorAll(".tool-io pre");
        if (pres.length > 1) pres[1].textContent = d.result || "";
      }
    }));
    this._offs.push(HA.onSSE("chat.tool.denied", d => {
      if (d.session_key !== sk()) return;
      this._appendSystem(`🚫 工具被拒绝: ${d.tool}（${d.reason}）`);
    }));
    this._offs.push(HA.onSSE("plan.changed", d => {
      if (d.action === "terminal_cards_cleared") {
        if (d.session_key === sk()) this._removeTerminalPlanCards();
        return;
      }
      const plan = d.plan;
      if (!plan || !plan.plan_id) return;
      const card = this._planCards.get(plan.plan_id);
      if (d.action === "archived") {
        if (card) this._removePlanCard(plan.plan_id);
        return;
      }
      if (card) this._renderPlanCard(card, plan);
    }));
    this._offs.push(HA.onSSE("chat.done", d => {
      if (d.session_key !== sk()) return;
      this._finishStream(d.full_text || "", d.message_id);
      this._setBusy(false);
    }));
    this._offs.push(HA.onSSE("chat.progress", d => {
      if (d.session_key !== sk()) return;
      this._appendSystem(d.text || "…");
    }));
    this._offs.push(HA.onSSE("approval.requested", d => {
      if (d.session_key !== sk()) return;
      this._showApproval(d);
    }));
    this._offs.push(HA.onSSE("approval.resolved", d => {
      const el = this._approvalHost.querySelector(`[data-aid="${d.id}"]`);
      if (el) el.remove();
    }));
  }

  _showApproval(d) {
    const answer = a => async () => {
      try {
        await HA.api("POST", `/api/approvals/${d.id}`, { answer: a });
      } catch (e) { }
      box.remove();
    };
    const box = HA.el("div", { class: "approval-card", "data-aid": d.id },
      HA.el("div", { class: "approval-title", text: `❓ 需要确认: ${d.tool}` }),
      HA.el("pre", { class: "approval-params", text: d.params_preview || "" }),
      HA.el("div", { class: "approval-actions" },
        HA.el("button", { class: "btn primary", text: "允许 (y)", onclick: answer("y") }),
        HA.el("button", { class: "btn danger", text: "拒绝 (n)", onclick: answer("n") }),
        HA.el("button", { class: "btn", text: "区内全放 (a)", onclick: answer("a") }),
        HA.el("button", { class: "btn", text: "跳过 (s)", onclick: answer("s") })));
    this._approvalHost.appendChild(box);
  }

  destroy() {
    this._offs.forEach(f => f());
    this._offs = [];
  }
};
