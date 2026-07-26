# learn-agent 🤖

一个基于 Python 的生产级 AI 智能体框架，支持调用多种工具完成复杂任务。

## ✨ 功能特性

### 已实现的 22 个内置工具 + MCP 扩展 + 技能

| 工具 | 功能 | 说明 |
|:----|:-----|:-----|
| **read** | 读取文件内容 | 支持行号显示、行范围控制 |
| **write** | 创建/覆盖文件 | 自动创建父目录 |
| **edit** | 精确查找替换 | 修改文件某段内容，默认替换第一处 |
| **grep** | 文本搜索 | 正则匹配，支持文件类型过滤 |
| **glob** | 通配符查找文件 | 查找某类文件、浏览项目结构 |
| **bash** | 执行 Shell 命令 | 一次性命令、git 操作，自动适配系统 |
| **proc_start** | 启动长驻进程 | dev server / watcher / REPL，跨步存活 |
| **proc_send** | 向进程投喂 stdin | REPL 交互、y/n 确认 |
| **proc_read** | 增量读进程输出 | 不阻塞，后台缓冲区取数据 |
| **proc_list** | 列出进程会话 | ID/名称/状态/退出码/空闲时长 |
| **proc_stop** | 终止进程 | 杀整树（Windows taskkill /F /T） |
| **search** | 网页搜索 | 基于 DuckDuckGo/Bing，无需 API Key |
| **web_fetch** | 读取网页正文 | 提取文章/新闻的纯文本内容 |
| **calculate** | 数学计算 | 安全执行表达式，支持 sqrt/sin 等 |
| **datetime** | 时间日期 | 获取当前日期、时间、星期 |
| **notes** | 笔记/记忆 | 跨对话记住关键信息 |
| **file_mgr** | 文件管理 | 复制/移动/删除/创建目录 |
| **python** | 执行 Python 代码 | 运行代码片段并返回输出 |
| **http** | HTTP 请求 | GET/POST，支持外部 API 调用 |
| **memory_search** | 搜索记忆 | 基于 BM25 + 中文分词的跨会话记忆检索 |
| **memory_update** | 更新记忆权重 | 标记记忆有用/无用，影响后续排序 |
| **create_skill** | 创建技能 | LLM 运行时将重复流程封装为可复用技能（写磁盘 + 注册 + 重建 prompt） |
| **MCP 工具** | 动态加载 | 连接外部 MCP Server，自动发现工具并注册（数量取决于服务器） |

### 核心能力

- **ReAct 循环** — 思考(THOUGHT) → 行动(ACTION) → 观察 → 循环直到给出最终回答
- **多工具并发** — 一次输出多个 ACTION，并发执行，合并结果，减少上下文开销
- **Plan-and-Execute** — 复杂任务自动出方案 → 用户确认 → 逐任务执行（`/plan` 手动触发）
- **上下文压缩** — 轻量压缩（tool_result 替换元数据）+ 全量压缩（LLM 结构化摘要），60%/80% 阈值自动触发
- **锚点跟踪** — 以 API 返回 `usage` 为锚点精确估算实时 token 占用
- **三级权限管理** — allow/ask/deny，工作区路径检测，bash 命令分类，A/Y/N/S 交互
- **多协议适配层** — 支持 OpenAI / Anthropic / Gemini 三种协议，`create_adapter()` 自动检测或显式指定，lazy import 按需加载
- **统一配置（config.json）** — 所有配置集中在 `config.json`（llm / permission / hooks / mcp / sandbox），支持环境变量覆盖 API Key 等敏感信息
- **模型配置** — `config.json` 按模型名分组配置，切换模型自动匹配参数和上下文长度
- **模型切换** — 运行时通过 `/model` 命令在线切换模型
- **MCP 集成** — 支持 Model Context Protocol，可连接外部 MCP Server（GitHub/filesystem/网页搜索等），支持 Stdio、HTTP+SSE、StreamableHTTP 三种传输方式
- **MCP 配置** — 通过 `config.json` 的 `mcp.servers` 配置，支持多服务器自动发现工具
- **调试日志** — `--debug` 参数开启，调试信息写入 `log/` 目录文件，控制台保持简洁
- **跨会话记忆** — 每轮对话自动归档到 `memory/daily/`，支持 BM25 全文检索回忆
- **学习型技能系统** — AI 每轮学习到的可复用能力持久化到 `SKILLS/` 目录，LLM 可通过 `create_skill` 工具运行时创建技能，交互式 `/skill` 命令查看和调用
- **沙箱执行器（L2 两层防护）** — L2-A 内容拦截（敏感文件/数据外发/Python AST/网络黑名单）+ L2-C subprocess 执行 + 超时控制；支持配置档切换（agent/restricted/permissive）和临时绕过（L2-B 资源隔离为设计预留）
- **长驻子进程模块** — `ProcessManager` 管理跨步存活的长驻进程（dev server / watcher / REPL），5 个 proc_* 工具，增量读取、stdin 投喂、杀整树；idle 超时自动清理；区外 cwd 拒绝启动
- **L2 危险命令下沉** — `rm -rf /` / `format` / `mkfs` 等 OS 危害强制 L2 硬拦（白名单不可绕过），bash 和 proc_start 统一兜底
- **安全闸门 SecurityGate** — capability 驱动的统一权限+沙箱检查点，覆盖内置/MCP/skill/未来工具；未知工具默认 ASK；技能注册自动 ALLOW
- **Hook 模块（事件驱动）** — 12 个生命周期事件（`pre_tool`/`post_tool`/`user_prompt`/`stop`/`notification`/`denied`…），支持 Python 回调 + 命令式 hook（JSON stdin/stdout 协议，与 Claude Code 兼容）；内置过滤器（敏感词/危险模式拦截）配好即生效；仅可加严不可放松（在 SecurityGate 之后运行）
- **交互命令** — `/model`、`/plan`、`/session`、`/mcp`、`/skill`、`/sandbox`、`/proc`、`/hook`、`/stats`、`/history`、`/compact`、`/clear`、`/help`
- **连续对话** — 跨多轮对话保留历史上下文，自动截断防超限
- **会话持久化** — 每轮对话自动保存到 `sessions/{id}.json`，含任务清单序列化
- **会话恢复** — `--resume <id>` 恢复指定会话，`--resume last` 按修改时间取最新会话
- **上下文监控** — `/stats` 命令实时查看 token 占用、消息统计和按角色分布
- **云端+本地** — 支持云端 API（OpenAI/DeepSeek）和本地模型（Ollama/LM Studio/vLLM/llama.cpp）

## 🚀 快速开始

### 环境要求

- Python 3.10+
- pip

### 安装

```bash
# 1. 克隆项目
git clone <你的仓库地址>
cd hello-agent

# 2. 创建虚拟环境（推荐）
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt
```

### 配置

复制 `config.example.json` 为 `config.json`，按需修改：

```bash
cp config.example.json config.json
```

`config.json` 包含五个 section：

```json
{
  "llm": {
    "model_id": "deepseek-v4-flash",
    "timeout": 60,
    "models": {
      "deepseek-v4-flash": {
        "api_key": "sk-xxx",
        "base_url": "https://api.deepseek.com",
        "context_length": 1048576
      },
      "claude-sonnet-4": {
        "api_key": "sk-ant-xxx",
        "protocol": "anthropic"
      }
    }
  },
  "permission": { "..." : "权限配置" },
  "hooks": { "..." : "Hook 配置（事件钩子 + 过滤器）" },
  "mcp": { "servers": ["MCP 服务器列表"] },
  "sandbox": { "..." : "沙箱配置（敏感文件/危险命令/网络黑名单）" }
}
```

API Key 等敏感信息也可通过环境变量注入（优先级高于 config.json）：
- `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY`
- `LLM_PROTOCOL` — 覆盖协议检测

### MCP 配置

在 `config.json` 的 `mcp.servers` 中配置外部 MCP 服务器：

```json
{
  "mcp": {
    "servers": [
      {
        "name": "web-search",
        "transport": "streamable",
        "url": "http://127.0.0.1:3000/mcp"
      },
      {
        "name": "filesystem",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
      }
    ]
  }
}
```

支持三种传输方式：

| 传输类型 | 说明 | 配置字段 |
|:---------|:-----|:---------|
| `stdio` | 本地子进程（npx 启动的服务） | `command` + `args` + `env` |
| `http+sse` | 远程 HTTP+SSE（旧版 MCP 协议） | `url` + `headers` |
| `streamable` | 远程 Streamable HTTP（新版 MCP 协议） | `url` + `headers` |

MCP 工具会自动添加 `{name}/` 前缀（如 `web-search/search`），LLM 无需感知工具是本地还是远程。

### 启动

```bash
# 交互式命令行模式
python agent.py

# 恢复历史会话
python agent.py --resume a7f3e2c9
python agent.py --resume last

# 直接提问
python agent.py "帮我看看当前目录有什么文件"

# 开启调试模式（查看完整通信内容）
python agent.py --debug
```

## 📁 目录结构

```
hello-agent/
├── agent.py                 # Agent 主程序入口（ReAct 循环 + 交互式 CLI）
├── config.json              # 统一配置文件（从 config.example.json 复制）
├── config.example.json      # 配置模板（含全部 section 示例）
├── requirements.txt         # Python 依赖列表
├── .gitignore               # Git 忽略规则
│
├── core/                    # 核心组件
│   ├── __init__.py
│   ├── config_loader.py     # 统一配置加载器（config.json 读取 + 环境变量覆盖 + 默认值）
│   ├── llm_client.py        # LLM 客户端（通过协议适配器调用，支持三种协议）
│   ├── message_store.py     # 消息存储模块（持久化、上下文统计、锚点跟踪、会话管理）
│   ├── compressor.py        # 上下文压缩（轻量 + 全量 LLM 摘要）
│   ├── task_list.py         # 任务清单（Plan-and-Execute 数据模型）
│   ├── debug.py             # 调试日志模块（带时间戳的 DEBUG 输出）
│   ├── permission.py        # 权限管理模块（allow/ask/deny 三级）
│   ├── system_prompt.py     # System Prompt 构建器（静态区/动态区）
│   ├── mcp_client.py        # MCP 客户端（Stdio/SSE/StreamableHTTP 传输 + JSON-RPC 协议）
│   ├── security_gate.py     # 安全闸门（capability 驱动的统一权限+沙箱检查点）
│   ├── protocols/           # LLM 协议适配器层
│   │   ├── __init__.py      # 工厂函数 create_adapter() + detect_protocol()
│   │   ├── base.py          # 抽象基类 ProtocolAdapter + ChatResponse
│   │   ├── openai_adapter.py    # OpenAI Chat Completions（兼容 DeepSeek/Ollama/vLLM）
│   │   ├── anthropic_adapter.py # Anthropic Messages API（Claude 系列）
│   │   └── gemini_adapter.py    # Gemini generateContent
│   ├── sandbox/             # 沙箱执行器（L2 两层防护）
│   │   ├── __init__.py
│   │   ├── executor.py      # SandboxExecutor（开关/配置档/绕过/核心执行流）
│   │   ├── guard.py         # L2-A 内容拦截（敏感文件/数据外发/Python AST/网络黑名单）
│   │   ├── profiles.py      # 配置档管理（从 config.json sandbox section 加载）
│   │   └── audit.py         # 审计日志（记录拦截/绕过/错误事件）
│   └── hook/                # Hook 模块（事件驱动生命周期钩子）
│       ├── __init__.py
│       ├── events.py        # HookEvent 枚举 + HookContext + Decision/HookResult
│       ├── hooks.py         # BaseHook / PythonHook / CommandHook（含 from_config 安全预检）
│       ├── manager.py       # HookManager（注册/分发/合并/配置加载/过滤器）
│       └── builtin.py       # 内置 hook（审计日志/通知/敏感词过滤/模式拦截）
│
├── tools/                   # 工具系统
│   ├── __init__.py
│   ├── base_tool.py         # 工具基类（接口规范）
│   ├── registry.py          # 工具注册表（三区分离展示：内置工具/MCP/技能）
│   ├── builtin_tools.py     # 内置工具（含沙箱注入的 6 个工具）
│   ├── memory_tools.py      # 记忆工具（memory_search + memory_update）
│   ├── web_tools.py         # 网页工具（search + web_fetch）
│   └── mcp_tools.py         # MCP 工具桥接层（将 MCP 工具适配为 BaseTool）
│
├── skills/                  # 学习型技能系统
│   ├── __init__.py
│   ├── skill.py             # Skill 数据模型
│   ├── manager.py           # SkillManager（磁盘 I/O + CRUD）
│   ├── skill_tool.py        # SkillTool + CreateSkillTool（执行/创建）
│   └── code-review/         # 预置技能示例
│       ├── skill.json
│       └── instruction.md
│
├── memory/                  # 跨会话记忆系统
│   ├── __init__.py
│   ├── manager.py           # MemoryManager（保存/检索/权重管理）
│   └── memory.md            # 记忆配置引用（加载到 System Prompt）
│
├── sessions/                # 会话文件（.gitignore）
│   └── *.json               # 自动保存的会话数据
│
└── log/                     # 调试日志文件（.gitignore）
    └── debug-*.log          # --debug 模式自动生成
```

## 💻 使用指南

### 交互式命令

进入交互模式后，支持以下命令：

| 命令 | 作用 |
|:----|:-----|
| `/model` | 查看当前模型 |
| `/model list` | 列出支持的本地服务商 |
| `/model local gemma4` | 切换到本地模型 |
| `/model deepseek-v4-flash` | 切换到云端模型 |
| `/session` | 查看/管理会话 |
| `/session list` | 列出所有历史会话 |
| `/session save` | 保存当前会话 |
| `/session delete <id>` | 删除指定会话（支持批量） |
| `/stats` | 查看上下文占用统计 |
| `/history` | 查看当前会话内容 |
| `/plan [任务描述]` | 生成并执行任务方案（Plan-and-Execute） |
| `/skill list` | 列出所有学习型技能 |
| `/skill <name>` | 直接调用指定技能 |
| `/skill delete <name>` | 删除指定技能 |
| `/sandbox` | 查看沙箱状态 |
| `/sandbox on/off` | 开启/关闭沙箱 |
| `/sandbox strict` | 切换到严格模式（restricted） |
| `/sandbox bypass` | 临时绕过沙箱（下一条命令） |
| `/sandbox profile <name>` | 切换沙箱配置档 |
| `/hook` | 查看已注册 hook |
| `/hook reload` | 重新加载 config.json 的 hooks 配置 |
| `/mcp` | 查看 MCP 服务器连接状态 |
| `/mcp list` | 查看 MCP 服务器详情与工具列表 |
| `/compact` | 手动触发全量上下文压缩 |
| `/clear` | 清空对话历史 |
| `/help` | 显示帮助信息 |
| `exit` | 退出程序 |

### 代码中使用

```python
from agent import create_agent

# 创建 Agent（自动从 config.json 加载配置）
agent = create_agent()

# 简单对话
result = agent.run("帮我看看当前目录")
print(result)

# 带调试日志
agent = create_agent(debug=True)

# 自定义模型
agent = create_agent(
    name="代码助手",
    max_steps=30,
    debug=True,
)

# 运行时切换模型
agent.switch_llm(provider="ollama", model="gemma4")
agent.switch_llm(model="gpt-4", base_url="https://api.openai.com/v1", llm_type="cloud")
```

### 创建自定义工具

```python
from tools import BaseTool, ToolRegistry

class MyTool(BaseTool):
    name: str = "my_tool"
    description: str = "我的自定义工具"
    parameters: dict = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "输入内容"},
        },
        "required": ["input"],
    }

    def execute(self, input: str) -> str:
        return f"处理结果: {input}"

# 注册到 Agent
from agent import create_agent
from tools.builtin_tools import register_all_tools
from tools.web_tools import register_web_tools

registry = ToolRegistry()
register_all_tools(registry)
register_web_tools(registry)
registry.register_tool(MyTool())
```

## ⚙️ 配置说明

`config.json` 的 `llm` section 支持以下配置项：

| 字段 | 说明 | 默认值 |
|:----|:-----|:------|
| `model_id` | 当前使用的模型名称 | `""` |
| `timeout` | 请求超时秒数 | `60` |
| `models.{name}.api_key` | 模型 API 密钥 | — |
| `models.{name}.base_url` | 模型 API 地址 | — |
| `models.{name}.protocol` | 协议类型（openai / anthropic / gemini） | 自动检测 |
| `models.{name}.context_length` | 模型上下文窗口大小 | 自动检测 |
| `models.{name}.provider` | 本地模型服务商（ollama / lm_studio / vllm） | — |

环境变量覆盖（优先级高于 config.json）：

| 变量 | 说明 |
|:----|:-----|
| `ANTHROPIC_API_KEY` | Anthropic API 密钥 |
| `GEMINI_API_KEY` | Gemini API 密钥 |
| `OPENAI_API_KEY` | OpenAI API 密钥 |
| `LLM_PROTOCOL` | 覆盖协议检测 |
| `LLM_TIMEOUT` | 覆盖请求超时 |

## 🧪 调试模式

```bash
# 启动时加 --debug 参数
python agent.py --debug

# 或在代码中开启
agent = create_agent(debug=True)
```

调试日志每行带 `[DEBUG 时间戳]` 前缀，与正常输出区分，显示：

- 发送给 LLM 的完整消息列表
- LLM 返回的原始响应
- 工具调用的参数与返回结果
- 上下文截断等内部状态

## 🏗️ 已实现的技术特性

- [x] ReAct（Reasoning + Acting）循环
- [x] 工具注册表模式（ToolRegistry）
- [x] 多种 Agent 范式支持（SimpleAgent / ReActAgent）
- [x] 流式输出（SSE）
- [x] 跨平台 Shell 执行（Windows / macOS / Linux）
- [x] 并发多工具调用（ThreadPoolExecutor）
- [x] 动态 LLM 配置（云端/本地/多 Provider）
- [x] 运行时模型切换
- [x] 连续对话历史
- [x] 调试日志系统（带时间戳）
- [x] 中文友好的注释和交互
- [x] 网页搜索（search + web_fetch）
- [x] 数学计算器（calculate）
- [x] 笔记/记忆系统（notes）
- [x] 文件管理（file_mgr）
- [x] Python 代码执行（python）
- [x] HTTP 请求（http）
- [x] 三级权限管理（PermissionChecker）
- [x] 多工具并发执行（ThreadPoolExecutor）
- [x] 模型前缀参数配置（`{model}_KEY` 模式）
- [x] 消息存储模块（MessageStore）
- [x] 会话持久化（save/load/delete 全量覆写）
- [x] 会话恢复（`--resume <id>` / `--resume last`）
- [x] 会话管理命令（`/session info/list/save/delete`）
- [x] 上下文占用统计（`/stats`）
- [x] 消息变更通知回调（`on_update`，为 UI 预留）
- [x] 跨会话长期记忆（MemoryManager）
- [x] 记忆搜索（memory_search + BM25 + jieba 中文分词）
- [x] 记忆权重管理（自动递增 + 软删除 < -5）
- [x] 记忆自动归档（同会话同天合并同一条，全量覆写）
- [x] 记忆配置引用（memory.md → SystemPrompt 动态加载）
- [x] 调试日志写入文件（`--debug` 输出到 `log/`，控制台精简）
- [x] XML 标签兼容（`<THOUGHT>` / `<FINAL_ANSWER>` 等）
- [x] ReAct 解析增强（INPUT 边界修复 + 特殊 token 清理）
- [x] /session delete 批量删除会话
- [x] 上下文压缩（轻量压缩 + 全量 LLM 摘要，60%/80% 阈值）
- [x] 锚点式 token 跟踪（API usage 校准）
- [x] Plan-and-Execute 任务清单（/plan 自动出方案 + 逐任务执行）
- [x] 任务清单持久化（保存到 session JSON）
- [x] file_mgr 批量删除（paths 参数）
- [x] 非 dict INPUT 防御（友好提示 LLM 修正）
- [x] LLM 重试逻辑（网络错误自动重试 3 次，退避 1s→2s→4s，流式降级非流式）
- [x] MCP 模块接入（Stdio/SSE/StreamableHTTP 三种传输，自动发现工具）
- [x] 常驻事件循环（`_MCPLoopRunner` 修复跨循环工具调用超时）
- [x] 接收循环早退修复（MCP 空闲超时/SSE keepalive/Streamable 阻塞）
- [x] 实时超时参数（`think()` 支持 `timeout` 参数，参数 > 环境变量）
- [x] SystemPrompt 三区分离（内置工具 / MCP 工具 / 技能 分区块展示）
- [x] MCP 工具前缀命名（`{server_name}/` 防冲突）+ 独立展示
- [x] 沙箱执行器（L2 两层防护：内容拦截 → subprocess 执行）
- [x] 沙箱内容拦截（敏感文件/数据外发/Python AST/网络黑名单/输出脱敏）
- [x] 沙箱配置档（agent/restricted/permissive，内存/CPU/超时/网络可控）
- [x] 沙箱绕过机制（`bypass_next()` 单条放行，执行后自动恢复）
- [x] 沙箱审计日志（拦截/绕过/错误事件记录到 `log/sandbox-audit.log`）
- [x] 6 工具沙箱注入（bash/python/write/edit/read/http 全部经过安全审查）
- [x] 学习型技能系统（Skills 持久化到 `SKILLS/` 目录，支持 CRUD）
- [x] SkillTool 执行适配器（返回指令文本，LLM 按步骤执行）
- [x] CreateSkillTool 运行时创建（LLM 自动写磁盘 + 注册 + 重建 prompt）
- [x] 预置 code-review 技能
- [x] 统一配置加载器（config.json 唯一来源 + 环境变量覆盖 + 硬编码默认值）
- [x] 多协议 LLM 适配层（OpenAI / Anthropic / Gemini，lazy import 按需加载）
- [x] 协议自动检测（detect_protocol 根据 base_url 域名推断）
- [x] Hook 配置式加载（CommandHook.from_config 含注册期安全预检）
- [x] Hook 内置过滤器（sensitive_words / block_patterns 配好即生效）

## 📄 许可证

本项目仅供学习使用。

## 📝 更新日志

版本历史请查看 [CHANGELOG.md](CHANGELOG.md)。
