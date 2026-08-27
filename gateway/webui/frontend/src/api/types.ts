export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type UnknownRecord = Record<string, unknown>;

export interface ApiErrorPayload {
  error?: string;
  code?: string;
  details?: unknown;
}

export interface OkResponse {
  ok: boolean;
}

export interface PaginationQuery {
  limit?: number;
  offset?: number;
}

export interface HistoryQuery {
  limit?: number;
  before?: number;
}

export interface HistoryPage<T> {
  messages: T[];
  total: number;
  start_index: number;
  has_more: boolean;
  reset_required: boolean;
}

export type MessageRole = "system" | "user" | "assistant" | "tool";

export interface ToolCallFunction {
  name?: string;
  arguments?: string | UnknownRecord;
}

export interface ToolCall {
  id?: string;
  tool_call_id?: string;
  type?: string;
  function?: ToolCallFunction;
  name?: string;
  arguments?: unknown;
  result?: unknown;
  status?: string;
  [key: string]: unknown;
}

export interface Message {
  role: MessageRole;
  content?: unknown;
  content_text?: string;
  name?: string;
  tool_call_id?: string;
  tool_calls?: ToolCall[];
  created_at?: number | string;
  [key: string]: unknown;
}

export interface Session {
  session_key: string;
  session_id: string;
  model?: string;
  message_count?: number;
  is_busy?: boolean;
  created_at?: number;
  last_active?: number;
  loaded?: boolean;
  source?: "memory" | "disk";
  [key: string]: unknown;
}

export interface SessionsResponse {
  sessions: Session[];
}

export interface ChatHistoryResponse extends HistoryPage<Message> {
  session_key: string;
  session_id: string;
  source?: "memory" | "disk" | "empty";
}

export interface WorkspaceHistoryResponse extends HistoryPage<Message> {
  workspace_id: string;
  workspace_session_id: string;
  session_key: string;
  source: "memory" | "disk" | "empty";
}

export interface ChatRequest {
  text?: string;
  images?: Array<{ data: string; media_type?: string }>;
  session_key?: string;
  timeout?: number;
}

export interface ChatResponse extends UnknownRecord {
  ok: boolean;
}

// archived：goal 归档态（useRuntimeFloat CLEAR_GOAL 等处按运行时字符串比较），
// 后端实际会下发，补全联合类型避免调用方反复断言。
export type RuntimeStatus = "created" | "queued" | "running" | "paused" | "completed" | "failed" | "cancelled" | "archived";

export interface PlanTask extends UnknownRecord {
  task_id?: string;
  title?: string;
  status?: RuntimeStatus;
}

export interface Plan extends UnknownRecord {
  plan_id: string;
  session_id?: string;
  title?: string;
  objective?: string;
  status: RuntimeStatus;
  tasks?: PlanTask[];
}

export interface Goal extends UnknownRecord {
  goal_id: string;
  session_id?: string;
  title?: string;
  objective?: string;
  status: RuntimeStatus;
  progress?: number;
  max_rounds?: number;
  rounds_started?: number;
  activation?: string;
  current_task_id?: string | null;
  blocked_reason?: UnknownRecord | null;
}

export interface Approval extends UnknownRecord {
  approval_id?: string;
  id?: string;
  session_key?: string;
  workspace_id?: string;
  workspace_session_id?: string;
  snapshot_id?: string;
  message_id?: string;
  prompt?: string;
  tool?: string;
  tool_name?: string;
  params_preview?: string;
  status?: string;
}

export interface QuestionOption {
  id: string;
  label: string;
  description?: string;
  recommended?: boolean;
}

export interface QuestionPrompt extends UnknownRecord {
  question_id: string;
  session_key: string;
  workspace_id?: string;
  workspace_session_id?: string;
  message_id?: string;
  /** 运行时快照 id（归属校验用；答复/取消 POST 需原样回传，缺失会 403）。 */
  snapshot_id?: string;
  question: string;
  description?: string;
  options: QuestionOption[];
  multiple?: boolean;
  required?: boolean;
  allow_custom?: boolean;
  custom_placeholder?: string;
  allow_cancel?: boolean;
}

export interface QuestionAnswer {
  selected_option_ids: string[];
  custom_text?: string;
}

/**
 * 提问/审批答复的归属校验上下文（后端 fail-closed 403）：
 * POST /api/questions/{qid} 与 POST /api/approvals/{aid} 必须携带
 * session_key 或工作区/消息上下文之一，否则返回 403。
 */
export interface OwnershipContext {
  session_key?: string;
  workspace_id?: string;
  workspace_session_id?: string;
  snapshot_id?: string;
  message_id?: string;
}

export interface QuestionsResponse {
  questions: QuestionPrompt[];
}

export interface Workspace extends UnknownRecord {
  workspace_id: string;
  name: string;
  project_path: string;
  working_directory?: string;
  status?: string;
  version?: number;
}

export interface WorkspaceSession extends UnknownRecord {
  session_id: string;
  workspace_id: string;
  session_key: string;
  name?: string;
  agent_profile_id?: string;
  status?: string;
  version?: number;
}

export interface WorkspaceListResponse {
  workspaces: Workspace[];
  total: number;
}

export interface WorkspaceSessionsResponse {
  sessions: WorkspaceSession[];
  total?: number;
}

export interface WorkspaceFileEntry {
  name: string;
  path: string;
  kind: "directory" | "file";
  size: number;
  modified_at?: number;
}

export interface WorkspaceDirectoryResponse {
  workspace_id: string;
  path: string;
  entries: WorkspaceFileEntry[];
  total: number;
  truncated: boolean;
}

export interface WorkspaceFileResponse {
  workspace_id: string;
  path: string;
  content: string;
  size: number;
  truncated: boolean;
  encoding?: string;
}

/** Workspace session runtime controls persisted via POST .../switch. */
export interface WorkspaceSessionSwitchPatch {
  model?: string;
  permission_mode?: string;
  chat_mode?: string;
  reasoning_level?: string;
  agent_profile_id?: string;
}

/** POST /api/workspaces body (legacy workspace-wizard payload). */
export interface WorkspaceCreateBody {
  name: string;
  description?: string;
  project_path: string;
  risk_confirmed?: boolean;
  first_session?: {
    name?: string;
    agent_profile_id?: string;
    model?: string;
    permission_mode?: string;
    chat_mode?: string;
    reasoning_level?: string;
  };
}

export interface WorkspaceCreateResponse extends UnknownRecord {
  workspace?: Workspace;
  first_session?: WorkspaceSession | null;
}

/** POST /api/workspaces/validate-path result. */
export interface PathValidationResult extends UnknownRecord {
  normalized?: string;
  exists?: boolean;
  is_directory?: boolean;
  readable?: boolean;
  writable?: boolean;
  status?: "ok" | "warning" | "blocked";
  blocked?: boolean;
  reasons?: string[];
  warnings?: string[];
  risk_level?: string;
}

export interface AgentProfile extends UnknownRecord {
  profile_id: string;
  name: string;
  description?: string;
  system_prompt?: string;
  tools?: string[];
  skills?: string[];
  mcp_servers?: string[];
  default_model?: string;
  permission_mode?: string;
  chat_mode?: string;
  max_steps?: number;
  include_tools?: string[];
  exclude_tools?: string[];
  ui_preferences?: UnknownRecord;
  status?: string;
  version?: number;
  is_system?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface AgentListResponse {
  agents: AgentProfile[];
  total: number;
  limit: number;
  offset: number;
}

export interface CatalogTool extends UnknownRecord {
  name: string;
  description?: string;
  risk?: string;
  available?: boolean;
}

export interface CatalogSkill extends UnknownRecord {
  id?: string;
  name: string;
  description?: string;
}

export interface CatalogMcpServer extends UnknownRecord {
  name: string;
  transport?: string;
  url?: string;
  status?: string;
  available?: boolean;
  tools?: string[];
}

export interface CatalogModel extends UnknownRecord {
  id: string;
  provider?: string;
  context_length?: number;
}

export interface AgentCatalog {
  tools: CatalogTool[];
  skills: CatalogSkill[];
  mcp: { servers: CatalogMcpServer[]; live?: UnknownRecord };
  models: CatalogModel[];
}

export interface AgentReferencesResponse {
  references: Array<{ workspace_id: string; name: string; status: string }>;
}

export interface PlanListResponse {
  plans: Plan[];
}

export interface GoalListResponse {
  goals: Goal[];
}

export interface PlanActionResponse {
  plan?: Plan;
}

export interface GoalActionResponse {
  goal?: Goal;
}

export interface McpServerConfig extends UnknownRecord {
  name: string;
  transport?: string;
  command?: string;
  args?: string[];
  url?: string;
  env?: Record<string, string>;
  headers?: Record<string, string>;
  enabled?: boolean;
  trust?: boolean;
}

export interface McpLiveState extends UnknownRecord {
  sessions?: number;
  initialized?: boolean;
  tools?: number;
}

export interface McpResponse {
  servers: McpServerConfig[];
  live: Record<string, McpLiveState>;
}

export interface SkillInfo extends UnknownRecord {
  name: string;
  version?: number;
  description?: string;
  tags?: string[];
  instruction_chars?: number;
}

export interface SkillsMeta {
  skills_dir?: string;
  exists?: boolean;
  platform_note?: string;
}

export interface SkillDetail extends UnknownRecord {
  name: string;
  instruction?: string;
}

export interface SkillsResponse {
  skills: SkillInfo[];
}

export interface PromptFileInfo extends UnknownRecord {
  name: string;
  exists: boolean;
  size: number;
  mtime_ns: number;
  injected: boolean;
}

export interface PromptFilesResponse {
  files: PromptFileInfo[];
}

export interface PromptFileContent extends UnknownRecord {
  name: string;
  content: string;
  size: number;
  mtime_ns: number;
  truncation_limit: number;
}

export interface PromptWriteResponse extends UnknownRecord {
  ok?: boolean;
  mtime_ns?: number;
  warning?: string;
  error?: string;
}

export interface MainSessionCaps {
  tools: string[] | null;
  skills: string[] | null;
  mcp_servers: string[] | null;
}

export interface MainSessionCapsResponse {
  session_key: string;
  config: MainSessionCaps;
  catalog: AgentCatalog;
}

export interface PromptPreviewSection {
  name: string;
  chars: number;
  estimated_tokens: number;
  content: string;
}

export interface PromptWarning {
  code: string;
  message: string;
}

export interface PromptPreviewData extends UnknownRecord {
  sections: PromptPreviewSection[];
  full_prompt: string;
  total_chars: number;
  estimated_tokens: number;
  expected_prompt_hash: string;
  warnings: PromptWarning[];
  provider_tools?: Array<{
    type: string;
    function?: { name?: string; description?: string; parameters?: UnknownRecord };
  }>;
  provider_tools_count?: number;
  mcp_tools_live?: boolean;
}

export interface CronJobConfig extends UnknownRecord {
  name: string;
  schedule: string;
  prompt?: string;
  session?: string;
  deliver?: { mode?: string; channel?: string; target?: string };
  timeout?: number;
  enabled?: boolean;
}

export interface CronJobItem extends CronJobConfig {
  running?: boolean;
  paused?: boolean;
  last_fire?: number;
  last_status?: string;
  runs?: number;
  failures?: number;
}

export interface CronJobsResponse {
  jobs: CronJobItem[];
}

export interface CronHistoryEntry extends UnknownRecord {
  at?: string;
  job?: string;
  status?: string;
  duration_s?: number;
  trigger?: string;
}

export interface CronHistoryResponse {
  history: CronHistoryEntry[];
}

export interface SchedulerChannel {
  channel: string;
  hint?: string;
}

export interface SchedulerChannelsResponse {
  channels: SchedulerChannel[];
  webhooks: string[];
  targets: Record<string, string[]>;
}

export interface StatusSessionEntry extends UnknownRecord {
  session_key: string;
  model?: string;
  message_count?: number;
  is_busy?: boolean;
}

export interface StatusResponse extends UnknownRecord {
  ok?: boolean;
  status?: string;
  executor?: { workers?: number; pending?: number };
  sessions?: {
    active?: number;
    max?: number;
    busy?: string[];
    list?: StatusSessionEntry[];
  };
  channels?: Record<string, UnknownRecord>;
  scheduler?: { present?: boolean; jobs?: number; running?: string[] };
  heartbeat?: { present?: boolean; paused?: boolean; every?: string; beats?: number };
}

export interface ConfigResponse {
  config: UnknownRecord;
  rev: number;
}

export interface ConfigPatchResponse extends UnknownRecord {
  ok?: boolean;
  rev?: number;
}

export interface ProviderSpec extends UnknownRecord {
  name?: string;
  desc?: string;
  base_url?: string;
  api_key?: string;
}

export interface CloudProtocol {
  protocol: string;
  label: string;
  default_url: string;
}

export interface ProvidersResponse {
  local: Record<string, ProviderSpec>;
  cloud: CloudProtocol[];
  env_hints: Record<string, string>;
}

export interface ConfigModelEntry extends UnknownRecord {
  name: string;
}

export interface ConfigModelsResponse {
  models: ConfigModelEntry[];
  default_model: string;
  rev: number;
}

export interface McpServer extends UnknownRecord {
  name: string;
  enabled?: boolean;
  status?: string;
}

export interface CronJob extends UnknownRecord {
  name: string;
  schedule: string;
  prompt?: string;
  enabled?: boolean;
}

export interface PromptFile extends UnknownRecord {
  file?: string;
  name?: string;
  content?: string;
}

export interface StatusResponse extends UnknownRecord {
  ok?: boolean;
  status?: string;
}

export interface ModelInfo extends UnknownRecord {
  id: string;
  name?: string;
  provider?: string;
}

export interface CommandInfo extends UnknownRecord {
  name?: string;
  command?: string;
  description?: string;
  usage?: string;
}

export interface Subagent extends UnknownRecord {
  child_id: string;
  parent_session_id?: string;
  session_id?: string;
  status?: RuntimeStatus;
  prompt?: string;
}

export interface ListResponse<T> {
  total?: number;
  items?: T[];
  [key: string]: unknown;
}
