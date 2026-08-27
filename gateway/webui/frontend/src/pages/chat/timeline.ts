import type { Message } from "@/api/types";
import type { TurnNode } from "@/gateway/types";

export type TimelineItem =
  | { key: string; kind: "message"; message: Message }
  | { key: string; kind: "reasoning"; text: string; live?: boolean; tokens?: number }
  | { key: string; kind: "notice"; text: string }
  | {
    key: string;
    kind: "thinking";
    /** 占位起始时刻（本地 ms），TurnStatus 计时口径一致 */
    startedAt: number;
  }
  | {
    key: string;
    kind: "projection";
    runtime_type: string;
    runtime_id: string;
    status: string;
    /** 终态完整最终回复 */
    finalText?: string;
    /** 运行中实时文本尾部（工具/思考/回答） */
    liveText?: string;
    summary?: string;
    /** 父会话同 turn 的 reasoning/tool 节点（卡片展开明细），不依赖 system 会话 */
    detailNodes?: TurnNode[];
    /** 兼容回退：旧数据仍按 system 会话拉取明细（新数据优先 detailNodes） */
    systemConversationId?: string;
    runtime_status?: string;
    /** 失败/阻断原因（metadata.error_code / metadata.message / metadata.error 容错） */
    errorCode?: string;
    message?: string;
    /** goal 轮次徽章（metadata.goal_round，后端 H-B 配套提供后消费） */
    goalRound?: number;
  }
  | {
    key: string;
    kind: "goalArchived";
    goalId: string;
    /** 归档占位文本（[Goal {goal_id} 第N轮终答已归档，详见目标页]） */
    text: string;
  }
  | {
    key: string;
    kind: "tool";
    name: string;
    input: string;
    result: string;
    orphaned: boolean;
    /** 折叠摘要（参数摘要首行，DSH ToolRow 风格） */
    summary?: string;
    /** 设计方案 17：大结果 result_ref，展开时按需读取完整内容 */
    resultRef?: string;
    /** Live (SSE-projected) tool calls carry execution state. */
    pending?: boolean;
    isError?: boolean;
    /** 错误原因（isError=true 时展示）。 */
    error?: string;
  }
  | {
    key: string;
    kind: "image";
    imageId: string;
    /** 会话图片端点（归属校验后回原图） */
    src: string;
    /** 图片名（ref 文件名，alt 用） */
    name: string;
  };
