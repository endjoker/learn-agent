# JKagent 配置指南

> 本文件供智能体按需读取——当用户询问如何配置、新增组件时，用 read 工具读取本文件获取操作步骤。
> 所有配置位于项目根目录 `config.json`（已被 .gitignore 忽略，不会提交；云端 key 也可用环境变量提供）。
> 交互式初始化：`python agent.py init`（LLM/MCP/Hook 向导 + 自动补齐缺失默认键）。

## 启动与命令

> 首次安装可用一键脚本：`bash scripts/install.sh`（依赖 + 前端构建 + 最小配置），
> 模型/密钥在 WebUI 设置页在线填写即可，init 向导可选。

```bash
jkagent-gateway run                # 启动 Gateway / WebUI（默认 http://127.0.0.1:9120/ui/）
jkagent-gateway run --port 9200    # 指定端口
jkagent-gateway doctor             # 配置体检（渠道凭据/定时任务/保留期/WebUI 门禁）
jkagent-gateway migrate-sessions [--dry-run]   # 旧 JSON 转录 → 统一会话库（见文末）
python agent.py init               # 交互式初始化向导
```

会话内命令（输入框以 `/` 触发）：`/model`、`/perm`、`/reasoning`、`/mcp`、`/compact`、`/clear`、`/hook reload` 等。

## 添加 / 切换模型

编辑 `config.json` 的 `llm` 段：

```json
"llm": {
  "model_id": "deepseek-v4-flash",
  "models": {
    "deepseek-v4-flash": {
      "api_key": "sk-xxx",
      "base_url": "https://api.deepseek.com",
      "context_length": 1048576
    },
    "gemma4": {
      "provider": "ollama",
      "context_length": 262144
    }
  }
}
```

- 修改 `model_id` 切换默认模型；`models` 中每项即一个可用模型
- 云端模型填 `api_key` + `base_url`；本地模型填 `provider`（ollama / lm_studio / vllm / llama_cpp）
- 运行期切换：WebUI 顶栏模型下拉或 `/model <名称>`（会话级偏好，持久化到该会话）

## 注册 MCP 服务器

`config.json` 的 `mcp.servers` 数组：

```json
{
  "name": "my-server",
  "transport": "streamable",
  "url": "http://localhost:3000/mcp",
  "enabled": true
}
```

- `transport`：`streamable`（HTTP）/ `stdio`（本地进程，用 `command` + `args`）/ `http+sse`
- `enabled`：false 可临时禁用而不删除配置
- Agent 创建后 MCP 后台预热（不阻塞首轮对话）；修改配置后重启网关生效，会话内 `/mcp` 查看连接状态
- MCP 工具统一归类为 `remote:call` 能力，走四档权限裁决（readonly/ask 档会要求确认）

## 配置 Hook

`config.json` 的 `hooks` 段。

**内置过滤器**（开箱即用）：

```json
"filters": {
  "sensitive_words": ["password", "api_key"],
  "block_patterns": ["rm -rf /", "format C:"]
}
```

**事件 hook**（按事件挂自定义脚本）：

```json
"hooks": {
  "pre_tool": [{"matcher": "bash|write", "hooks": [{"type": "command", "command": "python my_hook.py"}]}],
  "stop": [{"hooks": [{"type": "command", "command": "curl -X POST https://hook.example/done"}]}]
}
```

支持 8 个事件：`session_start` / `user_prompt` / `pre_tool` / `post_tool` / `notification` / `denied` / `stop` / `plan_approved`。payload 约定见 `core/hook/events.py`。修改后 `/hook reload` 热重载生效（重新读取 hooks 段；未知事件名与 python 类型 hook 会被忽略）。

## 发送图片（多模态）

**WebUI（主会话 / 工作区会话）**：输入框直接粘贴/附加图片 + 文字一起发送。

- 队列载荷支持图片信封（≤4 张、单张 ≤4MB、png/jpeg/webp/gif）
- 原图落盘 `workspace/.agent/state/images/<会话>/`，历史中以缩略图展示；删除会话时级联清理
- 会话重启后模型看到的是 `[图片已存档: …]` 占位（原图不回放进上下文，需要细看时重新贴图）

**飞书**：直接发图（bot 引导后续文字；富文本中的图片自动提取）。下载图存 `workspace/tmp/`，7 天自动清理。

**工具**：ReadTool 读取图片文件自动附加上下文（`[IMAGE:path=...]` 标记）。

限制：
- **必须视觉模型**（qwen-vl / gpt-4o / Gemini / Claude 等；纯文本模型对图片块报 400）。在视觉模型会话中发过图后切回纯文本模型，历史里的旧图片块仍会 400——建议 `/clear`
- 文件图 ≤5MB 直接用；5-20MB Pillow 自动压缩；>20MB 拒绝；SVG 不支持 vision
- 视觉图片源有四档权限感知的白名单（`core/protocols/vision.py`）

## 配置权限（四档，唯一权威 PolicyEngine）

```json
"permission": {
  "default_mode": "ask",
  "workspace": "./workspace",
  "extra_workspaces": ["."]
}
```

- `default_mode` 四档：
  - `readonly`：变更/执行/未分类工具一律拒绝；读放行；网络与系统路径需确认
  - `ask`：敏感操作每次询问（执行、写入、系统路径、网络）
  - `allow`：工作区内放行；工作区外副作用/执行、系统路径仍询问
  - `unreviewed`：全部放行；高危命令硬拦与 L2 硬闸（若启用）仍生效
- `workspace` / `extra_workspaces`：文件操作安全边界（allow 档"工作区外需确认"的判定依据）
- **没有 `tool_rules` 按工具配置**——工具权限由能力分类（fs:read / fs:write / exec:shell / net:egress…）经四档真值表统一裁决（`core/policy_engine.py`）；WebUI 顶栏可按会话即时切档

## 配置沙箱（L2 硬闸，默认关闭）

```json
"sandbox": {
  "enabled": true,
  "python": {
    "forbidden_imports": ["subprocess", "ctypes", "socket"],
    "forbidden_calls": ["eval", "exec", "__import__"],
    "forbidden_qualified_calls": ["os.system", "os.remove", "shutil.rmtree"]
  }
}
```

- `enabled`：硬闸门总开关（默认 false，安全兜底由四档权限承担）
- `python.forbidden_imports / forbidden_calls / forbidden_qualified_calls`：Python 代码黑名单
- **规则热更**：`sandbox` 段（dangerous_words / sensitive_files / system_paths / python）保存后下一次安全检查自动生效（mtime 校验），无需重启

## 长期记忆

- 主会话共享 `memory/daily/`；工作区会话独立 `memory/workspaces/<workspace_id>/`
- 检索靠 LLM 工具 `memory_search`（BM25 中文分词）；`memory_update` 反馈有用/无用（权重排序）
- 入库内容只保留：用户提问、assistant 终答（≤2000 字符）、工具结果单行短桩；过程旁白与系统注入不入库
- 保留期：≥15 天负分、≥30 天零分自动清除；正权重永久保留（归档时每日自动清理一次）

## 数据清理与保留期

| 数据 | 窗口 | 机制 |
|---|---|---|
| 终态任务 / Artifacts | 30 天 | `retention` 段，每小时循环 |
| 任务域 sessions 表 | 30 天（无引用才删） | 同上 |
| system 会话（sched:/heartbeat:） | 7 天 | `conversation.retention.system_days` |
| 已发布 Outbox / 幂等记录 / 回执 | 24h | 每小时 |
| 数据库空闲页 | 空闲 >30% 且 >50MB 时 VACUUM | 每日至多一次（自动） |
| 迁移备份 runtime.db.bak* | 最新 1 份；>30 天删除 | 同上 |
| 记忆条目 | 见上节 | 归档时每日一次 |
| workspace/tmp 与 agent 输出日志 | 7 天 | janitor 每轮 |

## 会话存储迁移（sessions/*.json 退役）

统一会话以 SQLite（`conversation_sessions`）为唯一权威，旧 JSON 转录已停写，恢复历史走 SQLite 回放。存量文件迁移：

1. 预览：`jkagent-gateway migrate-sessions --dry-run`
2. 迁移（幂等）：`jkagent-gateway migrate-sessions`
3. 验证：`SELECT session_key, origin, subtype FROM conversation_sessions WHERE origin='legacy_import';`
4. 清理旧文件：`python scripts/cleanup_legacy_sessions.py`（迁移命令只读不删）

启动时检测到未迁移文件会打印 warning（不阻断）。
