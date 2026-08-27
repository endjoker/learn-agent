import { useCallback, useEffect, useMemo, useState } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import type {
  CronHistoryEntry, CronHistoryResponse, CronJobConfig, CronJobItem,
  CronJobsResponse, SchedulerChannelsResponse,
} from "@/api/types";
import { confirmDialog } from "@/components/confirm";
import { FormField, Modal } from "@/components/Modal";
import { toast } from "@/components/toast";
import { useSse } from "@/hooks/useSse";

interface DeliverState {
  mode: string;
  channel: string;
  announceTarget: string;
  webhookTarget: string;
  customTarget: string;
}

interface EditorState {
  name: string;
  schedule: string;
  prompt: string;
  session: string;
  timeout: number;
  deliver: DeliverState;
}

const emptyEditor = (): EditorState => ({
  name: "",
  schedule: "0 9 * * 1-5",
  prompt: "",
  session: "isolated",
  timeout: 600,
  deliver: { mode: "none", channel: "", announceTarget: "", webhookTarget: "", customTarget: "" },
});

const fromJob = (j: CronJobConfig): EditorState => {
  const deliver = j.deliver ?? {};
  return {
    name: j.name,
    schedule: j.schedule ?? "",
    prompt: j.prompt ?? "",
    session: j.session ?? "isolated",
    timeout: j.timeout ?? 600,
    deliver: {
      mode: deliver.mode ?? "none",
      channel: deliver.channel ?? "",
      announceTarget: deliver.mode === "announce" ? (deliver.target ?? "") : "",
      webhookTarget: deliver.mode === "webhook" ? (deliver.target ?? "") : "",
      customTarget: deliver.target ?? "",
    },
  };
};

export function CronPage({ client = defaultApi }: { client?: ApiClient }) {
  const [jobs, setJobs] = useState<CronJobItem[]>([]);
  const [history, setHistory] = useState<CronHistoryEntry[]>([]);
  const [channels, setChannels] = useState<Array<{ channel: string; hint?: string }>>([]);
  const [webhooks, setWebhooks] = useState<string[]>([]);
  const [targets, setTargets] = useState<Record<string, string[]>>({});
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [editingName, setEditingName] = useState<string | null>(null);

  const loadChannels = useCallback(async () => {
    try {
      const data = await client.get<SchedulerChannelsResponse>("/api/scheduler/channels", { silent: true });
      setChannels(data.channels ?? []);
      setWebhooks(data.webhooks ?? []);
      setTargets(data.targets ?? {});
    } catch {
      setChannels([]);
      setWebhooks([]);
      setTargets({});
    }
  }, [client]);

  const refresh = useCallback(async () => {
    try {
      const data = await client.get<CronJobsResponse>("/api/scheduler/jobs", { silent: true });
      setJobs(data.jobs ?? []);
    } catch { /* silent */ }
    try {
      const data = await client.get<CronHistoryResponse>("/api/scheduler/history", { silent: true });
      setHistory(data.history ?? []);
    } catch { /* silent */ }
  }, [client]);

  useEffect(() => {
    void loadChannels();
    void refresh();
  }, [loadChannels, refresh]);
  useSse({}, (event) => { if (event.type === "cron.changed") void refresh(); });

  const action = useCallback(async (name: string, actionName: "run" | "pause" | "resume") => {
    try {
      const data = await client.post<{ reply?: string }>(`/api/scheduler/jobs/${encodeURIComponent(name)}/${actionName}`);
      toast(data.reply ?? "ok", "ok");
      void refresh();
    } catch { /* silent */ }
  }, [client, refresh]);

  const del = async (name: string) => {
    const ok = await confirmDialog(`删除定时任务 ${name}？`);
    if (!ok) return;
    try {
      await client.delete(`/api/scheduler/jobs/${encodeURIComponent(name)}`);
      toast("已删除", "ok");
      void refresh();
    } catch { /* silent */ }
  };

  const openEditor = (existing: CronJobConfig | null) => {
    setEditingName(existing?.name ?? null);
    setEditor(existing ? fromJob(existing) : emptyEditor());
  };

  const save = async () => {
    if (!editor) return;
    const m = editor.deliver.mode;
    let target: string | undefined;
    if (m === "announce") {
      target = (editor.deliver.announceTarget === "__custom__" || editor.deliver.announceTarget === "")
        ? editor.deliver.customTarget.trim()
        : editor.deliver.announceTarget;
    } else if (m === "webhook") {
      target = (editor.deliver.webhookTarget === "__custom__" || editor.deliver.webhookTarget === "")
        ? editor.deliver.customTarget.trim()
        : editor.deliver.webhookTarget;
    }
    const body: CronJobConfig = {
      name: editor.name.trim(),
      schedule: editor.schedule.trim(),
      prompt: editor.prompt.trim(),
      session: editor.session,
      deliver: {
        mode: m,
        channel: m === "announce" ? editor.deliver.channel : undefined,
        target: target || undefined,
      },
      timeout: Number(editor.timeout) || 600,
      enabled: true,
    };
    if (!body.name || !body.schedule || !body.prompt) { toast("name/schedule/prompt 必填", "err"); return; }
    if (m === "announce" && !(body.deliver?.channel && body.deliver.target)) { toast("announce 需选择通道和目标", "err"); return; }
    try {
      await client.post("/api/scheduler/jobs", body);
      toast(editingName === null ? "已创建" : "已更新", "ok");
      setEditor(null);
      void refresh();
    } catch { /* silent */ }
  };

  const deliverText = (j: CronJobItem) => {
    const d = j.deliver;
    return d ? `${d.mode ?? "none"}${d.channel ? `→${d.channel}` : ""}` : "none";
  };

  const recentHistory = useMemo(() => history.slice(-10).reverse(), [history]);

  const announceOptions = useMemo(() => {
    const channel = editor?.deliver.channel ?? "";
    const list = [...((targets[channel] ?? []))];
    const cur = editor?.deliver.announceTarget ?? "";
    if (cur && cur !== "__custom__" && !list.includes(cur)) list.push(cur);
    return list;
  }, [editor?.deliver.announceTarget, editor?.deliver.channel, targets]);

  const webhookOptions = useMemo(() => {
    const list = [...webhooks];
    const cur = editor?.deliver.webhookTarget ?? "";
    if (cur && cur !== "__custom__" && !list.includes(cur)) list.push(cur);
    return list;
  }, [editor?.deliver.webhookTarget, webhooks]);

  const showCustomTarget =
    (editor?.deliver.mode === "announce" && (editor.deliver.announceTarget === "__custom__" || editor.deliver.announceTarget === ""))
    || (editor?.deliver.mode === "webhook" && (editor.deliver.webhookTarget === "__custom__" || editor.deliver.webhookTarget === ""));

  return (
    <section className="page" aria-label="定时任务页面">
      <div className="page-head">
        <h1>⏰ 定时任务</h1>
        <button type="button" className="btn primary" onClick={() => openEditor(null)}>＋ 添加任务</button>
      </div>
      <div>
        {jobs.length === 0 ? <div className="placeholder">暂无定时任务，点 [＋ 添加任务]</div> : (
          <table>
            <thead><tr><th>名称</th><th>schedule</th><th>投递</th><th>会话</th><th>最近状态</th><th>操作</th></tr></thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.name}>
                  <td>
                    <b>{j.name}</b>
                    {j.running ? <span className="badge warn">进行中</span> : null}
                    {j.paused ? <span className="badge dim">暂停</span> : null}
                    {!j.enabled ? <span className="badge dim">禁用</span> : null}
                  </td>
                  <td className="mono">{j.schedule ?? ""}</td>
                  <td>{deliverText(j)}</td>
                  <td>{j.session ?? "isolated"}</td>
                  <td>{`${j.last_status ?? "-"} | ${j.runs ?? 0}次${j.failures ? `/fail ${j.failures}` : ""}`}</td>
                  <td className="ops">
                    <button type="button" className="btn" onClick={() => void action(j.name, j.paused ? "resume" : "pause")}>{j.paused ? "恢复" : "暂停"}</button>
                    <button type="button" className="btn primary" onClick={() => void action(j.name, "run")}>▶ 手动触发</button>
                    <button type="button" className="btn" onClick={() => openEditor(j)}>编辑</button>
                    <button type="button" className="btn danger" onClick={() => void del(j.name)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {recentHistory.length ? (
        <>
          <h2>最近执行历史</h2>
          <table>
            <thead><tr><th>时间</th><th>任务</th><th>状态</th><th>耗时</th><th>触发方式</th></tr></thead>
            <tbody>
              {recentHistory.map((h, index) => (
                <tr key={`${h.job}-${h.at}-${index}`}>
                  <td className="mono">{h.at ?? ""}</td>
                  <td>{h.job}</td>
                  <td><span className={`badge ${h.status === "ok" ? "ok" : h.status === "error" ? "err" : "dim"}`}>{h.status ?? ""}</span></td>
                  <td>{`${h.duration_s ?? 0}s`}</td>
                  <td>{h.trigger ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
      {editor ? (
        <Modal
          title={editingName === null ? "添加定时任务" : `编辑 ${editingName}`}
          actions={(
            <>
              <button type="button" className="btn primary" onClick={() => void save()}>保存</button>
              <button type="button" className="btn" onClick={() => setEditor(null)}>取消</button>
            </>
          )}
        >
          <FormField label="名称"><input value={editor.name} disabled={editingName !== null} onChange={(e) => setEditor({ ...editor, name: e.target.value })} /></FormField>
          <FormField label="cron 表达式"><input value={editor.schedule} onChange={(e) => setEditor({ ...editor, schedule: e.target.value })} /></FormField>
          <FormField label="prompt"><textarea rows={4} value={editor.prompt} onChange={(e) => setEditor({ ...editor, prompt: e.target.value })} /></FormField>
          <FormField label="会话模式">
            <select value={editor.session} onChange={(e) => setEditor({ ...editor, session: e.target.value })}>
              <option value="isolated">isolated（每次全新上下文）</option>
              <option value="persist">persist（固定会话累积）</option>
            </select>
          </FormField>
          <FormField label="投递模式">
            <select value={editor.deliver.mode} onChange={(e) => setEditor({ ...editor, deliver: { ...editor.deliver, mode: e.target.value } })}>
              <option value="none">none（仅日志）</option>
              <option value="announce">announce（推送到通道）</option>
              <option value="webhook">webhook（HTTP POST）</option>
            </select>
          </FormField>
          {editor.deliver.mode === "announce" ? (
            <>
              <FormField label="通道">
                <select value={editor.deliver.channel} onChange={(e) => setEditor({ ...editor, deliver: { ...editor.deliver, channel: e.target.value, announceTarget: "" } })}>
                  {channels.length === 0 ? <option value="">（无已启用通道）</option> : null}
                  {channels.map((c) => <option key={c.channel} value={c.channel}>{`${c.channel}${c.hint ? `（${c.hint}）` : ""}`}</option>)}
                </select>
              </FormField>
              <FormField label="announce 目标">
                <select value={editor.deliver.announceTarget} onChange={(e) => setEditor({ ...editor, deliver: { ...editor.deliver, announceTarget: e.target.value } })}>
                  <option value="">{announceOptions.length ? "（选择已有目标）" : "（无已有目标）"}</option>
                  {announceOptions.map((u) => <option key={u} value={u}>{u}</option>)}
                  <option value="__custom__">自定义…</option>
                </select>
              </FormField>
            </>
          ) : null}
          {editor.deliver.mode === "webhook" ? (
            <FormField label="webhook 目标">
              <select value={editor.deliver.webhookTarget} onChange={(e) => setEditor({ ...editor, deliver: { ...editor.deliver, webhookTarget: e.target.value } })}>
                <option value="">{webhookOptions.length ? "（选择已有 webhook）" : "（无已有 webhook）"}</option>
                {webhookOptions.map((u) => <option key={u} value={u}>{u}</option>)}
                <option value="__custom__">自定义…</option>
              </select>
            </FormField>
          ) : null}
          {showCustomTarget ? (
            <FormField label="自定义目标">
              <input value={editor.deliver.customTarget} placeholder="announce 填 chat_id（oc_ 开头）；webhook 填 URL" onChange={(e) => setEditor({ ...editor, deliver: { ...editor.deliver, customTarget: e.target.value } })} />
            </FormField>
          ) : null}
          <FormField label="超时（秒）"><input type="number" value={editor.timeout} onChange={(e) => setEditor({ ...editor, timeout: Number(e.target.value) })} /></FormField>
        </Modal>
      ) : null}
    </section>
  );
}
