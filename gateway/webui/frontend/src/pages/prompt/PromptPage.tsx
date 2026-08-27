import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import { ApiError } from "@/api/client";
import type {
  AgentCatalog, MainSessionCaps, MainSessionCapsResponse, PromptFileContent,
  PromptFilesResponse, PromptFileInfo, PromptPreviewData, PromptWriteResponse,
} from "@/api/types";
import { confirmDialog } from "@/components/confirm";
import { PromptPreview } from "@/components/PromptPreview";
import { SearchSelector, type SearchSelectorItem } from "@/components/SearchSelector";
import { toast } from "@/components/toast";
import { useSse } from "@/hooks/useSse";

type View = "files" | "caps";

const safeDecode = (value: string): string => {
  try { return decodeURIComponent(value); } catch { return value; }
};

const parseQueryView = (): View => {
  const q = (window.location.hash.split("?")[1] ?? "");
  return q.split("&").some((pair) => {
    const [k, v] = pair.split("=");
    return safeDecode(k ?? "") === "view" && safeDecode(v ?? "") === "caps";
  }) ? "caps" : "files";
};

// ---------- Prompt 文件 ----------

function PromptFilesView({ client }: { client: ApiClient }) {
  const [files, setFiles] = useState<PromptFileInfo[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [mtime, setMtime] = useState(0);
  const [limit, setLimit] = useState(8000);
  const [content, setContent] = useState("");

  const loadFiles = useCallback(async () => {
    try {
      const data = await client.get<PromptFilesResponse>("/api/prompt/files", { silent: true });
      const list = (data.files ?? []).filter((f) => f.name !== "GUIDE.md");
      setFiles(list);
      setCurrent((prev) => {
        if (prev) return prev;
        const initial = list.find((f) => f.injected && f.exists) ?? list[0];
        return initial?.name ?? null;
      });
    } catch { /* silent */ }
  }, [client]);

  useEffect(() => { void loadFiles(); }, [loadFiles]);
  useSse({}, (event) => { if (event.type === "prompt.updated") void loadFiles(); });

  // 代际序号守卫：快速切换文件时丢弃过期响应，避免旧内容覆盖新选中文件
  const selectGenerationRef = useRef(0);
  const select = useCallback(async (name: string) => {
    const generation = ++selectGenerationRef.current;
    setCurrent(name);
    try {
      const data = await client.get<PromptFileContent>(`/api/prompt/files/${encodeURIComponent(name)}`, { silent: true });
      if (generation !== selectGenerationRef.current) return; // 过期响应丢弃
      setMtime(data.mtime_ns ?? 0);
      setLimit(data.truncation_limit ?? 8000);
      setContent(data.content ?? "");
    } catch {
      if (generation !== selectGenerationRef.current) return;
      setMtime(0);
      setLimit(8000);
      setContent("");
    }
  }, [client]);

  useEffect(() => {
    if (current && files.length > 0 && !files.some((f) => f.name === current)) setCurrent(null);
  }, [current, files]);

  useEffect(() => {
    if (current) void select(current);
  }, [current, select]);

  const save = async () => {
    if (!current) return;
    // 超过注入截断阈值：保存前明确警告，避免以为保存了完整内容
    if (content.length > limit) {
      const ok = await confirmDialog(
        `内容 ${content.length} 字符，超过注入截断阈值 ${limit} 字符。\n\n` +
        `保存后运行时注入到系统提示词的内容将被截断（可在 config.json 的 prompt.bootstrap_max_chars_per_file 调高阈值）。\n\n仍要保存吗？`,
        { okText: "仍要保存", cancelText: "取消" },
      );
      if (!ok) return;
    }
    const payload = { content, base_mtime_ns: mtime };
    try {
      const result = await client.put<PromptWriteResponse>(`/api/prompt/files/${encodeURIComponent(current)}`, payload);
      if (result.mtime_ns !== undefined) setMtime(result.mtime_ns);
      toast(result.warning ? `已保存：${result.warning}` : "已保存", result.warning ? "err" : "ok");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        const conflict = (error.payload ?? {}) as { mtime_ns?: number };
        const overwrite = await confirmDialog("文件已被其他端修改。\n\n点【确定】用当前编辑内容覆盖，点【取消】放弃本次保存。", { okText: "确定", cancelText: "取消" });
        if (overwrite) {
          try {
            const retry = await client.put<PromptWriteResponse>(`/api/prompt/files/${encodeURIComponent(current)}`, {
              content,
              base_mtime_ns: conflict.mtime_ns,
            });
            if (retry.mtime_ns !== undefined) setMtime(retry.mtime_ns);
            toast(retry.warning ? `已保存：${retry.warning}` : "已保存（覆盖冲突）", retry.warning ? "err" : "ok");
          } catch (retryError) {
            const message = retryError instanceof ApiError ? (retryError.payload as { error?: string } | undefined)?.error ?? retryError.message : String(retryError);
            toast(message || "保存失败", "err");
          }
        } else {
          void select(current);
        }
      } else {
        const message = error instanceof ApiError ? (error.payload as { error?: string } | undefined)?.error ?? error.message : String(error);
        toast(message || "保存失败", "err");
      }
    }
  };

  const apply = async () => {
    try {
      const data = await client.post<{ queued?: number }>("/api/prompt/apply");
      toast(`已广播 /reload-prompt 到 ${data.queued ?? 0} 个会话`, "ok");
    } catch { /* silent */ }
  };

  const overLimit = content.length > limit;
  const overBytes = new TextEncoder().encode(content).byteLength > 64 * 1024;
  return (
    <div className="prompt-view">
      <div className="prompt-toolbar">
        <button type="button" className="btn" onClick={() => void apply()}>🔄 应用到运行中会话</button>
      </div>
      {overBytes ? (
        <div className="prompt-warn-bar">内容超过 64KB 上限，无法保存（后端 413）。请精简内容。</div>
      ) : overLimit ? (
        <div className="prompt-warn-bar">内容超过注入截断阈值 {limit} 字符，运行时注入将被截断。保存前会再次确认。</div>
      ) : null}
      <div className="prompt-tabs">
        {files.map((f) => (
          <button
            key={f.name}
            type="button"
            className={`prompt-tab${f.name === current ? " on" : ""}`}
            onClick={() => setCurrent(f.name)}
          >
            <span className={f.injected ? "dot-inj" : "dot-no"}>●</span>
            {` ${f.name}`}
            {f.injected ? null : <span className="dim"> (不注入)</span>}
          </button>
        ))}
      </div>
      <div className="prompt-editor">
        <div className="prompt-toolbar">
          <span className="dim" style={{ color: overLimit ? "var(--warn)" : undefined }}>
            {`${content.length} 字符${overLimit ? "（超截断阈值）" : ""}`}
          </span>
          <button type="button" className="btn primary" disabled={overBytes} onClick={() => void save()}>💾 保存</button>
        </div>
        <textarea
          className="prompt-ta"
          spellCheck={false}
          value={content}
          onChange={(event) => setContent(event.target.value)}
        />
      </div>
    </div>
  );
}

// ---------- 主会话能力配置 ----------

interface CapsState {
  loading: boolean;
  loaded: boolean;
  error: string;
  saving: boolean;
  previewing: boolean;
  dirty: boolean;
  catalog: AgentCatalog;
  config: MainSessionCaps;
  selected: { tools: Set<string>; skills: Set<string>; mcp_servers: Set<string> };
  previewData: PromptPreviewData | null;
}

const emptyCaps = (): CapsState => ({
  loading: false,
  loaded: false,
  error: "",
  saving: false,
  previewing: false,
  dirty: false,
  catalog: { tools: [], skills: [], mcp: { servers: [] }, models: [] },
  config: { tools: null, skills: null, mcp_servers: null },
  selected: { tools: new Set(), skills: new Set(), mcp_servers: new Set() },
  previewData: null,
});

const initSelected = (config: MainSessionCaps, catalog: AgentCatalog) => {
  const allTools = (catalog.tools ?? []).map((t) => t.name).filter(Boolean);
  const allSkills = (catalog.skills ?? []).map((s) => s.id ?? s.name).filter(Boolean);
  const allMcp = ((catalog.mcp ?? {}).servers ?? []).map((s) => s.name).filter(Boolean);
  return {
    tools: new Set(config.tools == null ? allTools : config.tools),
    skills: new Set(config.skills == null ? allSkills : config.skills),
    mcp_servers: new Set(config.mcp_servers == null ? allMcp : config.mcp_servers),
  };
};

function PromptCapsView({ client }: { client: ApiClient }) {
  const [caps, setCaps] = useState<CapsState>(emptyCaps);

  const loadCapabilities = useCallback(async () => {
    setCaps((prev) => ({ ...prev, loading: true, error: "" }));
    try {
      const data = await client.get<MainSessionCapsResponse>("/api/prompt/main-session", { silent: true });
      setCaps((prev) => ({
        ...prev,
        catalog: data.catalog ?? prev.catalog,
        config: data.config ?? prev.config,
        selected: initSelected(data.config ?? prev.config, data.catalog ?? prev.catalog),
        loaded: true,
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载主会话能力配置失败";
      setCaps((prev) => ({ ...prev, error: message }));
    } finally {
      setCaps((prev) => ({ ...prev, loading: false }));
    }
  }, [client]);

  useEffect(() => { void loadCapabilities(); }, [loadCapabilities]);

  const toggle = (field: keyof CapsState["selected"]) => (id: string, checked: boolean) => {
    setCaps((prev) => {
      const next = new Set(prev.selected[field]);
      if (checked) next.add(id); else next.delete(id);
      return { ...prev, selected: { ...prev.selected, [field]: next }, dirty: true };
    });
  };

  const preview = async () => {
    if (caps.previewing) return;
    setCaps((prev) => ({ ...prev, previewing: true }));
    try {
      const data = await client.post<PromptPreviewData>("/api/prompt/main-session/preview", {
        tools: [...caps.selected.tools],
        skills: [...caps.selected.skills],
        mcp_servers: [...caps.selected.mcp_servers],
      });
      setCaps((prev) => ({ ...prev, previewData: data }));
    } catch (error) {
      toast(`预览失败: ${error instanceof Error ? error.message : ""}`, "err");
    } finally {
      setCaps((prev) => ({ ...prev, previewing: false }));
    }
  };

  const saveCaps = async () => {
    if (caps.saving) return;
    setCaps((prev) => ({ ...prev, saving: true }));
    const payload = {
      tools: [...caps.selected.tools],
      skills: [...caps.selected.skills],
      mcp_servers: [...caps.selected.mcp_servers],
    };
    try {
      const data = await client.put<{ ok?: boolean; config: MainSessionCaps }>("/api/prompt/main-session", payload);
      setCaps((prev) => ({
        ...prev,
        config: data.config ?? payload,
        dirty: false,
      }));
      toast("非工作区会话能力配置已保存", "ok");
    } catch (error) {
      toast(`保存失败: ${error instanceof Error ? error.message : ""}`, "err");
    } finally {
      setCaps((prev) => ({ ...prev, saving: false }));
    }
  };

  const resetCaps = async () => {
    const ok = await confirmDialog("将非工作区会话工具、技能和 MCP 恢复为继承全部（null）？", { okText: "恢复" });
    if (!ok || caps.saving) return;
    setCaps((prev) => ({ ...prev, saving: true }));
    try {
      const data = await client.put<{ ok?: boolean; config: MainSessionCaps }>("/api/prompt/main-session", { tools: null, skills: null, mcp_servers: null });
      setCaps((prev) => ({
        ...prev,
        config: data.config ?? prev.config,
        selected: initSelected(data.config ?? prev.config, prev.catalog),
        dirty: false,
      }));
      toast("已恢复为继承全部", "ok");
    } catch (error) {
      toast(`恢复失败: ${error instanceof Error ? error.message : ""}`, "err");
    } finally {
      setCaps((prev) => ({ ...prev, saving: false }));
    }
  };

  const toolItems: SearchSelectorItem[] = useMemo(
    () => (caps.catalog.tools ?? []).map((t) => ({ id: t.name, name: t.name, risk: t.risk, available: t.available })),
    [caps.catalog.tools],
  );
  const skillItems: SearchSelectorItem[] = useMemo(
    () => (caps.catalog.skills ?? []).map((s) => ({ id: s.id ?? s.name, name: s.name, description: s.description ?? "" })),
    [caps.catalog.skills],
  );
  const mcpItems: SearchSelectorItem[] = useMemo(
    () => ((caps.catalog.mcp ?? {}).servers ?? []).map((s) => ({
      id: s.name,
      name: s.available ? s.name : `${s.name}（未连接；运行时会尝试连接）`,
      available: true,
      unavailable_reason: s.available ? "" : "当前未连接；运行时按配置建立连接",
    })),
    [caps.catalog.mcp],
  );

  return (
    <div className="prompt-view">
      <div className="ws-section-head">
        <div className="ws-section-title">主会话默认能力</div>
        <div className="ws-section-desc">
          自定义所有非工作区会话（WebUI 主会话、新建 WebUI 会话、飞书等）默认使用的工具、技能和 MCP 服务。保存后对新建/重新加载的非工作区会话生效；已加载的会话不会热切换。
        </div>
      </div>
      <div className="ws-editor-body" style={{ padding: 18 }}>
        {caps.loading ? <div className="ws-empty">加载中…</div> : null}
        {!caps.loading && caps.error && !caps.loaded ? (
          <>
            <div className="ws-empty">{caps.error}</div>
            <div className="ws-actions"><button type="button" className="btn" onClick={() => void loadCapabilities()}>重试</button></div>
          </>
        ) : null}
        {caps.loaded ? (
          <section className="ws-editor-section">
            <div className="ws-section-head">
              <div className="ws-section-title">能力选择</div>
              <div className="ws-section-desc">缺省值（null）表示继承全部；在这里保存后，未勾选的项将不会出现在非工作区会话默认工具列表中。</div>
            </div>
            <div className="ws-field">
              <label>{`工具（${caps.selected.tools.size}）`}</label>
              <div className="dim" style={{ fontSize: 11, margin: "3px 0 7px" }}>风险说明：低=只读/查询；中=写入/网络；高=执行命令或代码。实际执行仍受权限与审批控制。</div>
              <SearchSelector items={toolItems} selected={caps.selected.tools} onToggle={toggle("tools")} placeholder="搜索 工具…" />
            </div>
            <div className="ws-field">
              <label>{`技能（${caps.selected.skills.size}）`}</label>
              <SearchSelector items={skillItems} selected={caps.selected.skills} onToggle={toggle("skills")} placeholder="搜索 技能…" />
            </div>
            <div className="ws-field">
              <label>{`MCP 服务（${caps.selected.mcp_servers.size}）`}</label>
              <SearchSelector items={mcpItems} selected={caps.selected.mcp_servers} onToggle={toggle("mcp_servers")} placeholder="搜索 MCP 服务…" />
            </div>
          </section>
        ) : null}
        {caps.previewData ? <PromptPreview data={caps.previewData} /> : null}
        {caps.loaded ? (
          <div className="ws-savebar" style={{ margin: "18px 0 0" }}>
            <button type="button" className="btn primary" disabled={caps.saving} onClick={() => void saveCaps()}>{caps.saving ? "保存中…" : "💾 保存配置"}</button>
            <button type="button" className="btn" disabled={caps.previewing} onClick={() => void preview()}>{caps.previewing ? "预览中…" : "🔍 预览 Prompt"}</button>
            <button type="button" className="btn" onClick={() => void resetCaps()}>恢复继承全部</button>
            <span className="ws-dirty">{caps.dirty ? "● 未保存" : ""}</span>
            <span className="dim" style={{ fontSize: 12 }}>保存后仅影响后续新建的非工作区会话 Agent；已加载的会话不会热切换。</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ---------- 页面 ----------

export function PromptPage({ client = defaultApi }: { client?: ApiClient }) {
  const [view, setView] = useState<View>(() => parseQueryView());
  return (
    <section className="page" aria-label="Prompt 页面">
      <div className="page-head"><h1>📝 Prompt</h1></div>
      <div className="prompt-tabs">
        <button type="button" className={`prompt-tab${view === "files" ? " on" : ""}`} onClick={() => setView("files")}>📄 Prompt 文件</button>
        <button type="button" className={`prompt-tab${view === "caps" ? " on" : ""}`} onClick={() => setView("caps")}>🔵 非工作区会话能力</button>
      </div>
      {view === "files" ? <PromptFilesView client={client} /> : <PromptCapsView client={client} />}
    </section>
  );
}
