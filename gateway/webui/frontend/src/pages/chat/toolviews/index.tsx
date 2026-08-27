// 工具专属行卡注册表（优化方案 #12，借鉴 dsh ui-tool/toolviews：每工具一张
// 专属行卡 + Generic 兜底）。不引入 slot 系统，沿用现有代码风格：
// `toolRowViews: Record<kind, FC>` 注册表 + ToolRowView 统一分发；
// 未注册（generic）时回退渲染既有 summary 文本，行为与旧版完全一致。

import type { FC } from "react";

import { buildToolRowModel, type ToolRowModel } from "@/pages/chat/toolviews/model";

export interface ToolRowViewProps {
  name: string;
  /** params_summary（通常是 JSON 字符串）。 */
  input: string;
  /** 既有 toolSummary 生成的折叠摘要（generic 兜底展示）。 */
  summary: string;
  pending?: boolean;
}

const BashRow: FC<{ model: ToolRowModel }> = ({ model }) => (
  <span className="tool-row">
    <code className="tool-row-cmd">$ {model.primary}</code>
  </span>
);

const ReadRow: FC<{ model: ToolRowModel }> = ({ model }) => (
  <span className="tool-row">
    <span className="tool-row-path">{model.primary}</span>
    {model.badge ? <span className="tool-row-badge">{model.badge}</span> : null}
  </span>
);

const EditRow: FC<{ model: ToolRowModel }> = ({ model }) => (
  <span className="tool-row">
    <span className="tool-row-path">{model.primary}</span>
    <span className="tool-row-badge">✏️ 编辑</span>
  </span>
);

const SearchRow: FC<{ model: ToolRowModel }> = ({ model }) => (
  <span className="tool-row">
    <span className="tool-row-pattern">“{model.primary}”</span>
    {model.badge ? <span className="tool-row-badge">{model.badge}</span> : null}
  </span>
);

/** 注册表：kind → 专属行卡视图；generic 不注册（调用方兜底）。 */
export const toolRowViews: Partial<Record<ToolRowModel["kind"], FC<{ model: ToolRowModel }>>> = {
  bash: BashRow,
  read: ReadRow,
  edit: EditRow,
  write: EditRow,
  glob: SearchRow,
  grep: SearchRow,
  search: SearchRow,
};

/** 工具行摘要分发入口：命中注册表渲染专属行卡，否则回退 summary 文本。 */
export function ToolRowView({ name, input, summary, pending }: ToolRowViewProps) {
  const model = buildToolRowModel(name, input);
  const View = toolRowViews[model.kind];
  if (View && model.primary) return <View model={model} />;
  const fallbackText = summary || (pending ? "执行中…" : "");
  return fallbackText ? <span className="tool-summary">{fallbackText}</span> : null;
}
