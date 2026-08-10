// pages/chat.js —— 会话页（P3b）
// 会话选择 / 模型切换 / 权限三档 / 模式切换 / 工具调用卡片 / plan 两阶段 / ask 审批
"use strict";

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
    this._closedStreamIds = new Set();
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

    await Promise.all([this._loadModels(), this._loadCommands()]);
    await this._loadSessions();
    this._bindSSE();
    this._applyToolFilter();
  }

  // ---------- 顶部栏 ----------
  _buildTopbar() {
    const bar = HA.el("div", { class: "chat-topbar" });

    this._sessionSel = HA.el("select", {
      onchange: () => { this.sessionKey = this._sessionSel.value; this.loadHistory(); },
    });
    this._newSessionBtn = HA.el("button", { class: "btn", text: "＋ 新会话",
      onclick: () => this._newSession() });

    this._modelSel = HA.el("select", { onchange: () => this._switchModel() });
    this._permSeg = this._segmented([
      { value: "ask", label: "询问" },
      { value: "allow", label: "允许" },
      { value: "unreviewed", label: "免审" },
    ], m => this._setPermission(m));
    this._modeSeg = this._segmented([
      { value: "chat", label: "会话" },
      { value: "plan", label: "方案" },
    ], m => { this.mode = m; this._renderModeHint(); });

    this._toolToggle = HA.el("label", { class: "chk" },
      HA.el("input", { type: "checkbox", checked: "checked",
        onchange: e => { this.showTools = e.target.checked; this._applyToolFilter(); } }),
      " 工具");

    const clearBtn = HA.el("button", { class: "btn", text: "清空",
      onclick: () => this._clear() });
    const delBtn = HA.el("button", { class: "btn danger", text: "删除",
      onclick: () => this._deleteSession() });

    this._modeHint = HA.el("div", { class: "mode-hint" });

    bar.append(
      HA.el("div", { class: "tb-row" },
        HA.el("span", { class: "tb-label", text: "会话" }),
        this._sessionSel, this._newSessionBtn,
        HA.el("span", { class: "tb-label", text: "模型" }), this._modelSel,
        HA.el("span", { class: "tb-label", text: "权限" }), this._permSeg,
        HA.el("span", { class: "tb-label", text: "模式" }), this._modeSeg,
        this._toolToggle, clearBtn, delBtn),
      this._modeHint,
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
    return wrap;
  }

  _renderModeHint() {
    this._modeHint.textContent = this.mode === "plan"
      ? "plan 模式：发送将先生成方案，确认后执行（两阶段）"
      : "";
  }

  // ---------- 输入栏 ----------
  _buildInputbar() {
    const bar = HA.el("div", { class: "chat-inputbar" });
    this._acBox = HA.el("div", { class: "ac-box", style: "display:none" });

    // 图片缩略图预览区
    this._imgPrev = HA.el("div", { class: "img-preview" });
    this._imgList = [];  // [{media_type, data}] base64

    this._input = HA.el("textarea", {
      class: "chat-input", rows: "2",
      placeholder: "输入消息…（/ 触发命令补全；Ctrl+V 粘贴图片）",
    });
    this._input.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this._send(); }
    });
    this._input.addEventListener("input", () => this._maybeAutocomplete());
    this._input.addEventListener("paste", e => this._onPaste(e));

    // 文件导入按钮
    this._fileInp = HA.el("input", { type: "file", accept: "image/*", multiple: "multiple",
      style: "display:none", onchange: e => this._onFiles(e) });
    const attachBtn = HA.el("button", { class: "btn", text: "📎",
      title: "导入图片（或 Ctrl+V 粘贴）", onclick: () => this._fileInp.click() });

    this._sendBtn = HA.el("button", { class: "btn primary", text: "发送",
      onclick: () => this._send() });
    bar.append(this._acBox, attachBtn, this._imgPrev, this._fileInp, this._input, this._sendBtn);
    return bar;
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
      const d = await HA.api("GET", "/api/commands", undefined, { silent: true });
      this.commands = d.commands || [];
      this._commandsLoaded = true;
    } catch (e) { this.commands = []; }
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
    try {
      const d = await HA.api("GET",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/history`,
        undefined, { silent: true });
      this._renderMessages(d.messages || []);
    } catch (e) {
      this._msgArea.appendChild(HA.el("div",
        { class: "empty-hint", text: "（无历史消息，发送第一条开始对话）" }));
    }
    this._loadPermission();
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
      if (m.kind === "protocol_error" || m.name === "protocol_correction") {
        i++; continue;
      }
      // 内部格式纠正提示（format_hint）不在 UI 展示（name 标记 + 内容兜底兼容旧数据）
      const c0 = typeof m.content === "string" ? m.content : "";
      if (m.name === "format_hint" ||
          (m.role === "user" && c0.startsWith("你的回复中包含代码但没有使用工具调用"))) {
        i++; continue;
      }
      const content = m.content_text ?? (typeof m.content === "string" ? m.content : "");
      const isStructuredToolCall = m.role === "assistant" && (
        m.kind === "tool_calls" ||
        (content.includes("agent.turn.v1") &&
         /"type"\s*:\s*"tool_calls"/.test(content))
      );
      const isAssistantAction = m.role === "assistant" &&
        (/ACTION[：:]/.test(content) || isStructuredToolCall);
      const next = messages[i + 1];
      const nextIsTool = next && next.role === "user" && next.name === "tool_result";
      if (isAssistantAction && nextIsTool) {
        const nextContent = next.content_text ?? (typeof next.content === "string" ? next.content : "");
        this._msgArea.appendChild(this._toolCardGroup(content, nextContent));
        i += 2;
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
    const body = role === "user"
      ? HA.el("div", { class: "bubble-text", text })
      : HA.el("div", { class: "bubble-text md", html: HA.renderMd(text) });
    return HA.el("div", { class: `bubble ${role}` },
      HA.el("div", { class: "bubble-role",
        text: role === "user" ? "👤 我" : (role === "assistant" ? "🤖 助手" : "ℹ️") }),
      body);
  }

  _toolCardGroup(assistantText, toolResultText) {
    const cards = this._parseToolResults(toolResultText);
    const group = HA.el("div", { class: "tool-group" });
    for (const c of cards) group.appendChild(this._toolCard(c, false));
    return group;
  }

  _parseToolResults(text) {
    const out = [];
    const blocks = String(text).split("【工具执行结果】").slice(1);
    for (const b of blocks) {
      const name = (b.match(/工具[:：]\s*([^\n]+)/) || [])[1] || "";
      const input = (b.match(/输入摘要[:：]\s*([\s\S]*?)(?=\n\s*返回结果[:：])/) || [])[1] || "";
      const result = (b.match(/返回结果[:：]\s*([\s\S]*?)(?=【工具执行完毕】|$)/) || [])[1] || "";
      const ok = !/❌/.test(b.split("【工具执行完毕】")[0]);
      out.push({ tool: name.trim(), input: input.trim(), result: result.trim(), ok });
    }
    if (!out.length) out.push({ tool: "tool", input: "", result: String(text).slice(0, 300), ok: true });
    return out;
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
    this._sendBtn.textContent = this._busy ? "⏹ 停止" : "发送";
    this._sendBtn.className = "btn" + (this._busy ? " danger" : " primary");
    this._sendBtn.disabled = false;
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
    try {
      const r = await HA.api("POST", "/api/chat",
        { session_key: this.sessionKey, text, images: images || undefined, timeout: 120 });
      if (r && r.ok) this._finishStream(r.reply || "", r.message_id);
      else if (r && r.error) this._appendSystem(`⏳ ${r.error}（回复将稍后经事件到达）`);
    } catch (e) { /* toast 已弹 */ }
    this._setBusy(false);
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

  _appendPlanCard(p) {
    const list = HA.el("ol", { class: "plan-list" },
      ...(p.tasks || []).map(t => HA.el("li", { text: t.description })));
    const approve = HA.el("button", { class: "btn primary", text: "✅ 批准执行",
      onclick: async () => {
        try {
          await HA.api("POST", `/api/plan/${p.plan_id}/approve`);
          card.remove();
          this._appendSystem("▶ 方案已提交执行，进度见工具事件与最终回复");
        } catch (e) { }
      } });
    const reject = HA.el("button", { class: "btn", text: "❌ 拒绝",
      onclick: async () => {
        try { await HA.api("POST", `/api/plan/${p.plan_id}/reject`); } catch (e) { }
        card.remove();
      } });
    const card = HA.el("div", { class: "plan-card" },
      HA.el("div", { class: "plan-title", text: "📋 方案预览" }), list,
      HA.el("div", { class: "plan-actions" }, approve, reject));
    this._msgArea.appendChild(card);
    this._scrollBottom();
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

  _startStream() {
    if (this._streamBubble) return;
    this._streamText = "";
    this._streamBubble = this._bubble({ role: "assistant", content: "" });
    this._msgArea.appendChild(this._streamBubble);
  }

  _appendStreamDelta(text, messageId) {
    if (messageId && this._closedStreamIds.has(messageId)) return;
    if (!text) return;
    this._startStream();
    this._streamText += text;
    const body = this._streamBubble.querySelector(".bubble-text");
    body.innerHTML = HA.renderMd(this._streamText);
    this._scrollBottom();
  }

  _resetStream(messageId) {
    if (messageId && this._closedStreamIds.has(messageId)) return;
    if (this._streamBubble) this._streamBubble.remove();
    this._streamBubble = null;
    this._streamText = "";
  }

  _finishStream(answer, messageId) {
    if (messageId) {
      this._closedStreamIds.add(messageId);
      if (this._closedStreamIds.size > 64) {
        this._closedStreamIds.delete(this._closedStreamIds.values().next().value);
      }
    }
    if (this._streamBubble) {
      this._streamText = answer;
      const body = this._streamBubble.querySelector(".bubble-text");
      body.innerHTML = HA.renderMd(answer);
      this._streamBubble = null;
      this._streamText = "";
      this._scrollBottom();
    } else if (answer) {
      this._appendAssistant(answer);
    }
  }

  // ---------- 运行期操作 ----------
  async _switchModel() {
    const model = this._modelSel.value;
    if (!model) return;
    try {
      await HA.api("POST",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/model`, { model });
      HA.toast(`已切换模型: ${model}`, "ok");
    } catch (e) { }
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
      await HA.api("POST",
        `/api/sessions/${encodeURIComponent(this.sessionKey)}/clear`);
      this.loadHistory();
    } catch (e) { }
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
      this._acBox.appendChild(HA.el("div", {
        class: "ac-item", text: `${c.name} ${c.args || ""}`.trim(),
        title: c.help,
        onclick: () => {
          this._input.value = c.name + " ";
          this._hideAutocomplete();
          this._input.focus();
        },
      }));
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
    this._offs.push(HA.onSSE("chat.tool.start", d => {
      if (d.session_key !== sk()) return;
      const card = this._toolCard({ tool: d.tool, input: d.params_preview, result: "" }, true);
      card.dataset.tool = d.tool;
      this._msgArea.appendChild(card);
      this._scrollBottom();
    }));
    this._offs.push(HA.onSSE("chat.tool.done", d => {
      if (d.session_key !== sk()) return;
      const cards = [...this._msgArea.querySelectorAll(".tool-card")];
      const card = [...cards].reverse().find(c => c.dataset.tool === d.tool);
      if (card) {
        card.querySelector(".tool-ico").textContent = d.ok ? "✅" : "❌";
        const pres = card.querySelectorAll(".tool-io pre");
        if (pres.length > 1) pres[1].textContent = d.preview || "";
      }
    }));
    this._offs.push(HA.onSSE("chat.tool.denied", d => {
      if (d.session_key !== sk()) return;
      this._appendSystem(`🚫 工具被拒绝: ${d.tool}（${d.reason}）`);
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
