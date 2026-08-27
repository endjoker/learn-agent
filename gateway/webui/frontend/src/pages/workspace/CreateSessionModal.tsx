import { useState } from "react";


import type { AgentCatalog, AgentProfile } from "@/api/types";
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

interface CreateSessionModalProps {
  agents: AgentProfile[];
  catalogs: AgentCatalog;
  defaultAgentId: string;
  /** 新建会话时"项目目录"的默认值：该工作区的项目路径。 */
  defaultProjectDir?: string;
  onCreate: (draft: Record<string, unknown>) => Promise<unknown>;
  onClose: () => void;
}

export function CreateSessionModal({ agents, catalogs, defaultAgentId, defaultProjectDir, onCreate, onClose }: CreateSessionModalProps) {
  const [name, setName] = useState("新会话");
  const [agentProfileId, setAgentProfileId] = useState(defaultAgentId);
  const [model, setModel] = useState("");
  const [permissionMode, setPermissionMode] = useState("ask");
  const [reasoningLevel, setReasoningLevel] = useState("inherit");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!agentProfileId) { toast("请选择智能体", "err"); return; }
    setBusy(true);
    try {
      await onCreate({
        name: name.trim() || "新会话",
        agent_profile_id: agentProfileId,
        model: model || undefined,
        permission_mode: permissionMode,
        reasoning_level: reasoningLevel,
        chat_mode: "chat",
      });
      toast("会话已创建", "ok");
      onClose();
    } catch (error) {
      toast(`创建会话失败: ${error instanceof Error ? error.message : "未知错误"}`, "err");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="新建会话"
      wide
      actions={(
        <>
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={busy} onClick={() => void submit()}>
            {busy ? "创建中…" : "创建会话"}
          </button>
        </>
      )}
    >
      <div className="ws-field">
        <label>会话名称</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      {defaultProjectDir ? (
        <div className="ws-field">
          <label>项目目录</label>
          <div className="ws-detail-value">{defaultProjectDir}</div>
          <div className="dim" style={{ fontSize: 12 }}>默认使用该工作区的项目路径。</div>
        </div>
      ) : null}
      <div className="ws-field">
        <label>智能体 *</label>
        <select value={agentProfileId} onChange={(e) => setAgentProfileId(e.target.value)}>
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
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            <option value="">（继承 Gateway 默认）</option>
            {(catalogs.models ?? []).map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
          </select>
        </div>
        <div className="ws-field">
          <label>思考模式</label>
          <select value={reasoningLevel} onChange={(e) => setReasoningLevel(e.target.value)}>
            {REASONING_LEVELS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        <div className="ws-field">
          <label>权限</label>
          <select value={permissionMode} onChange={(e) => setPermissionMode(e.target.value)}>
            {PERMISSION_MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
      </div>
      <div className="dim" style={{ fontSize: 12 }}>工具、Skills 和 MCP 由所选智能体统一管理，无需在会话中重复配置。</div>
    </Modal>
  );
}
