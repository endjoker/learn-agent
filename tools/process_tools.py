# -*- coding: utf-8 -*-
"""
长驻子进程工具集 —— 将 ProcessManager 暴露给 LLM

5 个独立工具，各声明静态 capability，SecurityGate 据此跑 L2 检查：
  proc_start  (proc:manage, exec:shell)  启动长驻进程
  proc_send   (proc:manage,)             向 stdin 投喂（input 过 L2 check_proc_send_input）
  proc_read   (proc:read,)               增量读 stdout/stderr（只读）
  proc_list   (proc:read,)               列出会话状态
  proc_stop   (proc:manage,)             终止进程

设计见 code/learn/AIsubwey/design.md。与 BashTool 互补：一次性命令用 bash，长驻/交互用 proc_*。
"""

from .base_tool import BaseTool


class _ProcessToolBase(BaseTool):
    """共享 ProcessManager 注入。"""
    def __init__(self):
        self._pm = None

    def set_process_manager(self, pm):
        """注入 ProcessManager（register_all_tools 时调用）"""
        self._pm = pm


class ProcessStartTool(_ProcessToolBase):
    """启动长驻进程（dev server / watcher / REPL），返回 session_id + 初始输出。"""
    name: str = "proc_start"
    capabilities = ("proc:manage", "exec:shell")
    description: str = (
        "启动一个长期运行或交互式的进程（dev server / watcher / REPL），返回 session_id。"
        "与 bash 的区别：进程跨多个 ReAct 步骤保持运行，可后续用 proc_read 读输出、proc_send 投喂输入。"
        "适用：启动服务、进入 REPL、持续监控命令。command 为 shell 字符串"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "启动命令（shell 字符串，如 'npm run dev'）"},
            "cwd": {"type": "string", "description": "工作目录（默认工作区根，区外会返回错误）"},
            "name": {"type": "string", "description": "会话别名（可选，仅展示，可重名）"},
        },
        "required": ["command"],
    }

    def execute(self, command: str, cwd: str = None, name: str = None) -> str:
        if not self._pm:
            return "❌ 进程管理器未就绪"
        sid, init = self._pm.start(command, cwd=cwd, name=name)
        if sid < 0:
            return init   # init 实为错误信息
        label = name or f"proc-{sid}"
        return f"✅ 已启动 {label} (session={sid})\n初始输出:\n{init}"


class ProcessSendTool(_ProcessToolBase):
    """向运行中进程的 stdin 投喂一行文本。"""
    name: str = "proc_send"
    capabilities = ("proc:manage",)
    description: str = (
        "向指定 session 的进程 stdin 投喂一行文本（自动追加换行）。"
        "适用于 REPL 交互、y/n 确认、向运行中的服务发指令"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "session": {"type": "integer", "description": "会话 ID（proc_start 返回）"},
            "input": {"type": "string", "description": "投喂文本"},
        },
        "required": ["session", "input"],
    }

    def execute(self, session: int, input: str) -> str:
        if not self._pm:
            return "❌ 进程管理器未就绪"
        return self._pm.send(session, input)


class ProcessReadTool(_ProcessToolBase):
    """增量读取进程 stdout/stderr（自上次读取），不阻塞。"""
    name: str = "proc_read"
    capabilities = ("proc:read",)
    description: str = (
        "读取指定 session 的进程输出增量（自上次读取以来新增的内容）。"
        "不阻塞，从后台缓冲区取数据。返回 stdout / stderr / 是否截断 / 进程状态"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "session": {"type": "integer", "description": "会话 ID"},
        },
        "required": ["session"],
    }

    def execute(self, session: int) -> str:
        if not self._pm:
            return "❌ 进程管理器未就绪"
        out, err, trunc, status = self._pm.read(session)
        parts = []
        if out:
            parts.append(f"📤 stdout:\n{out.rstrip()}")
        if err:
            parts.append(f"📕 stderr:\n{err.rstrip()}")
        if trunc:
            parts.append("⚠️ 部分输出因缓冲区满被丢弃（truncated）")
        if status:
            parts.append(f"📊 {status.strip()}")
        if not parts:
            parts.append("（暂无新输出）")
        return "\n".join(parts)


class ProcessListTool(_ProcessToolBase):
    """列出所有进程会话状态。"""
    name: str = "proc_list"
    capabilities = ("proc:read",)
    description: str = "列出所有当前活动的进程会话状态（ID / 名称 / 状态 / 退出码 / 空闲时长）"
    parameters: dict = {"type": "object", "properties": {}, "required": []}

    def execute(self) -> str:
        if not self._pm:
            return "❌ 进程管理器未就绪"
        sessions = self._pm.list_sessions()
        if not sessions:
            return "📭 暂无进程会话"
        lines = ["🖥️  进程会话列表", "─" * 40]
        for s in sessions:
            lines.append(
                f"  [{s['id']}] {s['name']} | {s['status']}"
                f" | exit={s['exit_code']} | idle={s['idle_for']}s"
            )
        return "\n".join(lines)


class ProcessStopTool(_ProcessToolBase):
    """终止指定 session 的进程（terminate → kill）。"""
    name: str = "proc_stop"
    capabilities = ("proc:manage",)
    description: str = "终止指定 session 的进程（先关闭 stdin + 杀整树，5 秒内退出）"
    parameters: dict = {
        "type": "object",
        "properties": {
            "session": {"type": "integer", "description": "会话 ID"},
        },
        "required": ["session"],
    }

    def execute(self, session: int) -> str:
        if not self._pm:
            return "❌ 进程管理器未就绪"
        return self._pm.stop(session)


PROCESS_TOOLS = [
    ProcessStartTool, ProcessSendTool, ProcessReadTool,
    ProcessListTool, ProcessStopTool,
]


def register_process_tools(registry, process_manager=None):
    """注册 5 个 proc_* 工具到注册表，注入 ProcessManager。"""
    for tool_cls in PROCESS_TOOLS:
        tool = tool_cls()
        if process_manager is not None:
            tool.set_process_manager(process_manager)
        registry.register_tool(tool)
    return registry
