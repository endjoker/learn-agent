import { useEffect, useMemo, useState } from "react";


import { ApprovalHost } from "@/approvals/ApprovalHost";
import { useApprovalQueue } from "@/approvals/useApprovalQueue";
import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import type { Workspace, WorkspaceSession, WorkspaceSessionSwitchPatch } from "@/api/types";
import { ChatComposer } from "@/components/ChatComposer";
import { confirmDialog } from "@/components/confirm";
import { RuntimeFloat } from "@/components/RuntimeFloat";
import { useRuntimeFloat } from "@/components/useRuntimeFloat";
import { toast } from "@/components/toast";
import { QueuePanel } from "@/pages/chat/QueuePanel";
import { TimelineRow } from "@/pages/chat/timelineRow";
import { VirtualMessageList } from "@/pages/chat/VirtualMessageList";
import { CreateSessionModal } from "@/pages/workspace/CreateSessionModal";
import { CreateWorkspaceWizard } from "@/pages/workspace/CreateWorkspaceWizard";
import { WorkspaceFileList } from "@/pages/workspace/WorkspaceFileList";
import { WorkspaceFileViewer } from "@/pages/workspace/WorkspaceFileViewer";
import { useWorkspaceConversation } from "@/pages/workspace/useWorkspaceConversation";
import { useWorkspaceController } from "@/pages/workspace/useWorkspaceController";
import { QuestionHost } from "@/questions/QuestionHost";
import { useQuestionQueue } from "@/questions/useQuestionQueue";


/** Coerce an unknown record field to a string with a fallback (never "[object Object]"). */
const str = (value: unknown, fallback = ""): string => typeof value === "string" ? value : fallback;
const strList = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

// L5：上下文轮询周期 —— 忙时 5s 快轮询，空闲 30s 兜底（事件驱动为主，轮询为辅）
const CTX_POLL_BUSY_MS = 5000;
const CTX_POLL_IDLE_MS = 30_000;

interface WorkspaceContextData {
  available?: boolean;
  usage_ratio?: number;
  total_messages?: number;
  total_tokens?: number;
  model?: string;
  model_context_length?: number;
  max_tokens?: number;
  remaining_tokens?: number;
  anchored?: boolean;
  anchored_tokens?: number;
}

const fmtWorkspaceTokens = (value: number): string => {
  if (value >= 1048576) return `${(value / 1048576).toFixed(2).replace(/\.?0+$/, "")}M`;
  if (value >= 1024) return `${(value / 1024).toFixed(1).replace(/\.?0+$/, "")}K`;
  return String(Math.round(value));
};

const safeDecode = (value: string): string => {
  try { return decodeURIComponent(value); } catch { return value; }
};

const parseQueryId = (): string => {
  const q = (window.location.hash.split("?")[1] ?? "");
  for (const pair of q.split("&")) {
    if (!pair) continue;
    const [k, v] = pair.split("=");
    if (safeDecode(k ?? "") === "id") return safeDecode(v ?? "");
  }
  return "";
};

const REASONING_LEVELS: Array<[string, string]> = [
  ["inherit", "思考：继承模型"],
  ["provider_default", "思考：服务商默认"],
  ["none", "思考：关闭"],
  ["minimal", "思考：极低"],
  ["low", "思考：低"],
  ["medium", "思考：中"],
  ["high", "思考：高"],
  ["xhigh", "思考：极高"],
  ["max", "思考：最大"],
];

const PERMISSION_MODES: Array<[string, string]> = [
  ["readonly", "只读"],
  ["ask", "询问"],
  ["allow", "允许"],
  ["unreviewed", "免审"],
];

interface ChatCommand {
  name: string;
  args?: string;
  help?: string;
  insert_text?: string;
}

interface WorkspaceChatProps {
  workspaceId: string;
  sessionId: string;
  workspaceName: string;
  sessionName: string;
  agentName: string;
  session: WorkspaceSession;
  models: string[];
  client: ApiClient;
  onSwitch: (patch: WorkspaceSessionSwitchPatch, label: string) => Promise<void>;
  onStop: () => Promise<void>;
  onClear: () => Promise<void>;
  onDelete: () => Promise<void>;
}

function WorkspaceChat({ workspaceId, sessionId, workspaceName, sessionName, agentName, session, models, client, onSwitch, onStop, onClear, onDelete }: WorkspaceChatProps) {
  const sessionKey = str(session.session_key, `workspace:${workspaceId}:${sessionId}`);
  const [ctx, setCtx] = useState<WorkspaceContextData | null>(null);
  // 统一链路数据源（设计方案 19.2/20）：Conversation/Turn/Node + Gateway SSE。
  // L5：双 SSE 冗余合并 —— 只保留 useWorkspaceConversation 内部那一条
  // sessionKey 作用域连接；旧浮层事件（审批/问题/运行时）通过 onLegacyEvent
  // 沿同一连接转发（后端 matches_scope 对 session_key 过滤已包含这些事件）。
  const chat = useWorkspaceConversation(sessionKey, {
    onLegacyEvent: (event) => {
      questions.onSse(event);
      approvals.onSse(event);
      runtime.onSse(event);
    },
    // 需求：输入新消息后，旧的终态 plan/goal 状态框应消失。
    onMessageSent: () => runtime.dismissStale(),
  });
  const questions = useQuestionQueue(client, { workspaceId, workspaceSessionId: sessionId });
  const approvals = useApprovalQueue(client, { workspaceId, workspaceSessionId: sessionId });
  const runtime = useRuntimeFloat(client, { sessionKey });

  // L5：上下文轮询降频/事件化 —— 忙时 5s，空闲退到 30s；页面不可见时暂停；
  // WorkspaceChat 只在选中会话时挂载，未加载/未选中时不存在任何轮询
  // （不会每 5s 拉整历史文件）。
  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const loadContext = async () => {
      try {
        const data = await client.get<{ context?: WorkspaceContextData | null; model?: string; is_busy?: boolean }>(
          `/api/workspaces/${encodeURIComponent(workspaceId)}/sessions/${encodeURIComponent(sessionId)}/runtime-status`,
          { silent: true },
        );
        if (active) {
          setCtx(data.context
            ? { ...data.context, model: data.model ?? data.context.model }
            // agent 未加载时 context 为 null：保留快照模型，让详情面板仍能显示
            : (data.model ? { available: false, model: data.model } : null));
        }
      } catch {
        if (active) setCtx(null);
      }
    };
    const schedule = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      if (!active || document.visibilityState === "hidden") { timer = undefined; return; }
      timer = window.setTimeout(() => {
        timer = undefined;
        if (!active) return;
        void loadContext();
        schedule();
      }, chat.busy ? CTX_POLL_BUSY_MS : CTX_POLL_IDLE_MS);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") schedule();
      else if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
    };
    void loadContext();
    schedule();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [client, sessionId, workspaceId, chat.busy]);

  const [showTools, setShowTools] = useState(true);
  const [wsCommands, setWsCommands] = useState<ChatCommand[]>([]);

  useEffect(() => {
    let active = true;
    void client.get<{ commands?: ChatCommand[] }>(
      `/api/workspaces/${encodeURIComponent(workspaceId)}/sessions/${encodeURIComponent(sessionId)}/commands`,
      { silent: true },
    ).then((data) => { if (active) setWsCommands(data.commands ?? []); })
      .catch(() => setWsCommands([]));
    return () => { active = false; };
  }, [client, sessionId, workspaceId]);

  const displayed = chat.displayed;
  const busy = chat.busy;
  const model = str(session.model);
  const reasoning = str(session.reasoning_level, "inherit");
  const permission = str(session.permission_mode, "ask");
  const ctxAvailable = Boolean(ctx && ctx.available !== false);
  const ctxPct = ctxAvailable ? Math.round((ctx?.usage_ratio ?? 0) * 100) : null;
  const ctxClass = !ctxAvailable ? " dim" : ctxPct != null && ctxPct >= 90 ? " danger" : ctxPct != null && ctxPct >= 70 ? " warn" : "";
  const ctxRows: Array<[string, string]> = [];
  if (!ctx || ctx.available === false) {
    ctxRows.push(["状态", "会话未加载或没有活动上下文"]);
    ctxRows.push(["模型", ctx?.model || str(session.model) || "—"]);
  } else {
    ctxRows.push(["模型", ctx.model || str(session.model) || "—"]);
    ctxRows.push(["消息数", String(ctx.total_messages ?? 0)]);
    ctxRows.push(["已用", `${fmtWorkspaceTokens(Number(ctx.total_tokens) || 0)} tokens`]);
    ctxRows.push(["占用", `${ctxPct ?? 0}%`]);
    if (Number(ctx.model_context_length) > 0) ctxRows.push(["模型上下文", `${fmtWorkspaceTokens(Number(ctx.model_context_length))} tokens`]);
    if (Number(ctx.max_tokens) > 0) {
      ctxRows.push(["历史预算", `${fmtWorkspaceTokens(Number(ctx.max_tokens))} tokens`]);
      ctxRows.push(["剩余", `${fmtWorkspaceTokens(Number(ctx.remaining_tokens ?? 0))} tokens`]);
    }
    if (Number(ctx.anchored_tokens) > 0) ctxRows.push(["锚定", `${fmtWorkspaceTokens(Number(ctx.anchored_tokens))} tokens`]);
  }

  return <section className={`ws-chat-shell${showTools ? "" : " hide-tools"}`}>
    <header className="ws-chat-toolbar">
      <div className="ws-chat-context">
        <div className="ws-chat-context-title">{sessionName}</div>
        <div className="ws-chat-context-sub">{`${workspaceName} · ${agentName}`}</div>
      </div>
      <div className="ws-chat-config-row">
        <span className="ws-chat-label">模型</span>
        <select aria-label="模型" value={model} onChange={(event) => void onSwitch({ model: event.target.value }, "模型")}>
          {models.length === 0 ? <option value="">继承默认</option> : null}
          {models.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
        <select aria-label="推理等级" value={reasoning} onChange={(event) => void onSwitch({ reasoning_level: event.target.value }, "思考模式")}>
          {REASONING_LEVELS.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
        </select>
        <span className="ws-chat-label">权限</span>
        <div className="segmented ws-chat-segmented" role="group" aria-label="权限档位">
          {PERMISSION_MODES.map(([value, text]) => (
            <button key={value} type="button" className={`seg-btn${permission === value ? " on" : ""}`} onClick={() => void onSwitch({ permission_mode: value }, "权限")}>{text}</button>
          ))}
        </div>
        <label className="chk ws-chat-tools">
          <input type="checkbox" checked={showTools} onChange={(event) => setShowTools(event.target.checked)} />
          {" 工具过程"}
        </label>
      </div>
      <div className="ws-chat-actions">
        <span className={`ws-chat-status${busy ? " busy" : ""}`}>{busy ? "● 运行中" : "● 空闲"}</span>
        <button type="button" className="btn" onClick={() => void onStop()}>停止</button>
        <button type="button" className="btn" onClick={() => void onClear()}>清空</button>
        <button type="button" className="btn danger" onClick={() => void onDelete()}>删除</button>
      </div>
    </header>
    {chat.error ? <div className="error-box" role="alert">{chat.error}</div> : null}
    {chat.loading ? <div className="ws-empty">加载会话历史…</div> : null}
    {chat.loadingOlder ? <div className="ws-empty">加载更早历史…</div> : null}
    {!chat.loading && displayed.length === 0 ? (
      <div className="ws-chat-empty-holder">
        <div className="ws-chat-empty">
          <div className="ws-chat-empty-icon">✦</div>
          <div className="ws-chat-empty-title">从项目上下文开始</div>
          <div className="ws-chat-empty-desc">{`${workspaceName} · ${sessionName}`}</div>
          <div className="ws-chat-empty-tips">可以让智能体阅读代码、定位问题、规划方案或执行已授权的工具。</div>
        </div>
      </div>
    ) : null}
    <ApprovalHost approvals={approvals.state.items} submittingId={approvals.state.submittingId} error={approvals.state.error} onAnswer={(approval, answer) => void approvals.answer(approval, answer)} />
    <VirtualMessageList className="ws-chat-timeline" items={displayed} autoFollow={busy} onNearTop={() => void chat.loadOlder()} renderItem={(item) => <TimelineRow item={item} sessionKey={sessionKey} convId={chat.convId} />} />
    <RuntimeFloat
      plan={runtime.plan}
      goal={runtime.goal}
      onPlanAction={(action, plan) => void runtime.action("plan", action, plan.plan_id)}
      onGoalAction={(action, goal) => void runtime.action("goal", action, goal.goal_id)}
    />
    {/* 运行中队列等待窗口（设计方案 8，与主会话共用）：运行中发送的消息入队并在此展示，
        Turn 终态后倒计时自动分派队首；"插入"= 统一立即发送（运行中 Steering 注入 /
        空闲立即分派）。活动项为空时自隐藏。 */}
    <QueuePanel
      queue={chat.queueDispatch.queue}
      countdown={chat.queueDispatch.countdown}
      onInject={(queueItemId) => void chat.queueDispatch.injectQueueItem(queueItemId)}
      pausedReason={chat.queueDispatch.pausedReason}
    />
    <ChatComposer
      commands={wsCommands}
      busy={busy}
      ariaLabel="工作区消息"
      steeringAvailable={chat.queueDispatch.steeringAvailable}
      onSteering={() => void chat.queueDispatch.insertSteeringHint()}
      onSend={async (text, files) => {
        // 图片随队列信封发送（修正版方案 A）；非图片附件仍拒绝
        const images = (files ?? [])
          .filter((f) => f.media_type?.startsWith("image/"))
          .map((f) => ({ data: f.data, media_type: f.media_type }));
        if (images.length !== (files?.length ?? 0)) {
          toast("附件仅支持图片（png/jpeg/webp/gif）", "err");
          throw new Error("附件仅支持图片");
        }
        await chat.send(text, images.length ? images : undefined);
      }}
      onStop={onStop}
      contextSlot={(
        <div className={`ctx-meter${ctxClass}`}>
          <span className="ctx-icon">📊</span>
          <span className="ctx-pct">{ctxPct != null ? `${ctxPct}%` : "–"}</span>
          <div className="ctx-tip">
            <div className="ctx-tip-title">上下文占用</div>
            {ctxRows.map(([key, value]) => <div key={key} className="ctx-tip-row"><span>{key}</span><b>{value}</b></div>)}
            {Number(ctx?.model_context_length) > 0 && Number(ctx?.max_tokens) > 0 ? (
              <div className="ctx-tip-foot">历史预算 = 模型上下文 − 输出预留，达到阈值自动压缩</div>
            ) : null}
          </div>
        </div>
      )}
    />
    <QuestionHost queue={questions} />
  </section>;
}

export function WorkspacePage({ client = defaultApi }: { client?: ApiClient }) {
  const controller = useWorkspaceController({ client });
  const { state } = controller;
  const [expandedWorkspaceId, setExpandedWorkspaceId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [sessionCreateOpen, setSessionCreateOpen] = useState(false);

  const selected = state.workspaces.find((item) => item.workspace_id === state.selectedWorkspaceId);
  const selectedSession = state.sessions.find((session) => session.session_id === state.selectedSessionId);
  const parentPath = state.directoryPath.split("/").slice(0, -1).join("/");

  const agentName = (profileId?: string) => {
    if (!profileId) return "未配置智能体";
    const found = state.agents.find((agent) => agent.profile_id === profileId);
    return found ? found.name : profileId;
  };

  const modelOptions = useMemo(() => {
    const list = (state.catalogs.models ?? [])
      .map((m) => typeof m === "string" ? m : (m?.id ?? ""))
      .filter(Boolean);
    const current = str(selectedSession?.model);
    if (current && !list.includes(current)) list.unshift(current);
    return list;
  }, [selectedSession?.model, state.catalogs.models]);

  // Restore a shared deep link (#/workspace?id=…) once the list has loaded.
  useEffect(() => {
    const id = parseQueryId();
    if (!id || state.selectedWorkspaceId === id) return;
    if (state.workspaces.some((workspace) => workspace.workspace_id === id)) {
      void controller.selectWorkspace(id);
    }
  }, [controller, state.selectedWorkspaceId, state.workspaces]);

  const toggleWorkspace = (workspace: Workspace) => {
    if (state.selectedWorkspaceId !== workspace.workspace_id) {
      window.location.hash = `#/workspace?id=${encodeURIComponent(workspace.workspace_id)}`;
      setExpandedWorkspaceId(workspace.workspace_id);
      void controller.selectWorkspace(workspace.workspace_id);
    } else {
      setExpandedWorkspaceId(expandedWorkspaceId === workspace.workspace_id ? null : workspace.workspace_id);
    }
  };

  const removeWorkspace = async (workspace: Workspace) => {
    const ok = await confirmDialog(
      `删除工作区「${workspace.name || workspace.workspace_id}」？\n\n该操作将彻底删除工作区及其所有会话、长期记忆，不可恢复；项目文件不会被删除。`,
      { okText: "彻底删除" },
    );
    if (!ok) return;
    try {
      await controller.deleteWorkspace(workspace.workspace_id);
      if (expandedWorkspaceId === workspace.workspace_id) setExpandedWorkspaceId(null);
      toast("工作区已删除", "ok");
    } catch (error) {
      toast(`删除工作区失败: ${error instanceof Error ? error.message : "未知错误"}`, "err");
    }
  };

  const removeSession = async (session: WorkspaceSession) => {
    const ok = await confirmDialog(`删除会话「${session.name || session.session_id}」？\n\n会话历史将被移出活动列表。`, { okText: "删除" });
    if (!ok) return;
    try {
      await controller.deleteSession(session);
      toast("会话已删除", "ok");
    } catch (error) {
      toast(`删除会话失败: ${error instanceof Error ? error.message : "未知错误"}`, "err");
    }
  };

  const handleSwitch = async (patch: WorkspaceSessionSwitchPatch, label: string) => {
    if (!selected || !state.selectedSessionId) return;
    try {
      await controller.switchSession(selected.workspace_id, state.selectedSessionId, patch);
      toast(`${label}已更新，下条消息生效`, "ok");
    } catch (error) {
      const message = error instanceof Error ? error.message : "";
      toast(`切换失败${message ? `: ${message}` : "（会话可能正在运行）"}`, "err");
    }
  };

  const stopChat = async () => {
    if (!selected || !state.selectedSessionId) return;
    try {
      await controller.stopChat(selected.workspace_id, state.selectedSessionId);
      toast("已请求停止", "ok");
    } catch { /* silent */ }
  };

  const clearChat = async () => {
    const ok = await confirmDialog("清空该会话的消息历史？", { okText: "清空" });
    if (!ok || !selected || !state.selectedSessionId) return;
    try {
      await controller.clearChat(selected.workspace_id, state.selectedSessionId);
      toast("已清空", "ok");
    } catch { /* silent */ }
  };

  const deleteChat = async () => {
    const ok = await confirmDialog("删除该会话？历史与运行资源将被释放，不会删除项目文件。", { okText: "删除" });
    if (!ok || !selectedSession) return;
    try {
      await controller.deleteSession(selectedSession);
      toast("会话已删除", "ok");
    } catch (error) {
      toast(`删除失败: ${error instanceof Error ? error.message : "未知错误"}`, "err");
    }
  };

  const shownWorkspaces = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return state.workspaces;
    return state.workspaces.filter((w) =>
      (w.name ?? "").toLowerCase().includes(q) || (w.project_path ?? "").toLowerCase().includes(q));
  }, [search, state.workspaces]);

  const activeExpanded = state.selectedWorkspaceId !== undefined && expandedWorkspaceId === state.selectedWorkspaceId;

  return <section className="ws-page workspace-page" aria-label="工作区页面">
    <div className="page-head">
      <div>
        <h1>▣ 工作区</h1>
        <div className="dim">按项目管理工作区、会话与文件</div>
      </div>
      <button type="button" className="btn primary" onClick={() => setWizardOpen(true)}>＋ 新建工作区</button>
      <button type="button" className="btn" onClick={() => void controller.refresh()}>刷新</button>
    </div>
    {state.error ? <div className="error-box" role="alert">{state.error}</div> : null}
    <div className={`ws-layout${rightCollapsed ? " ws-no-right" : ""}`}>
      <aside className="ws-panel ws-left">
        <div className="ws-panel-head"><span>工作区</span><button type="button" className="btn" onClick={() => setWizardOpen(true)}>＋ 新建</button></div>
        <div className="ws-panel-body">
          <input type="text" className="ws-nav-search" placeholder="搜索项目…" value={search} onChange={(event) => setSearch(event.target.value)} />
          {state.loading ? <div className="ws-empty">加载中…</div> : null}
          {!state.loading && shownWorkspaces.length === 0 ? <div className="ws-empty">{search ? "无匹配工作区" : "暂无工作区"}</div> : null}
          {shownWorkspaces.map((workspace) => {
            const active = workspace.workspace_id === state.selectedWorkspaceId;
            const expanded = active && activeExpanded;
            return (
              <div key={workspace.workspace_id}>
                <div
                  className={`ws-item ws-workspace-item${active ? " active" : ""}`}
                  title={`${workspace.name || "工作区"}\n${workspace.project_path || ""}`}
                  onClick={() => toggleWorkspace(workspace)}
                >
                  <div className="ws-item-title">
                    <button
                      type="button"
                      className="ws-tree-toggle"
                      title={expanded ? "折叠会话" : "展开会话"}
                      onClick={(event) => { event.stopPropagation(); toggleWorkspace(workspace); }}
                    >{expanded ? "⌄" : "›"}</button>
                    <span className="ws-nav-name">{workspace.name || "未命名工作区"}</span>
                    <span className="ws-session-count">{state.sessionCounts[workspace.workspace_id] ?? 0}</span>
                    <button
                      type="button"
                      className="ws-nav-action danger"
                      title="删除工作区"
                      onClick={(event) => { event.stopPropagation(); void removeWorkspace(workspace); }}
                    >×</button>
                  </div>
                  <div className="ws-item-sub ws-project-path">{workspace.project_path || "（未配置目录）"}</div>
                </div>
                {expanded ? (
                  <div className="ws-session-tree">
                    <button type="button" className="ws-session-new" onClick={() => setSessionCreateOpen(true)}>＋ 新会话</button>
                    {state.sessions.length === 0 ? <div className="ws-empty">暂无会话</div> : null}
                    {state.sessions.map((session) => (
                      <div
                        key={session.session_id}
                        className={`ws-item ws-session-item${state.selectedSessionId === session.session_id ? " active" : ""}`}
                        title={`${session.name || session.session_id}\n${agentName(session.agent_profile_id)}`}
                        onClick={() => controller.selectSession(session.session_id)}
                      >
                        <div className="ws-item-title">
                          <span className="ws-nav-name">{session.name ?? session.session_id}</span>
                          <button
                            type="button"
                            className="ws-nav-action danger"
                            title="删除会话"
                            onClick={(event) => { event.stopPropagation(); void removeSession(session); }}
                          >×</button>
                        </div>
                        <div className="ws-item-sub">{agentName(session.agent_profile_id)}</div>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </aside>
      <main className="ws-panel">
        <div className="ws-panel-head">
          <span>{selectedSession ? `会话：${selectedSession.name ?? selectedSession.session_id}` : (selected?.name ?? "工作区详情")}</span>
        </div>
        {selected && state.selectedSessionId ? (
          <div className="ws-panel-body ws-chat-body">
            <WorkspaceChat
              workspaceId={selected.workspace_id}
              sessionId={state.selectedSessionId}
              workspaceName={selected.name}
              sessionName={selectedSession?.name ?? state.selectedSessionId}
              agentName={agentName(selectedSession?.agent_profile_id)}
              session={selectedSession ?? ({ session_id: state.selectedSessionId, workspace_id: selected.workspace_id, session_key: "" } as WorkspaceSession)}
              models={modelOptions}
              client={client}
              onSwitch={handleSwitch}
              onStop={stopChat}
              onClear={clearChat}
              onDelete={deleteChat}
            />
          </div>
        ) : selected ? (
          <div className="ws-panel-body ws-detail-body">
            <div className="ws-session-config">
              <div className="ws-section-head">
                <div className="ws-section-title">工作区详情</div>
                <div className="ws-section-desc">项目元数据与会话入口；选择左侧会话进入时间线</div>
              </div>
              <div className="ws-field"><label>项目目录</label><div className="ws-detail-value">{selected.project_path}</div></div>
              <div className="ws-field"><label>描述</label><div className="ws-detail-value">{str(selected.description, "（未填写）")}</div></div>
              <hr className="ws-divider" />
              <div className="ws-session-list-head">
                <span>会话（{state.sessions.length}）</span>
                <button type="button" className="btn" onClick={() => setSessionCreateOpen(true)}>＋ 新会话</button>
              </div>
              {state.sessions.length === 0 ? <div className="ws-empty">暂无会话，点击「＋ 新会话」开始</div> : null}
              {state.sessions.map((session) => (
                <div key={session.session_id} className="ws-item" onClick={() => controller.selectSession(session.session_id)}>
                  <div className="ws-item-title">{session.name ?? session.session_id}</div>
                  <div className="ws-item-sub">{`${agentName(session.agent_profile_id)} · ${str(session.model, "继承默认")}`}</div>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="ws-panel-body">
            <div className="ws-hero-empty">
              <div className="ws-hero-icon">▣</div>
              <div className="ws-hero-title">还没有创建工作区</div>
              <div className="ws-hero-desc">把本地项目与智能体、模型、工具和权限绑定，在项目上下文中完成分析、开发和规划任务。</div>
              <button type="button" className="btn primary" onClick={() => setWizardOpen(true)}>创建第一个工作区</button>
            </div>
          </div>
        )}
      </main>
      <aside className={`ws-panel ws-right${rightCollapsed ? " collapsed" : ""}`}>
        <div className="ws-panel-head">
          <span>项目信息</span>
          <button
            type="button"
            className="ws-right-toggle"
            title="隐藏项目信息"
            onClick={() => setRightCollapsed(true)}
          ><span>»</span></button>
        </div>
        <div className="ws-panel-body">
          {!selected ? <div className="ws-empty">选择工作区查看配置</div> : (
            <>
              <div className="ws-field"><label>项目名称</label><div className="ws-detail-value">{selected.name}</div></div>
              <div className="ws-field"><label>项目目录</label><div className="ws-detail-value">{selected.project_path}</div></div>
              <div className="ws-field"><label>描述</label><div className="ws-detail-value">{str(selected.description, "（未填写）")}</div></div>
              <div className="ws-field"><label>会话</label><div className="ws-detail-value">{`${state.sessions.length} 个`}</div></div>
              {strList(selected.path_warnings).length ? (
                <div className="ws-field">
                  {strList(selected.path_warnings).map((warn, index) => (
                    <div key={index} className="ws-warn">{`⚠️ ${warn}`}</div>
                  ))}
                </div>
              ) : null}
              <hr className="ws-divider" />
              <div className="ws-session-list-head"><span>项目文件</span></div>
              <div className="ws-file-breadcrumb">
                <button type="button" className="btn" disabled={!state.directoryPath} onClick={() => void controller.loadDirectory(selected.workspace_id, parentPath)}>上一级</button>
                <span className="ws-file-path">{state.directoryPath || "/"}</span>
                <button type="button" className="btn" onClick={() => void controller.loadDirectory(selected.workspace_id, state.directoryPath)}>刷新目录</button>
              </div>
              {state.fileError ? <div role="alert" className="error-box">{state.fileError}</div> : null}
              <WorkspaceFileList entries={state.files} onEnter={(path) => void controller.loadDirectory(selected.workspace_id, path)} onOpen={(path) => void controller.openFile(selected.workspace_id, path)} />
              {state.fileLoading ? <div className="ws-empty">读取文件…</div> : null}
              {state.openFile ? <WorkspaceFileViewer file={state.openFile} /> : null}
            </>
          )}
        </div>
      </aside>
      {rightCollapsed ? (
        <button
          type="button"
          className="ws-right-show"
          style={{ display: "block" }}
          title="显示项目信息"
          onClick={() => setRightCollapsed(false)}
        ><span>«</span></button>
      ) : null}
    </div>
    {wizardOpen ? (
      <CreateWorkspaceWizard
        client={client}
        agents={state.agents}
        catalogs={state.catalogs}
        onCreated={(body) => controller.createWorkspace(body)}
        onClose={() => setWizardOpen(false)}
      />
    ) : null}
    {sessionCreateOpen && selected ? (
      <CreateSessionModal
        agents={state.agents}
        catalogs={state.catalogs}
        defaultAgentId={state.agents[0]?.profile_id ?? ""}
        defaultProjectDir={selected.project_path ?? ""}
        onCreate={(draft) => controller.createSession(selected.workspace_id, draft)}
        onClose={() => setSessionCreateOpen(false)}
      />
    ) : null}
  </section>;
}
