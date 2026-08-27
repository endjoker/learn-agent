import { useState } from "react";

import type { PromptPreviewData } from "@/api/types";

export function PromptPreview({ data }: { data: PromptPreviewData | null }) {
  const [copied, setCopied] = useState(false);
  if (!data) {
    return <div className="ws-preview"><div className="ws-empty">点击「预览 Prompt」生成完整预览</div></div>;
  }

  const copyFullPrompt = async () => {
    try {
      await navigator.clipboard.writeText(data.full_prompt || "");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch { /* clipboard unavailable */ }
  };

  return (
    <div className="ws-preview">
      <div className="ws-field">
        <div>{`完整 Prompt：${data.total_chars} 字符 · 约 ${data.estimated_tokens} tokens`}</div>
        <div className="dim" style={{ fontSize: 11 }}>{`hash: ${(data.expected_prompt_hash || "").slice(0, 24)}…`}</div>
        <div className="ws-actions">
          <button type="button" className="btn" onClick={() => void copyFullPrompt()}>{copied ? "已复制" : "复制完整 Prompt"}</button>
          <span className="dim">Provider tools：{data.provider_tools_count ?? 0} 个{data.mcp_tools_live ? "（含已连接 MCP 工具）" : ""}</span>
        </div>
      </div>
      {(data.warnings || []).map((w, index) => (
        <div key={`${w.code}-${index}`} className="ws-warn">{`⚠️ ${w.message}`}</div>
      ))}
      <details className="ws-preview-section" open>
        <summary>完整 System Prompt · {data.total_chars} 字符</summary>
        <pre className="ws-preview-full-prompt">{data.full_prompt}</pre>
      </details>
      <details className="ws-preview-section">
        <summary>{`Provider tools schema · ${data.provider_tools_count ?? 0} 个`}</summary>
        <pre>{JSON.stringify(data.provider_tools || [], null, 2)}</pre>
      </details>
      {(data.sections || []).filter((s) => s.content).map((s) => (
        <details key={s.name} className="ws-preview-section">
          <summary>{`${s.name} · ${s.chars} 字符 · ~${s.estimated_tokens} tokens`}</summary>
          <pre>{s.content}</pre>
        </details>
      ))}
    </div>
  );
}
