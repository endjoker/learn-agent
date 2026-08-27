import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import type { McpResponse, McpServerConfig } from "@/api/types";
import { confirmDialog } from "@/components/confirm";
import { FormField, Modal } from "@/components/Modal";
import { toast } from "@/components/toast";
import { useSse } from "@/hooks/useSse";

const TRANSPORTS = ["stdio", "streamable", "sse", "http"];

const parseKV = (text: string): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const line of String(text).split("\n")) {
    const i = line.indexOf("=");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
};

function LiveBadge({ live }: { live: { sessions?: number; initialized?: boolean; tools?: number } }) {
  if (!live.sessions) return <span className="badge dim">未连接</span>;
  if (live.initialized) return <span className="badge ok">{`${live.sessions} 会话 · ${live.tools ?? 0} 工具`}</span>;
  return <span className="badge warn">{`${live.sessions} 会话 · 连接中`}</span>;
}

interface EditorState {
  name: string;
  transport: string;
  command: string;
  args: string;
  url: string;
  env: string;
  trust: boolean;
  enabled: boolean;
}

const emptyEditor = (): EditorState => ({
  name: "", transport: "stdio", command: "", args: "", url: "", env: "", trust: false, enabled: true,
});

const fromServer = (s: McpServerConfig): EditorState => ({
  name: s.name ?? "",
  transport: s.transport ?? "stdio",
  command: s.command ?? "",
  args: (s.args ?? []).join(" "),
  url: s.url ?? "",
  env: Object.entries(s.env ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"),
  trust: Boolean(s.trust),
  enabled: s.enabled !== false,
});

export function McpPage({ client = defaultApi }: { client?: ApiClient }) {
  const [servers, setServers] = useState<McpServerConfig[]>([]);
  const [live, setLive] = useState<Record<string, { sessions?: number; initialized?: boolean; tools?: number }>>({});
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [editingName, setEditingName] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await client.get<McpResponse>("/api/mcp", { silent: true });
      setServers(data.servers ?? []);
      setLive(data.live ?? {});
    } catch { /* silent */ }
  }, [client]);

  useEffect(() => { void refresh(); }, [refresh]);
  // SSE 刷新节流：只关注 mcp.changed 事件，且 1s 内最多触发一次刷新
  //（避免其它事件类型/事件突发导致 /api/mcp 被高频拉取）。
  const refreshTimerRef = useRef<number | null>(null);
  useSse({}, (event) => {
    if (event.type !== "mcp.changed") return;
    if (refreshTimerRef.current !== null) return;
    refreshTimerRef.current = window.setTimeout(() => { refreshTimerRef.current = null; }, 1000);
    void refresh();
  });
  useEffect(() => () => {
    if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
  }, []);

  const openEditor = (existing: McpServerConfig | null) => {
    setEditingName(existing?.name ?? null);
    setEditor(existing ? fromServer(existing) : emptyEditor());
  };

  const save = async () => {
    if (!editor) return;
    const body: McpServerConfig = {
      name: editor.name.trim(),
      transport: editor.transport,
      enabled: editor.enabled,
      trust: editor.trust,
    };
    if (editor.transport === "stdio") {
      body.command = editor.command.trim();
      body.args = editor.args.trim().split(/\s+/).filter(Boolean);
    } else {
      body.url = editor.url.trim();
    }
    body.env = parseKV(editor.env);
    if (!body.name) { toast("name 必填", "err"); return; }
    try {
      if (editingName === null) {
        await client.post("/api/mcp/servers", body);
      } else {
        await client.put(`/api/mcp/servers/${encodeURIComponent(editingName)}`, body);
      }
      toast("已写入配置，点 [应用到运行中会话] 立即生效", "ok");
      setEditor(null);
      void refresh();
    } catch { /* silent */ }
  };

  const reconnect = async (name: string) => {
    try {
      await client.post(`/api/mcp/servers/${encodeURIComponent(name)}/reconnect`);
      toast(`已广播重连 ${name}`, "ok");
    } catch { /* silent */ }
  };

  const remove = async (name: string) => {
    const ok = await confirmDialog(`删除 MCP 服务器 ${name}？`);
    if (!ok) return;
    try {
      await client.delete(`/api/mcp/servers/${encodeURIComponent(name)}`);
      toast("已删除，点 [应用] 生效", "ok");
      void refresh();
    } catch { /* silent */ }
  };

  const apply = async () => {
    try {
      const data = await client.post<{ queued?: number }>("/api/mcp/apply");
      toast(`已广播 /mcp reload 到 ${data.queued ?? 0} 个会话`, "ok");
    } catch { /* silent */ }
  };

  const isStdio = editor?.transport === "stdio";

  return (
    <section className="page" aria-label="MCP 页面">
      <div className="page-head">
        <h1>🔌 MCP 服务器</h1>
        <button type="button" className="btn primary" onClick={() => openEditor(null)}>＋ 添加</button>
        <button type="button" className="btn" onClick={() => void apply()}>🔄 应用到运行中会话</button>
      </div>
      <div className="mcp-list">
        {servers.length === 0 ? <div className="placeholder">暂无 MCP 服务器，点右上 [＋ 添加]</div> : (
          <table>
            <thead>
              <tr><th>名称</th><th>传输</th><th>command / url</th><th>env</th><th>实时状态</th><th>操作</th></tr>
            </thead>
            <tbody>
              {servers.map((s) => {
                const target = s.transport === "stdio"
                  ? `${s.command ?? ""} ${(s.args ?? []).join(" ")}`.trim()
                  : (s.url ?? "");
                return (
                  <tr key={s.name}>
                    <td><b>{s.name}</b>{s.trust ? <span className="dim"> · trust</span> : null}</td>
                    <td>{s.transport ?? ""}</td>
                    <td className="mono">{target}</td>
                    <td className="mono">{s.env ? JSON.stringify(s.env) : "-"}</td>
                    <td><LiveBadge live={live[s.name] ?? {}} /></td>
                    <td className="ops">
                      <button type="button" className="btn" onClick={() => void reconnect(s.name)}>重连</button>
                      <button type="button" className="btn" onClick={() => openEditor(s)}>编辑</button>
                      <button type="button" className="btn danger" onClick={() => void remove(s.name)}>删除</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      {editor ? (
        <Modal
          title={editingName === null ? "添加 MCP 服务器" : `编辑 ${editingName}`}
          actions={(
            <>
              <button type="button" className="btn primary" onClick={() => void save()}>保存</button>
              <button type="button" className="btn" onClick={() => setEditor(null)}>取消</button>
            </>
          )}
        >
          <FormField label="名称"><input value={editor.name} disabled={editingName !== null} onChange={(e) => setEditor({ ...editor, name: e.target.value })} /></FormField>
          <FormField label="传输">
            <select value={editor.transport} onChange={(e) => setEditor({ ...editor, transport: e.target.value })}>
              {TRANSPORTS.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </FormField>
          {isStdio ? (
            <div className="form-row">
              <input value={editor.command} placeholder="如 node" onChange={(e) => setEditor({ ...editor, command: e.target.value })} />
              <input value={editor.args} placeholder="空格分隔参数" onChange={(e) => setEditor({ ...editor, args: e.target.value })} />
            </div>
          ) : (
            <div className="form-row">
              <input value={editor.url} placeholder="http://…" onChange={(e) => setEditor({ ...editor, url: e.target.value })} />
            </div>
          )}
          <FormField label="env"><textarea rows={3} placeholder="KEY=value 每行一个" value={editor.env} onChange={(e) => setEditor({ ...editor, env: e.target.value })} /></FormField>
          <FormField label=""><label><input type="checkbox" checked={editor.trust} onChange={(e) => setEditor({ ...editor, trust: e.target.checked })} /> 信任（工具调用免确认）</label></FormField>
          <FormField label=""><label><input type="checkbox" checked={editor.enabled} onChange={(e) => setEditor({ ...editor, enabled: e.target.checked })} /> 启用该服务器</label></FormField>
        </Modal>
      ) : null}
    </section>
  );
}
