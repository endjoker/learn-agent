# 更新日志

## 2026-07-29 提示词架构 v3 + 代码审查修复 + Python 安全配置化 + 防卡死

**提示词架构 v3（对标 OpenClaw）**：
- 新建 `prompt/` 目录，4 个引导文件集中管理：`AGENT.md`（行为约束）、`SOUL.md`（人格语气）、`TOOLS.md`（工具规则，仅静态）、`MEMORY.md`（索引模式长记忆）
- `system_prompt.py` 全面重写：从 `prompt/` 读取引导文件，代码内嵌默认值 fallback；新增引导文件说明段落、截断上限（单文件 8K / 累计 32K 字符）、缓存边界日志
- 迁移 `memory/memory.md` → `prompt/MEMORY.md`（旧文件删除）；迁移根目录 `AGENT.md` → `prompt/AGENT.md`
- 工作区分层：`workspace/` 新增 `ref/`/`scripts/`/`downloads/`/`tmp/`/`output/` 子目录约定
- `config.json` 新增 `prompt`（截断配置）和 `workspace`（路径+子目录）配置段
- 修复 `_build_system_prompt` 未设置 workspace 路径导致系统提示词声明项目根而非工作区

**代码审查修复（7 项）**：
- `gateway/dispatcher.py`：修复 `agent.run()` 软超时后双重执行竞态（改用 `asyncio.shield` 保护同一 future）
- `gateway/agent_factory.py`：修复 `_save_map` 无锁读写竞争（加 `threading.Lock`）
- `core/llm_client.py`：修复不可达的提供商错误分支（从 `_requires_base_url` 条件内拆出）
- `gateway/dispatcher.py`：去掉硬编码 `"feishu"` 魔法字符串（Channel 基类加 `handles_chunking` 属性）
- `gateway/dispatcher.py`：`entry.agent.llm.model` 增加 `llm` None 保护
- `core/system_prompt.py`：section 数量"五个"→"六个"
- `gateway/server.py`：消除每次构造 3 次 `load_config()` 冗余调用

**Python 安全配置化 + 放宽**：
- `FORBIDDEN_IMPORTS`/`FORBIDDEN_CALLS` 从 guard.py 硬编码移至 `config.json` 的 `sandbox.python` 段，用户可按需调整
- 默认值放宽：`forbidden_imports` 从 7 个减至 3 个（subprocess/ctypes/socket），移除 os/sys/pathlib/shutil 整体禁止
- `forbidden_calls` 从 5 个减至 3 个（eval/exec/__import__），移除 open/compile
- 新增 `forbidden_qualified_calls`（函数级精确拦截）：os.system/os.remove/shutil.rmtree 等 11 个危险调用
- 效果：`import os`/`import pathlib`/`open()` 现在可用，`os.system()` 等仍被拦截

**防命令卡死**：
- BashTool / SandboxExecutor / PythonTool 的 subprocess 调用全部加 `stdin=subprocess.DEVNULL`
- 任何等待交互输入的命令（SSH 密码、sudo 提示、确认对话框等）立即拿到 EOF，不再卡住
- ProcessManager 保持 `stdin=PIPE`（交互式场景需要）

**其他**：
- `notes` 工具描述修正：明确为"临时键值暂存（仅当前会话有效，重启后丢失）"，与 MEMORY.md 的持久存储流程不再冲突
- `.gitignore` 补充 `memory/config.json` 规则

## 2026-07-27 统一配置 + 多协议适配层 + 代码审查全量修复

**背景**：对本轮全部改动（config 统一重构 + 三协议 LLM 适配层，12 个修改文件 + 6 个新文件，约 2400 行）进行 8 角度代码审查，确认 24 条发现（5 CRITICAL / 7 HIGH / 7 MEDIUM / 5 LOW），全部修复。

### 一、统一配置（config.json 唯一来源）

- **新增 `core/config_loader.py`**：统一配置加载器，从 `config.json` 读取全部配置（llm / permission / hooks / mcp / sandbox 五个 section），缺失时用硬编码默认值补齐
- **新增 `config.example.json`**：完整配置模板，可直接复制为 `config.json` 使用
- **删除全部旧文件读取**：
  - `_load_legacy_config()`（config_loader.py）— 删除 .env / config/hooks.json / config/mcp_config.json / config/sandbox.json 四段 fallback（116 行）
  - `_load_dotenv_to_llm()`（config_loader.py）— 删除手动 .env 解析（64 行）
  - `_load_env_file()` + `load_dotenv`（llm_client.py）— 删除 python-dotenv 依赖
  - `_find_config()` + sandbox.json fallback（profiles.py）— 删除（30 行）
  - `_resolve_config_path()` + hooks.json fallback（manager.py）— 删除（30 行）
  - `_load_mcp_config` 旧文件回退（agent.py）— 删除（18 行）
- **保留 `os.getenv` 运行时环境变量覆盖**（API Key 等敏感信息仍可通过环境变量注入）
- **requirements.txt**：删除 `python-dotenv>=1.0.0`

### 二、多协议 LLM 适配层

- **新增 `core/protocols/` 包**（5 个文件）：
  - `base.py` — 抽象基类 `ProtocolAdapter`（generate / generate_stream）+ `ChatResponse` + 消息翻译辅助
  - `openai_adapter.py` — OpenAI Chat Completions 协议（兼容 DeepSeek / Ollama / vLLM）
  - `anthropic_adapter.py` — Anthropic Messages API 协议（Claude 系列）
  - `gemini_adapter.py` — Gemini generateContent 协议
  - `__init__.py` — 工厂函数 `create_adapter()` + `detect_protocol()` URL 自动检测
- **llm_client.py 重构**：`think()` 通过适配器调用，不再直接依赖特定 SDK

### 三、代码审查修复（24 条）

**CRITICAL（5 条安全/可用性修复）**：
1. `guard.py` 正则重建 alternation 被粘连 → 提取 `_compile_words_re()` 统一编译，消除模块级与热更新的分叉
2. `CommandHook.from_config` 全仓库不存在 → 补回类方法（含 `check_command_safety` 注册期安全预检）
3. `filters`（sensitive_words / block_patterns）安全过滤器被整体删除 → 在 `_load_from_dict` 中恢复注册为 PythonHook
4. `protocols/__init__.py` 顶层硬 import 三个适配器 → anthropic / gemini 改为 lazy import，未装依赖不影响启动
5. `permission.py` 匹配语义收窄（`sudo shutdown` 从 DENY 降为 ASK）→ 正则增加 `\s` 边界

**HIGH（5 条功能修复）**：
6. `LLM_PROTOCOL` 环境变量对裸 env 配置不生效 → `_get_from_cfg` 增加 `LLM_{key}` fallback + `_apply_env_overrides` 用 `setdefault` 自动创建条目
7. 流式路径无条件取 `adapter.last_usage` 导致过期 usage → 仅在非 None 时更新
8. `config.json` 的 `llm.timeout` 对实际请求无效 → 保存 `_config_timeout`，纳入 `req_timeout` fallback 链
9. `/hook reload` 无去重 → `_try_load_unified` 加载前 `_hooks.clear()`
10. Gemini `chunk.text` 空 candidates 抛 ValueError → try/except + `last_chunk` 显式跟踪

**MEDIUM（7 条重复/死代码/规范）**：
11. 协议检测双份分叉实现 → 新增 `detect_protocol()` 唯一真相源
12. system 消息提取逐行重复 → 上提为基类 `_split_system_messages()`
13. `_extract_usage` 四份拷贝 → 删除 llm_client 孤儿 + gemini 死分支
14. anthropic kwargs 构建重复 → 提取 `_build_kwargs()`
15. 未使用代码清理：permission.py / guard.py 冗余 logging、`get_config_section()` 零调用、`first_word` 死代码
16. `_get_from_cfg` 翻倍查找 → 局部变量收敛
17. `_merge_consecutive_same_role` O(n²) 字符串拷贝 → parts 列表 + join，O(n)

## 2026-07-20 Hook 模块：事件驱动生命周期钩子系统

**背景**：agent 执行流程封闭，扩展行为只能改源码。本次新增 Hook 模块，让用户在关键执行点插入自定义逻辑（审计/通知/改写/拦截），不改动 agent.py 主流程。

**新建 `core/hook/` 模块（5 个文件）**：

- **events.py** — 12 个生命周期事件枚举（`session_start`/`user_prompt`/`pre_llm`/`post_llm`/`pre_tool`/`post_tool`/`notification`/`denied`/`stop`/`session_end`/`plan_approved`/`task_complete`）+ `HookContext`/`Decision`/`HookResult`/`_coerce`
- **hooks.py** — `BaseHook` 基类 + `PythonHook`（进程内回调 fn(ctx)→HookResult）+ `CommandHook`（进程外命令，JSON stdin/stdout + exit code 0/2 + `TimeoutExpired` 兜底）
- **manager.py** — `HookManager`：register/dispatch（matcher 过滤 + DEBUG 日志 + 异常兜底）/_merge（最严裁决：BLOCK>MODIFY>ALLOW>CONTINUE）/load_config（从 `config/hooks.json` 批量加载）+ 6 个便捷方法
- **builtin.py** — 3 个内置 hook：`audit_logger`（post_tool+denied 审计）、`webhook_notifier`（daemon 线程异步通知）、`sensitive_word_filter`/`block_pattern_filter`（用户输入/工具调用过滤）
- **__init__.py** — 包导出

**配置文件 `config/hooks.json`**：
- `filters.sensitive_words`（8 个敏感词）→ 自动注册 `user_prompt` hook
- `filters.block_patterns`（7 个危险模式）→ 自动注册 `pre_tool` hook
- 支持按事件 + matcher 配置 `CommandHook`（JSON stdin/stdout 协议，与 Claude Code 兼容）

**agent.py 集成（+147/-16 行）**：
- `Agent.__init__` 新增 `hooks_enabled` 参数，SecurityGate 之后创建 `HookManager`
- `create_agent()` 新增 `hooks` 参数，自动加载 `config/hooks.json` + `bind_agent` + `session_start`
- `run()` 接入 6 个事件点：
  - `user_prompt`：用户输入前（line 800），可 BLOCK/MODIFY
  - `denied`：gate DENY 时（line 957），审计用
  - `notification`：ASK 提示前（line 959），可提前 BLOCK
  - `pre_tool`：gate 通过后、执行前（line 1002），可 BLOCK/MODIFY
  - `post_tool`：结果聚合后、`_combine_results` 前（line 1069），可 MODIFY 改写结果
  - `stop`：FINAL_ANSWER/ELSE return 前（line 921/1133），BLOCK 则 `continue`（上限 3 次）
- CLI 新增 `/hook`（列出）+ `/hook reload`（热更新），启动横幅 + `/help` 同步

**安全分层**：Hook 在 SecurityGate（L1+L2）**之后**运行，只能加严不能放松——结构上无法绕过 L2 沙箱。`CommandHook` 注册前过 `check_command_safety`。

**设计文档**：`code/learn/HOOK/design.md`（697 行，含 12 事件、7 集成代码、3 期实施步骤、22 条设计取舍）

## 2026-07-19 沙箱安全加固：L2 覆盖盲区修复 + Plan 模式步数限制修复

**plan 模式步数限制修复（agent.py）**：
- 子任务步数从默认 30 → `max(max_steps, 200)`，避免复杂子任务因步数限制中断
- 单任务超步不再终止整个流程，改为标记失败并继续下一个任务

**安全性修复（core/sandbox/guard.py + tools/builtin_tools.py）**：

以下 5 个 L2 盲区均基于 capability 视角全量审查发现并修复：

**1. `exec:shell` 不检查操作路径（高）**：
- `SYSTEM_PATHS_*` 定义完备但 `check_command_safety` 未调用 `_is_system_path()`
- 修复：新增 `_check_command_for_paths()`，从命令字符串提取绝对路径并校验
- `_is_system_path()` 重写：POSIX 绝对路径（`/etc/…`）用字符串前缀匹配，避免 Windows 上 `Path.resolve()` 把 `/etc` 解析成 `D:\etc` 漏检
- 敏感文件（`.env`/`agent.py` 等）token 匹配同步接入命令检查

**2. `exec:code` pathlib 绕过（中）**：
- `check_python_code` 禁 `open()` 但 `Path.write_text()` 不受限
- 修复：`FORBIDDEN_IMPORTS` 扩展 `"pathlib"`、`"shutil"`

**3. `fs:read` 输出不脱敏（中）**：
- `sanitize_output` 只用于 bash 输出端；ReadTool/GrepTool/GlobTool 读 `.env` 返回原始凭据
- 修复：ReadTool 改为无条件脱敏；GrepTool/GlobTool 返回前新增脱敏调用

**4. `net:egress` 不检查 POST 数据泄露（中）**：
- `.env` 内容可通过 `HttpTool POST` 外发，只检查了域名黑名单不管 payload
- 修复：HttpTool POST 前新增 `_contains_secrets()` 检查 API Key/私钥

**5. 配套工程**：
- `builtin_tools.py` 顶层导入 `sanitize_output`（替代 ReadTool 内的条件 import）
- 新增 `_CREDENTIAL_LEAK_RE` 正则在 POST 数据中检测凭据泄漏

## 2026-07-19 Bug 修复：进程管理/安全拦截/代码审查反馈

**修正（基于 code-review 反馈）**：

**进程管理器 `ProcessManager`**：
- **deque 竞态修复**：`_read_internal` 从 `list()+clear()` 改为 `popleft()` 逐条消费，避免与 drain 线程的 append 并发丢失 ~64KB chunk（#1）
- **孤儿子进程泄露修复**：drainer 创建/启动包在 try/except 中，失败时 `_kill_tree` 清理已启动的 Popen（#5）
- **UnicodeEncodeError 修复**：`send()` 的 encode 加 `errors="replace"`，except 加 `UnicodeEncodeError`（#6）
- **容量竞态修复**：容量检查→ID 分配→session 存储全程持 `self._lock`，消除中间释放窗口（#8）
- **移除 `is_external` 死字段**：字段恒为 False，移除 dataclass 字段 + 3 处守卫 + `is_external()` 方法（#14）
- **线程安全增强**：`_close_session` 加 `self._lock`；`cleanup_all` 末尾 join watchdog 线程（#15）
- **sleep(0.8) 优化**：改为 4×200ms 轮询，有输出立即返回（#10）

**安全检查 `guard.py`**：
- **DANGEROUS_SUBSTRINGS 补充**：新增 `diskpart`/`taskkill /f`/`chmod 0`/`chown -r`，消除 proc_start 相对 BashTool 的安全缺口（#2）
- **DANGEROUS_WORDS 误杀修复**：正则从 `(?:^|\s)` 改为 `(?:^|;\s*|\|\|\s*|&&\s*|\|\s*)`，只匹配命令分隔符后的危险词，不再误杀 `ls format`/`echo shutdown` 等合法命令（#4）

**沙箱执行器 `SandboxExecutor`**：
- **私有方法公开化**：`_get_current_profile()`/`_get_max_output_bytes()`/`_get_idle_timeout()` → 去下划线前缀，消除 ProcessManager 对私有 API 的依赖（#12）

**agent.py**：
- **`/proc tail` trunc 提示**：检测 `trunc` 标志时打印缓冲区溢出警告（#9）

**security_gate.py**：
- **proc:manage 文档化**：添加注释说明 `input` 参数名与 proc_send 的隐式耦合（#7）

## 2026-07-16 长驻子进程模块 & L2 危险命令下沉 & OutputDrainer 重构

**新功能：长驻子进程管理器 `ProcessManager`**：
- 新增 `core/process_manager.py`——长驻交互式子进程会话管理器
  - `ProcessSession` 数据类：id / name / proc / ring buffer / 读指针 / idle watchdog
  - `ProcessManager`：start（shell 字符串+Popen 包装）/ send（stdin 投喂）/ read（增量，per-session 消费）/ stop（杀整树）/ list_sessions / cleanup_all
  - Popen 包装 shell：POSIX `bash -c` / Windows `cmd.exe /c`；`CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T` 杀整树（防 Windows 下杀父进程残留孤儿持有管道）
  - drain 用 `read1()` 而非 `read(n)`——避免低频输出进程（dev server）被 64KB 阻塞
  - ring buffer（`deque(maxlen)`）内存有界，长驻不因输出量 kill；病态 spam 由 idle watchdog 兜底
  - idle watchdog：无 read/send 超 `idle_timeout_seconds` → 自动 kill；同时检测自然退出 + 保留 exited 会话 600s
  - cwd 校验：区外 cwd → 拒绝启动；env 不脱敏（继承 `os.environ`，与 MCP stdio 一致）
- 新增 `tools/process_tools.py`——5 个长驻进程工具
  - `proc_start`（`proc:manage`+`exec:shell`）启动长驻进程
  - `proc_send`（`proc:manage`）向 stdin 投喂
  - `proc_read`（`proc:read`）增量读取（只读）
  - `proc_list`（`proc:read`）列出会话状态
  - `proc_stop`（`proc:manage`）终止进程
  - 各工具静态声明 `capabilities`，SecurityGate 按标签自动跑 L2

**新增 `check_proc_send_input`**（`core/sandbox/guard.py`）：
- proc_send 的 L2 内容检查——投喂到 REPL 的 input 可能含 shell 命令或 python 代码
- 同时检查 shell 危险模式（复用 check_command_safety，含 DANGEROUS）+ python 危险模式（`os.system`/`subprocess`/`eval`/`exec`/`__import__`/`shutil.rmtree`/`ctypes`）

**L2 危险命令下沉**（`core/sandbox/guard.py`）：
- `DANGEROUS_WORDS`（format/shutdown/reboot/halt，令牌边界）+ `DANGEROUS_SUBSTRINGS`（rm -rf //mkfs/dd if=/fork 炸弹…）合并进 `check_command_safety`
- **DANGEROUS 检查在白名单之前**——关键修正：`ls; rm -rf /` 不再被 `^ls` 白名单绕过
- 原 `permission.DANGEROUS` 保留作 L1 二级防线

**OutputDrainer 重构**（`core/sandbox/executor.py`）：
- 把 `SandboxExecutor._execute` 的内嵌三层闭包 drain 提取为 `OutputDrainer` 类
- 双模式：list sink + kill_on_exceed（一次性，BashTool）+ deque(maxlen) sink + dropped 计数（长驻，ProcessManager）
- `SandboxExecutor._execute` 改用 `OutputDrainer`，行为不变（BashTool 回归通过）

**SecurityGate 新增 `proc:manage` L2 分支**（`core/security_gate.py`）：
- `proc:manage` + 有 `input` 参数 → `check_proc_send_input`
- gate 不依赖 ProcessManager（内容检查只是字符串扫描）

**agent 长驻进程接线**（`agent.py`）：
- `create_agent` 创建 `ProcessManager` 并注入 `register_all_tools`
- `Agent.__init__` 注入 `process_manager` 并批量 `set_rule`（proc_* → ALLOW，危险判定全交 L2）
- 退出时 `process_manager.cleanup_all()`（与 `mcp_manager.close_all` 并列）
- 新增交互命令 `/proc`（list / stop <id> / tail <id>），启动 banner + /help 同步更新

**配置落地**：
- `config/sandbox.json` 顶层加 `idle_timeout_seconds: 300`（长驻进程空闲上限）
- `core/sandbox/profiles.py` `_DEFAULT_CONFIG` 顶层同步
- `SandboxExecutor._get_idle_timeout()` 读取
- profile `max_processes` 字段此前一直未用，现由 ProcessManager 生效

**依赖**：全标准库（`subprocess`/`threading`/`collections.deque`/`dataclasses`/`pathlib`/`time`），无新增外部依赖。

## 2026-07-15 沙箱系统 & 技能系统 & SystemPrompt 三区分离

**新功能：沙箱执行器（两层防护）：**
- 新增 `core/sandbox/` 模块：L2 安全沙箱
  - `executor.py` — SandboxExecutor 主执行器（开关/配置档/绕过/核心执行流）
  - `guard.py` — L2-A 内容拦截（敏感文件保护/数据外发检测/Python AST 审查/安全白名单/网络黑名单/输出脱敏）
  - `profiles.py` — 配置档管理（加载 `config/sandbox.json`）
  - `audit.py` — 审计日志记录（拦截/绕过/错误 → `log/sandbox-audit.log`）
  - 两层架构：L2-A 内容拦截 → L2-C subprocess 执行（L2-B 资源隔离为设计预留，暂未实现）
- 6 个内置工具注入沙箱检查：
  - `BashTool` — 命令经 L2-A 安全审查后执行
  - `PythonTool` — AST 级代码审查（禁止 os/subprocess/ctypes/eval/exec）
  - `WriteTool` / `EditTool` — 敏感文件路径保护 + 系统路径拦截
  - `HttpTool` — 外发请求目标黑名单域名/IP 检查
  - `ReadTool` — 输出脱敏（API Key/私钥/密码 自动掩码）
- 网络控制黑名单模式：默认全放行，仅拦截 `*.xyz`、`*.tk`、内网 IP 段等
- 三种配置档：agent（默认 512MB/60s）、restricted（64MB/10s 严格）、permissive（2048MB/300s 宽松）
- 绕过机制：`bypass_next()` 单条命令临时放行，执行后自动恢复
- 交互式命令：`/sandbox on/off/bypass/strict/profile/list`

**新功能：学习型技能系统：**
- 新增 `skills/` 包：AI 学习到的可复用能力
  - `skills/skill.py` — Skill 数据模型（名称/描述/指令/参数/标签）
  - `skills/manager.py` — SkillManager 磁盘 I/O（加载/创建/删除/更新，目录结构 `SKILLS/{name}/skill.json + instruction.md`）
  - `skills/skill_tool.py` — SkillTool（执行已有技能，返回指令文本供 LLM 逐步执行）+ CreateSkillTool（LLM 运行时创建技能，自动写磁盘 + 注册 + 重建 prompt）
- 预置技能：`code-review` — 代码审查（支持指定文件列表）
- 交互式命令：`/skill list`、`/skill <name>`、`/skill delete <name>`
- SystemPrompt 自动加载技能列表到动态区
- 工具注册分离：`register_skill_tool()` / `register_mcp_tool()` 独立追踪，与内置工具分三区展示

**架构改进：**
- `SystemPrompt` 三区分离：内置工具 / MCP 工具 / 技能 分别注入，LLM 清晰区分
- `ToolRegistry` 新增 `get_skill_tool_descriptions()` / `get_mcp_tool_descriptions()` 分区块描述
- `llm_client.think()` 新增 `timeout` 参数（默认 60s，参数 > 环境变量）
- `Compressor` 全量摘要超时提升至 300s（大文本摘要不中断）
- `memory.md` 补充技能系统说明，LLM 可感知技能创建能力
- `create_agent()` 新增 `skills` 和 `sandbox` 参数，按需启用
- 启动状态行增加沙箱状态显示，方便排查

**Bug 修复：**
- 修复 agent.py 中 MCP 工具名引用路径（`self._mcp_tool_names` → `self.tool_registry._mcp_tool_names`）
- 修复 BashTool 沙箱模式下输出格式不统一的问题（抽取 `_format_output()` 复用）

**兼容性：**
- L2-B nanosandbox 在 PyPI 上不存在对应包，该层暂未实现；L2-A 内容拦截 + L2-C subprocess 始终生效
- 所有新功能在 `create_agent()` 中默认启用，可通过 `sandbox=False` / `skills=False` 关闭

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
