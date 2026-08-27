// 工作区会话统一链路 hook —— 设计方案 19.2/20（与主会话一致的 Gateway 数据源）。
//
// 替换旧 useWorkspaceChatController（旧 /api/chat + chat.* SSE + MessageStore）：
// - 会话数据 = Conversation/Turn/Node（SQLite 权威）
// - 事件 = 统一 Gateway SSE（scope=sessionKey）
// - 发送/停止/清空 = conversationApi（统一模型）
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { toast } from "@/components/toast";
import { conversationApi } from "@/gateway/api";
import { gatewayStore, selectLiveTurn, selectQueue, selectTurnNodes, selectTurnWithNodes, useGatewaySelector } from "@/gateway/store";
import { useConversation } from "@/gateway/useConversation";
import { historyToTimeline, mergeTerminalTurn, turnToTimeline } from "@/pages/chat/chatTimeline";
import { useQueueDispatch } from "@/pages/chat/useQueueDispatch";
import type { TimelineItem } from "@/pages/chat/timeline";
import type { Turn, TurnNode } from "@/gateway/types";
import type { ParsedSseEvent } from "@/sse/events";
import { useSse } from "@/hooks/useSse";

const TERMINAL = new Set(["done", "stopped", "error", "interrupted"]);
const HISTORY_PAGE_SIZE = 50;

export interface WorkspaceConversation {
  displayed: TimelineItem[];
  busy: boolean;
  loading: boolean;
  loadingOlder: boolean;
  error: string | null;
  convId: string;
  send: (text: string, images?: Array<{ data: string; media_type?: string }>) => Promise<unknown>;
  stop: () => Promise<void>;
  clear: () => Promise<void>;
  loadOlder: () => void;
  onSse: (event: ParsedSseEvent) => void;
  /** 运行中队列等待窗口（与主会话共用，设计方案 8）：队列项 + 倒计时 + Steering。 */
  queueDispatch: ReturnType<typeof useQueueDispatch>;
}

export interface UseWorkspaceConversationOptions {
  /** 复用本 hook 的唯一 SSE 连接转发旧浮层事件（审批/问题/运行时）——
   *  L5：替代 WorkspacePage 里第二条 useSseFeed 连接（双 SSE 冗余合并）。 */
  onLegacyEvent?: (event: ParsedSseEvent) => void;
  /** 需求：输入新消息后旧终态 plan/goal 状态框应消失——消息入队成功后回调
   *  （WorkspacePage 传 runtime.dismissStale）。 */
  onMessageSent?: () => void;
}

export function useWorkspaceConversation(sessionKey: string, options: UseWorkspaceConversationOptions = {}): WorkspaceConversation {
  const { onLegacyEvent } = options;
  const legacyEventRef = useRef(onLegacyEvent);
  legacyEventRef.current = onLegacyEvent;
  const messageSentRef = useRef(options.onMessageSent);
  messageSentRef.current = options.onMessageSent;
  const [convId, setConvId] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<{ turn: Turn; nodes: TurnNode[] }>>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const historyLoadingRef = useRef(false);
  // P1 会话切换分页竞态：历史请求代次（convId/sessionKey 变化即自增），
  // await 返回后代次不符的响应直接丢弃，避免旧会话数据串台到新会话。
  const historyGenRef = useRef(0);
  // 被防重入挡掉的新会话首屏加载：旧请求结束后补一次触发信号。
  const [historyRetryTick, setHistoryRetryTick] = useState(0);

  const { handleEvent } = useConversation({ sessionKey, enabled: true });

  // 会话 conversation 幂等取得
  useEffect(() => {
    let active = true;
    // 同步失效旧 convId（P0 修复）：create 往返窗口内 convId 仍指向旧会话，
    // selectLiveTurn(旧convId) 会把旧会话的 live turn 渲染进新会话视图
    //（双会话同时运行时切换，内容输出错乱的直接根源）。置 null → liveTurn
    // 立即消失，加载态由 loading 遮罩，杜绝跨会话内容串染。
    setConvId(null);
    setLoading(true);
    setError(null);
    void conversationApi.create(sessionKey).then((result) => {
      if (!active) return;
      if (result.ok && result.data) setConvId(result.data.conversation.conversation_id);
      else setError(result.error?.message ?? "会话初始化失败");
      setLoading(false);
    });
    return () => { active = false; };
  }, [sessionKey]);

  const liveTurn = useGatewaySelector(selectLiveTurn(convId ?? ""));
  const liveNodes = useGatewaySelector(selectTurnNodes(liveTurn?.turn_id ?? ""));
  const busy = liveTurn != null && !TERMINAL.has(liveTurn.status);
  // 运行中队列等待窗口（设计方案 8，与主会话共用）：运行中 send 只入队，
  // 面板展示等待项；Turn 终态后倒计时自动分派队首；支持 Steering 插入。
  const queueDispatch = useQueueDispatch(convId);

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

  // P1 竞态修复：sessionKey/convId 变化即推进历史代次并清空上一会话的分页数据，
  // 在途旧请求的响应因代次不符被丢弃；声明顺序保证先重置、后触发加载。
  useEffect(() => {
    historyGenRef.current += 1;
    setHistory([]);
    setHistoryCursor(undefined);
    setLoading(true);
  }, [convId, sessionKey]);

  useEffect(() => { void loadHistory(); }, [loadHistory, historyRetryTick]);

  // Turn 终态后：优先复用本地 store 权威数据（chat.done 已写入全量 text），
  // 直接并入历史分页，避免整页重拉；仅当本地缺失该 turn 时才回退 loadHistory。
  // 收官修复：后端先发 turn.status(done)、后发 chat.done（权威 full_text），
  // 若在 turn.status 时立即用节点快照并史，权威文本不会反映进历史分页（冻结在
  // 流式旧文本）。因此并入改为订阅 selectTurnWithNodes：初始并入 + 权威数据
  // 到达后原位重合并（turn/node 引用变化才触发，无关事件不重渲染）。
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
      if (prev.conversation_id !== convId) return;
      const state = gatewayStore.getState();
      const turn = state.turnsById[prev.turn_id];
      if (turn) {
        // 零闪断：立即以当前 store 快照并入历史，避免 live→终态切换窗口期
        // 内容闪没；chat.done 权威文本晚到后由 terminalData 订阅重合并。
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

  // 时间线最终防线（多会话切换串扰修复）：任何上游竞态（分页在途/终态并入与
  // 会话切换交错/快照晚到）漏进 history 的跨会话条目与重复 turn，在此按
  // conversation_id 过滤 + turn_id 去重，保证当前视图永远只渲染当前会话、
  // 每轮只出现一次。
  const displayed = useMemo<TimelineItem[]>(() => {
    const seen = new Set<string>();
    const scoped = history.filter(({ turn }) => {
      if (turn.conversation_id !== convId) return false;
      if (seen.has(turn.turn_id)) return false;
      seen.add(turn.turn_id);
      return true;
    });
    const items = historyToTimeline(scoped);
    if (liveTurn && liveTurn.conversation_id === convId
      && !seen.has(liveTurn.turn_id)) {
      // TTFT 占位判定：乐观回显的 user 节点不算实质输出——首个 reasoning/
      // assistant/tool 节点到达前在 live 渲染（user 气泡）之后追加"正在思考…"
      // 占位行；实质节点到达后原子替换为完整 live 渲染。
      const hasSubstantive = liveNodes.some((n) => n.type !== "user" && n.type !== "user_steering");
      items.push(...turnToTimeline(liveTurn, liveNodes, { live: true }));
      if (!hasSubstantive) {
        const parsed = Date.parse(liveTurn.started_at || "");
        items.push({
          key: "thinking:" + liveTurn.turn_id,
          kind: "thinking",
          startedAt: Number.isFinite(parsed) ? parsed : Date.now(),
        });
      }
    }
    return items;
  }, [history, liveTurn, liveNodes, convId]);

  const send = useCallback(async (text: string, images?: Array<{ data: string; media_type?: string }>) => {
    if (!convId || !text.trim()) return;
    setError(null);
    const result = await conversationApi.enqueue(convId, text, { images });
    // 需求：输入新消息后，旧的终态 plan/goal 状态框应消失（运行中的保留）。
    if (result.ok) messageSentRef.current?.();
    // 入队可见确认（审计问题 2）：与主会话页同语义。
    if (result.ok && busy) {
      const waiting = selectQueue(convId)(gatewayStore.getState())
        .filter((q) => q.status === "waiting").length;
      toast(`已加入队列，当前第 ${waiting} 位，将在当前任务完成后自动发送`, "ok");
    }
    if (result.ok && result.data && !busy) {
      const next = await conversationApi.sendNext(convId);
      // 乐观回显：sendNext 返回 user_node 时立即并入时间线（无需等 SSE），
      // 让用户消息第一时间显示在会话中（设计方案：发送即时回显）。
      if (next.ok && next.data?.user_node && next.data.turn) {
        gatewayStore.mergeUserNode(next.data.turn, next.data.user_node);
        for (const imgNode of next.data.image_nodes ?? []) {
          gatewayStore.mergeUserNode(next.data.turn, imgNode);
        }
      }
    }
    return result;
  }, [convId, busy]);

  const stop = useCallback(async () => {
    if (convId) await conversationApi.stop(convId);
  }, [convId]);

  const clear = useCallback(async () => {
    if (!convId) return;
    try { await conversationApi.clear(convId); } catch { /* silent */ }
    setHistory([]);
    setHistoryCursor(undefined);
    void loadHistory();
  }, [convId, loadHistory]);

  const loadOlder = useCallback(() => {
    if (historyCursor) void loadHistory(historyCursor);
  }, [historyCursor, loadHistory]);

  const onSse = useCallback((event: ParsedSseEvent) => {
    const type = (event as { type?: string }).type;
    const eventData = event.data as { conversation_id?: unknown };
    // 归属双保险（与 ChatPage.onSseEvent 对齐）：SSE 理论上已按 sessionKey
    // 订阅过滤，这里再按 conversation_id 兜底，防 scope 过滤失效（如事件
    // 缺 session_key）时其它会话的流式事件污染本页。
    if (type === "node.delta" || type === "conversation.cleared") {
      if (convId && eventData?.conversation_id != null && eventData.conversation_id !== convId) return;
    }
    handleEvent(event);
    if (type === "conversation.cleared") {
      setHistory([]);
      void loadHistory();
    }
    // 唯一 SSE 连接顺带转发旧浮层事件（L5：双 SSE 冗余合并）
    legacyEventRef.current?.(event);
  }, [handleEvent, loadHistory, convId]);

  // 全局 SSE：统一 Gateway 事件（scope=sessionKey 过滤本会话）。
  // L5：这是工作区唯一一条 SSE 连接（WorkspacePage 不再开第二条 useSseFeed）。
  useSse({ sessionKey }, onSse);

  return { displayed, busy, loading, loadingOlder, error, convId: convId ?? "", send, stop, clear, loadOlder, onSse, queueDispatch };
}
