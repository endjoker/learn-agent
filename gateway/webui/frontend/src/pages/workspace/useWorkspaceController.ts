import { useCallback, useEffect, useReducer, useRef } from "react";

import type { ApiClient } from "@/api/client";
import { api as defaultApi } from "@/api/client";
import type {
  AgentCatalog,
  AgentListResponse,
  AgentProfile,
  PathValidationResult,
  Workspace,
  WorkspaceCreateBody,
  WorkspaceCreateResponse,
  WorkspaceDirectoryResponse,
  WorkspaceFileResponse,
  WorkspaceListResponse,
  WorkspaceSession,
  WorkspaceSessionSwitchPatch,
  WorkspaceSessionsResponse,
} from "@/api/types";
import { chatDerivedCache } from "@/pages/chat/byteLru";

interface WorkspaceState {
  workspaces: Workspace[];
  sessions: WorkspaceSession[];
  files: WorkspaceDirectoryResponse["entries"];
  selectedWorkspaceId?: string;
  selectedSessionId?: string;
  directoryPath: string;
  openFile?: WorkspaceFileResponse;
  agents: AgentProfile[];
  catalogs: AgentCatalog;
  sessionCounts: Record<string, number>;
  loading: boolean;
  loadingSelection: boolean;
  fileLoading: boolean;
  error?: string;
  fileError?: string;
}

const initialState: WorkspaceState = {
  workspaces: [], sessions: [], files: [], directoryPath: "",
  agents: [], catalogs: { tools: [], skills: [], mcp: { servers: [] }, models: [] },
  sessionCounts: {},
  loading: true, loadingSelection: false, fileLoading: false,
};

type Action = { type: "patch"; patch: Partial<WorkspaceState> };
const reducer = (state: WorkspaceState, action: Action) => ({ ...state, ...action.patch });

export function useWorkspaceController({ client = defaultApi }: { client?: ApiClient } = {}) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const listRequest = useRef<AbortController>();
  const selectionRequest = useRef<AbortController>();
  const fileRequest = useRef<AbortController>();
  const countRequests = useRef<AbortController[]>([]);
  const countsRef = useRef<Record<string, number>>({});
  const selectionGeneration = useRef(0);
  const fileGeneration = useRef(0);
  const selectedWorkspaceRef = useRef<string>();

  const loadAgents = useCallback(async () => {
    try {
      const data = await client.get<AgentListResponse>("/api/agents?status=active&limit=200", { silent: true });
      dispatch({ type: "patch", patch: { agents: data.agents ?? [] } });
    } catch { /* catalog is optional; wizard degrades gracefully */ }
    try {
      const catalog = await client.get<AgentCatalog>("/api/agents/catalog", { silent: true });
      dispatch({ type: "patch", patch: { catalogs: catalog } });
    } catch { /* optional */ }
  }, [client]);

  const refresh = useCallback(async () => {
    listRequest.current?.abort();
    countRequests.current.forEach((controller) => controller.abort());
    countRequests.current = [];
    countsRef.current = {};
    const controller = new AbortController();
    listRequest.current = controller;
    dispatch({ type: "patch", patch: { loading: true, error: undefined } });
    let workspaces: Workspace[] = [];
    try {
      const response = await client.get<WorkspaceListResponse>("/api/workspaces?limit=200", { signal: controller.signal, silent: true });
      if (!controller.signal.aborted) {
        workspaces = response.workspaces;
        dispatch({ type: "patch", patch: { workspaces: response.workspaces, loading: false } });
      }
    } catch (error) {
      if (!controller.signal.aborted) dispatch({ type: "patch", patch: { loading: false, error: error instanceof Error ? error.message : "工作区加载失败" } });
    }
    // Best-effort session-count badges; must never block the list render.
    for (const workspace of workspaces) {
      const countController = new AbortController();
      countRequests.current.push(countController);
      void client.get<WorkspaceSessionsResponse>(`/api/workspaces/${encodeURIComponent(workspace.workspace_id)}/sessions`, {
        signal: countController.signal, silent: true,
      }).then((data) => {
        if (!countController.signal.aborted) {
          countsRef.current = { ...countsRef.current, [workspace.workspace_id]: (data.sessions ?? []).length };
          dispatch({ type: "patch", patch: { sessionCounts: countsRef.current } });
        }
      }).catch(() => undefined);
    }
  }, [client]);

  const loadDirectory = useCallback(async (workspaceId: string, path = "") => {
    const current = selectionGeneration.current;
    try {
      const response = await client.get<WorkspaceDirectoryResponse>(`/api/workspaces/${encodeURIComponent(workspaceId)}/files`, {
        query: { path }, signal: selectionRequest.current?.signal, silent: true,
      });
      if (current === selectionGeneration.current && workspaceId === selectedWorkspaceRef.current) {
        dispatch({ type: "patch", patch: { files: response.entries, directoryPath: response.path, fileError: undefined } });
      }
    } catch (error) {
      if (!selectionRequest.current?.signal.aborted && current === selectionGeneration.current) {
        dispatch({ type: "patch", patch: { files: [], fileError: error instanceof Error ? error.message : "目录加载失败" } });
      }
    }
  }, [client]);

  const loadSessions = useCallback(async (workspaceId: string) => {
    if (!workspaceId) return;
    try {
      const data = await client.get<WorkspaceSessionsResponse>(`/api/workspaces/${encodeURIComponent(workspaceId)}/sessions`, {
        signal: selectionRequest.current?.signal, silent: true,
      });
      if (workspaceId === selectedWorkspaceRef.current) {
        dispatch({ type: "patch", patch: { sessions: data.sessions ?? [] } });
      }
      return data.sessions ?? [];
    } catch { return []; }
  }, [client]);

  const selectWorkspace = useCallback(async (workspaceId: string) => {
    if (selectedWorkspaceRef.current && selectedWorkspaceRef.current !== workspaceId) {
      chatDerivedCache.deletePrefix(`workspace:${selectedWorkspaceRef.current}:`);
    }
    selectedWorkspaceRef.current = workspaceId;
    selectionRequest.current?.abort();
    fileRequest.current?.abort();
    const controller = new AbortController();
    selectionRequest.current = controller;
    const current = ++selectionGeneration.current;
    dispatch({ type: "patch", patch: {
      selectedWorkspaceId: workspaceId, selectedSessionId: undefined, sessions: [], files: [],
      directoryPath: "", openFile: undefined, loadingSelection: true, error: undefined, fileError: undefined,
    } });
    try {
      const sessions = await client.get<WorkspaceSessionsResponse>(`/api/workspaces/${encodeURIComponent(workspaceId)}/sessions`, {
        signal: controller.signal, silent: true,
      });
      if (current !== selectionGeneration.current || controller.signal.aborted) return;
      dispatch({ type: "patch", patch: { sessions: sessions.sessions, loadingSelection: false } });
      await loadDirectory(workspaceId, "");
    } catch (error) {
      if (!controller.signal.aborted && current === selectionGeneration.current) {
        dispatch({ type: "patch", patch: { loadingSelection: false, error: error instanceof Error ? error.message : "工作区详情加载失败" } });
      }
    }
  }, [client, loadDirectory]);

  const openFile = useCallback(async (workspaceId: string, path: string) => {
    fileRequest.current?.abort();
    const controller = new AbortController();
    fileRequest.current = controller;
    const current = ++fileGeneration.current;
    dispatch({ type: "patch", patch: { fileLoading: true, fileError: undefined } });
    try {
      const response = await client.get<WorkspaceFileResponse>(`/api/workspaces/${encodeURIComponent(workspaceId)}/file`, {
        query: { path }, signal: controller.signal, silent: true,
      });
      if (current === fileGeneration.current && !controller.signal.aborted && workspaceId === selectedWorkspaceRef.current) {
        dispatch({ type: "patch", patch: { openFile: response, fileLoading: false } });
      }
    } catch (error) {
      if (!controller.signal.aborted && current === fileGeneration.current) dispatch({ type: "patch", patch: { fileLoading: false, fileError: error instanceof Error ? error.message : "文件读取失败" } });
    }
  }, [client]);

  const validatePath = useCallback(async (path: string, riskConfirmed: boolean) => {
    try {
      return await client.post<PathValidationResult>("/api/workspaces/validate-path", {
        path, purpose: "project_root", risk_confirmed: riskConfirmed,
      }, { silent: true });
    } catch (error) {
      const message = error instanceof Error ? error.message : "路径校验失败";
      return { blocked: true, reasons: [message] } as PathValidationResult;
    }
  }, [client]);

  const createWorkspace = useCallback(async (body: WorkspaceCreateBody) => {
    const data = await client.post<WorkspaceCreateResponse>("/api/workspaces", body);
    const created = data.workspace;
    if (!created) throw new Error("创建失败：服务器未返回工作区");
    await refresh();
    await selectWorkspace(created.workspace_id);
    if (data.first_session?.session_id) {
      dispatch({ type: "patch", patch: { selectedSessionId: data.first_session.session_id } });
    }
    return created;
  }, [client, refresh, selectWorkspace]);

  const deleteWorkspace = useCallback(async (workspaceId: string) => {
    await client.delete(`/api/workspaces/${encodeURIComponent(workspaceId)}`, { silent: true });
    if (selectedWorkspaceRef.current === workspaceId) {
      selectedWorkspaceRef.current = undefined;
      dispatch({ type: "patch", patch: {
        selectedWorkspaceId: undefined, selectedSessionId: undefined, sessions: [],
        files: [], directoryPath: "", openFile: undefined,
      } });
    }
    await refresh();
  }, [client, refresh]);

  const createSession = useCallback(async (workspaceId: string, draft: Record<string, unknown>) => {
    const created = await client.post<WorkspaceSession>(`/api/workspaces/${encodeURIComponent(workspaceId)}/sessions`, draft);
    await loadSessions(workspaceId);
    dispatch({ type: "patch", patch: { selectedSessionId: created.session_id } });
    return created;
  }, [client, loadSessions]);

  const deleteSession = useCallback(async (session: WorkspaceSession) => {
    await client.delete(
      `/api/workspaces/${encodeURIComponent(session.workspace_id)}/sessions/${encodeURIComponent(session.session_id)}`,
      { silent: true },
    );
    chatDerivedCache.deletePrefix(`workspace:${session.workspace_id}:${session.session_id}:`);
    dispatch({ type: "patch", patch: { selectedSessionId: undefined } });
    await loadSessions(session.workspace_id);
  }, [client, loadSessions]);

  const switchSession = useCallback(async (workspaceId: string, sessionId: string, patch: WorkspaceSessionSwitchPatch) => {
    const updated = await client.post<WorkspaceSession>(
      `/api/workspaces/${encodeURIComponent(workspaceId)}/sessions/${encodeURIComponent(sessionId)}/switch`,
      patch, { silent: true },
    );
    dispatch({ type: "patch", patch: {
      sessions: state.sessions.map((session) => session.session_id === sessionId ? updated : session),
    } });
    return updated;
  }, [client, state.sessions]);

  const stopChat = useCallback(async (workspaceId: string, sessionId: string) => {
    await client.post(`/api/workspaces/${encodeURIComponent(workspaceId)}/sessions/${encodeURIComponent(sessionId)}/stop`, {}, { silent: true });
  }, [client]);

  const clearChat = useCallback(async (workspaceId: string, sessionId: string) => {
    await client.post(`/api/workspaces/${encodeURIComponent(workspaceId)}/sessions/${encodeURIComponent(sessionId)}/clear`, {}, { silent: true });
  }, [client]);

  useEffect(() => {
    void loadAgents();
    void refresh();
    return () => {
      listRequest.current?.abort();
      selectionRequest.current?.abort();
      fileRequest.current?.abort();
      countRequests.current.forEach((controller) => controller.abort());
    };
  }, [loadAgents, refresh]);

  return {
    state,
    refresh,
    selectWorkspace,
    loadDirectory,
    loadSessions,
    openFile,
    validatePath,
    createWorkspace,
    deleteWorkspace,
    createSession,
    deleteSession,
    switchSession,
    stopChat,
    clearChat,
    selectSession: (sessionId: string) => dispatch({ type: "patch", patch: { selectedSessionId: sessionId } }),
  };
}
