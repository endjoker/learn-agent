import { useEffect, useState } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import type { SessionsResponse, WorkspaceListResponse, WorkspaceSessionsResponse } from "@/api/types";

export const DEFAULT_SESSION_KEY = "webui:default";

export interface SessionScopeSelectProps {
  client?: ApiClient;
  value: string;
  onChange: (sessionKey: string) => void;
}

/** Session scope selector used by Plan/Goal pages (legacy runtime-scope). */
export function SessionScopeSelect({ client = defaultApi, value, onChange }: SessionScopeSelectProps) {
  const [sessions, setSessions] = useState<string[]>([]);
  const [fallback, setFallback] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      const keys: string[] = [];
      try {
        const data = await client.get<SessionsResponse>("/api/sessions", { silent: true });
        keys.push(...(data.sessions ?? []).map((s) => s.session_key).filter(Boolean));
      } catch { /* 继续加载 workspace 会话 */ }
      // Workspace 会话不写 sessions_map.json，单独从工作区 API 拉取合并，
      // 否则挂在工作区会话下的 Plan/Goal 永远无法在下拉里选中。
      try {
        const ws = await client.get<WorkspaceListResponse>("/api/workspaces", { silent: true });
        for (const w of ws.workspaces ?? []) {
          try {
            const sess = await client.get<WorkspaceSessionsResponse>(
              "/api/workspaces/" + encodeURIComponent(w.workspace_id) + "/sessions?status=active", { silent: true });
            keys.push(...(sess.sessions ?? []).map((s) => s.session_key).filter(Boolean));
          } catch { /* 单个工作区失败不影响其余 */ }
        }
      } catch { /* workspace API 不可用时退化为原行为 */ }
      if (!active) return;
      const unique = Array.from(new Set(keys));
      setSessions(unique);
      if (!unique.includes(value) && unique.length > 0) setFallback(unique[0] ?? null);
    };
    void load();
    return () => { active = false; };
  }, [client, value]);

  useEffect(() => {
    if (fallback && fallback !== value) onChange(fallback);
  }, [fallback, onChange, value]);

  const options = sessions.length > 0 ? sessions : [value];
  return (
    <div className="runtime-scope">
      <label>会话</label>
      <select className="runtime-scope-input" aria-label="会话" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((key) => <option key={key} value={key}>{key}</option>)}
      </select>
    </div>
  );
}
