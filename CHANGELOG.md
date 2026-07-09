# 更新日志

## 2026-07-09 权限管理 & 调用链 & 网页工具

**新增：**
- 新增 `core/permission.py`：三级权限模块（allow/ask/deny）
  - 按工具和工作区路径动态判断权限
  - A/Y/N/S 交互式确认，A 选项可一次性放行工作区
  - bash 命令自动分类：只读命令放行、写命令确认、危险命令拒绝
- 新增 `tools/web_tools.py`：网页搜索和抓取工具
  - `search` — 基于 DuckDuckGo/Bing 的网页搜索，无需 API Key
  - `web_fetch` — 读取指定网页正文内容
- 新增 6 个内置工具到 `tools/builtin_tools.py`
  - `calculate` — 安全数学计算（支持 sqrt、len、列表等）
  - `datetime` — 获取当前日期/时间
  - `notes` — 跨对话笔记存储
  - `file_mgr` — 文件管理（ls/copy/move/delete/mkdir）
  - `python` — 执行 Python 代码片段
  - `http` — HTTP 请求（GET/POST）

**调用链优化：**
- 支持多 ACTION 并发执行：`ThreadPoolExecutor` 并发运行多个独立工具
- 合并结果为一条消息，减少 LLM 往返次数和上下文开销

**模型配置重构：**
- `.env` 改为按 `{模型名}_` 前缀匹配参数，每种模型独立配置
- 新增 `detect_context_length()` 根据模型名匹配上下文长度
- 切换模型时自动更新截断阈值（上下文一半）
- 简化命令：`/model <name>` 云端、`/model local <name>` 本地

**权限控制：**
- 默认启用（`create_agent(permission=True)`）
- 工作区内读操作放行，写操作确认，区外操作 ask/deny
- 危险操作（rm -rf / 等）直接拒绝

**其他：**
- `.gitignore` 增加 `code/`、`__pycache__`、编辑器配置等忽略规则
- 新增 `requirements.txt` 项目依赖清单

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

## 2026-07-08 System Prompt 模块

**新增：**
- 新增 `core/system_prompt.py`：`SystemPrompt` 构建器，将提示词分为静态区和动态区
  - `<SYSTEM_STATIC_CONTEXT>`：角色定义、回答风格、工具使用原则、行为规范、修改代码要求
  - `<SYSTEM_DYNAMIC_CONTEXT>`：工作目录、日期（YYYY-MM-DD）、操作系统、AGENT.md 记忆、会话指令
- 新增 `AGENT.md`：项目级行为准则，自动加载到动态区
- 新增 `Agent.add_instruction()` 方法，支持动态注入会话指令

**改进：**
- `agent.py` 集成 `SystemPrompt` 构建器，替换原有扁平提示词
- `create_agent()` 默认名称改为 `helloworld agent`
- 规则 1 更新为支持多工具并行调用
- 日期格式明确仅显示年月日，不含时间
- `AGENT.md` 简化聚焦智能体行为准则

**向后兼容：**
- 传入自定义 `system_prompt` 字符串时仍直接使用，跳过构建器

**调试优化：**
- 提升 debug 日志截断上限：消息 1000→5000、LLM 响应 2000→6000、工具返回 1500→4000 字符
- 每个消息/响应/工具返回现在都会显示实际总字符数，超出时标注共多少字符
- 统一使用 `_truncate()` 工具函数管理截断逻辑
- `agent.py` 中批量工具结果截断同步提升至 5000 字符

**解析修复：**
- 修复 `FINAL_ANSWER` 尾部换行导致解析为空的问题：`(.*?)$` → `(.*)` + `rstrip()`
- 修复 `_ACTION_RE` 的 INPUT 正则，`{.*?}` 改为前瞻边界匹配，支持嵌套 JSON 参数（如 `http` 工具传 `data: {"key": "val"}`）
- 修复 `else` 分支（LLM 无标签回复）未打印回答内容的问题：现在会显示 `🤖 {回答内容}`，之前只显示"直接回复（无标签）"就结束了
- **修复标签大小写不敏感**：`ACTION`/`THOUGHT`/`FINAL_ANSWER` 正则加入 `re.IGNORECASE`，LLM 输出 `Action：` 或 `action：` 也能正确解析
- **修复工具名带方括号 `[search]` 无法解析**：`(\w+)` → `\[?(\w+)\]?`，兼容 LLM 输出 `ACTION：[search]` 的格式

**异步工具执行加固：**
- `future.result()` 加 `try/except` 兜底，防止单个工具抛异常拖垮整个并发
- 异步执行结果现在分 ✅ 成功 / ❌ 失败 显示汇总（如 "✅ 2 成功，❌ 1 失败"）
- `_combine_results()` 合并结果带每个工具的 ✅/❌ 标记，LLM 能清楚识别哪个工具执行失败
