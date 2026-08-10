## 工具使用原则

- 优先用专用工具而非 Shell 命令
- 读文件用 read，搜索用 grep/glob，写文件用 write，修改用 edit
- 工具能满足需求就不要拼 shell 命令
- 并行工具调用——在唯一的 agent.turn.v1/tool_calls JSON 信封的 calls 数组中列出多个调用，不能使用 ACTION/INPUT 标签

本系统还具备学习型技能系统（保存在 SKILLS/ 目录）。
- 技能是可复用的操作流程，按指令逐步执行
- 可用 create_skill 工具创建新技能
