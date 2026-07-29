# HelloAgent 配置指南

> 本文件供智能体按需读取——当用户询问如何配置、新增组件时，用 read 工具读取本文件获取操作步骤。

## 添加 MCP 服务器

编辑项目根目录 `config.json`，在 `mcp.servers` 数组中添加一个对象：

```json
{
  "name": "my-server",
  "transport": "streamable",
  "url": "http://localhost:3000/mcp",
  "enabled": true
}
```

字段说明：
- `name`：服务器名称（唯一标识）
- `transport`：传输方式，`streamable`（HTTP）/ `stdio`（本地进程）/ `http+sse`
- `url`：HTTP 传输时的服务器地址
- `command` + `args`：stdio 传输时的启动命令和参数
- `enabled`：是否启用（默认 true，设为 false 可临时禁用而不删除配置）

添加后重启 agent 生效，或在交互模式发送 `/mcp` 查看连接状态。


## 创建技能

**方式 1（推荐）**：在对话中直接告诉 agent "创建一个 xxx 技能"，agent 会使用 `create_skill` 工具自动完成。

**方式 2（手动）**：在 `SKILLS/{技能名}/` 目录下创建两个文件：

`skill.json`：
```json
{"name": "my-skill", "description": "技能描述（LLM 据此判断何时使用）"}
```

`instruction.md`：
```markdown
# 技能名称

## 步骤
1. 第一步操作说明
2. 第二步操作说明
...

完成后输出 FINAL_ANSWER。
```

## 配置 Hook

编辑 `config.json` 的 `hooks` 段：

**内置过滤器**（开箱即用）：
```json
"filters": {
  "sensitive_words": ["password", "api_key"],
  "block_patterns": ["rm -rf /", "format C:"]
}
```

**事件 hook**（按事件配置自定义脚本）：
```json
"hooks": {
  "pre_tool": [{"matcher": "bash|write", "hooks": [{"type": "command", "command": "python my_hook.py"}]}],
  "stop": [{"hooks": [{"type": "command", "command": "curl -X POST https://hook.example/done"}]}]
}
```

支持的 12 个事件：`session_start` / `user_prompt` / `pre_llm` / `post_llm` / `pre_tool` / `post_tool` / `notification` / `denied` / `stop` / `session_end` / `plan_approved` / `task_complete`

修改后发送 `/hook reload` 即时生效。

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

- 修改 `model_id` 切换默认模型
- 在 `models` 中添加新模型配置
- 本地模型填 `provider`（ollama/lm_studio/vllm/llama_cpp），云端模型填 `api_key` + `base_url`

也可运行 `python agent.py init` 使用交互式向导，或在交互模式发送 `/model <名称>` 临时切换。

## 多模态图片

给 agent 发图的途径：
- **飞书**：直接发送图片（bot 会回复"收到图片，你想让我做什么"，同一用户随后发的文字会自动带上该图片）；富文本消息中包含的图片也会一并提取
- **CLI / 工具**：ReadTool 读取图片文件时自动识别并附加到上下文（返回 `[IMAGE:path=...]` 标记）

限制与注意事项：
- **必须使用视觉模型**：纯文本模型（DeepSeek 全系等）不接受图片块，会返回 400（`unknown variant 'image_url'`）。用 `/model` 切换到视觉模型，如阿里云 `qwen-vl-max-latest` / `qwen3-vl-plus`、OpenAI `gpt-4o`、Gemini、Claude
- 注意图片块会留在会话历史中：在视觉模型上发过图后切回纯文本模型，历史中的旧图片同样会导致 400，建议换模型时 `/clear` 清空会话
- 大小：≤5MB 直接使用；5-20MB 由 Pillow 自动压缩；>20MB 拒绝
- 格式：png / jpg / jpeg / gif / webp / bmp / tiff（SVG 不支持 vision，会按文本读取）
- 飞书下载的图片存于 `workspace/tmp/`，超过 7 天自动清理；待认领的图片缓存 30 分钟过期

## 配置权限

```json
"permission": {
  "default_mode": "ask",
  "workspace": "./workspace",
  "tool_rules": {
    "bash": "ask",
    "python": "ask",
    "read": "allow",
    "write": "ask"
  }
}
```

- `default_mode`：`ask`（每次询问）/ `allow`（全部允许）/ `deny`（全部拒绝）
- `workspace`：agent 的工作目录（文件操作的安全边界）
- `tool_rules`：按工具名设置权限级别

## 配置沙箱

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

- `enabled`：沙箱总开关
- `python.forbidden_imports`：Python 代码中禁止导入的模块（可按需增减）
- `python.forbidden_calls`：禁止调用的函数
- `python.forbidden_qualified_calls`：禁止的限定调用（如 `os.system` 禁止但 `os.path.join` 允许）
