// 统一会话节点 → 旧 ChatPage 时间线项的适配器。
// 保留旧布局（气泡/工具卡/思考卡）的渲染组件，仅替换数据源为新的
// Gateway Store 节点（设计方案：节点化流式 + chat.done 权威）。

import type { Message } from "@/api/types";
import type { Turn, TurnNode } from "@/gateway/types";
import { normalizeToolName, summarizeToolParams } from "@/pages/chat/toolSummary";
import type { TimelineItem } from "@/pages/chat/timeline";

const nodeText = (node: TurnNode): string => node.text ?? "";

// 工具结果错误前缀（与后端 ToolRuntime 的 is_error 判定一致：❌/⛔/⏰/⏭️/⏹️）。
// 用于在 meta.error 缺失时也能把失败/超时/停止的工具调用卡片标红（视觉区分）。
const TOOL_ERROR_PREFIX_RE = /^(?:❌|⛔|⏰|⏭️|⏹️)/;

// A5 归档占位文本：[Goal {goal_id} 第{i}轮终答已归档，详见目标页]（dispatcher 生成）。
// 命中时渲染"已归档，详见目标页"链接跳 GoalPage，不再把占位文本当普通消息平铺。
const GOAL_ARCHIVED_RE = /^\[Goal\s+([A-Za-z0-9_-]+)\s+第\d+轮终答已归档，详见目标页\]$/;

const warnUnknownNode = (type: string): void => {
  console.warn(`[chatTimeline] 未知 node.type=${type}，按默认气泡兜底渲染`);
};

// 配置命令属于 UI 控制操作，不应作为聊天消息显示。
export const isUiConfigCommand = (text: string): boolean =>
  /^\/(model|perm|permission|reasoning)\b/i.test(text.trim());

// 配置命令回显的紧凑单行格式（后端 dispatcher/api_chat 的实际输出）。
// 每条正则都锚定整行：可选 emoji 前缀 + 固定短语 + 冒号 + 短值；自然语言
// 回复里出现"当前模型/推理等级"等词但不是这种紧凑格式时不会命中。
const UI_CONFIG_FEEDBACK_LINE_RES: RegExp[] = [
  /^(?:✅|🤖|🧠)?\s*已切换到模型[:：]\s*\S{1,128}$/,
  /^(?:✅|🤖|🧠)?\s*当前模型[:：]\s*\S{1,128}$/,
  /^(?:✅|🤖|🧠)?\s*当前推理等级[:：]\s*\S{1,128}$/,
  /^(?:✅|🤖|🧠)?\s*推理等级(?:已切换为)?[:：]\s*\S{1,128}$/,
  /^(?:✅|🔐|🛡️)?\s*权限(?:已切换|模式)[:：]\s*\S{1,128}$/,
  // /reasoning 多行回显的缩进续行："  来源: 会话覆盖"、"  可选: inherit / …"
  /^来源[:：]\s*\S{1,128}$/,
  /^可选[:：]\s*\S.{0,255}$/,
];

/** 是否为配置命令回显（应从时间线隐藏）。保守判定：消息拆行后，所有非空行
 *  都必须是上面的紧凑回显格式（最多 4 行，/reasoning 查询回显共 3 行）才整体
 *  隐藏；任何一行含正文叙述即正常渲染，避免误杀提到"当前模型"等词的回复。 */
export const isUiConfigFeedback = (text: string): boolean => {
  const lines = String(text).split("\n").map((line) => line.trim()).filter((line) => line !== "");
  if (lines.length === 0 || lines.length > 4) return false;
  return lines.every((line) => UI_CONFIG_FEEDBACK_LINE_RES.some((re) => re.test(line)));
};

export const nodeToMessage = (node: TurnNode): Message => ({
  role: node.type === "user" || node.type === "user_steering" ? "user" : "assistant",
  content: nodeText(node),
  content_text: nodeText(node),
});

// ---- 节点级 item 引用缓存（优化方案 #3/#6，借鉴 dsh partial 的 block 级不可变）----
// store 中 node 为不可变替换（文本 delta ⇒ 新 node 引用），因此「node 引用相同
// ⇒ 派生 item 相同」恒成立。live 流式构建走缓存：每次 delta 只为内容变化的节点
// （通常是流式尾部那个）新建 TimelineItem，其余行复用同一对象引用 —— TimelineRow
// 的浅比较 memo 得以真正短路，「只重渲流式那一行」成为结构性保证。这同时实现了
// harness 结构/内容分离签名（#6）的语义：结构变化（新增/状态变更节点）必然是新
// 引用 ⇒ 新 item；纯内容追加只有尾部 item 是新引用。
// 终态（live=false）构建不走缓存：markFinalAnswer 需要按整轮消息重推 final 标记
// （会改写 message.kind），且历史重建是低频路径（分页/终态并入），保持现状零风险。
// WeakMap 以 node 为 key：旧节点被 store 替换/淘汰后条目随之可回收，无界增长。
const itemCacheByNode = new WeakMap<TurnNode, TimelineItem>();

const cachedItem = (turn: Turn, node: TurnNode, live: boolean, build: () => TimelineItem): TimelineItem => {
  if (!live) return build();
  const hit = itemCacheByNode.get(node);
  if (hit && hit.key.endsWith(`${turn.turn_id}:${node.node_id}`)) return hit;
  const item = build();
  itemCacheByNode.set(node, item);
  return item;
};

// 思考节点 → 思考时间线项（runtime 明细平铺与普通节点共用同一渲染协议）。
const reasoningToItem = (turn: Turn, node: TurnNode, live: boolean): TimelineItem =>
  cachedItem(turn, node, live, () => ({
    key: `reasoning:${turn.turn_id}:${node.node_id}`,
    kind: "reasoning",
    text: nodeText(node),
    live,
  }));

// 工具节点 → 工具时间线项（与主会话工具卡同构：一行可扫描 + 展开明细）。
const toolToItem = (turn: Turn, node: TurnNode, live = false): TimelineItem =>
  cachedItem(turn, node, live, () => {
    const meta = node.metadata ?? {};
    const paramsSummary = meta.params_summary != null ? String(meta.params_summary) : "";
    const resultSummary = meta.result_summary != null ? String(meta.result_summary) : "";
    const toolName = normalizeToolName(String(meta.tool ?? meta.call_id ?? "工具"));
    // 错误判定：显式 meta.error 优先；否则按结果文本前缀识别（超时/拒绝/失败）。
    const resultError = meta.error != null
      || (resultSummary !== "" && TOOL_ERROR_PREFIX_RE.test(resultSummary));
    return {
      key: `tool:${turn.turn_id}:${node.node_id}`,
      kind: "tool",
      name: toolName,
      input: paramsSummary,
      result: resultSummary,
      // 折叠摘要：参数摘要转成一行可读文本（DSH ToolRow 风格：一行可扫描）
      summary: summarizeToolParams(toolName, paramsSummary),
      // 设计方案 17：大结果 result_ref 按需读取（展开时拉完整结果）
      resultRef: meta.result_ref != null ? String(meta.result_ref) : undefined,
      orphaned: false,
      pending: node.status === "running",
      isError: resultError,
      error: meta.error != null ? String(meta.error) : (resultError ? resultSummary : ""),
    };
  });

/** 图片节点 → 图片时间线项（归属校验端点回原图；node 不可变可长缓存）。 */
const imageToItem = (turn: Turn, node: TurnNode): TimelineItem => ({
  key: `image:${turn.turn_id}:${node.node_id}`,
  kind: "image",
  imageId: node.node_id,
  src: `/api/conversations/${turn.conversation_id}/images/${node.node_id}`,
  name: String((node.metadata as Record<string, unknown> | null)?.ref ?? "图片")
    .split("/").pop() || "图片",
});

/** 单个 Turn 的节点 → TimelineItem[]（保持节点顺序）。 */
export const turnToTimeline = (turn: Turn, nodes: TurnNode[], opts: { live?: boolean } = {}): TimelineItem[] => {
  const items: TimelineItem[] = [];
  // 对齐 dsh：plan/goal 在主会话累积。runtime 轮（任一节点带 runtime 标记）的
  // reasoning/tool 折叠进所属 step 的投影卡——中间步骤输出不直接平铺在时间线，
  // 收进 plan 卡片；assistant 节点：中间 step → 紧凑投影卡，最终答复（runtime_final）
  // → 投影卡（承载该步工具明细）+ 正式渲染消息（持久可见）。live 与终态同构
  // （running 卡与 step 卡同 key），无"先渲染再变身工具卡"的转换。
  const isRuntimeTurn = nodes.some((n) => hasRuntimeNode(n));
  let detail: TurnNode[] = [];
  let emittedStep = false;
  for (const node of nodes) {
    if (node.type === "user" || node.type === "user_steering") {
      const text = nodeText(node);
      if (text && !isUiConfigCommand(text)) {
        items.push(cachedItem(turn, node, Boolean(opts.live), () => ({
          key: `msg:${turn.turn_id}:${node.node_id}`,
          kind: "message",
          message: nodeToMessage(node),
        })));
      }
      continue;
    }
    if (node.type === "image") {
      items.push(imageToItem(turn, node));
      continue;
    }
    if (node.type === "reasoning") {
      const text = nodeText(node);
      if (!text) continue;
      if (isRuntimeTurn) { detail.push(node); continue; }
      items.push(reasoningToItem(turn, node, Boolean(opts.live && node.status === "streaming")));
      continue;
    }
    if (node.type === "tool") {
      if (isRuntimeTurn) { detail.push(node); continue; }
      items.push(toolToItem(turn, node, Boolean(opts.live)));
      continue;
    }
    if (node.type === "assistant") {
      const text = nodeText(node);
      const meta = node.metadata ?? {};
      // 方案 B：后端权威分类标记优先（intermediate/final 随 node.delta 与
      // 快照下发），前端不再靠"是否最后一条"推断——主会话/工作区/plan/goal
      // 四种模式同一语义。
      const explicitKind: "final" | "intermediate" | undefined =
        meta.intermediate === true ? "intermediate"
          : meta.final === true ? "final"
            : undefined;
      if (meta.runtime_type != null || meta.runtime_id != null) {
        // runtime 标记的 assistant 节点 → 内联卡片（不平铺 message）。
        const rtType = String(meta.runtime_type ?? "plan");
        const rtId = String(meta.runtime_id ?? "");
        // 状态优先级：metadata.status（后端 H-B 新增，failed/blocked 等运行时状态）
        // → metadata.runtime_status（现有字段）→ 节点 status（容错兜底）。
        const rawStatus = meta.status !== undefined
          ? String(meta.status)
          : meta.runtime_status !== undefined ? String(meta.runtime_status) : String(node.status);
        const rtStatus = rawStatus || "done";
        // 失败/阻断原因：error_code 优先，message/error 兜底（未提供则 undefined）。
        const errorCode = meta.error_code !== undefined
          ? String(meta.error_code)
          : meta.error !== undefined ? String(meta.error) : undefined;
        const message = meta.message !== undefined
          ? String(meta.message)
          : meta.error !== undefined ? String(meta.error) : undefined;
        const goalRound = meta.goal_round != null ? Number(meta.goal_round) : undefined;
        // 中间 step 与最终答复统一：工具/思考明细折叠进投影卡（"中间步骤输出
        // 不直接平铺在时间线，收进 plan 卡片"）；最终答复（runtime_final）额外
        // 平铺为正式 assistant 消息（渲染后 Markdown、持久可见）。
        items.push({
          key: `projection:${turn.turn_id}:${rtId || node.node_id}`,
          kind: "projection",
          runtime_type: rtType,
          runtime_id: rtId,
          status: rtStatus,
          runtime_status: rtStatus,
          errorCode,
          message,
          goalRound: Number.isFinite(goalRound ?? NaN) ? goalRound : undefined,
          finalText: text || undefined,
          detailNodes: detail.slice(),
        });
        detail = [];
        emittedStep = true;
        if (meta.runtime_final === true || meta.runtime_final === "true") {
          // goal 每轮终答收进 Goal 卡片（每阶段输出不单独平铺气泡，参考 plan
          // 模式优化）；plan 最终 step 保留正式消息（最终答复持久可见）。
          if (rtType !== "goal" && text && !isUiConfigFeedback(text)) {
            items.push(cachedItem(turn, node, Boolean(opts.live), () => ({
              key: `msg:${turn.turn_id}:${node.node_id}`,
              kind: "message",
              // runtime_final 节点后端同时带 metadata.final（方案 B 统一标记）
              message: { ...nodeToMessage(node), ...(explicitKind ? { kind: explicitKind } : {}) },
            })));
          }
        }
        continue;
      }
      // A5 归档占位文本：识别并渲染"已归档，详见目标页"链接（跳 GoalPage），
      // 不再把占位文本当普通消息平铺。
      const archived = GOAL_ARCHIVED_RE.exec(text);
      if (archived) {
        items.push(cachedItem(turn, node, false, () => ({
          key: `goalArchived:${turn.turn_id}:${node.node_id}`,
          kind: "goalArchived",
          goalId: archived[1]!,
          text,
        })));
        continue;
      }
      if (text && !isUiConfigFeedback(text)) {
        items.push(cachedItem(turn, node, Boolean(opts.live), () => ({
          key: `msg:${turn.turn_id}:${node.node_id}`,
          kind: "message",
          // 后端权威标记：intermediate → 单行条卡；final → 正式气泡；
          // 未标记（旧数据/流式中）→ 保持气泡，由 markFinalAnswer 兜底
          message: { ...nodeToMessage(node), ...(explicitKind ? { kind: explicitKind } : {}) },
        })));
      }
    } else {
      // 未知 node.type：console.warn + 默认气泡兜底（fail-open，绝不静默丢弃）
      warnUnknownNode(node.type);
      const unknownText = nodeText(node);
      if (unknownText) {
        items.push(cachedItem(turn, node, false, () => ({
          key: `msg:${turn.turn_id}:${node.node_id}`,
          kind: "message",
          message: nodeToMessage(node),
        })));
      }
    }
  }
  // 标记本轮最后一条 assistant 正式答复为 final（工作区/主聊天据此区分
  // "运行进度"与"助手"正式答复；未标记时 message.kind 恒缺省，工作区会把
  // 最终答复误判为运行进度卡片）。
  // 收官修复：流式阶段（live=true）不标记 final——live 消息保持 isFinal=false，
  // Markdown 的 80ms 节流与 >8KB <pre> 降级正常生效（chat.done 前用节流值）；
  // 终态并入历史（非 live）后才标 final，触发权威全量文本的最终同步渲染。
  // 思考过程卡应在其产出的回复之前：若 reasoning 项排在最后一条 assistant
  // 消息之后（message_start 先建 assistant 节点的历史数据），且两者之间无
  // 工具项（保持 agentic 时序），把它们移到该消息前面。
  moveTrailingReasoning(items);
  markFinalAnswer(items, Boolean(opts.live));
  // 流式阶段（本 step 答复节点尚未到达）：running 卡承载已到达的工具/思考
  // 明细（折叠在卡内），答复到达后同 key 平滑过渡为 step 卡，无平铺转换。
  if (detail.length > 0 && (opts.live || !emittedStep)) {
    const rt = runtimeMetaOf(detail);
    if (rt) {
      const last = detail[detail.length - 1]!;
      items.push({
        key: `projection:${turn.turn_id}:${rt.runtime_id || "running"}`,
        kind: "projection",
        runtime_type: rt.runtime_type,
        runtime_id: rt.runtime_id,
        status: "running",
        runtime_status: "running",
        liveText: nodeText(last),
        detailNodes: detail.slice(),
      });
    }
  }
  return items;
};

/** 兜底标记：仅为**没有后端权威标记**（metadata.intermediate/final 均缺失，
 *  如旧历史数据）的 assistant 消息按顺序推断——最后一条非空 → final，其余 →
 *  intermediate。新数据由后端 node.metadata 驱动（方案 B），此处不覆盖。
 *  live=true（流式投影阶段）跳过：live 消息保持无 kind 走节流渲染。 */
const markFinalAnswer = (items: TimelineItem[], live = false): void => {
  if (live) return;
  // 任一 assistant 已带后端权威标记 → 本 turn 是新数据，跳过顺序推断
  // （避免混用时把未标记节点误标；新数据全量由 metadata 驱动）
  const assistantMsgs: Array<{ kind?: string; content_text?: string }> = [];
  let hasExplicit = false;
  for (const item of items) {
    if (item.kind !== "message" || item.message.role !== "assistant") continue;
    const msg = item.message as { kind?: string; content_text?: string };
    assistantMsgs.push(msg);
    if (msg.kind) hasExplicit = true;
  }
  if (hasExplicit) return;
  let finalMarked = false;
  for (let i = assistantMsgs.length - 1; i >= 0; i--) {
    const msg = assistantMsgs[i]!;
    if ((msg.content_text ?? "") === "") continue;
    if (!finalMarked) {
      msg.kind = "final";
      finalMarked = true;
      continue;
    }
    msg.kind = "intermediate";
  }
};

/** 把最后一条 assistant 消息之后的 reasoning 项移到该消息前（中间无 tool 项时）。
 *  修复 message_start 先建 assistant 节点导致的"思考过程卡沉到回复下方"。 */
const moveTrailingReasoning = (items: TimelineItem[]): void => {
  let lastAssistant = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i]!;
    if (item.kind === "message" && item.message.role === "assistant") {
      lastAssistant = i;
      break;
    }
  }
  if (lastAssistant < 0) return;
  const trailing: TimelineItem[] = [];
  for (let i = items.length - 1; i > lastAssistant; i--) {
    const item = items[i]!;
    if (item.kind === "tool") return; // 有工具间隔：保持 agentic 时序，不移动
    if (item.kind === "reasoning") trailing.unshift(item);
  }
  if (!trailing.length) return;
  const assistantItem = items[lastAssistant]!;
  const filtered = items.filter((item, i) => !(i > lastAssistant && item.kind === "reasoning"));
  const insertAt = filtered.indexOf(assistantItem);
  filtered.splice(insertAt, 0, ...trailing);
  items.length = 0;
  items.push(...filtered);
};

/** 节点是否带 runtime 标记（plan/goal runtime turn 判定）。 */
const hasRuntimeNode = (node: TurnNode): boolean => {
  const m = node.metadata ?? {};
  return m.runtime_type != null || m.runtime_id != null;
};

/** 从明细节点提取首个 runtime 标记（流式 running 卡识别用）。 */
const runtimeMetaOf = (detail: TurnNode[]): { runtime_type: string; runtime_id: string } | null => {
  for (const node of detail) {
    const m = node.metadata ?? {};
    if (m.runtime_type != null || m.runtime_id != null) {
      return {
        runtime_type: String(m.runtime_type ?? "plan"),
        runtime_id: String(m.runtime_id ?? ""),
      };
    }
  }
  return null;
};

/** 历史分页（完整 Turn 为单位）→ TimelineItem[]，按时间正序。 */
export const historyToTimeline = (items: Array<{ turn: Turn; nodes: TurnNode[] }>): TimelineItem[] => {
  const out: TimelineItem[] = [];
  for (const item of items) {
    out.push(...turnToTimeline(item.turn, item.nodes));
  }
  return out;
};

/**
 * L5：Turn 终态后把本地 store 的权威条目并入历史分页（ChatPage /
 * useWorkspaceConversation 共用）。已含该 turn（终态字段权威化）→ 原位替换，
 * 避免重复；否则追加到末尾（新完成的 turn 必然是最新一条）。
 * 相比整页重拉历史（loadHistory），省掉一次全量分页请求与渲染。
 */
export const mergeTerminalTurn = (
  items: Array<{ turn: Turn; nodes: TurnNode[] }>,
  entry: { turn: Turn; nodes: TurnNode[] },
): Array<{ turn: Turn; nodes: TurnNode[] }> => {
  const idx = items.findIndex((item) => item.turn.turn_id === entry.turn.turn_id);
  if (idx >= 0) {
    const next = items.slice();
    next[idx] = entry;
    return next;
  }
  return [...items, entry];
};
