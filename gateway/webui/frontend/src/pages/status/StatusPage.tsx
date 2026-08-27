import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import type { StatusResponse } from "@/api/types";
import { useSse } from "@/hooks/useSse";

const LOG_EVENT_TYPES = [
  "chat.started", "chat.done", "chat.error", "chat.progress",
  "session.created", "session.evicted",
  "cron.fired", "cron.done", "cron.skipped", "heartbeat.done",
];

interface LogEntry {
  id: number;
  text: string;
}

let logSeq = 0;

/** 通道详情格式化：去除 status 字段，展示 `k=v` 简洁键值（替代原始 JSON）。 */
const formatChannelDetail = (st: unknown): string => {
  if (!st || typeof st !== "object") return String(st ?? "-");
  const parts: string[] = [];
  for (const [key, value] of Object.entries(st as Record<string, unknown>)) {
    if (key === "status") continue;
    const rendered = typeof value === "object" ? JSON.stringify(value) : String(value);
    parts.push(`${key}=${rendered}`);
  }
  const text = parts.join(" ");
  return text.length > 60 ? `${text.slice(0, 60)}…` : text || "-";
};

export function StatusPage({ client = defaultApi }: { client?: ApiClient }) {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const timerRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await client.get<StatusResponse>("/api/status", { silent: true });
      setStatus(data);
    } catch { /* 静默：保留上次渲染 */ }
  }, [client]);

  useEffect(() => {
    void refresh();
    timerRef.current = window.setInterval(() => { void refresh(); }, 30000);
    return () => {
      if (timerRef.current !== null) window.clearInterval(timerRef.current);
    };
  }, [refresh]);

  const pushLog = useCallback((type: string, data: unknown) => {
    const summary = JSON.stringify(data ?? {}).slice(0, 160);
    const text = `[${new Date().toLocaleTimeString()}] ${type}: ${summary}`;
    setLog((prev) => [{ id: ++logSeq, text }, ...prev].slice(0, 50));
  }, []);

  useSse({}, (event) => {
    if (event.type === "channel.status") {
      void refresh();
    } else if (LOG_EVENT_TYPES.includes(event.type)) {
      pushLog(event.type, event.data);
    }
  });

  const s = status ?? {};
  const ex = s.executor ?? {};
  const ses = s.sessions ?? {};
  const cards: Array<[string, string]> = [
    ["会话", `${ses.active ?? 0}/${ses.max ?? 0}`],
    ["线程池", `${ex.workers ?? 0} 槽 · 排队 ${ex.pending ?? 0}`],
  ];
  if (s.scheduler && s.scheduler.present) {
    cards.push(["定时任务", `${s.scheduler.jobs ?? 0} 个 · 运行 ${(s.scheduler.running ?? []).length}`]);
  }
  if (s.heartbeat && s.heartbeat.present) {
    const hb = s.heartbeat;
    cards.push(["心跳", hb.paused ? "已暂停" : `${hb.every ?? ""} · ${hb.beats ?? 0} 轮`]);
  }

  const channels = Object.entries(s.channels ?? {});
  const busy = new Set(ses.busy ?? []);
  const sessionRows = ses.list ?? [];

  return (
    <section className="page" aria-label="状态页面">
      <h1>📊 状态面板</h1>
      <div className="cards">
        {cards.map(([k, v]) => (
          <div key={k} className="card"><div className="k">{k}</div><div className="v">{v}</div></div>
        ))}
      </div>
      <h2>通道</h2>
      <div>
        <table>
          <thead><tr><th>通道</th><th>状态</th><th>详情</th></tr></thead>
          <tbody>
            {channels.map(([name, st]) => (
              <tr key={name}>
                <td>{name}</td>
                <td><span className={`badge ${st?.status === "running" || st?.status === "ok" ? "ok" : "dim"}`}>{String(st?.status ?? "?")}</span></td>
                <td className="mono" title={JSON.stringify(st)}>{formatChannelDetail(st)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>会话明细</h2>
      <div>
        <table>
          <thead><tr><th>会话</th><th>模型</th><th>消息数</th><th>状态</th></tr></thead>
          <tbody>
            {sessionRows.map((e) => (
              <tr key={e.session_key}>
                <td>{e.session_key}</td>
                <td>{e.model ?? "-"}</td>
                <td>{String(e.message_count ?? 0)}</td>
                <td>{busy.has(e.session_key) ? <span className="badge warn">busy</span> : <span className="badge dim">idle</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>SSE 事件流（实时）</h2>
      <div id="evt-log">
        {log.length === 0 ? <div className="dim">暂无事件</div> : null}
        {log.map((entry) => <div key={entry.id}>{entry.text}</div>)}
      </div>
    </section>
  );
}
