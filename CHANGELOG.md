# 更新日志

## 2026-07-15 MCP 连接生命周期 & 权限匹配 & ReAct 解析修复

**MCP 严重 Bug 修复（跨事件循环导致每次工具调用超时）：**
- 引入 MCP 专用常驻事件循环 `_MCPLoopRunner`（`core/mcp_client.py`）
  - 后台 daemon 线程跑 `run_forever()`，init / discover / call_tool / close 全部经 `run_coroutine_threadsafe` 调度到同一循环
  - 修复原 `asyncio.run` 每次新建并关闭循环、初始化结束时 `_recv_task` 被取消、后续工具调用永远收不到响应（30s 超时）的问题
  - `agent.py`、`tools/mcp_tools.py` 的 `asyncio.run` 调用点全部改用 `run_in_mcp_loop`，同时支持在异步宿主（notebook / async web）中调用
  - 退出清理改在同一循环内 `close_all`，不再泄漏 npx 子进程

**接收循环早退修复：**
- `receive()` 契约统一为：`None` 仅表示真正断连（EOF / 已关闭），空闲与非数据行继续等待
- `StdioTransport.receive` 移除 30s 空闲超时 → 修复 LLM 思考超过 30s 后 stdio 连接被误判断开
- `SSEHttpTransport.receive` 改为循环跳过注释 / `event:` / 保活行 → 修复 SSE keepalive 杀死连接
- `StreamableHttpTransport.receive` 去掉 30s 超时，改为阻塞 `queue.get()`，由 `close()` 取消任务解除阻塞

**Streamable HTTP 流式响应修复：**
- `StreamableHttpTransport.send` 改为后台读取模型：`send()` 拿到响应后立即返回，由 `_read_response` 任务流式解析帧入队
- 按 `Content-Type` 分流：`text/event-stream` 逐行入队 `data:` 帧；纯 JSON 整体入队
- 修复原 `resp.text()` 在长 SSE 流上阻塞到 30s 超时、`_send_request` 无法 await 响应 Future 的问题
- `close()` 取消并等待所有后台读取任务

**权限匹配优化：**
- `core/permission.py` 危险命令匹配从朴素子串改为分类匹配
  - `DANGEROUS_WORDS`（`format`/`shutdown`/`reboot`/`halt`）编译为令牌边界正则，要求前后为空白/行首行尾才命中 → 避免 `"format"` 误伤 PowerShell 的 `Format-Table` / `Format-List`
  - `DANGEROUS_SUBSTRINGS`（`rm -rf /`、`mkfs`、`dd if=` 等特异模式）保留子串匹配，`mkfs` 仍能拦 `mkfs.ext4`
  - 真实危险命令（`format C:`、`shutdown`、`rm -rf /`）仍 `DENY`；原被误拒的 `powershell ... Format-Table` 现降为 `ASK`，按 A 信任工作区后可执行

**ReAct 解析修复：**
- `_ACTION_RE` 工具名捕获组 `(\w+)` → `([\w\-/.]+)`，支持 `-` / `/` / `.`
- 修复 MCP 前缀命名（如 `web-search/search`）含 `-` `/` 导致正则匹配失败 → `actions=0` → 走 ELSE 分支把工具调用当最终答案直接结束会话的问题
- `registry.get_tool` 本为字典精确查找，带 `/` 名字可正常命中 MCPTool

**验证：**
- 端到端 stdio 全流程：init → discover → 空闲 32s → `call_tool` 正确返回（修复前必超时）
- Streamable HTTP：mock SSE 服务器返回 keepalive + `data:` 帧，`send` 不阻塞、`receive` 正确取到 payload
- 持久循环：调用 1 启动的后台 task 在调用 2 仍存活
- 权限：`format-table` → `ask`、`format C:` → `deny`、`mkfs.ext4` → `deny`、`echo shutdown-server` → `allow`

## 2026-07-15 MCP 模块接入

**新功能：**
- MCP（Model Context Protocol）客户端模块
  - `core/mcp_client.py` — 三层架构：传输层（Stdio/SSE/StreamableHTTP）+ 协议层（JSON-RPC 2.0）+ MCPClientManager
  - `tools/mcp_tools.py` — MCPTool 桥接层，将 MCP 工具透明适配为 BaseTool，LLM 无感调用
  - 支持三种传输方式：`stdio`（本地子进程）、`http+sse`（远程 SSE）、`streamable`（新版 Streamable HTTP）
- 配置文件系统
  - `config/mcp_config.json` — 用户 MCP 配置（含 Token，已 gitignore）
  - `config/mcp_config.template.json` — 配置模板
  - `create_agent()` 自动加载：代码参数 > config/mcp_config.json > 项目根目录（兼容旧路径）
- 交互式 `/mcp` 命令
  - `/mcp` — 概览 MCP 服务器连接状态
  - `/mcp list` — 查看服务器详情与工具列表
  - 按需初始化：未运行前配置在首次使用或输入 `/mcp` 时自动初始化

**架构改进：**
- MCP 工具自动添加 `{server_name}/` 前缀，无命名冲突
- 优雅降级：单台 MCP 服务器初始化失败不影响其他工具
- 退出时自动关闭所有 MCP 连接（清理 aiohttp session 和子进程）
- 工具调用经过 PermissionChecker 权限控制

**依赖变更：**
- `requirements.txt` 新增 `aiohttp>=3.9.0`（SSE/StreamableHTTP 传输所需）

## 2026-07-14 代码审查优化 & LLM 重试逻辑

**安全修复：**
- 修复 `_run_task_list`（Plan 模式 Phase 2）完全绕过权限检查的问题
  - 现在加入完整权限检查流程（allow/ask/deny）
  - 进入时自动 `allow_workspace()`，区内操作免确认，区外仍需审批
  - 补充轻量压缩、上下文检查、紧急截断，防止长任务撑爆上下文
- 修复 `stream_run` 缺少权限检查和上下文管理的问题
  - 流式模式自动信任工作区，区内放行，区外返回跳过提示
  - 补充轻量压缩、上下文检查、紧急截断、会话保存、记忆归档
- 优化 `PermissionChecker.check()` 路径感知
  - 新增 `_is_operation_external()` 方法，检查 file_path/path/dest/paths 是否在工作区外
  - trusted 模式下 callable 规则返回 ASK 时，先判断路径是否在区内
  - 区内 → ALLOW，区外 → ASK，`allow_workspace()` 不再一刀切放行
  - 危险命令（rm -rf / 等）仍直接 DENY，不受 trusted 影响

**稳定性改进：**
- `llm_client.think()` 新增重试逻辑
  - 最多重试 3 次，退避间隔 1s → 2s → 4s
  - 只重试网络类错误（RemoteProtocolError / ReadTimeout / ConnectError / APIConnectionError 等）
  - HTTP 5xx 和 429（限流）也自动重试，400/401 等直接失败
  - 流式失败后自动降级为非流式重试（非流式连接更短、更不容易中途断开）
  - plan 模式（silent=True）同样重试，仅写日志不打印
  - 非流式重试成功后，如果原始请求是流式，自动打印完整内容
  - 新增 `_get_retryable_types()` 和 `_is_retryable()` 辅助方法

**Bug 修复：**
- 修复 `CalculatorTool._safe_eval` 中 `ast.Index` 在 Python 3.12+ 已移除的问题
  - 改为直接处理 `ast.Slice` 和裸表达式分支
  - 完善了 slice 的 step 参数处理
- 修复 `Compressor.light_compress()` 返回值永远为 True 的问题
  - 原来比较消息数量（轻量压缩不改消息数量所以永远 True）
  - 改为比较压缩前后内容总长度，正确反映是否实际压缩
- 修复 `_truncate_history` 中 token 统计口径不一致
  - 原来初始用 `store.live_tokens()`（锚点值），循环内用 `_estimate_tokens()` 累减
  - 改为每次 pop 后重新调用 `store.live_tokens()`，保证口径统一
- 修复 `SystemPrompt` 规则编号跳过 5（1,2,3,4,6 → 1,2,3,4,5）

## 2026-07-12 上下文压缩 & 任务清单 & 多项优化

**新增：**

## 2026-07-12 上下文压缩 & 任务清单 & 多项优化

**新增：**
- 新增 `core/compressor.py`：两级上下文压缩
  - 轻量压缩：已消费的 tool_result 替换为元数据摘要（0 LLM 成本）
  - 全量压缩：LLM 结构化摘要，WARN_RATIO=60% 提示 / AUTO_RATIO=80% 自动触发
  - `find_safe_tail_boundary()` 安全截断，不切断 tool_use/tool_result 配对
- 新增 `core/task_list.py`：Plan-and-Execute 任务清单
  - `TaskList` / `TaskItem` 模型，支持状态推进（pending → in_progress → completed）
  - `from_plan_text()` 解析 LLM 的 PLAN 输出
  - `to_prompt_section()` 注入 system prompt 动态区，LLM 感知进度
  - `to_summary()` 渲染完成总结（含每个任务结果）
  - 序列化到 session JSON，`--resume` 后自动恢复进度
- 新增 `/plan [任务描述]` 命令：手动触发方案生成 + 确认 + 逐任务执行
- 新增 `/history` 命令：查看当前会话内容
- 新增 `/compact` 命令：手动触发全量压缩
- 新增会话级 Plan-and-Execute 流程：
  - LLM 首轮输出 PLAN: 标签自动检测
  - 用户确认后自动创建任务清单 → 框架驱动逐步执行
  - 每完成一步输出 COMPLETE_TASK → 自动推进到下一步
  - 全部完成后输出带清单的完成总结
- `file_mgr` 工具新增 `paths` 参数支持批量删除

**架构优化：**
- 锚点式 token 跟踪：以 API 返回的 `response.usage` 为锚点，`live_tokens()` 增量估算
- `llm_client.think()` 新增 `silent` 参数（内部 LLM 调用不打印）
- `_extract_usage()` 兼容 DeepSeek（prompt_tokens）和 OpenAI（input_tokens）
- `message_store.set_anchor(usage)` + `live_tokens()` 方法
- `run()` 重构：Phase 1 ReAct → break → Phase 2 `_run_task_list()` 独立执行
- agent.py 参数解析同时支持 `--resume` 和 `-resume`
- 切换模型时同步 `store.max_tokens` 和 `store.model_*` 字段
- 系统 prompt 动态区注入（`<SYSTEM_TASK_LIST>`），任务进度增量更新

**Bug 修复：**
- permission `_workspace_trusted` 只在 callable 规则中检查 → 现在 None/str/callable 三条分支都检查
- Memory save `re.error: bad escape \p` → 用 lambda 替换 + 反斜杠转义
- 非 dict 的 INPUT（如 JSON 数组）导致 `AttributeError` → 加类型检查，走拒绝提示给 LLM
- `/plan` 确认方案后会话退出 → `/plan` handler 调用 `_run_task_list()` 执行任务
- 任务完成输出只显示"全部完成" → 改为带任务清单和结果的完整总结

## 2026-07-12 跨会话记忆系统 & BM25 检索

**新增：**
- 新增 `memory/` 包：跨会话长期记忆系统
  - `MemoryManager` — 记忆保存（upsert 语义，同会话同天合并）、BM25 检索、权重管理
  - 自动归档每轮对话到 `memory/daily/index.json` + `daily/YYYY-MM-DD.md`
  - 归档时过滤工具执行结果和 ACTION/INPUT，保留用户提问和 LLM 推理/回答
- 新增 `tools/memory_tools.py`：`memory_search` + `memory_update` 工具
- 新增 `memory/memory.md`：记忆配置引用，由 SystemPrompt 动态加载到 LLM
- 新增 `code/simple-py/clear_memory.py`：记忆清理脚本（支持单条/批量/全部清除）
- 新增 `log/` 目录：`--debug` 模式调试信息写入文件，控制台保持 INFO

**检索优化：**
- BM25 算法（`rank_bm25`）替代子串匹配，支持中英文混合搜索
- 引入 `jieba` 中文分词，解决中文无空格分词问题
- 排序公式：`score = BM25 × 10 + weight + date_numeric / 100000`
- 权重自动递增（命中 +1），`weight < -5` 自动隐藏（软删除）

**ReAct 解析增强：**
- 新增 `_normalize_tags()` 预处理，支持 XML 风格标签 `<THOUGHT>` 等
- INPUT 正则边界增加 THOUGHT 标记，防止跨行捕获
- 清理模型特殊 token（`<|im_end|>` 等），防止混入工具参数

**其他改进：**
- `save_conversation()` 改为 upsert 语义（同 `session_id` + 同 `date` 合并到一条记忆）
- 权重命中自动递增，`memory_update` 保留供 LLM 手动降权
- `/session delete` 支持批量删除（`/session delete id1 id2 ...`）
- `/clear` 后记忆走新条目，不覆盖清空前的内容
- 调试日志改为文件输出，控制台不再刷屏
- `CLAUDE.md` 加入 Plan-Then-Execute 行为准则
- `memory-plan.md` 补充后续混合搜索（BM25 + 语义向量 + RRF）方案

## 2026-07-11 会话持久化 & MessageStore

**新增：**
- 新增 `core/message_store.py`：消息存储模块
  - `save_session()` — 全量覆写会话文件
  - `load_session_data()` — 从字典恢复会话
  - `list_session_files()` — 列出所有已保存会话
  - `delete_session_file()` — 删除指定会话
  - 消息变更通知回调（`on_update`，为 UI 预留）
- 新增 `sessions/` 目录，存放会话 JSON 文件
- 新增 `--resume <id>` / `--resume last` 命令行参数
  - `--resume last` 按文件修改时间取最新会话
- 新增交互式命令：`/session`（info/list/save/delete）
- 新增 `/stats` 命令查看上下文占用统计
- 新增 `--help` 参数显示使用帮助

**优化：**
- `run()` 每次对话结束后自动保存会话
- `/clear` 先保存再清空，保留文件历史
- `create_agent()` 新增 `provider` 参数，支持恢复本地模型会话
- 启动界面精简，不再列出 14 个工具
- 工具描述增加跨平台说明

**Bug 修复：**
- 修复 `load_session_data()` 列表引用断裂导致 resume 后消息不保存
  - 改用 `clear() + extend()` 保持列表对象不变
- 修复 `--resume last` 误入单轮模式的问题
- 修复 `list_session_files()` 按文件名而非修改时间排序

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
