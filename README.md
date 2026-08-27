# JKagent 🤖

基于 Python 的 AI 智能体网关（Agent Gateway）：通过 aiohttp 提供 HTTP/SSE 服务与 Web UI，内置统一 Agent 循环、工具注册表、Plan/Goal/Subagent 结构化任务、多会话（主会话 + 工作区会话）与长期记忆。

## 核心能力

- **统一 Agent 循环**：原生工具调用（function calling）、reasoning 流式透传、上下文自动压缩
- **统一会话**：主会话 / 工作区会话 / 渠道会话全部以 SQLite 为唯一权威（Conversation / Turn / Node），SSE 实时流 + 断线重放 + 版本缺口自愈
- **结构化任务**：Plan（分步执行）/ Goal（多轮自主续跑）/ Subagent（子任务委派），后台任务与对话互不阻塞
- **工具与安全**：内置工具 + MCP 扩展 + Skill；四档权限（readonly / ask / allow / unreviewed，唯一权威 PolicyEngine）+ 可选沙箱硬闸；出站 HTTP SSRF 防护（内网段可配放行、云元数据恒拦）
- **多模态**：WebUI 直接发图片+文字（图片信封入队、原图落盘、历史缩略图）；飞书图片；视觉白名单四档感知
- **长期记忆**：跨会话 BM25 检索 + 权重反馈；保留期自动清理（负分 15 天 / 零分 30 天 / 正分永久）
- **数据自洁**：任务/会话/Outbox/审批/记忆逐层保留期，数据库空闲页每日自动 VACUUM 收缩
- **自定义agent**：项目支持主会话和工作区会话；主会话可实现日常对话，定时任务，数据整理等；工作区会话支持自定义agent，有默认agent，也可自己diy自己的agent，prompt、skill、mcp、内置工具自由搭配；所有会话使用的智能体均可自定义，一键导出完整的syste prompt。

## 模块总览

```
├── agent.py               Agent 主类：统一工具循环、事件、上下文
├── core/
│   ├── agent_runtime/       AgentLoop / 事件 / 上下文 / 工具批执行
│   ├── runtime/             任务运行时、SQLite 存储、Artifacts、保留策略
│   ├── message_store.py     消息存储与 token 统计（文件转录已退役）
│   ├── compressor.py        上下文压缩（轻量 / 全量）
│   ├── llm_client.py        LLM 客户端（流式、工具调用、统一重试）
│   ├── protocols/           协议适配器（OpenAI / Anthropic / Gemini，原生工具调用 + 视觉）
│   ├── mcp_client.py        MCP 客户端（stdio / HTTP 传输）
│   ├── policy_engine.py     四档权限唯一权威（permission.py 为兼容门面）
│   ├── safe_http.py         出站 HTTP 安全层（SSRF 防护、内网放行、云元数据恒拦）
│   ├── security_gate.py     工具执行前安全闸（L2 可选）
│   ├── init_wizard.py       交互式初始化向导
│   ├── plan/ goal/ subagent/  结构化任务域
│   └── sandbox/             沙箱执行器与规则（默认关闭）
├── gateway/
│   ├── server.py            aiohttp 服务入口（默认 127.0.0.1:9120）
│   ├── dispatcher.py        消息分发、执行与超时 / 隔离
│   ├── conversation/        统一会话核心（service / store / bridge / runner / outbox / images）
│   ├── channels/            渠道适配（webui / 飞书 / 微信 / debug）
│   ├── webui/               REST API + SSE + React 前端（frontend/ 源码 → static/ 构建产物）
│   ├── scheduler.py         定时任务（cron）
│   ├── heartbeat.py         心跳自检任务
│   ├── session_migrate.py   旧 JSON 转录 → 统一会话库迁移
│   └── cli.py               jkagent-gateway 命令入口
├── tools/                工具注册表与内置工具（fs / exec / web / memory / process / cron…）
├── SKILLS/ skills/       技能资产与技能管理器
├── memory/               长期记忆（BM25 + 中文分词；主会话池 + 工作区隔离池）
├── tests/                后端测试（pytest，776+ 用例）
├── scripts/              运维与验证脚本（verify_all / api_smoke / seed_real_e2e…）
└── config.example.json   配置模板（复制为 config.json 使用）
```

## 环境要求

- Python ≥ 3.10（开发/验证环境为 3.14）
- Node.js ≥ 20.19（仅前端构建需要）

## 安装（一键）

前置：Python ≥ 3.10、Node.js ≥ 20.19（构建前端用）。

```bash
bash scripts/install.sh          # 安装依赖 + 构建前端 + 生成最小配置
bash scripts/install.sh --run    # 完成后直接启动网关
```

脚本自动完成：`.venv` 创建与依赖安装（优先 requirements.lock）→ Web UI 构建
并同步 → 无 config.json 时生成最小骨架。完成后：

```bash
source .venv/bin/activate
jkagent-gateway run              # 打开 http://127.0.0.1:9120/ui/
```

模型与 API Key **直接在 WebUI「设置」页在线配置**即可使用；
`python agent.py init`（交互式向导，含 MCP/Hook/安全段）为可选项。
脚本幂等，重复执行安全；`--skip-frontend` 跳过前端构建（使用仓库自带产物时），
`FORCE_FRONTEND=1` 强制重建。

<details>
<summary>手动安装（等效分解步骤）</summary>

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # 基础（WebUI + OpenAI 兼容 + 记忆检索）
pip install -e ".[all]"            # 完整（+ 飞书/微信/视觉/Anthropic/Gemini/web 工具）
pip install -e ".[dev]"            # 开发（+ pytest/ruff）
# 可复现构建：pip install -r requirements.lock && pip install -e .

python agent.py init               # 可选：交互式向导
jkagent-gateway doctor             # 可选：配置体检
```

</details>

## 启动

```bash
jkagent-gateway run                # http://127.0.0.1:9120/ui/
jkagent-gateway run --port 9200    # 指定端口
```

其他命令：

```bash
jkagent-gateway doctor             # 配置体检
jkagent-gateway migrate-sessions [--dry-run]   # 旧 JSON 转录迁移到统一会话库
```

## 配置

复制 `config.example.json` 为 `config.json`（或直接跑 `python agent.py init`）。要点：

- `llm.model_id` + `llm.models`：至少一个可用模型（云端 api_key+base_url，或本地 provider）
- `permission.default_mode`：四档权限（默认 ask）
- `gateway.webui.auth_token` / `allowed_ips`：非环回访问必须配置（同时配置时 AND 语义）
- 敏感信息可用环境变量覆盖（`LLM_API_KEY` 等），避免落盘

## 安全与运维要点

- **默认环回绑定**：`127.0.0.1`；绑定非环回须配 `auth_token` 或 `allowed_ips`（同时配置时须同时满足）
- **调试通道默认关闭**：`/debug/chat` 需显式启用；配置 token 后环回也强制校验
- **密钥脱敏**：`GET /api/config` 统一掩码；写回时掩码值自动保留原密钥
- **保留期自洁**：任务/会话/Outbox/审批/记忆逐层过期清理（30 天 / 7 天 / 24h 分层）；数据库空闲页每日自动 VACUUM 收缩

## 日志

- 控制台 INFO；`log/debug-YYYY-MM-DD.log`（debug=True 时，0600 权限、密钥打码）
- 审计：`log/sandbox-audit.log`、`log/hook-audit.log`（独立 logger，追加式）
- 网关运行日志：`gateway/gateway-run.log`（10MB × 5 轮转）

## Web UI 构建

前端为 React 18 + TypeScript + Vite；构建产物不入库。

```bash
cd gateway/webui/frontend
npm install
npm run release:ui     # build + sync 到 gateway/webui/static/（网关热生效，无需重启）

# 开发模式（/api 代理到网关）
npm run dev            # http://127.0.0.1:5173/ui/
```

## 测试

```bash
bash scripts/verify_all.sh      # 一键全量（后端 + 前端）

# 后端
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check core gateway tools memory skills scripts tests agent.py

# 前端
cd gateway/webui/frontend && npm test && npx tsc -b

# 真实浏览器 e2e（需种子网关：python scripts/seed_real_e2e.py）
cd gateway/webui/frontend && npx playwright test --config=playwright.real.config.ts

# 接口冒烟（网关运行中）
.venv/bin/python scripts/api_smoke.py
```

CI：`.github/workflows/ci.yml`（push/PR 自动运行后端 pytest+ruff、前端 tsc+vitest）。

## 文档

- **[CHANGELOG.md](CHANGELOG.md)**：版本记录与功能清单
