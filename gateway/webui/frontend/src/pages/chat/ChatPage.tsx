import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApprovalHost } from "@/approvals/ApprovalHost";
import { useApprovalQueue } from "@/approvals/useApprovalQueue";
import { api } from "@/api/client";
import type { SessionsResponse } from "@/api/types";
import { ChatComposer } from "@/components/ChatComposer";
import { GoalBar } from "@/components/GoalBar";
import { RuntimeFloat } from "@/components/RuntimeFloat";
import { useRuntimeFloat } from "@/components/useRuntimeFloat";
import { toast } from "@/components/toast";
import { conversationApi } from "@/gateway/api";
import { gatewayStore, selectLiveTurn, selectQueue, selectTurnNodes, selectTurnWithNodes, useGatewaySelector } from "@/gateway/store";
import { chatDerivedCache } from "@/pages/chat/byteLru";
import { useConversation } from "@/gateway/useConversation";
import { QueuePanel } from "@/pages/chat/QueuePanel";
import { useQueueDispatch } from "@/pages/chat/useQueueDispatch";
import type { Turn, TurnNode } from "@/gateway/types";
import { historyToTimeline, mergeTerminalTurn, turnToTimeline } from "@/pages/chat/chatTimeline";
import type { TimelineItem } from "@/pages/chat/timeline";
import { TimelineRow } from "@/pages/chat/timelineRow";
import { TurnStatus } from "@/pages/chat/TurnStatus";
import { VirtualMessageList } from "@/pages/chat/VirtualMessageList";
import { QuestionHost } from "@/questions/QuestionHost";
import { useQuestionQueue } from "@/questions/useQuestionQueue";
import { useSse } from "@/hooks/useSse";
import type { ParsedSseEvent } from "@/sse/events";

const REASONING_LEVELS: Array<[string, string]> = [
  ["inherit", "推理：继承模型"],
  ["provider_default", "推理：服务商默认"],
  ["none", "推理：关闭"],
  ["minimal", "推理：极低"],
  ["low", "推理：低"],
  ["medium", "推理：中"],
  ["high", "推理：高"],
  ["xhigh", "推理：极高"],
  ["max", "推理：最大"],
];

const PERMISSION_MODES: Array<[string, string]> = [
  ["readonly", "只读"],
  ["ask", "询问"],
  ["allow", "允许"],
  ["unreviewed", "免审"],
];

const TERMINAL = new Set(["done", "stopped", "error", "interrupted"]);
const HISTORY_PAGE_SIZE = 20;
// L5：上下文轮询周期 —— 忙时 5s 快轮询，空闲 30s 兜底（事件驱动为主，轮询为辅）
const CTX_POLL_BUSY_MS = 5000;
const CTX_POLL_IDLE_MS = 30_000;

interface ChatCommand {
  name: string;
  args?: string;
  help?: string;
  insert_text?: string;
  client_hint?: string;
}

interface CtxData {
  available?: boolean;
  usage_ratio?: number;
  total_messages?: number;
  total_tokens?: number;
  model?: string;
  model_context_length?: number;
  max_tokens?: number;
  remaining_tokens?: number;
}

export function ChatPage() {
  const [sessionKey, setSessionKey] = useState("webui:default");
  const [sessions, setSessions] = useState<string[]>([sessionKey]);
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const modelForSessionRef = useRef<string | null>(null);
  const [reasoning, setReasoning] = useState("inherit");
  const [permission, setPermission] = useState("ask");
  const [commands, setCommands] = useState<ChatCommand[]>([]);
  const [ctx, setCtx] = useState<CtxData | null>(null);

  // ---- 新数据源：Gateway Store + 快照优先恢复（设计方案 19.2）----
  const [convId, setConvId] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<{ turn: Turn; nodes: TurnNode[] }>>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const historyLoadingRef = useRef(false);
  // P1 会话切换分页竞态：历史请求代次（convId/sessionKey 变化即自增），
  // await 返回后代次不符的响应直接丢弃，避免旧会话数据串台到新会话。
  const historyGenRef = useRef(0);
  // 被防重入挡掉的新会话首屏加载：旧请求结束后补一次触发信号。
  const [historyRetryTick, setHistoryRetryTick] = useState(0);

  const { handleEvent, scope } = useConversation({ sessionKey, conversationId: convId ?? undefined, enabled: convId != null });

  // 历史分页（完整 Turn 为单位）——提前定义，onSseEvent 依赖 loadLatest
  const loadHistory = useCallback(async (cursor?: string | null) => {
    if (!convId || historyLoadingRef.current) return;
    const gen = historyGenRef.current;
    historyLoadingRef.current = true;
    if (cursor) setLoadingOlder(true); else setLoading(true);
    try {
      const result = await conversationApi.history(convId, { cursor: cursor ?? undefined, limit: HISTORY_PAGE_SIZE });
      // 代次复核：await 期间已切换会话 → 响应属于旧会话，丢弃（自愈见 finally）
      if (gen === historyGenRef.current && result.ok && result.data) {
        const page = result.data.items as Array<{ turn: Turn; nodes: TurnNode[] }>;
        setHistory((prev) => (cursor ? [...page, ...prev] : page));
        setHistoryCursor(result.data.next_cursor ?? null);
      }
    } finally {
      historyLoadingRef.current = false;
      if (gen === historyGenRef.current) {
        setLoading(false);
        setLoadingOlder(false);
      } else {
        // 代次已变：新会话的首屏加载可能已被防重入挡掉，这里补发触发信号
        // （loadHistory effect 依赖该 tick），保证新会话历史一定会重新加载。
        setHistoryRetryTick((tick) => tick + 1);
      }
    }
  }, [convId]);
  const loadOlder = useCallback(() => { void loadHistory(historyCursor); }, [loadHistory, historyCursor]);
  const loadLatest = useCallback(() => { void loadHistory(); }, [loadHistory]);

  // 上下文占用（/context）：useCallback 化供发送/事件驱动即时刷新复用（L5）
  const loadContext = useCallback(async () => {
    try {
      const data = await api.get<CtxData>(`/api/sessions/${encodeURIComponent(sessionKey)}/context`, { silent: true });
      setCtx(data);
    } catch { setCtx(null); }
  }, [sessionKey]);

  const clearSession = useCallback(async () => {
    // 统一模型清空（设计方案 §30）：/clear 与"清空聊天"同一入口。
    // 事件 conversation.cleared 会让 store 清掉本地缓存；这里再兜底刷新历史。
    if (convId) {
      try {
        await conversationApi.clear(convId);
      } catch { /* silent */ }
    }
    try {
      await api.post(`/api/sessions/${encodeURIComponent(sessionKey)}/clear`);
    } catch { /* silent */ }
    toast("聊天已清空", "ok");
    void loadLatest();
    // 清空后立即刷新上下文占用面板（Agent 内存上下文已联动清空，消息数/token 应归零附近）。
    void loadContext();
  }, [convId, sessionKey, loadLatest, loadContext]);

  // 旧浮层（审批/问题/运行时）继续监听事件
  const questions = useQuestionQueue(api, { sessionKey });
  const approvals = useApprovalQueue(api, { sessionKey });
  const runtime = useRuntimeFloat(api, { sessionKey });

  const onSseEvent = useCallback((event: ParsedSseEvent) => {
    const type = (event as { type?: string }).type;
    const eventData = event.data as { conversation_id?: unknown; session_key?: unknown };
    // SSE 已按 sessionKey 订阅（useConversation 返回的 scope）；这里再做归属双保险：
    // 不属于当前会话的 node.delta / conversation.cleared 直接忽略，避免污染全局 store。
    if (type === "node.delta" || type === "conversation.cleared") {
      if (eventData?.conversation_id != null && eventData.conversation_id !== convId) return;
      if (eventData?.session_key !== undefined && eventData.session_key !== sessionKey) return;
    }
    handleEvent(event);
    // /clear 或"清空聊天"：统一模型已清空，本地 store 已清理，
    // 兜底刷新历史分页（设计方案 §30）。
    if (type === "conversation.cleared") {
      void loadLatest();
    }
    // L5 事件化：turn 开始/终态即时刷新上下文占用，替代固定 5s 轮询的等待
    if ((type === "chat.started" || type === "chat.done")
      && (eventData?.session_key === undefined || eventData.session_key === sessionKey)) {
      void loadContext();
    }
    questions.onSse(event);
    approvals.onSse(event);
    runtime.onSse(event);
  }, [handleEvent, questions, approvals, runtime, loadLatest, sessionKey, convId, loadContext]);

  useSse(scope, onSseEvent);

  // 会话 conversation 幂等取得
  useEffect(() => {
    let active = true;
    // 同步失效旧 convId（P0 修复）：create 往返窗口内 convId 仍指向旧会话，
    // selectLiveTurn(旧convId) 会把旧会话的 live turn 渲染进新会话视图
    //（双会话同时运行时切换，内容输出错乱的直接根源）。置 null → liveTurn
    // 立即消失，加载态由 loading 遮罩，杜绝跨会话内容串染。
    setConvId(null);
    setLoading(true);
    void conversationApi.create(sessionKey).then((result) => {
      if (!active) return;
      if (result.ok && result.data) setConvId(result.data.conversation.conversation_id);
      setLoading(false);
    });
    return () => { active = false; };
  }, [sessionKey]);

  const liveTurn = useGatewaySelector(selectLiveTurn(convId ?? ""));
  const liveNodes = useGatewaySelector(selectTurnNodes(liveTurn?.turn_id ?? ""));
  const busy = liveTurn != null && !TERMINAL.has(liveTurn.status);
  // 运行中队列分派（设计方案 8.3/8.4/9）：队列订阅、终态倒计时自动分派、
  // Steering 注入 —— 与工作区会话共用同一 hook。
  const queueDispatch = useQueueDispatch(convId);

  // P1 竞态修复：sessionKey/convId 变化即推进历史代次并清空上一会话的分页数据，
  // 在途旧请求的响应因代次不符被丢弃；声明顺序保证先重置、后触发加载。
  useEffect(() => {
    historyGenRef.current += 1;
    setHistory([]);
    setHistoryCursor(undefined);
    setLoading(true);
  }, [convId, sessionKey]);

  useEffect(() => { void loadHistory(); }, [loadHistory, historyRetryTick]);

  // 切换 convId（换会话/删除重建）时驱逐上一会话的派生缓存（展开的大结果等），
  // 避免跨会话残留（LRU 只是容量兜底，这里主动清理）。
  const lastConvRef = useRef<{ sessionKey: string; convId: string } | null>(null);
  useEffect(() => {
    const prev = lastConvRef.current;
    if (convId) lastConvRef.current = { sessionKey, convId };
    if (prev && convId && prev.convId !== convId) {
      chatDerivedCache.deletePrefix(`${prev.sessionKey}:`);
    }
  }, [convId, sessionKey]);

  // Turn 终态后：live 节点并入历史显示。L5：优先复用本地 store 权威数据
  //（chat.done 已写入全量 text，无需整页重拉历史），直接并入历史分页；
  // 仅当本地缺失该 turn（快照/事件竞态）时才回退 loadHistory 整页重拉。
  // 同时记录终态类型，供 8.4 倒计时分派（error/interrupted/done 空回复 → 暂停）。
  // 收官修复：后端 complete_turn 先发 turn.status(done)、后发 chat.done（权威
  // full_text）。若在 turn.status 到达时立即用节点快照并史，chat.done 的权威
  // 文本不会反映进历史分页（组件不再重渲染，最终回复冻结在流式旧文本，刷新
  // 才恢复）。因此并入改为订阅 selectTurnWithNodes：初始并入 + 权威数据到达后
  // 原位重合并（turn/node 引用变化才触发，无关事件不重渲染，无性能回退）。
  // （终态类型/空回复记录已移入共用 useQueueDispatch，供 8.4 倒计时分派。）
  const prevLiveTurnRef = useRef<Turn | null | undefined>(undefined);
  const [terminalTurnId, setTerminalTurnId] = useState<string | null>(null);
  const terminalData = useGatewaySelector(selectTurnWithNodes(terminalTurnId ?? ""));

  // 切换会话：清空上一会话的终态订阅，避免跨会话合并污染新会话历史。
  useEffect(() => {
    setTerminalTurnId(null);
  }, [convId]);

  useEffect(() => {
    const prev = prevLiveTurnRef.current;
    prevLiveTurnRef.current = liveTurn;
    if (prev && !liveTurn) {
      // 会话切换残留的旧 live turn（convId 已变）：不并入新会话历史。
      if (prev.conversation_id !== convId) return;
      const state = gatewayStore.getState();
      const turn = state.turnsById[prev.turn_id];
      if (turn) {
        // 零闪断：live→终态切换瞬间立即以当前 store 快照并入历史，避免
        // displayed 在"live 已清空、历史未并入"的窗口期丢失整个 turn 的
        // 内容（最终输出闪现即消失的观感）。chat.done 权威文本晚到后由
        // terminalData 订阅原位重合并（turn/node 引用变化才触发）。
        setHistory((h) => mergeTerminalTurn(
          h, { turn, nodes: selectTurnNodes(prev.turn_id)(gatewayStore.getState()) }));
        setTerminalTurnId(prev.turn_id);
      } else {
        void loadHistory();
      }
    }
  }, [liveTurn, loadHistory, convId]);

  // 终态 turn 与 store 权威数据同步：turn/node 引用变化（chat.done full_text
  // 覆盖流式文本）→ 原位重合并，保证最终回复立即切换为权威完整渲染。
  useEffect(() => {
    if (!terminalData || terminalData.turn.conversation_id !== convId) return;
    setHistory((prevHistory) => mergeTerminalTurn(prevHistory, terminalData));
  }, [terminalData, convId]);

  // 时间线：历史（正序）+ 实时节点（节点化流式）。plan/goal 轮次已由后端写入
  // 父会话并带 runtime 标记，turnToTimeline 据此折叠成内联卡片（不再读投影表）。
  // 时间线拆分 memo：history 部分只在历史分页变化时重算；live delta 只拼增量，
  // 避免每次 node.delta 都重跑 historyToTimeline（设计方案 20.3）。
  // 时间线最终防线（多会话切换串扰修复）：history 先按 conversation_id 过滤 +
  // turn_id 去重，live turn 校验归属后才并入——上游任何竞态漏网的跨会话条目
  // 或重复轮次都被拦在渲染前。
  const historyTimeline = useMemo<TimelineItem[]>(() => {
    const seen = new Set<string>();
    const scoped = history.filter(({ turn }) => {
      if (turn.conversation_id !== convId) return false;
      if (seen.has(turn.turn_id)) return false;
      seen.add(turn.turn_id);
      return true;
    });
    return historyToTimeline(scoped);
  }, [history, convId]);
  const liveTimeline = useMemo<TimelineItem[]>(
    () => {
      if (!liveTurn || liveTurn.conversation_id !== convId) return [];
      const base = turnToTimeline(liveTurn, liveNodes, { live: true });
      // TTFT 占位（审计问题 1）：乐观回显的 user 节点不算实质输出——首个
      // reasoning/assistant/tool 节点到达前在 live 渲染（user 气泡）之后追加
      // "正在思考…"占位行；实质节点到达后原子替换为完整 live 渲染。
      const hasSubstantive = liveNodes.some((n) => n.type !== "user" && n.type !== "user_steering");
      if (!hasSubstantive) {
        const parsed = Date.parse(liveTurn.started_at || "");
        base.push({
          key: "thinking:" + liveTurn.turn_id,
          kind: "thinking",
          startedAt: Number.isFinite(parsed) ? parsed : Date.now(),
        });
      }
      return base;
    },
    [liveTurn, liveNodes, convId],
  );
  const displayed = useMemo<TimelineItem[]>(
    () => (liveTimeline.length ? [...historyTimeline, ...liveTimeline] : historyTimeline),
    [historyTimeline, liveTimeline],
  );

  // ---- 发送 / 队列（设计方案 8.3：空闲立即发送，运行中入队） ----
  // 前端拦截中文清空意图（整句匹配），避免"清空会话"被当作普通任务发给 LLM
  const CLEAR_INTENTS = new Set(["/clear", "清空会话", "清空聊天", "清除会话", "清空历史", "清空对话"]);
  const send = useCallback(async (text: string, images?: Array<{ data: string; media_type?: string }>) => {
    const trimmed = text.trim();
    if (!convId || !trimmed) return;
    if (CLEAR_INTENTS.has(trimmed)) {
      await clearSession();
      return;
    }
    const result = await conversationApi.enqueue(convId, text, { images });
    // 需求：输入新消息后，旧的终态 plan/goal 状态框应消失（运行中的保留）。
    if (result.ok) runtime.dismissStale();
    // 入队可见确认（审计问题 2）：忙碌时消息只入队不立即执行——POST 成功但
    // 时间线/队列面板可能因 SSE 延迟而短暂无变化，用户无从判断是否发出。
    if (result.ok && busy) {
      const waiting = selectQueue(convId)(gatewayStore.getState())
        .filter((q) => q.status === "waiting").length;
      toast(`已加入队列，当前第 ${waiting} 位，将在当前任务完成后自动发送`, "ok");
    }
    if (result.ok && result.data && !busy) {
      const next = await conversationApi.sendNext(convId);
      // 乐观回显：sendNext 返回 user_node 时立即并入时间线（无需等 SSE）。
      if (next.ok && next.data?.user_node && next.data.turn) {
        gatewayStore.mergeUserNode(next.data.turn, next.data.user_node);
        for (const imgNode of next.data.image_nodes ?? []) {
          gatewayStore.mergeUserNode(next.data.turn, imgNode);
        }
      }
    }
    void loadContext();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [convId, busy, clearSession]);

  const stop = useCallback(async () => {
    if (!convId) return;
    const result = await conversationApi.stop(convId);
    if (result.ok) {
      toast("⏹ 已请求停止，任务将在下一步检查点退出", "ok");
    } else {
      toast("停止请求失败", "err");
    }
  }, [convId]);

  // （队列 Steering 注入 / 队首倒计时自动分派已移入共用 useQueueDispatch。）

  // ---- 保留的旧逻辑：会话/模型/权限/命令/上下文/计时器 ----
  useEffect(() => {
    let active = true;
    void api.get<SessionsResponse>("/api/sessions", { silent: true }).then((data) => {
      if (!active) return;
      const keys = data.sessions.map((session) => session.session_key);
      setSessions(keys.includes(sessionKey) ? keys : [sessionKey, ...keys]);
    }).catch(() => undefined);
    return () => { active = false; };
  }, [sessionKey]);

  useEffect(() => {
    let active = true;
    void api.get<{ commands?: ChatCommand[] }>(`/api/commands?session_key=${encodeURIComponent(sessionKey)}`, { silent: true })
      .then((data) => { if (active) setCommands(data.commands ?? []); })
      .catch(() => setCommands([]));
    return () => { active = false; };
  }, [sessionKey]);

  // L5：上下文轮询降频/事件化 —— 忙时 5s 快轮询，空闲退到 30s；
  // 页面不可见时暂停；chat.started / chat.done / 发送已由事件即时刷新，轮询仅兜底。
  useEffect(() => {
    let timer: number | undefined;
    const schedule = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      if (document.visibilityState === "hidden") { timer = undefined; return; }
      timer = window.setTimeout(() => {
        timer = undefined;
        void loadContext();
        schedule();
      }, busy ? CTX_POLL_BUSY_MS : CTX_POLL_IDLE_MS);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") schedule();
      else if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
    };
    void loadContext();
    schedule();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [sessionKey, busy, loadContext]);

  // （原顶栏 busy 计时器已由列表尾部 TurnStatus 统一承载：锚定 turn 开始、
  // ≥15s 出现计时、结束后展示总用时 5s。）

  useEffect(() => {
    let active = true;
    void api.get<{ models?: Array<string | { name?: string }> }>("/api/config/models", { silent: true }).then((data) => {
      if (!active) return;
      const names = (data.models ?? []).map((m) => typeof m === "string" ? m : (m?.name ?? ""));
      setModels(names);
      // 注意：这里不要 setModel——模型的权威来源是 /context（route_metadata.prefs.model）。
      // 若在此处按可用列表重置，会与下方 restore 竞态，把用户已切换/持久化的模型清空，
      // 导致刷新后顶部模型与"实际使用"不一致。
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);

  // 恢复当前会话持久化的模型（对齐 permission/reasoning：会话级偏好 mount 时回填，
  // 否则刷新后模型下拉回落到默认，用户误以为"切换不生效"）。
  // 注意：必须在切换会话时先清空，再以后端 /context 返回的持久化偏好为准，
  // 避免残留上一会话的模型（后端 /context 已优先读 route_metadata.prefs.model）。
  useEffect(() => {
    let active = true;
    setModel("");
    void api.get<{ model?: string }>(`/api/sessions/${encodeURIComponent(sessionKey)}/context`, { silent: true })
      .then((data) => { if (active) setModel(data.model ? String(data.model) : ""); })
      .catch(() => undefined);
    return () => { active = false; };
  }, [sessionKey]);

  useEffect(() => {
    let active = true;
    void api.get<{ selected?: string; mode?: string }>(`/api/sessions/${encodeURIComponent(sessionKey)}/reasoning`, { silent: true })
      .then((data) => { if (active && data.selected) setReasoning(data.selected); })
      .catch(() => undefined);
    void api.get<{ mode?: string }>(`/api/sessions/${encodeURIComponent(sessionKey)}/permission`, { silent: true })
      .then((data) => { if (active && data.mode) setPermission(data.mode); })
      .catch(() => undefined);
    return () => { active = false; };
  }, [sessionKey]);

  const switchModel = async (next: string) => {
    if (!next) return;
    try {
      const result = await api.post<{ reply?: string }>(`/api/sessions/${encodeURIComponent(sessionKey)}/model`, { model: next });
      modelForSessionRef.current = sessionKey;
      setModel(next);
      // 模型切换反馈只在顶部显示，避免把配置操作重复注入聊天时间线。
      toast(result.reply ?? `已切换模型: ${next}`, "ok");
    } catch { /* silent */ }
  };

  const switchReasoning = async (level: string) => {
    setReasoning(level);
    try {
      const data = await api.post<{ selected?: string; effective?: string }>(`/api/sessions/${encodeURIComponent(sessionKey)}/reasoning`, { level });
      // 用持久化的 selected（刚写入的偏好）而非 live llm.reasoning_level（可能滞更新），
      // 避免切到新等级后 toast 仍显示旧等级。
      toast(`本会话推理等级：${data.selected ?? level}`, "ok");
    } catch { /* silent */ }
  };

  const setPermissionMode = async (mode: string) => {
    setPermission(mode);
    try {
      await api.post(`/api/sessions/${encodeURIComponent(sessionKey)}/permission`, { mode });
      toast(`权限档位：${mode}`, "ok");
    } catch { /* silent */ }
  };

  const deleteSession = async () => {
    if (!window.confirm(`删除会话 ${sessionKey}？`)) return;
    try {
      await api.delete(`/api/sessions/${encodeURIComponent(sessionKey)}`);
      setSessions((prev) => prev.filter((key) => key !== sessionKey));
      // 删除当前会话后立即重载时间线（否则 UI 停留旧数据，观感"删除不生效"）
      setHistory([]);
      setHistoryCursor(undefined);
      setConvId(null);
      // 删除成功后无论是否默认会话都切换到新会话键，强制 create effect 重建会话，
      // 避免删除 webui:default 后 convId 恒为 null 导致页面失活。
      setSessionKey(sessionKey === "webui:default" ? `webui:${Date.now().toString(36)}` : "webui:default");
      toast("会话已删除", "ok");
    } catch {
      toast("删除会话失败，请重试", "err");
    }
  };

  const newSession = () => {
    setSessionKey(`webui:${Date.now().toString(36)}`);
  };

  const ctxPct = ctx?.usage_ratio != null ? Math.round(ctx.usage_ratio * 100) : null;
  const ctxClass = ctxPct != null ? (ctxPct >= 90 ? " ctx-hot" : ctxPct >= 70 ? " ctx-warm" : "") : "";
  const ctxRows: Array<[string, string]> = [];
  if (ctx) {
    if (ctx.model) ctxRows.push(["模型", ctx.model]);
    ctxRows.push(["消息数", String(ctx.total_messages ?? 0)]);
    ctxRows.push(["占用", ctx.usage_ratio != null ? `${Math.round(ctx.usage_ratio * 100)}%` : "–"]);
    ctxRows.push(["已用", `${ctx.total_tokens ?? 0} tokens`]);
    ctxRows.push(["剩余", `${ctx.remaining_tokens ?? 0} tokens`]);
    if (ctx.model_context_length) {
      ctxRows.push(["上下文窗口", `${ctx.model_context_length.toLocaleString()} · 预算 ${(ctx.max_tokens ?? 0).toLocaleString()}`]);
    }
  }

  return (
    <section className="chat-page" data-testid="chat-page">
      <header className="chat-topbar">
        <div className="tb-row">
          <span className="tb-label">会话</span>
          <select aria-label="会话" value={sessionKey} onChange={(event) => setSessionKey(event.target.value)}>
            {sessions.map((key) => <option key={key}>{key}</option>)}
          </select>
          <span className="tb-label">模型</span>
          <select aria-label="模型" value={model} onChange={(event) => { const next = event.target.value; setModel(next); void switchModel(next); }}>
            {models.length === 0 ? <option value="">（模型）</option> : null}
            {models.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
          <select aria-label="推理等级" value={reasoning} onChange={(event) => void switchReasoning(event.target.value)}>
            {REASONING_LEVELS.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
          </select>
          <span className="tb-label">权限</span>
          <div className="segmented" role="group" aria-label="权限档位">
            {PERMISSION_MODES.map(([value, text]) => (
              <button
                key={value}
                type="button"
                className={`seg-btn${permission === value ? " on" : ""}`}
                onClick={() => void setPermissionMode(value)}
              >
                {text}
              </button>
            ))}
          </div>
          <button className="btn" type="button" onClick={newSession}>＋ 新会话</button>
          <button className="btn" type="button" onClick={() => void loadLatest()}>刷新</button>
          <button className="btn" type="button" onClick={() => void clearSession()}>清空聊天</button>
          <button className="btn danger" type="button" onClick={() => void deleteSession()}>删除</button>
        </div>
      </header>
      <ApprovalHost approvals={approvals.state.items} submittingId={approvals.state.submittingId} error={approvals.state.error} onAnswer={(approval, answer) => void approvals.answer(approval, answer)} />
      {loading ? <div className="empty-hint">加载历史消息…</div> : null}
      {loadingOlder ? <div className="empty-hint">加载更早历史…</div> : null}
      {!loading && displayed.length === 0 ? <div className="empty-hint">（无历史消息，发送第一条开始对话）</div> : null}
      <VirtualMessageList
        items={displayed}
        estimateSize={(index) => {
          const item = displayed[index];
          if (!item) return 56;
          if (item.kind === "reasoning") return 44;
          if (item.kind === "tool") return 52;
          if (item.kind === "notice") return 32;
          if (item.kind === "projection") return 48;
          if (item.kind === "goalArchived") return 40;
          if (item.kind === "image") return 240;
          if (item.kind !== "message") return 56;
          // 中间输出条卡（单行）比正式答复气泡矮
          if (item.message.role === "assistant" && (item.message as { kind?: string }).kind === "intermediate") return 44;
          return item.message.role === "user" ? 56 : 76;
        }}
        autoFollow={busy}
        onNearTop={loadOlder}
        renderItem={(item) => <TimelineRow item={item} sessionKey={sessionKey} convId={convId ?? ""} />}
      />
      {/* Turn 级统一加载指示：骑在整个运行中 turn 上，不随过程节点闪烁（#5） */}
      <TurnStatus
        busy={busy}
        turnId={liveTurn?.turn_id ?? null}
        turnStartedAt={liveTurn?.started_at ?? null}
        resetKey={convId}
      />
      <RuntimeFloat
        plan={runtime.plan}
        goal={runtime.goal}
        onPlanAction={(action, plan) => void runtime.action("plan", action, plan.plan_id)}
        onGoalAction={(action, goal) => void runtime.action("goal", action, goal.goal_id)}
      />
      {/* 运行中队列等待窗口：活动项为空时组件自隐藏（空闲倒计时期间仍可见） */}
      <QueuePanel
        queue={queueDispatch.queue}
        countdown={queueDispatch.countdown}
        onInject={(queueItemId) => void queueDispatch.injectQueueItem(queueItemId)}
        pausedReason={queueDispatch.pausedReason}
      />
      {/* 常驻目标条（#8）：有活跃 goal 才显示，停靠输入框上方；动作失败回滚 + toast（#9） */}
      <GoalBar
        goal={runtime.goal}
        onAction={(action, goal) => runtime.action("goal", action, goal.goal_id)}
      />
      <ChatComposer
        commands={commands}
        busy={busy}
        steeringAvailable={queueDispatch.steeringAvailable}
        onSteering={() => void queueDispatch.insertSteeringHint()}
        onSend={async (text, files) => {
          // 图片随队列信封发送（修正版方案 A）；附件里的非图片类型仍拒绝
          const images = (files ?? [])
            .filter((f) => f.media_type?.startsWith("image/"))
            .map((f) => ({ data: f.data, media_type: f.media_type }));
          if (images.length !== (files?.length ?? 0)) {
            toast("附件仅支持图片（png/jpeg/webp/gif）", "err");
            throw new Error("附件仅支持图片");
          }
          await send(text, images.length ? images : undefined);
        }}
        onStop={() => void stop()}
        contextSlot={(
          <div className={`ctx-meter${ctxClass}`}>
            <span className="ctx-icon">📊</span>
            <span className="ctx-pct">{ctxPct != null ? `${ctxPct}%` : "–"}</span>
            <div className="ctx-tip">
              <div className="ctx-tip-title">上下文占用</div>
              {ctxRows.map(([k, v]) => (
                <div key={k} className="ctx-tip-row"><span>{k}</span><b>{v}</b></div>
              ))}
            </div>
          </div>
        )}
      />
      <QuestionHost queue={questions} />
    </section>
  );
}
