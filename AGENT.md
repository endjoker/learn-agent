# 项目记忆

## 项目概述

- **项目名称**: HelloAgent
- **项目位置**: `d:\project\hello-agent`
- **项目定位**: 一个基于 Python 的生产级 AI 智能体框架，支持调用多种工具完成复杂任务
- **工作模式**: ReAct（Reasoning + Acting）循环

## 架构概览

```
hello-agent/
├── agent.py                 # Agent 主程序（ReAct 循环 + CLI）
├── core/                    # 核心组件
│   ├── llm_client.py        # LLM 客户端（云端/本地/多 Provider）
│   ├── debug.py             # 调试日志模块
│   └── system_prompt.py     # System Prompt 构建器
├── tools/                   # 工具系统
│   ├── base_tool.py         # 工具基类
│   ├── registry.py          # 工具注册表
│   ├── builtin_tools.py     # 内置工具（12 个）
│   └── web_tools.py         # 网页工具（search + web_fetch）
├── requirements.txt
└── .env                     # API 配置
```

## 核心技术决策

### 工具系统
- **注册表模式**: 所有工具通过 `ToolRegistry` 统一管理
- **基类规范**: 每个工具继承 `BaseTool`，实现 `name`、`description`、`parameters`、`execute()`
- **自描述**: 工具通过 JSON Schema 描述参数，自动生成 LLM 可读的说明

### LLM 客户端
- **双模式**: 支持云端 API（OpenAI、DeepSeek 等）和本地模型（Ollama、LM Studio、vLLM、llama.cpp）
- **配置驱动**: 通过 `.env` 文件配置，`HelloAgentsLLM` 自动加载
- **自动 Provider**: 本地模式下通过 `LLM_PROVIDER` 自动补全地址

### Agent 核心
- **ReAct 循环**: THOUGHT → ACTION → INPUT → 观察 → 循环 → FINAL_ANSWER
- **多工具并发**: 单步可输出多个 ACTION，并发执行后合并结果
- **动态步数**: 根据任务复杂度自动估算最大步数
- **对话历史**: 跨 `run()` 调用保留历史，支持上下文截断

### System Prompt
- **分区设计**: 静态区（角色/风格/规则）+ 动态区（目录/时间/OS/记忆）
- **会话指令**: 通过 `add_session_instruction()` 动态注入本轮指令
- **项目记忆**: 自动加载根目录 `AGENT.md` 到动态区

## 关键约定

- 代码文件使用 `# -*- coding: utf-8 -*-` 编码声明
- 日志使用 `logging.getLogger('hello_agent')`，统一 logger 名称
- 工具注册用 `registry.register_tool(MyTool())`
- ReAct 标签必须用英文（`THOUGHT`、`ACTION`、`INPUT`、`FINAL_ANSWER`）
- 回复内容用中文，标签必须用英文
