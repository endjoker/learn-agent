// 工具专属行卡的模型层（优化方案 #12，借鉴 dsh ui-tool/toolviews 的
// 「模型层与视图层分离」）：把工具入参解析为可展示字段，纯函数可测。
// 视图层见 ./index.tsx 的注册表；未注册工具由调用方落 Generic 兜底。

import { normalizeToolName } from "@/pages/chat/toolSummary";

export type ToolRowKind =
  | "bash" | "read" | "write" | "edit"
  | "glob" | "grep" | "search"
  | "generic";

export interface ToolRowModel {
  kind: ToolRowKind;
  /** 主摘要：一行可扫描的核心信息（命令 / 路径 / 模式）。 */
  primary: string;
  /** 徽章：行范围、目标目录等次要信息。 */
  badge?: string;
  /** 原始命令文本（bash 行卡用等宽字体渲染）。 */
  mono?: boolean;
}

const str = (value: unknown): string => (value == null ? "" : String(value));

/** 安全解析 params_summary（通常是 JSON 字符串；非对象输入返回 null）。 */
export function parseToolInput(input: string): Record<string, unknown> | null {
  if (!input) return null;
  try {
    const data: unknown = JSON.parse(input);
    return data != null && typeof data === "object" && !Array.isArray(data)
      ? (data as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

const truncate = (value: string, limit = 72): string =>
  value.length > limit ? `${value.slice(0, limit)}…` : value;

const pathTail = (path: string, max = 2): string => {
  const parts = path.replace(/\/+$/, "").split("/").filter(Boolean);
  return parts.length > max ? `…/${parts.slice(-max).join("/")}` : path;
};

/** read 行卡行范围徽章：offset(/limit) 解析为 L 起-止；不可解析返回 undefined。 */
export function readLineBadge(obj: Record<string, unknown>): string | undefined {
  const offsetRaw = obj.offset ?? obj.start_line ?? obj.line;
  const limitRaw = obj.limit ?? obj.max_lines;
  const offset = Number(offsetRaw);
  if (!Number.isFinite(offset) || offset <= 0) return undefined;
  const limit = Number(limitRaw);
  return Number.isFinite(limit) && limit > 0
    ? `L${offset}-${offset + Math.floor(limit) - 1}`
    : `L${offset}起`;
}

/** 由工具名 + 入参构建专属行卡模型；未知工具返回 generic 兜底（primary 为空，
 *  由视图层回退到既有 summary 文本）。 */
export function buildToolRowModel(rawName: string, input: string): ToolRowModel {
  const name = normalizeToolName(rawName);
  const obj = parseToolInput(input) ?? {};
  const get = (key: string) => str(obj[key]);
  switch (name) {
    case "bash": {
      const cmd = get("command");
      if (!cmd) break;
      return { kind: "bash", primary: truncate(cmd, 96), mono: true };
    }
    case "read": {
      const path = get("file_path") || get("path");
      if (!path) break;
      return { kind: "read", primary: truncate(pathTail(path)), badge: readLineBadge(obj) };
    }
    case "write":
    case "edit": {
      const path = get("file_path") || get("path");
      if (!path) break;
      return { kind: "edit", primary: truncate(pathTail(path)) };
    }
    case "glob":
    case "grep":
    case "search": {
      const pattern = get("pattern") || get("query");
      if (!pattern) break;
      const path = get("path");
      return { kind: "search", primary: truncate(pattern), badge: path ? `@ ${pathTail(path, 1)}` : undefined };
    }
    default:
      break;
  }
  return { kind: "generic", primary: "" };
}
