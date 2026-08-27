import { useEffect, useState } from "react";

import { AgentEditorPage } from "@/pages/agent-editor/AgentEditorPage";
import { ChatPage } from "@/pages/chat/ChatPage";
import { CronPage } from "@/pages/cron/CronPage";
import { McpPage } from "@/pages/mcp/McpPage";
import { PromptPage } from "@/pages/prompt/PromptPage";
import { SettingsPage } from "@/pages/settings/SettingsPage";
import { SkillsPage } from "@/pages/skills/SkillsPage";
import { StatusPage } from "@/pages/status/StatusPage";
import { WorkspacePage } from "@/pages/workspace/WorkspacePage";

const ROUTES = [
  ["chat", "💬", "主会话", false],

  ["mcp", "🔌", "MCP", false],
  ["skills", "🧩", "Skills", false],
  ["prompt", "📝", "Prompt", false],
  ["status", "📊", "状态", false],
  ["cron", "⏰", "定时任务", false],
  ["workspace", "▣", "工作区", true],
  ["agent-editor", "◇", "智能体编辑", true],
  ["settings", "⚙️", "设置", false],
] as const;

type RouteName = (typeof ROUTES)[number][0];

function currentRoute(): RouteName {
  const raw = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  const route = raw as RouteName;
  // 默认进入主会话聊天页；未知路由回落到主会话
  return ROUTES.some(([name]) => name === route) ? route : "chat";
}

function Page({ route }: { route: RouteName }) {
  switch (route) {
    case "chat": return <ChatPage />;

    case "mcp": return <McpPage />;
    case "skills": return <SkillsPage />;
    case "prompt": return <PromptPage />;
    case "status": return <StatusPage />;
    case "cron": return <CronPage />;
    case "workspace": return <WorkspacePage />;
    case "agent-editor": return <AgentEditorPage />;
    case "settings": return <SettingsPage />;
  }
}

export function App() {
  const [route, setRoute] = useState<RouteName>(() => currentRoute());
  const [sseState, setSseState] = useState<"connecting" | "ok" | "err">("connecting");
  useEffect(() => {
    const update = () => setRoute(currentRoute());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);

  // Global SSE heartbeat: drives the sidebar connection indicator.  Pages
  // open their own scoped EventSource for payload delivery; this one only
  // observes the connection lifecycle (mirrors the legacy app.js).
  useEffect(() => {
    const source = new EventSource("/api/events");
    source.onopen = () => setSseState("ok");
    source.onerror = () => setSseState("err");
    return () => source.close();
  }, []);

  return (
    <div id="layout">
      <nav id="sidebar" aria-label="主导航">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">◈</span> JKagent
        </div>
        {ROUTES.map(([name, icon, label, sub]) => (
          <div key={name}>
            {name === "workspace" ? <div className="nav-group">工作区</div> : null}
            <a
              className={`nav-item${sub ? " sub" : ""}${route === name ? " active" : ""}`}
              href={`#/${name}`}
              data-page={name}
            >
              <span className="nav-ico">{icon}</span>
              <span className="nav-label">{label}</span>
            </a>
          </div>
        ))}
        <div id="sse-state" className={`sse-state ${sseState}`}>
          {sseState === "ok" ? "SSE: 已连接" : sseState === "err" ? "SSE: 断开，重连中…" : "SSE: 连接中…"}
        </div>
      </nav>
      <main id="main" data-route={route}>
        <Page route={route} />
      </main>
    </div>
  );
}
