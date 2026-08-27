import { useCallback, useEffect, useMemo, useState } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import { ApiError } from "@/api/client";
import type { ConfigModelEntry, ConfigResponse, ProvidersResponse, UnknownRecord } from "@/api/types";
import { confirmDialog } from "@/components/confirm";
import { FormField, Modal } from "@/components/Modal";
import { toast } from "@/components/toast";
import { useSse } from "@/hooks/useSse";

const WEBUI_THEME_STORAGE_KEY = "jkagent.theme";

const readWebUITheme = (): "light" | "dark" => {
  try { return localStorage.getItem(WEBUI_THEME_STORAGE_KEY) === "dark" ? "dark" : "light"; }
  catch { return "light"; }
};

const applyWebUITheme = (theme: string) => {
  const next = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem(WEBUI_THEME_STORAGE_KEY, next); } catch { /* ignore */ }
};

const REASONING_LEVELS = ["provider_default", "none", "minimal", "low", "medium", "high", "xhigh", "max"] as const;
const REASONING_LEVEL_LABELS: Record<string, string> = {
  provider_default: "服务商默认",
  none: "关闭推理",
  minimal: "极低",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "极高",
  max: "最大",
};

const REASONING_SELECT_OPTIONS = REASONING_LEVELS.map((level) => (
  <option key={level} value={level}>{REASONING_LEVEL_LABELS[level]}</option>
));

const errorMessage = (error: unknown): string => {
  if (error instanceof ApiError) {
    const payload = error.payload as { error?: string } | undefined;
    return payload?.error ?? error.message;
  }
  return error instanceof Error ? error.message : String(error);
};

/** API Key 脱敏展示（安全）：仅显示末 4 位 + 长度，避免设置页泄露密钥。 */
const maskApiKey = (key: string): string => {
  if (!key || key === "-") return "-";
  const tail = key.slice(-4);
  return `••••••${tail}（${key.length}）`;
};

interface ModelEditorState {
  name: string;
  baseUrl: string;
  contextLength: string;
  reasoningLevel: string;
  apiKey: string;
  supportsReasoning: boolean;
}

interface ProviderChoice {
  type: "local" | "cloud";
  provider?: string;
  protocol?: string;
  base_url: string;
  api_key: string;
  label: string;
}

/** 会话段可编辑字段（对应 config["sessions"]，服务端启动时读取）。 */
const SESSION_NUMERIC_FIELDS: Array<{ key: string; label: string; hint: string }> = [
  { key: "max_sessions", label: "会话上限", hint: "同时保有的会话总数上限，超出后拒绝新建" },
  { key: "idle_timeout_minutes", label: "空闲回收（分钟）", hint: "会话空闲超过该时长后回收 Agent 实例" },
  { key: "soft_timeout_seconds", label: "软超时（秒）", hint: "超过后向任务发送停止信号" },
  { key: "hard_timeout_seconds", label: "硬超时（秒）", hint: "超过后强制终止任务" },
];

export function SettingsPage({ client = defaultApi }: { client?: ApiClient }) {
  const [config, setConfig] = useState<UnknownRecord>({});
  const [theme, setTheme] = useState<"light" | "dark">(() => readWebUITheme());
  const [modelEditor, setModelEditor] = useState<ModelEditorState | null>(null);
  const [addingModel, setAddingModel] = useState<ProviderChoice | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);

  const loadConfig = useCallback(async () => {
    try {
      const data = await client.get<ConfigResponse>("/api/config", { silent: true });
      setConfig(data.config ?? {});
    } catch { /* silent */ }
  }, [client]);

  useEffect(() => { void loadConfig(); }, [loadConfig]);
  useSse({}, (event) => { if (event.type === "config.updated") void loadConfig(); });

  const patch = useCallback(async (section: string, patchBody: Record<string, unknown>, okMsg: string) => {
    try {
      await client.patch(`/api/config/${encodeURIComponent(section)}`, { patch: patchBody });
      toast(okMsg, "ok");
      void loadConfig();
    } catch (error) {
      if (error instanceof ApiError) {
        toast(((error.payload as { error?: string } | undefined)?.error ?? error.message) || "保存失败", "err");
      }
    }
  }, [client, loadConfig]);

  const llm = (config.llm ?? {}) as UnknownRecord & { models?: Record<string, UnknownRecord>; model_id?: string; timeout?: number; reasoning?: { level?: string } };
  const models = useMemo(() => llm.models ?? {}, [llm.models]);
  const modelEntries = useMemo<ConfigModelEntry[]>(
    () => Object.entries(models).map(([name, cfg]) => ({ name, ...(cfg) }) as ConfigModelEntry),
    [models],
  );

  const changeTheme = (next: string) => {
    applyWebUITheme(next);
    setTheme(next === "dark" ? "dark" : "light");
    toast(next === "dark" ? "已切换为夜间主题" : "已切换为明亮主题", "ok");
  };

  const setDefaultModel = async (name: string) => {
    try {
      await client.put("/api/config/llm", { model_id: name });
      toast(`默认模型: ${name}`, "ok");
      void loadConfig();
    } catch { /* silent */ }
  };

  const setLLMReasoning = async (level: string) => {
    try {
      await client.put("/api/config/llm", { reasoning: { level } });
      toast(`全局推理等级：${REASONING_LEVEL_LABELS[level] ?? level}`, "ok");
      void loadConfig();
    } catch { /* silent */ }
  };

  const deleteModel = async (name: string) => {
    const ok = await confirmDialog(`删除模型 ${name}？`);
    if (!ok) return;
    try {
      await client.delete(`/api/config/models/${encodeURIComponent(name)}`);
      toast("已删除", "ok");
      void loadConfig();
    } catch { /* silent */ }
  };

  const openEditModel = (name: string, cfg: UnknownRecord) => {
    const supportsReasoning = String(cfg.protocol ?? "openai") === "openai";
    setModelEditor({
      name,
      baseUrl: String(cfg.base_url ?? ""),
      contextLength: cfg.context_length != null ? String(cfg.context_length) : "",
      reasoningLevel: (cfg.reasoning as { level?: string } | undefined)?.level ?? "provider_default",
      apiKey: "",
      supportsReasoning,
    });
  };

  const saveEditModel = async () => {
    if (!modelEditor) return;
    const body: Record<string, unknown> = { base_url: modelEditor.baseUrl.trim(), reasoning: { level: modelEditor.reasoningLevel } };
    if (modelEditor.contextLength) body.context_length = parseInt(modelEditor.contextLength, 10);
    if (modelEditor.apiKey) body.api_key = modelEditor.apiKey;
    try {
      await client.put(`/api/config/models/${encodeURIComponent(modelEditor.name)}`, body);
      toast("已保存", "ok");
      setModelEditor(null);
      void loadConfig();
    } catch (error) {
      toast(errorMessage(error), "err");
    }
  };

  const openAddWizard = async () => {
    try {
      await client.get<ProvidersResponse>("/api/config/providers");
      setWizardOpen(true);
    } catch { /* silent */ }
  };

  // ---- Prompt 截断（config.prompt，新会话生效） ----
  const promptCfg = (config.prompt ?? {}) as UnknownRecord;
  const bootstrapCharsPerFile = String(promptCfg.bootstrap_max_chars_per_file ?? 8000);
  const bootstrapCharsTotal = String(promptCfg.bootstrap_max_chars_total ?? 32000);
  const truncationWarning = String(promptCfg.truncation_warning ?? "once");

  const [perFile, setPerFile] = useState("");
  const [totalChars, setTotalChars] = useState("");
  const [truncWarn, setTruncWarn] = useState("once");

  useEffect(() => {
    setPerFile(bootstrapCharsPerFile);
    setTotalChars(bootstrapCharsTotal);
    setTruncWarn(truncationWarning);
  }, [bootstrapCharsPerFile, bootstrapCharsTotal, truncationWarning]);

  const savePrompt = () => void patch("prompt", {
    bootstrap_max_chars_per_file: parseInt(perFile, 10),
    bootstrap_max_chars_total: parseInt(totalChars, 10),
    truncation_warning: truncWarn,
  }, "Prompt 截断已保存（新会话生效）");

  // ---- 会话（config["gateway"]["sessions"]，GatewayServer 的 config 即
  // gateway 子段（get_gateway_config），sess_cfg = config.get("sessions")
  // 读到的就是这里；服务端启动时读取，重启生效） ----
  const gatewayCfg = (config.gateway ?? {}) as UnknownRecord;
  const gatewaySessions = (gatewayCfg.sessions ?? {}) as UnknownRecord;
  const [sessFields, setSessFields] = useState<Record<string, string>>({});
  const [sessPersist, setSessPersist] = useState(true);

  useEffect(() => {
    setSessFields({
      max_sessions: String(gatewaySessions.max_sessions ?? 50),
      idle_timeout_minutes: String(gatewaySessions.idle_timeout_minutes ?? 60),
      soft_timeout_seconds: String(gatewaySessions.soft_timeout_seconds ?? 90),
      hard_timeout_seconds: String(gatewaySessions.hard_timeout_seconds ?? 1200),
    });
    setSessPersist(gatewaySessions.persist !== false);
  }, [
    gatewaySessions.max_sessions,
    gatewaySessions.idle_timeout_minutes,
    gatewaySessions.soft_timeout_seconds,
    gatewaySessions.hard_timeout_seconds,
    gatewaySessions.persist,
  ]);

  const softNum = parseInt(sessFields.soft_timeout_seconds ?? "", 10);
  const hardNum = parseInt(sessFields.hard_timeout_seconds ?? "", 10);
  const sessionRangeError = Number.isFinite(softNum) && Number.isFinite(hardNum) && softNum > hardNum
    ? "软超时不能大于硬超时"
    : "";
  const sessionFieldsValid = SESSION_NUMERIC_FIELDS.every(
    ({ key }) => Number.isFinite(parseInt(sessFields[key] ?? "", 10)),
  ) && !sessionRangeError;

  const saveSessions = () => {
    if (!sessionFieldsValid) return;
    const patchBody: Record<string, unknown> = { persist: sessPersist };
    for (const { key } of SESSION_NUMERIC_FIELDS) patchBody[key] = parseInt(sessFields[key] ?? "", 10);
    void patch("gateway.sessions", patchBody, "会话配置已保存（重启网关后生效）");
  };

  // ---- LLM 请求超时（config.llm.timeout） ----
  const [llmTimeout, setLlmTimeout] = useState("");
  useEffect(() => {
    setLlmTimeout(llm.timeout != null ? String(llm.timeout) : "");
  }, [llm.timeout]);

  const saveLlmTimeout = async () => {
    const parsed = parseInt(llmTimeout, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      toast("超时必须是正整数（秒）", "err");
      return;
    }
    try {
      await client.put("/api/config/llm", { timeout: parsed });
      toast(`请求超时：${parsed}s`, "ok");
      void loadConfig();
    } catch (error) {
      toast(errorMessage(error), "err");
    }
  };

  return (
    <section className="page" aria-label="设置页面">
      <h1>⚙️ 设置</h1>
      <div className="settings-sections">
        <div className="settings-card">
          <h2>界面</h2>
          <div className="dim" style={{ marginBottom: 10 }}>主题保存在当前浏览器，刷新后仍会保留。</div>
          <div className="form-row">
            <label className="dim">主题
              <select className="settings-primary-select" value={theme} onChange={(e) => changeTheme(e.target.value)}>
                <option value="light">明亮</option>
                <option value="dark">夜间</option>
              </select>
            </label>
          </div>
        </div>

        <div className="settings-card">
          <h2>大模型</h2>
          <div className="dim" style={{ marginBottom: 10 }}>默认模型与推理等级即时生效；模型增删改写盘后对新对话生效。</div>
          <div className="form-row">
            <label className="dim">默认模型
              <select className="settings-primary-select" value={llm.model_id ?? ""} onChange={(e) => void setDefaultModel(e.target.value)}>
                {Object.keys(models).map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
          </div>
          <div className="form-row">
            <label className="dim">全局推理等级
              <select className="settings-primary-select" value={((llm.reasoning ?? {}).level ?? "provider_default")} onChange={(e) => void setLLMReasoning(e.target.value)}>
                {REASONING_SELECT_OPTIONS}
              </select>
            </label>
          </div>
          <table>
            <thead><tr><th>名称</th><th>协议 / 厂商</th><th>base_url</th><th>上下文</th><th>api_key</th><th>操作</th></tr></thead>
            <tbody>
              {modelEntries.map((entry) => {
                const isDefault = llm.model_id === entry.name;
                return (
                  <tr key={entry.name}>
                    <td><b>{entry.name}</b>{isDefault ? <span className="badge ok">默认</span> : null}</td>
                    <td className="mono">{String(entry.protocol ?? entry.provider ?? "openai")}</td>
                    <td className="mono">{String(entry.base_url ?? "-")}</td>
                    <td className="mono">{entry.context_length != null ? Number(entry.context_length).toLocaleString() : "-"}</td>
                    <td className="mono" title={entry.api_key ? "已脱敏，仅显示末 4 位" : undefined}>{maskApiKey(String(entry.api_key ?? "-"))}</td>
                    <td className="ops">
                      <button type="button" className="btn" onClick={() => openEditModel(entry.name, entry)}>编辑</button>
                      {isDefault ? null : <button type="button" className="btn" onClick={() => void setDefaultModel(entry.name)}>设为默认</button>}
                      <button type="button" className="btn danger" onClick={() => void deleteModel(entry.name)}>删除</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="form-row" style={{ marginTop: 10 }}>
            <button type="button" className="btn primary" onClick={() => void openAddWizard()}>＋ 按厂商添加模型</button>
          </div>
          <FormField label="请求超时（秒，llm.timeout）" hint="单次 LLM 请求的最长等待时间；留空表示使用默认值">
            <input type="number" value={llmTimeout} placeholder="默认" onChange={(e) => setLlmTimeout(e.target.value)} style={{ width: 240 }} />
          </FormField>
          <div className="form-row">
            <button type="button" className="btn" onClick={() => void saveLlmTimeout()}>保存超时</button>
          </div>
        </div>

        <div className="settings-card">
          <h2>Prompt 截断</h2>
          <div className="dim" style={{ marginBottom: 10 }}>控制启动上下文（bootstrap 文件）注入 prompt 的体量上限。</div>
          <FormField label="每文件上限（字符）" hint="bootstrap_max_chars_per_file">
            <input type="number" value={perFile} onChange={(e) => setPerFile(e.target.value)} />
          </FormField>
          <FormField label="总量上限（字符）" hint="bootstrap_max_chars_total">
            <input type="number" value={totalChars} onChange={(e) => setTotalChars(e.target.value)} />
          </FormField>
          <FormField label="截断告警" hint="truncation_warning：once 仅首次告警 / always 每次 / never 关闭">
            <select value={truncWarn} onChange={(e) => setTruncWarn(e.target.value)}>
              <option value="once">once</option>
              <option value="always">always</option>
              <option value="never">never</option>
            </select>
          </FormField>
          <div className="form-row"><button type="button" className="btn primary" onClick={savePrompt}>保存</button></div>
        </div>

        <div className="settings-card">
          <h2>会话</h2>
          <div className="dim" style={{ marginBottom: 10 }}>网关会话管理参数，服务端启动时读取——修改后需重启网关生效。</div>
          {SESSION_NUMERIC_FIELDS.map(({ key, label, hint }) => (
            <FormField key={key} label={label} hint={hint}>
              <input
                type="number"
                value={sessFields[key] ?? ""}
                onChange={(e) => setSessFields((prev) => ({ ...prev, [key]: e.target.value }))}
              />
            </FormField>
          ))}
          <FormField label="会话持久化" hint="persist：重启后恢复会话绑定（关闭则会话仅存于内存）">
            <select value={sessPersist ? "true" : "false"} onChange={(e) => setSessPersist(e.target.value === "true")}>
              <option value="true">开启</option>
              <option value="false">关闭</option>
            </select>
          </FormField>
          {sessionRangeError ? (
            <div className="dim" role="alert" style={{ color: "var(--err)", marginBottom: 8 }}>⚠️ {sessionRangeError}</div>
          ) : null}
          <div className="form-row">
            <button type="button" className="btn primary" disabled={!sessionFieldsValid} onClick={saveSessions}>保存</button>
          </div>
        </div>
      </div>

      {modelEditor ? (
        <Modal
          title={`编辑模型 ${modelEditor.name}`}
          actions={(
            <>
              <button type="button" className="btn primary" onClick={() => void saveEditModel()}>保存</button>
              <button type="button" className="btn" onClick={() => setModelEditor(null)}>取消</button>
            </>
          )}
        >
          <FormField label="服务地址（base_url）"><input value={modelEditor.baseUrl} onChange={(e) => setModelEditor({ ...modelEditor, baseUrl: e.target.value })} /></FormField>
          <FormField label="上下文长度（context_length）"><input type="number" value={modelEditor.contextLength} onChange={(e) => setModelEditor({ ...modelEditor, contextLength: e.target.value })} /></FormField>
          <FormField label={modelEditor.supportsReasoning ? "推理等级" : "推理等级（该协议仅支持 provider_default）"}>
            <select value={modelEditor.reasoningLevel} disabled={!modelEditor.supportsReasoning} onChange={(e) => setModelEditor({ ...modelEditor, reasoningLevel: e.target.value })}>
              {REASONING_SELECT_OPTIONS}
            </select>
          </FormField>
          <FormField label="api_key（不明文显示）"><input type="password" value={modelEditor.apiKey} placeholder={`当前 ${String(models[modelEditor.name]?.api_key ?? "（无）")} — 留空保持不变`} onChange={(e) => setModelEditor({ ...modelEditor, apiKey: e.target.value })} /></FormField>
        </Modal>
      ) : null}

      {wizardOpen ? (
        <Modal
          title="选择厂商 / 协议"
          actions={<button type="button" className="btn" onClick={() => setWizardOpen(false)}>取消</button>}
        >
          <AddModelWizard client={client} onPick={(choice) => { setWizardOpen(false); setAddingModel(choice); }} />
        </Modal>
      ) : null}

      {addingModel ? (
        <AddModelForm client={client} choice={addingModel} onDone={() => { setAddingModel(null); void loadConfig(); }} onClose={() => setAddingModel(null)} />
      ) : null}
    </section>
  );
}

function AddModelWizard({ client, onPick }: { client: ApiClient; onPick: (choice: ProviderChoice) => void }) {
  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  useEffect(() => {
    let active = true;
    client.get<ProvidersResponse>("/api/config/providers").then((data) => {
      if (active) setProviders(data);
    }).catch(() => undefined);
    return () => { active = false; };
  }, [client]);
  if (!providers) return <div className="ws-empty">加载中…</div>;
  return (
    <div className="prov-grid">
      {Object.entries(providers.local ?? {}).map(([key, p]) => (
        <button key={key} type="button" className="prov-card" onClick={() => onPick({
          type: "local", provider: key, base_url: p.base_url ?? "", api_key: p.api_key ?? "", label: p.name ?? key,
        })}>
          <b>{p.name ?? key}</b>
          <div className="dim">{p.desc ?? ""}</div>
        </button>
      ))}
      {(providers.cloud ?? []).map((c) => (
        <button key={c.protocol} type="button" className="prov-card" onClick={() => onPick({
          type: "cloud", protocol: c.protocol, base_url: c.default_url, api_key: "", label: c.label,
        })}>
          <b>{c.label}</b>
          <div className="dim">{`env: ${(providers.env_hints ?? {})[c.protocol] ?? "—"}`}</div>
        </button>
      ))}
    </div>
  );
}

function AddModelForm({ client, choice, onDone, onClose }: { client: ApiClient; choice: ProviderChoice; onDone: () => void; onClose: () => void }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState(choice.base_url);
  const [apiKey, setApiKey] = useState("");
  const [contextLength, setContextLength] = useState(choice.type === "local" ? "131072" : "128000");
  const [reasoningLevel, setReasoningLevel] = useState("provider_default");
  const [busy, setBusy] = useState(false);
  const supportsReasoning = String(choice.protocol ?? "openai") === "openai";

  const save = async () => {
    const body: Record<string, unknown> = {
      name: name.trim(),
      type: choice.type,
      base_url: url.trim(),
      api_key: apiKey,
      context_length: parseInt(contextLength || "0", 10),
      reasoning: { level: reasoningLevel },
    };
    if (choice.type === "local") body.provider = choice.provider;
    else body.protocol = choice.protocol;
    if (!body.name) { toast("名称必填", "err"); return; }
    setBusy(true);
    try {
      await client.post("/api/config/models", body);
      toast(`已添加模型 ${body.name}，可在会话页 /model 切换`, "ok");
      onDone();
    } catch (error) {
      toast(errorMessage(error), "err");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`添加模型 — ${choice.label}`}
      actions={(
        <>
          <button type="button" className="btn primary" disabled={busy} onClick={() => void save()}>保存</button>
          <button type="button" className="btn" onClick={onClose}>取消</button>
        </>
      )}
    >
      <FormField label="名称"><input value={name} placeholder="模型名称（唯一）" onChange={(e) => setName(e.target.value)} /></FormField>
      <FormField label="服务地址（base_url）"><input value={url} onChange={(e) => setUrl(e.target.value)} /></FormField>
      <FormField label="API 密钥"><input type="password" value={apiKey} placeholder="api_key" onChange={(e) => setApiKey(e.target.value)} /></FormField>
      <FormField label="上下文长度（context_length）"><input type="number" value={contextLength} onChange={(e) => setContextLength(e.target.value)} /></FormField>
      <FormField label={supportsReasoning ? "推理等级" : "推理等级（该协议仅支持 provider_default）"}>
        <select value={reasoningLevel} disabled={!supportsReasoning} onChange={(e) => setReasoningLevel(e.target.value)}>
          {REASONING_SELECT_OPTIONS}
        </select>
      </FormField>
    </Modal>
  );
}
