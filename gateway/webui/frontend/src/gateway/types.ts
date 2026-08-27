// 统一会话前端类型 —— 镜像后端 gateway/conversation 领域模型（设计方案 5/8/10/14/17）。

export type ConversationOrigin = "webui" | "channel" | "system" | "legacy_import";
export type ConversationSubtype =
  | "main" | "workspace" | "feishu" | "weixin" | "scheduler" | "heartbeat"
  | "plan" | "goal" | "subagent" | "debug" | "other";

export interface ConversationSession {
  conversation_id: string;
  session_key: string;
  origin: ConversationOrigin;
  subtype: ConversationSubtype;
  workspace_id?: string | null;
  execution_scope: string;
  route_metadata: Record<string, unknown>;
  session_version: number;
  created_at: string;
  updated_at: string;
}

export type TurnStatus =
  | "queued" | "thinking" | "tool" | "approval" | "answering" | "steering"
  | "stopping" | "done" | "stopped" | "error" | "interrupted";

export interface Turn {
  turn_id: string;
  conversation_id: string;
  status: TurnStatus;
  turn_version: number;
  runtime_snapshot_id?: string | null;
  started_at: string;
  finished_at?: string | null;
  final_assistant_node_id?: string | null;
  error_code?: string | null;
  parent_conversation_id?: string | null;
  parent_turn_id?: string | null;
  /** 最近活跃时间（事件应用刷新，LRU 裁剪排序用）；后端无该字段时前端本地戳记。 */
  updated_at?: string | null;
}

export type TurnNodeType =
  | "user" | "reasoning" | "tool" | "assistant" | "user_steering"
  | "runtime_projection" | "status" | "image";

export interface TurnNode {
  node_id: string;
  conversation_id: string;
  turn_id?: string | null;
  type: TurnNodeType;
  position: number | null;
  status: string;
  /** 契约①：node.delta 节点内递增序号（按 (node_id, seq) 追加增量文本）。
   *  旧后端无 delta/seq（携带全量 text），该字段保持 undefined。 */
  delta_seq?: number | null;
  text?: string | null;
  metadata: Record<string, unknown>;
  source_channel?: string | null;
  source_message_id?: string | null;
  sender_id?: string | null;
  sender_name?: string | null;
  created_at: string;
  updated_at: string;
}

export type QueueItemStatus =
  | "waiting" | "waiting_for_steering" | "injecting" | "sending"
  | "pending_delete" | "failed" | "sent" | "injected" | "deleted";

export interface QueueItem {
  queue_item_id: string;
  conversation_id: string;
  position: number;
  revision: number;
  status: QueueItemStatus;
  text: string;
  target_turn_id?: string | null;
  created_turn_id?: string | null;
  created_node_id?: string | null;
  operation_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface GatewayEventData {
  conversation_id: string;
  session_key: string;
  origin: ConversationOrigin;
  subtype: ConversationSubtype;
  workspace_id?: string | null;
  turn_id?: string;
  scope: "session" | "turn" | "runtime" | "delivery";
  version: number;
  data: Record<string, unknown>;
}

export interface ConversationSnapshot {
  conversation: ConversationSession;
  session_version: number;
  queue: QueueItem[];
  live_turn: Turn | null;
  turn_version: number;
  nodes: TurnNode[];
  queued_nodes: TurnNode[];
  pending_approvals: Array<Record<string, unknown>>;
  server_time: string;
}

export interface HistoryPage {
  items: Array<{ turn: Turn; nodes: TurnNode[] }>;
  next_cursor: string | null;
}

export type GatewayEventType =
  | "conversation.upserted" | "queue.updated"
  | "turn.status" | "node.delta" | "node.tool" | "node.user_steering" | "node.image"
  | "approval.requested" | "approval.resolved" | "chat.done"
  | "delivery.status" | "version_gap";

export interface GatewayEvent {
  type: string;
  data: GatewayEventData;
}
