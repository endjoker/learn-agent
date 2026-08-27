import { useEffect, useRef, useState } from "react";


import type { ApiClient } from "@/api/client";
import type { AgentCatalog, AgentProfile, PathValidationResult, WorkspaceCreateBody } from "@/api/types";
import { Modal } from "@/components/Modal";
import { toast } from "@/components/toast";

const REASONING_LEVELS: Array<[string, string]> = [
  ["inherit", "继承模型默认"],
  ["none", "关闭"],
  ["minimal", "最少"],
  ["low", "低"],
  ["medium", "中"],
  ["high", "高"],
  ["xhigh", "很高"],
  ["max", "最高"],
];

const PERMISSION_MODES: Array<[string, string]> = [
  ["readonly", "只读与搜索"],
  ["ask", "每次询问"],
  ["allow", "允许已授权操作"],
  ["unreviewed", "不经确认执行"],
];

interface WizardProps {
  client: ApiClient;
  agents: AgentProfile[];
  catalogs: AgentCatalog;
  onCreated: (body: WorkspaceCreateBody) => Promise<unknown>;
  onClose: () => void;
}

const emptyFirst = () => ({
  name: "首个会话",
  agent_profile_id: "",
  model: "",
  permission_mode: "ask",
  reasoning_level: "inherit",
});

export function CreateWorkspaceWizard({ client, agents, catalogs, onCreated, onClose }: WizardProps) {
  const [step, setStep] = useState<0 | 1>(0);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [projectPath, setProjectPath] = useState("");
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [createFirstSession, setCreateFirstSession] = useState(false);
  const [pathResult, setPathResult] = useState<PathValidationResult | null>(null);
  const [first, setFirst] = useState(emptyFirst);
  const [creating, setCreating] = useState(false);
  const validateTimer = useRef<number | null>(null);

  const validate = async (path: string, confirmed: boolean): Promise<PathValidationResult | null> => {
    if (!path.trim()) return null;
    try {
      const result = await client.post<PathValidationResult>("/api/workspaces/validate-path", {
        path: path.trim(), purpose: "project_root", risk_confirmed: confirmed,
      }, { silent: true });
      setPathResult(result);
      return result;
    } catch {
      /* best effort until submit */
      return null;
    }
  };

  useEffect(() => {
    if (validateTimer.current !== null) window.clearTimeout(validateTimer.current);
    validateTimer.current = window.setTimeout(() => { void validate(projectPath, riskConfirmed); }, 400);
    return () => {
      if (validateTimer.current !== null) window.clearTimeout(validateTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectPath, riskConfirmed]);

  const next = async () => {
    if (step === 0) {
      if (!name.trim() || !projectPath.trim()) { toast("名称与项目目录为必填", "err"); return; }
      // await 最新校验结果直接参与判定，避免与 400ms 防抖 validate 竞态
      //（点"下一步"时校验可能尚未完成，pathResult 仍是旧值/旧路径）。
      const fresh = await validate(projectPath, riskConfirmed);
      if (fresh?.blocked) { toast("目录被拒绝", "err"); return; }
      if (fresh?.status === "warning" && !riskConfirmed) { toast("存在风险项，请勾选确认", "err"); return; }
      if (!createFirstSession) { await submit(); return; }
      setStep(1);
      return;
    }
    await submit();
  };

  const submit = async () => {
    if (creating) return;
    if (createFirstSession && !first.agent_profile_id) { toast("请选择首个会话使用的智能体", "err"); return; }
    setCreating(true);
    try {
      const body: WorkspaceCreateBody = {
        name: name.trim(),
        description: description.trim() || undefined,
        project_path: projectPath.trim(),
        risk_confirmed: riskConfirmed,
      };
      if (createFirstSession) {
        body.first_session = {
          name: first.name.trim() || "首个会话",
          agent_profile_id: first.agent_profile_id,
          model: first.model || undefined,
          permission_mode: first.permission_mode,
          reasoning_level: first.reasoning_level,
        };
      }
      await onCreated(body);
      toast("工作区已创建", "ok");
      onClose();
    } catch (error) {
      const message = error instanceof Error ? error.message : "创建失败";
      toast(`创建失败: ${message}`, "err");
    } finally {
      setCreating(false);
    }
  };

  const pathLines: string[] = [];
  if (pathResult) {
    pathLines.push(`规范化路径：${pathResult.normalized || ""}`);
    if (pathResult.exists !== undefined) {
      pathLines.push(`存在（${pathResult.is_directory ? "目录" : "文件"}）· 读:${pathResult.readable} 写:${pathResult.writable}`);
    }
    if (pathResult.blocked) pathLines.push(`⛔ ${(pathResult.reasons ?? []).join("；")}`);
    for (const warn of pathResult.warnings ?? []) pathLines.push(`⚠️ ${warn}`);
  }
  const pathOk = pathResult && !pathResult.blocked && pathResult.status !== "warning";

  return (
    <Modal
      title="创建工作区"
      wide
      actions={(
        <>
          <button type="button" className="btn" onClick={onClose}>取消</button>
          {step === 1 ? <button type="button" className="btn" onClick={() => setStep(0)}>上一步</button> : null}
          <button
            type="button"
            className="btn primary"
            disabled={creating}
            onClick={() => void next()}
          >
            {creating ? "创建中…" : step === 0 ? (createFirstSession ? "下一步：配置首会话" : "创建工作区") : "创建工作区"}
          </button>
        </>
      )}
    >
      <div className="dim" style={{ fontSize: 12, marginBottom: 10 }}>{`步骤 ${step + 1} / ${createFirstSession ? 2 : 1}`}</div>
      {step === 0 ? (
        <>
          <div className="ws-field"><label>名称 *</label><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></div>
          <div className="ws-field"><label>描述</label><input type="text" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
          <div className="ws-field">
            <label>项目目录 *</label>
            <input
              type="text"
              value={projectPath}
              placeholder="/path/to/project"
              onChange={(e) => setProjectPath(e.target.value)}
            />
            {pathLines.length ? (
              <div className={`ws-path-result${pathOk ? "" : " warn"}`}>
                {pathLines.map((line, index) => <div key={index}>{line}</div>)}
              </div>
            ) : null}
          </div>
          <label className="ws-check ws-risk-confirm">
            <input type="checkbox" checked={riskConfirmed} onChange={(e) => setRiskConfirmed(e.target.checked)} />
            <span>我已了解路径风险并确认（出现风险提示时需要）</span>
          </label>
          <label className="ws-check ws-first-session-option">
            <input type="checkbox" checked={createFirstSession} onChange={(e) => setCreateFirstSession(e.target.checked)} />
            <span>创建后继续配置首个会话（可选）</span>
          </label>
          <div className="dim" style={{ fontSize: 12, marginTop: 8 }}>
            工作区只保存项目名称、描述和目录。智能体、模型、思考模式与权限在每个会话中设置。
          </div>
        </>
      ) : (
        <>
          <div className="ws-section-head">
            <div className="ws-section-title">配置首个会话（可选）</div>
            <div className="ws-section-desc">此配置仅作用于首个会话，不会成为整个工作区的默认智能体或权限。</div>
          </div>
          <div className="ws-field">
            <label>会话名称</label>
            <input type="text" value={first.name} onChange={(e) => setFirst({ ...first, name: e.target.value })} />
          </div>
          <div className="ws-field">
            <label>智能体 *</label>
            <select value={first.agent_profile_id} onChange={(e) => setFirst({ ...first, agent_profile_id: e.target.value })}>
              <option value="">{agents.length ? "请选择智能体" : "正在加载智能体…"}</option>
              {agents.map((agent) => (
                <option key={agent.profile_id} value={agent.profile_id}>
                  {agent.is_system ? `${agent.name}（内置）` : agent.name}
                </option>
              ))}
            </select>
            {agents.length === 0 ? <div className="ws-warn">暂无可用智能体，请先到「智能体编辑」创建。</div> : null}
          </div>
          <div className="ws-editor-grid">
            <div className="ws-field">
              <label>模型</label>
              <select value={first.model} onChange={(e) => setFirst({ ...first, model: e.target.value })}>
                <option value="">（继承 Gateway 默认）</option>
                {(catalogs.models ?? []).map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
              </select>
            </div>
            <div className="ws-field">
              <label>思考模式</label>
              <select value={first.reasoning_level} onChange={(e) => setFirst({ ...first, reasoning_level: e.target.value })}>
                {REASONING_LEVELS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </div>
            <div className="ws-field">
              <label>权限</label>
              <select value={first.permission_mode} onChange={(e) => setFirst({ ...first, permission_mode: e.target.value })}>
                {PERMISSION_MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </div>
          </div>
          <div className="dim" style={{ fontSize: 12 }}>工具、Skills 和 MCP 由所选智能体统一管理，无需在会话中重复配置。</div>
        </>
      )}
    </Modal>
  );
}
