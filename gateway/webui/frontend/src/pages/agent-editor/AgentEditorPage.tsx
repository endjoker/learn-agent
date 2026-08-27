import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import { ApiError } from "@/api/client";
import type {
  AgentCatalog, AgentListResponse, AgentProfile, AgentReferencesResponse,
  PromptPreviewData,
} from "@/api/types";
import { confirmDialog } from "@/components/confirm";
import { PromptPreview } from "@/components/PromptPreview";
import { SearchSelector, type SearchSelectorItem } from "@/components/SearchSelector";
import { toast } from "@/components/toast";

const PROMPT_MAX_CHARS = 16000;

const PERMISSION_MODES = [
  ["readonly", "只读 · 仅阅读与搜索"],
  ["ask", "逐次确认 · 每次操作前确认"],
  ["allow", "允许 · 执行已授权操作"],
  ["unreviewed", "免审查 · 跳过审批"],
] as const;

const blankProfile = (): AgentProfile => ({
  profile_id: "",
  name: "",
  description: "",
  system_prompt: "",
  tools: [],
  skills: [],
  mcp_servers: [],
  default_model: "",
  permission_mode: "ask",
  chat_mode: "chat",
  max_steps: 100,
  include_tools: [],
  exclude_tools: [],
  version: 1,
});

const cloneProfile = (profile: AgentProfile): AgentProfile => JSON.parse(JSON.stringify(profile)) as AgentProfile;

const safeDecode = (value: string): string => {
  try { return decodeURIComponent(value); } catch { return value; }
};

const parseQueryId = (): string => {
  const q = (window.location.hash.split("?")[1] ?? "");
  for (const pair of q.split("&")) {
    if (!pair) continue;
    const [k, v] = pair.split("=");
    if (safeDecode(k ?? "") === "id") return safeDecode(v ?? "");
  }
  return "";
};

export function AgentEditorPage({ client = defaultApi }: { client?: ApiClient }) {
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [serverProfile, setServerProfile] = useState<AgentProfile | null>(null);
  const [draft, setDraft] = useState<AgentProfile | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [previewData, setPreviewData] = useState<PromptPreviewData | null>(null);
  const [versionConflict, setVersionConflict] = useState(false);
  const [references, setReferences] = useState<Array<{ workspace_id: string; name: string; status: string }>>([]);
  const [catalogs, setCatalogs] = useState<AgentCatalog>({ tools: [], skills: [], mcp: { servers: [] }, models: [] });
  const [filter, setFilter] = useState("");
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;
  const selectedIdRef = useRef<string | null>(null);

  const loadProfile = useCallback(async (id: string): Promise<boolean> => {
    try {
      const profile = await client.get<AgentProfile>(`/api/agents/${encodeURIComponent(id)}`, { silent: true });
      setServerProfile(profile);
      setDraft(cloneProfile(profile));
      setPreviewData(null);
      return true;
    } catch {
      // 加载失败：清空草稿，避免残留上一智能体的内容（"选择新智能体看到旧草稿"）
      setServerProfile(null);
      setDraft(null);
      setPreviewData(null);
      return false;
    }
  }, [client]);

  const refreshList = useCallback(async (selectAfter?: string | null) => {
    try {
      const [active, archived] = await Promise.all([
        client.get<AgentListResponse>("/api/agents?status=active&limit=200", { silent: true }),
        client.get<AgentListResponse>("/api/agents?status=archived&limit=200", { silent: true }),
      ]);
      const list = [...(active.agents ?? []), ...(archived.agents ?? [])];
      setAgents(list);
      if (selectAfter === null) return;
      if (selectAfter) {
        selectedIdRef.current = selectAfter;
        setSelectedId(selectAfter);
        setDirty(false);
        setVersionConflict(false);
        await loadProfile(selectAfter);
      } else {
        // Auto-select: keep the current selection when it still exists,
        // otherwise fall back to the first agent (legacy refreshList semantics).
        const current = selectedIdRef.current;
        const target = current && list.some((a) => a.profile_id === current)
          ? current
          : (list[0]?.profile_id ?? null);
        if (target) {
          selectedIdRef.current = target;
          setSelectedId(target);
          setDirty(false);
          setVersionConflict(false);
          await loadProfile(target);
        } else {
          selectedIdRef.current = null;
          setSelectedId(null);
          setServerProfile(null);
          setDraft(null);
        }
      }
    } catch (error) {
      setAgents([]);
      selectedIdRef.current = null;
      setSelectedId(null);
      setServerProfile(null);
      setDraft(null);
      toast(`智能体列表加载失败: ${error instanceof Error ? error.message : "未知错误"}`, "err");
    }
  }, [client, loadProfile]);

  // Initial load + route query
  useEffect(() => {
    let active = true;
    void client.get<AgentCatalog>("/api/agents/catalog", { silent: true }).then((data) => {
      if (active) setCatalogs(data);
    }).catch(() => {
      if (active) toast("Catalog 加载失败（局部降级）", "err");
    });
    const id = parseQueryId();
    void refreshList(id || undefined);
    return () => { active = false; };
  }, [client, refreshList]);

  // beforeunload protection
  useEffect(() => {
    const before = (event: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, []);

  const select = useCallback(async (id: string) => {
    if (dirtyRef.current) {
      const ok = await confirmDialog("当前有未保存的修改，切换将丢失。继续？", { okText: "继续" });
      if (!ok) return;
    }
    selectedIdRef.current = id;
    setSelectedId(id);
    setDirty(false);
    setVersionConflict(false);
    const okLoaded = await loadProfile(id);
    if (!okLoaded) {
      // 加载失败：清空选中与草稿并提示，避免残留上一智能体草稿
      toast("智能体加载失败，请重试", "err");
      selectedIdRef.current = null;
      setSelectedId(null);
      setDirty(false);
    }
  }, [loadProfile]);

  const patchDraft = useCallback((patch: Partial<AgentProfile>) => {
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev));
    setDirty(true);
  }, []);

  const newProfile = useCallback(() => {
    if (dirtyRef.current) {
      toast("请先保存或放弃当前修改", "err");
      return;
    }
    setServerProfile(null);
    setDraft(blankProfile());
    selectedIdRef.current = null;
    setSelectedId(null);
    setDirty(true);
    setVersionConflict(false);
    setPreviewData(null);
  }, []);

  const save = useCallback(async () => {
    if (!draft) return;
    if (saving) return;
    if (!draft.name || !draft.name.trim()) { toast("名称不能为空", "err"); return; }
    setSaving(true);
    try {
      let saved: AgentProfile;
      if (draft.profile_id) {
        saved = await client.put<AgentProfile>(`/api/agents/${encodeURIComponent(draft.profile_id)}`, {
          ...draft,
          version: serverProfile ? serverProfile.version : draft.version,
        }, { silent: true });
      } else {
        saved = await client.post<AgentProfile>("/api/agents", draft);
      }
      if (!saved || (saved as unknown as { error?: string }).error) return;
      setServerProfile(saved);
      setDraft(cloneProfile(saved));
      selectedIdRef.current = saved.profile_id;
      setSelectedId(saved.profile_id);
      setDirty(false);
      setVersionConflict(false);
      toast("已保存", "ok");
      void refreshList(saved.profile_id);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setVersionConflict(true);
        toast("服务器已有新版本，请重新加载后编辑", "err");
      } else {
        toast(`保存失败: ${error instanceof Error ? error.message : String(error)}`, "err");
      }
    } finally {
      setSaving(false);
    }
  }, [client, draft, refreshList, saving, serverProfile]);

  const remove = useCallback(async () => {
    if (!draft || !draft.profile_id || draft.is_system) return;
    const ok = await confirmDialog("删除该智能体？已保存的运行快照仍会保留；被工作区引用时无法删除。", { okText: "删除" });
    if (!ok) return;
    try {
      await client.request("DELETE", `/api/agents/${encodeURIComponent(draft.profile_id)}`, {
        version: serverProfile ? serverProfile.version : draft.version,
      });
      toast("已删除", "ok");
      selectedIdRef.current = null;
      setSelectedId(null);
      setServerProfile(null);
      setDraft(null);
      setDirty(false);
      setVersionConflict(false);
      await refreshList(null);
    } catch (error) {
      toast(`删除失败: ${error instanceof Error ? error.message : String(error)}`, "err");
    }
  }, [client, draft, refreshList, serverProfile]);

  const duplicate = useCallback(async () => {
    if (!draft || !draft.profile_id) return;
    try {
      const dup = await client.post<AgentProfile>(`/api/agents/${encodeURIComponent(draft.profile_id)}/duplicate`, {});
      setDirty(false);
      toast(`已复制为「${dup.name}」`, "ok");
      await refreshList(dup.profile_id);
    } catch (error) {
      toast(`复制失败: ${error instanceof Error ? error.message : String(error)}`, "err");
    }
  }, [client, draft, refreshList]);

  const toggleEnabled = useCallback(async () => {
    if (!draft || !draft.profile_id || draft.is_system) return;
    const enabling = draft.status === "archived";
    const action = enabling ? "启用" : "停用";
    const message = enabling
      ? "启用该智能体？"
      : "停用该智能体？停用后不能被新工作区或会话选择，但保留配置与版本记录。";
    const ok = await confirmDialog(message, { okText: action });
    if (!ok) return;
    try {
      const updated = await client.post<AgentProfile>(
        `/api/agents/${encodeURIComponent(draft.profile_id)}/${enabling ? "activate" : "archive"}`,
        { version: serverProfile ? serverProfile.version : draft.version },
      );
      setServerProfile(updated);
      setDraft(cloneProfile(updated));
      setDirty(false);
      toast(enabling ? "已启用" : "已停用", "ok");
      await refreshList(updated.profile_id);
    } catch (error) {
      toast(`${action}失败: ${error instanceof Error ? error.message : String(error)}`, "err");
    }
  }, [client, draft, refreshList, serverProfile]);

  const preview = useCallback(async () => {
    if (!draft) return;
    setPreviewing(true);
    try {
      const data = await client.post<PromptPreviewData>("/api/agents/preview", { profile: draft });
      setPreviewData(data);
    } catch (error) {
      toast(`预览失败: ${error instanceof Error ? error.message : String(error)}`, "err");
    } finally {
      setPreviewing(false);
    }
  }, [client, draft]);

  // References
  useEffect(() => {
    let active = true;
    if (!draft?.profile_id) { setReferences([]); return undefined; }
    void client.get<AgentReferencesResponse>(`/api/agents/${encodeURIComponent(draft.profile_id)}/references`, { silent: true })
      .then((data) => { if (active) setReferences(data.references ?? []); })
      .catch(() => undefined);
    return () => { active = false; };
  }, [client, draft?.profile_id]);

  const shownAgents = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return agents;
    return agents.filter((a) => (a.name ?? "").toLowerCase().includes(q) || (a.description ?? "").toLowerCase().includes(q));
  }, [agents, filter]);

  const toolItems: SearchSelectorItem[] = useMemo(
    () => (catalogs.tools ?? []).map((t) => ({ id: t.name, name: t.name, risk: t.risk, available: t.available })),
    [catalogs.tools],
  );
  const skillItems: SearchSelectorItem[] = useMemo(
    () => (catalogs.skills ?? []).map((s) => ({ id: s.id ?? s.name, name: s.name, description: s.description ?? "" })),
    [catalogs.skills],
  );
  const mcpItems: SearchSelectorItem[] = useMemo(
    () => ((catalogs.mcp ?? {}).servers ?? []).map((s) => ({
      id: s.name,
      name: s.available ? s.name : `${s.name}（未连接；运行时会尝试连接）`,
      available: true,
      unavailable_reason: s.available ? "" : "当前未连接；会在运行时按配置建立连接",
    })),
    [catalogs.mcp],
  );

  const toggleList = useCallback((key: "tools" | "skills" | "mcp_servers") => (id: string, checked: boolean) => {
    setDraft((prev) => {
      if (!prev) return prev;
      const list = prev[key] ?? [];
      const next = checked
        ? (list.includes(id) ? list : [...list, id])
        : list.filter((x) => x !== id);
      return { ...prev, [key]: next };
    });
    setDirty(true);
  }, []);

  const isBuiltin = Boolean(draft?.is_system);
  const promptLen = draft?.system_prompt?.length ?? 0;

  return (
    <section className="ws-page agent-editor-page" aria-label="智能体编辑页面">
      <div className="ws-layout ws-layout-2col">
        <aside className="ws-panel ws-left">
          <div className="ws-panel-head">
            <span>智能体</span>
            <button type="button" className="btn" onClick={newProfile}>＋ 新建</button>
          </div>
          <div className="ws-panel-body">
            <input
              type="text"
              placeholder="搜索智能体…"
              style={{ width: "100%", boxSizing: "border-box", marginBottom: 8 }}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            {shownAgents.length === 0 ? (
              <div className="ws-empty">{filter ? "无匹配智能体" : "暂无智能体，点击「新建」创建第一个"}</div>
            ) : null}
            {shownAgents.map((p) => {
              const active = p.profile_id === selectedId;
              const capabilities = `${(p.tools ?? []).length} 工具 · ${(p.skills ?? []).length} 技能 · ${(p.mcp_servers ?? []).length} MCP`;
              return (
                <div
                  key={p.profile_id}
                  className={`ws-item ws-agent-nav-item${active ? " active" : ""}`}
                  title={`${p.name}\n${p.description ?? ""}\n${capabilities}`}
                  onClick={() => void select(p.profile_id)}
                >
                  <div className="ws-item-title">
                    <span>{p.name}</span>
                    {p.is_system ? <span className="ws-tag system">内置</span> : null}
                    {p.status === "archived"
                      ? <span className="ws-tag disabled">已停用</span>
                      : <span className="ws-tag enabled">已启用</span>}
                  </div>
                  <div className="ws-item-sub">{capabilities}</div>
                </div>
              );
            })}
          </div>
        </aside>
        <main className="ws-panel">
          <div className="ws-panel-head">
            <span>{draft?.profile_id ? `编辑：${draft.name}` : "新建智能体"}</span>
            {isBuiltin ? (
              <>
                <span className="ws-tag system">内置</span>
                <span className="dim" style={{ fontSize: 11 }}>内置默认智能体可以编辑与保存；不允许删除或归档</span>
              </>
            ) : null}
          </div>
          <div className="ws-panel-body ws-editor-body">
            {!draft ? <div className="ws-empty">选择或创建一个智能体开始编辑</div> : (
              <>
                <section className="ws-editor-section">
                  <div className="ws-section-head">
                    <div className="ws-section-title">基础信息与提示词</div>
                    <div className="ws-section-desc">定义智能体身份、职责和运行时默认设置</div>
                  </div>
                  <div className="ws-editor-grid">
                    <div className="ws-field">
                      <label>名称 *
                        <input type="text" value={draft.name ?? ""} onChange={(e) => patchDraft({ name: e.target.value })} />
                      </label>
                    </div>
                    <div className="ws-field">
                      <label>描述
                        <input type="text" value={draft.description ?? ""} onChange={(e) => patchDraft({ description: e.target.value })} />
                      </label>
                    </div>
                  </div>
                  <div className="ws-field">
                    <label>系统提示词
                      <span className="dim" style={{ fontSize: 11, marginLeft: 8 }}>{`字符 ${promptLen} / ${PROMPT_MAX_CHARS}`}</span>
                      <textarea
                        className="ws-prompt-input"
                        value={draft.system_prompt ?? ""}
                        onChange={(e) => patchDraft({ system_prompt: e.target.value })}
                      />
                    </label>
                  </div>
                  <div className="ws-editor-grid">
                    <div className="ws-field">
                      <label>默认模型
                        <select value={draft.default_model ?? ""} onChange={(e) => patchDraft({ default_model: e.target.value })}>
                          <option value="">（继承 Gateway 默认）</option>
                          {(catalogs.models ?? []).map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
                        </select>
                      </label>
                    </div>
                    <div className="ws-field">
                      <label>权限
                        <select value={draft.permission_mode ?? "ask"} onChange={(e) => patchDraft({ permission_mode: e.target.value })}>
                          {PERMISSION_MODES.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
                        </select>
                      </label>
                    </div>
                    <div className="ws-field">
                      <label>会话模式
                        <select value={draft.chat_mode ?? "chat"} onChange={(e) => patchDraft({ chat_mode: e.target.value })}>
                          <option value="chat">chat</option>
                        </select>
                      </label>
                    </div>
                    <div className="ws-field">
                      <label>最大步数
                        <input type="number" min={1} value={draft.max_steps ?? 100} onChange={(e) => patchDraft({ max_steps: parseInt(e.target.value, 10) || 100 })} />
                      </label>
                    </div>
                  </div>
                  <div className="ws-field">
                    {references.length ? (
                      <>
                        <label>被以下工作区引用</label>
                        <div className="ws-tags">
                          {references.map((r) => <span key={r.workspace_id} className="ws-tag">{`${r.name}（${r.status}）`}</span>)}
                        </div>
                      </>
                    ) : null}
                  </div>
                </section>
                <hr className="ws-divider" />
                <section className="ws-editor-section">
                  <div className="ws-section-head">
                    <div className="ws-section-title">能力配置与最终提示词</div>
                    <div className="ws-section-desc">选择工具、技能和 MCP，并在保存前预览最终提示词</div>
                  </div>
                  <div className="ws-field">
                    <label>{`工具（${(draft.tools ?? []).length}）`}</label>
                    <div className="dim" style={{ fontSize: 11, margin: "3px 0 7px" }}>风险说明：低风险=只读/查询；中风险=写入文件或网络请求；高风险=执行命令或代码。实际执行仍受权限与审批控制。</div>
                    <SearchSelector items={toolItems} selected={new Set(draft.tools ?? [])} onToggle={toggleList("tools")} placeholder="搜索工具…" />
                  </div>
                  <div className="ws-field">
                    <label>{`技能（${(draft.skills ?? []).length}）`}</label>
                    <SearchSelector items={skillItems} selected={new Set(draft.skills ?? [])} onToggle={toggleList("skills")} placeholder="搜索 技能…" />
                  </div>
                  <div className="ws-field">
                    <label>{`MCP 服务（${(draft.mcp_servers ?? []).length}）`}</label>
                    <SearchSelector items={mcpItems} selected={new Set(draft.mcp_servers ?? [])} onToggle={toggleList("mcp_servers")} placeholder="搜索 MCP 服务…" />
                  </div>
                  <div className="ws-actions">
                    <button type="button" className="btn" onClick={() => void preview()}>{previewing ? "预览中…" : "🔍 预览 提示词"}</button>
                  </div>
                  {previewData ? <PromptPreview data={previewData} /> : null}
                </section>
                <div className="ws-savebar">
                  <button type="button" className="btn primary" disabled={saving} onClick={() => void save()}>{saving ? "保存中…" : "保存"}</button>
                  <button type="button" className="btn" onClick={() => void duplicate()}>复制</button>
                  {draft.profile_id && !isBuiltin ? (
                    <button type="button" className="btn" onClick={() => void toggleEnabled()}>{draft.status === "archived" ? "启用" : "停用"}</button>
                  ) : null}
                  {draft.profile_id && !isBuiltin ? (
                    <button type="button" className="btn danger" onClick={() => void remove()}>删除</button>
                  ) : null}
                  <span className="ws-dirty">{dirty ? "● 未保存" : ""}</span>
                  {versionConflict ? <span className="ws-warn">⚠️ 服务器已有新版本</span> : null}
                </div>
              </>
            )}
          </div>
        </main>
      </div>
    </section>
  );
}
