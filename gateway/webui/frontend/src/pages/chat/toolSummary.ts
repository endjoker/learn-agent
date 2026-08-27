// 工具调用摘要：把后端 params_summary（通常是 JSON 字符串）转成一行可读的
// 折叠摘要，而不是把原始 JSON 塞进卡片头。DSH ToolRow 风格：一行可扫描。

/** 去掉工具名上的调用后缀（如 file_mgr__70de5fbe → file_mgr）。 */
export const normalizeToolName = (name: string): string => name.replace(/__[0-9a-fA-F]{6,}$/, "");

const SHORTEN = 72;

const truncate = (value: string, limit = SHORTEN): string =>
  value.length > limit ? `${value.slice(0, limit)}…` : value;

const lastSegments = (path: string, max = 2): string => {
  const parts = path.replace(/\/+$/, "").split("/").filter(Boolean);
  return parts.length > max ? `…/${parts.slice(-max).join("/")}` : path;
};

const str = (value: unknown): string => (value == null ? "" : String(value));

/**
 * 折叠摘要用：去掉 Markdown 语法标记，输出一行可读纯文本。
 * 卡片头摘要不渲染富文本（保持工具卡一行可扫描的密度），但 LLM 回复常含
 * `**加粗**` / 反引号 / 链接等标记——直接展示会出现原始语法符号，
 * 这里统一剥掉（代码块丢弃正文，行内代码/链接保留内容）。
 */
export function stripMdSummary(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/(\*\*|__|\*|~~)/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * 从工具名 + 参数 JSON 生成一行可读摘要。
 * 已知工具提取关键字段；未知工具取第一个非空字段，兜底截断原始 JSON。
 */
export function summarizeToolParams(name: string, params: string): string {
  if (!params) return "";
  let data: unknown;
  try {
    data = JSON.parse(params);
  } catch {
    return truncate(params);
  }
  if (data == null || typeof data !== "object" || Array.isArray(data)) {
    return truncate(str(data));
  }
  const obj = data as Record<string, unknown>;
  const get = (key: string) => str(obj[key]);

  switch (normalizeToolName(name)) {
    case "bash": {
      const cmd = get("command");
      return cmd ? truncate(cmd) : truncate(params);
    }
    case "read":
    case "write":
    case "edit": {
      const path = get("file_path") || get("path");
      return path ? truncate(lastSegments(path)) : truncate(params);
    }
    case "file_mgr": {
      const action = get("action");
      const path = get("path") || get("dest") || get("src");
      const label = [action, path ? lastSegments(path) : ""].filter(Boolean).join(" ");
      return label ? truncate(label) : truncate(params);
    }
    case "glob":
    case "grep": {
      const pattern = get("pattern") || get("query");
      const path = get("path");
      const label = [pattern, path ? lastSegments(path, 1) : ""].filter(Boolean).join(" @ ");
      return label ? truncate(label) : truncate(params);
    }
    case "search": {
      const query = get("query") || get("pattern");
      return query ? truncate(query) : truncate(params);
    }
    case "http":
    case "web_fetch": {
      const method = get("method");
      const url = get("url");
      const label = [method, url].filter(Boolean).join(" ");
      return label ? truncate(label) : truncate(params);
    }
    // —— Plan/Goal 结构化能力（模型可调用控制工具）——
    // 查询/创建：突出 objective/title；控制动作：突出 action。
    case "create_goal":
    case "create_plan":
    case "edit_goal": {
      const obj = get("objective") || get("title");
      return obj ? truncate(obj) : truncate(params);
    }
    case "get_goal":
    case "get_plan":
    case "list_goals":
    case "list_plans": {
      const id = get("goal_id") || get("plan_id");
      return id ? truncate(id.slice(-24)) : "查询当前执行状态";
    }
    case "update_goal":
    case "update_plan": {
      const id = get("goal_id") || get("plan_id");
      const action = get("action");
      const label = [action ? `⚡${action}` : "", id ? id.slice(-24) : ""].filter(Boolean).join(" ");
      return label ? truncate(label) : truncate(params);
    }
    case "pause_goal":
    case "resume_goal":
    case "complete_goal":
    case "cancel_goal": {
      const id = get("goal_id");
      return id ? truncate(id.slice(-24)) : truncate(params);
    }
    case "calculate": {
      const expr = get("expression") || get("query");
      return expr ? truncate(expr) : truncate(params);
    }
    default: {
      // 未知工具：取第一个非空字段值，避免整段 JSON。
      for (const value of Object.values(obj)) {
        const text = str(value);
        if (text) return truncate(text);
      }
      return truncate(params);
    }
  }
}
