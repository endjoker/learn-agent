# HelloAgent 🤖

一个基于 Python 的生产级 AI 智能体框架，支持调用多种工具完成复杂任务。

## ✨ 功能特性

### 已实现的 14 个工具

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

### 核心能力

- **ReAct 循环** — 思考(THOUGHT) → 行动(ACTION) → 观察 → 循环直到给出最终回答
- **多工具并发** — 一次输出多个 ACTION，并发执行，合并结果，减少上下文开销
- **动态步数估算** — 根据任务自动估算最大执行步数
- **模型切换** — 运行时通过 `/model` 命令在线切换模型
- **调试日志** — `--debug` 参数开启，显示 Agent 与 LLM 的完整通信
- **连续对话** — 跨多轮对话保留历史上下文
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

复制 `.env` 文件并填写你的 API 配置：

```env
# 云端模式（DeepSeek 示例）
LLM_API_KEY="your-api-key"
LLM_BASE_URL="https://api.deepseek.com"
LLM_MODEL_ID="deepseek-v4-flash"

# 本地模式（Ollama 示例）
# LLM_PROVIDER=ollama
# LLM_MODEL_ID=gemma4

# 本地模式（手动指定）
# LLM_TYPE=local
# LLM_MODEL_ID=gemma4
# LLM_BASE_URL=http://localhost:11434/v1
# LLM_API_KEY=not-needed
```

### 启动

```bash
# 交互式命令行模式
python agent.py

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
│   ├── llm_client.py        # LLM 客户端（云端/本地模型适配）
│   └── debug.py             # 调试日志模块（带时间戳的 DEBUG 输出）
│
├── tools/                   # 工具系统
│   ├── __init__.py
│   ├── base_tool.py         # 工具基类（接口规范）
│   ├── registry.py          # 工具注册表（管理所有工具）
│   ├── builtin_tools.py     # 内置工具（12 个：文件操作+计算+笔记+HTTP 等）
│   └── web_tools.py         # 网页工具（search + web_fetch）
│
└── code/                    # 学习笔记（可选）
    └── learn/
        └── plan-1.md
```

## 💻 使用指南

### 交互式命令

进入交互模式后，支持以下命令：

| 命令 | 作用 |
|:----|:-----|
| `/model` | 查看当前模型 |
| `/model list` | 列出支持的本地服务商 |
| `/model ollama gemma4` | 切换到本地 Ollama + Gemma4 |
| `/model cloud gpt-4 https://...` | 切换到云端指定模型 |
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
| `LLM_API_KEY` | API 密钥（云端必填，本地可选） | — |
| `LLM_BASE_URL` | API 服务地址 | — |
| `LLM_MODEL_ID` | 模型名称 | — |
| `LLM_TYPE` | 模式：`cloud` 或 `local` | `cloud` |
| `LLM_PROVIDER` | 本地服务商：`ollama` / `lm_studio` / `vllm` / `llama_cpp` | — |
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

## 📄 许可证

本项目仅供学习使用。

## 📝 更新日志

### 2026-07-08 代码优化

**Bug 修复：**
- 修复 `stream_run` 方法中的错误引用（`parsed["action"]` → `parsed["actions"]`），流式模式现在可以正常工作
- 删除 `agent.py` 中重复定义的 `clear_history` 方法

**改进：**
- 为所有 Python 文件添加 UTF-8 编码声明，确保跨平台兼容性
- 重写 `core/debug.py`，使用 Python 标准 `logging` 模块替代全局变量方案
  - 新增 `setup_logging(debug, log_file)` 函数，支持控制台和文件输出
  - 保留所有原有函数接口，向后兼容
- 在 `agent.py`、`core/llm_client.py`、`tools/builtin_tools.py`、`tools/web_tools.py` 中添加错误日志记录
  - LLM 调用失败、工具执行异常等关键错误现在会记录到日志
  - 支持 `exc_info=True` 输出完整堆栈信息
- `create_agent()` 现在自动调用 `setup_logging()` 初始化日志系统

**技术细节：**
- 所有新增的 `import` 都是 Python 标准库（`logging`），无需安装新的外部依赖
- 日志系统使用 `hello_agent` 作为 logger 名称，便于集成到其他项目
