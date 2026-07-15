# learn-agent 🤖

一个基于 Python 的生产级 AI 智能体框架，支持调用多种工具完成复杂任务。

## ✨ 功能特性

### 已实现的 16 个内置工具 + MCP 扩展

| 工具 | 功能 | 说明 |
|:----|:-----|:-----|
| **read** | 读取文件内容 | 支持行号显示、行范围控制 |
| **write** | 创建/覆盖文件 | 自动创建父目录 |
| **edit** | 精确查找替换 | 修改文件某段内容，默认替换第一处 |
| **grep** | 文本搜索 | 正则匹配，支持文件类型过滤 |
| **glob** | 通配符查找文件 | 查找某类文件、浏览项目结构 |
| **bash** | 执行 Shell 命令 | 运行脚本、git 操作，自动适配系统 |
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
| **MCP 工具** | 动态加载 | 连接外部 MCP Server，自动发现工具并注册（数量取决于服务器） |

### 核心能力

- **ReAct 循环** — 思考(THOUGHT) → 行动(ACTION) → 观察 → 循环直到给出最终回答
- **多工具并发** — 一次输出多个 ACTION，并发执行，合并结果，减少上下文开销
- **Plan-and-Execute** — 复杂任务自动出方案 → 用户确认 → 逐任务执行（`/plan` 手动触发）
- **上下文压缩** — 轻量压缩（tool_result 替换元数据）+ 全量压缩（LLM 结构化摘要），60%/80% 阈值自动触发
- **锚点跟踪** — 以 API 返回 `usage` 为锚点精确估算实时 token 占用
- **三级权限管理** — allow/ask/deny，工作区路径检测，bash 命令分类，A/Y/N/S 交互
- **模型配置** — `.env` 按模型名前缀分组，切换模型自动匹配参数和上下文长度
- **模型切换** — 运行时通过 `/model` 命令在线切换模型
- **MCP 集成** — 支持 Model Context Protocol，可连接外部 MCP Server（GitHub/filesystem/网页搜索等），支持 Stdio、HTTP+SSE、StreamableHTTP 三种传输方式
- **MCP 配置** — 通过 `config/mcp_config.json` 文件或代码参数配置，支持多服务器自动发现工具
- **调试日志** — `--debug` 参数开启，调试信息写入 `log/` 目录文件，控制台保持简洁
- **跨会话记忆** — 每轮对话自动归档到 `memory/daily/`，支持 BM25 全文检索回忆
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

`.env` 文件按模型名前缀分组配置：

```env
# 当前使用的模型
LLM_MODEL_ID=deepseek-v4-flash

# deepseek-v4-flash 配置
deepseek-v4-flash_BASE_URL=https://api.deepseek.com
deepseek-v4-flash_API_KEY=sk-xxx
deepseek-v4-flash_CONTEXT_LENGTH=1048576

# 本地模型配置（通过 /model local gemma4 切换）
gemma4_PROVIDER=ollama
gemma4_CONTEXT_LENGTH=262144
```

### MCP 配置

通过 `config/mcp_config.json` 配置外部 MCP 服务器（复制模板文件修改）：

```json
[
  {
    "name": "web-search",
    "transport": "streamable",
    "url": "http://192.168.1.110:3000/mcp"
  },
  {
    "name": "filesystem",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
  }
]
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
├── requirements.txt         # Python 依赖列表
├── .env                     # 环境变量配置（API Key、模型等）
├── .gitignore               # Git 忽略规则
│
├── core/                    # 核心组件
│   ├── __init__.py
│   ├── llm_client.py        # LLM 客户端（云端/本地模型适配，模型前缀参数读取）
│   ├── message_store.py     # 消息存储模块（持久化、上下文统计、锚点跟踪、会话管理）
│   ├── compressor.py        # 上下文压缩（轻量 + 全量 LLM 摘要）
│   ├── task_list.py         # 任务清单（Plan-and-Execute 数据模型）
│   ├── debug.py             # 调试日志模块（带时间戳的 DEBUG 输出）
│   ├── permission.py        # 权限管理模块（allow/ask/deny 三级）
│   ├── system_prompt.py     # System Prompt 构建器（静态区/动态区）
│   └── mcp_client.py        # MCP 客户端（Stdio/SSE/StreamableHTTP 传输 + JSON-RPC 协议）
│
├── tools/                   # 工具系统
│   ├── __init__.py
│   ├── base_tool.py         # 工具基类（接口规范）
│   ├── registry.py          # 工具注册表（管理所有工具）
│   ├── builtin_tools.py     # 内置工具（14 个：文件操作+计算+笔记+HTTP+记忆等，含 file_mgr 批量删除）
│   ├── memory_tools.py      # 记忆工具（memory_search + memory_update）
│   ├── web_tools.py         # 网页工具（search + web_fetch）
│   └── mcp_tools.py         # MCP 工具桥接层（将 MCP 工具适配为 BaseTool）
│
├── config/                  # 配置文件目录
│   ├── mcp_config.json      # MCP 服务器配置（含 Token，已 gitignore）
│   └── mcp_config.template.json  # MCP 配置模板（含三种传输方式示例）
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
| `/mcp` | 查看 MCP 服务器连接状态 |
| `/mcp list` | 查看 MCP 服务器详情与工具列表 |
| `/compact` | 手动触发全量上下文压缩 |
| `/clear` | 清空对话历史 |
| `/help` | 显示帮助信息 |
| `exit` | 退出程序 |

### 代码中使用

```python
from agent import create_agent

# 创建 Agent（自动从 .env 加载配置）
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

`.env` 文件支持以下配置项：

| 变量 | 说明 | 默认值 |
|:----|:-----|:------|
| `LLM_MODEL_ID` | 当前使用的模型名称 | `deepseek-v4-flash` |
| `{model}_BASE_URL` | 模型专属 API 地址 | `https://api.deepseek.com` |
| `{model}_API_KEY` | 模型专属 API 密钥 | — |
| `{model}_CONTEXT_LENGTH` | 模型上下文窗口大小 | `1048576` |
| `{model}_PROVIDER` | 本地模型服务商 | `ollama` |
| `LLM_TIMEOUT` | 请求超时秒数 | `60` |

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

## 📄 许可证

本项目仅供学习使用。

## 📝 更新日志

版本历史请查看 [CHANGELOG.md](CHANGELOG.md)。
