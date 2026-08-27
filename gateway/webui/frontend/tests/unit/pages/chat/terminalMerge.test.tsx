import { act, render } from "@testing-library/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { describe, expect, it, beforeEach } from "vitest";

import { gatewayStore, selectLiveTurn, selectTurnNodes, selectTurnWithNodes, useGatewaySelector } from "@/gateway/store";
import { historyToTimeline, mergeTerminalTurn, turnToTimeline } from "@/pages/chat/chatTimeline";
import { TimelineRow } from "@/pages/chat/timelineRow";
import type { GatewayEvent, Turn, TurnNode } from "@/gateway/types";

const CONV = "conv-1";
const ev = (type: string, scope: "session" | "turn", version: number, data: Record<string, unknown>, turnId?: string): GatewayEvent => ({
  type,
  data: { conversation_id: CONV, session_key: "webui:default", origin: "webui", subtype: "main", workspace_id: null, turn_id: turnId, scope, version, data }});

const seed = () => {
  gatewayStore.reset();
  gatewayStore.applySnapshot({
    conversation: { conversation_id: CONV, session_key: "webui:default", origin: "webui", subtype: "main", workspace_id: null, execution_scope: "gateway:default", route_metadata: {}, session_version: 0, created_at: "", updated_at: "" },
    session_version: 0, queue: [], live_turn: { turn_id: "t1", conversation_id: CONV, status: "queued", turn_version: 0, started_at: "", finished_at: null, final_assistant_node_id: null, error_code: null, parent_conversation_id: null, parent_turn_id: null },
    turn_version: 0, nodes: [], queued_nodes: [], pending_approvals: [], server_time: ""});
};

// 与 ChatPage / useWorkspaceConversation 相同的终态并入逻辑（含收官修复：
// 订阅 selectTurnWithNodes，chat.done 权威数据晚到时原位重合并）。
function ChatHarness() {
  const [history, setHistory] = useState<Array<{ turn: Turn; nodes: TurnNode[] }>>([]);
  const [terminalTurnId, setTerminalTurnId] = useState<string | null>(null);
  const liveTurn = useGatewaySelector(selectLiveTurn(CONV));
  const liveNodes = useGatewaySelector(selectTurnNodes(liveTurn?.turn_id ?? ""));
  const terminalData = useGatewaySelector(selectTurnWithNodes(terminalTurnId ?? ""));
  const prevLiveTurnRef = useRef<Turn | null | undefined>(undefined);
  useEffect(() => {
    const prev = prevLiveTurnRef.current;
    prevLiveTurnRef.current = liveTurn;
    if (prev && !liveTurn && prev.conversation_id === CONV) {
      const state = gatewayStore.getState();
      if (state.turnsById[prev.turn_id]) setTerminalTurnId(prev.turn_id);
    }
  }, [liveTurn]);
  useEffect(() => {
    if (!terminalData || terminalData.turn.conversation_id !== CONV) return;
    setHistory((prevHistory) => mergeTerminalTurn(prevHistory, terminalData));
  }, [terminalData]);
  const historyTimeline = useMemo(() => historyToTimeline(history), [history]);
  const liveTimeline = useMemo(() => (liveTurn ? turnToTimeline(liveTurn, liveNodes, { live: true }) : []), [liveTurn, liveNodes]);
  const displayed = useMemo(() => (liveTimeline.length ? [...historyTimeline, ...liveTimeline] : historyTimeline), [historyTimeline, liveTimeline]);
  return (
    <div data-testid="timeline">
      {displayed.map((item) => <TimelineRow key={item.key} item={item} sessionKey="webui:default" convId={CONV} />)}
    </div>
  );
}

describe("terminal turn merge (chat.done authoritative data after turn.status)", () => {
  beforeEach(() => { seed(); });

  it("BUG: final reply freezes at the streamed snapshot when turn.status and chat.done arrive in separate tasks", () => {
    render(<ChatHarness />);
    act(() => {
      gatewayStore.applyEvent(ev("turn.status", "turn", 1, { status: "answering", turn_id: "t1" }, "t1"));
      gatewayStore.applyEvent(ev("node.delta", "turn", 2, { node_id: "n-u", type: "user", text: "hi", position: 1, status: "done" }, "t1"));
      gatewayStore.applyEvent(ev("node.delta", "turn", 3, { node_id: "n-a", type: "assistant", delta: "流式草稿", seq: 1, position: 2, status: "streaming" }, "t1"));
    });
    expect(document.body.textContent).toContain("流式草稿");

    // SSE 帧 1：turn.status(done) —— 与 chat.done 分属不同任务（真实网络时序）
    act(() => {
      gatewayStore.applyEvent(ev("turn.status", "turn", 4, { status: "done", turn_id: "t1" }, "t1"));
    });
    // SSE 帧 2：chat.done 权威全量文本（独立任务，晚于 turn.status）
    act(() => {
      gatewayStore.applyEvent(ev("chat.done", "turn", 5, { full_text: "**权威最终回复** 完整", final_assistant_node_id: "n-a" }, "t1"));
    });

    // 修复后：终态显示权威全量文本的 Markdown，流式旧文本不残留。
    const md = document.querySelector(".bubble.assistant .md");
    expect(md).not.toBeNull();
    expect(md?.innerHTML).toContain("<strong>权威最终回复</strong>");
    expect(document.body.textContent).not.toContain("流式草稿");
  });

  it("fast reply: assistant node created by chat.done only still shows the reply after done", () => {
    render(<ChatHarness />);
    act(() => {
      gatewayStore.applyEvent(ev("turn.status", "turn", 1, { status: "answering", turn_id: "t1" }, "t1"));
      gatewayStore.applyEvent(ev("node.delta", "turn", 2, { node_id: "n-u", type: "user", text: "hi", position: 1, status: "done" }, "t1"));
    });
    // 流式期间：无 assistant 消息
    expect(document.querySelectorAll(".bubble.assistant")).toHaveLength(0);
    act(() => {
      gatewayStore.applyEvent(ev("turn.status", "turn", 3, { status: "done", turn_id: "t1" }, "t1"));
    });
    act(() => {
      gatewayStore.applyEvent(ev("chat.done", "turn", 4, { full_text: "快速回复", final_assistant_node_id: "n-a" }, "t1"));
    });
    const bubble = document.querySelector(".bubble.assistant .md");
    expect(bubble).not.toBeNull();
    expect(bubble?.textContent).toContain("快速回复");
  });

  it(">8KB live message keeps real markdown during streaming (incremental), exits to authoritative markdown after done", () => {
    // 优化方案 #2：块级增量解析上线后，原「>8KB 非终态降级 <pre> 直出」取消
    // ——长回复流式期间也保持实时 Markdown 排版，终态切换权威全量文本。
    const big = "# 大标题\n\n" + "内容".repeat(5000);
    render(<ChatHarness />);
    act(() => {
      gatewayStore.applyEvent(ev("turn.status", "turn", 1, { status: "answering", turn_id: "t1" }, "t1"));
      gatewayStore.applyEvent(ev("node.delta", "turn", 2, { node_id: "n-u", type: "user", text: "hi", position: 1, status: "done" }, "t1"));
      gatewayStore.applyEvent(ev("node.delta", "turn", 3, { node_id: "n-a", type: "assistant", delta: big, seq: 1, position: 2, status: "streaming" }, "t1"));
    });
    // 流式期间：不再 <pre> 降级，标题/段落以真实 Markdown 渲染。
    expect(document.querySelector(".bubble.assistant pre.large-text-fallback")).toBeNull();
    const liveMd = document.querySelector(".bubble.assistant .md");
    expect(liveMd).not.toBeNull();
    expect(liveMd?.innerHTML).toContain("<h1>大标题</h1>");
    act(() => {
      gatewayStore.applyEvent(ev("turn.status", "turn", 4, { status: "done", turn_id: "t1" }, "t1"));
    });
    act(() => {
      gatewayStore.applyEvent(ev("chat.done", "turn", 5, { full_text: big + "\n\n# 终态补全", final_assistant_node_id: "n-a" }, "t1"));
    });
    // 终态：完整 Markdown 渲染权威文本
    const md = document.querySelector(".bubble.assistant .md");
    expect(document.querySelector(".bubble.assistant pre.large-text-fallback")).toBeNull();
    expect(md).not.toBeNull();
    expect(md?.innerHTML).toContain("终态补全");
  });
});
