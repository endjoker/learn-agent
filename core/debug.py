"""
调试日志模块 —— 窥探 Agent 与 LLM 之间的每一句对话

通过一个全局开关控制，开启后可以看到（每行带 [DEBUG] 时间戳前缀）：
1. Agent 发给 LLM 的完整消息内容（system / user / assistant）
2. LLM 返回的原始响应
3. 工具调用的输入参数和返回结果

用法：
    from core.debug import set_debug, log_messages, log_llm

    set_debug(True)   # 开启调试
    log_messages(1, messages)  # 打印第 1 步的消息
    log_llm(1, response)       # 打印 LLM 的回复
"""

import json
from datetime import datetime
from typing import List, Dict


# ============================================================
# 全局调试开关
# ============================================================

_DEBUG = False
"""全局调试开关，False=关闭，True=开启"""


def set_debug(enabled: bool):
    """开启或关闭调试日志"""
    global _DEBUG
    _DEBUG = enabled


def is_debug() -> bool:
    """当前是否开启调试模式"""
    return _DEBUG


# ============================================================
# 格式化输出工具
# ============================================================

def _prefix() -> str:
    """
    生成每行调试日志的前缀
    格式: [DEBUG 2026-07-03 14:30:00]
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"[DEBUG {now}]"


def _section(title: str, char: str = "═") -> str:
    """画一条带标题的分隔线"""
    line = char * 50
    return f"\n{_prefix()} {line}\n{_prefix()} {title}\n{_prefix()} {line}"


def _truncate(text: str, max_len: int = 10000) -> str:
    """如果文本太长就截断"""
    if len(text) > max_len:
        return text[:max_len] + f"\n{_prefix()} ……（截断，共 {len(text)} 字符）"
    return text


def _format_role(role: str) -> str:
    """给角色名加 emoji 区分"""
    icons = {
        "system":     "⚙️  SYSTEM",
        "user":       "👤  USER",
        "assistant":  "🤖  ASSISTANT",
        "tool":       "🔧  TOOL",
    }
    return icons.get(role, f"❓  {role.upper()}")


# ============================================================
# 核心调试函数
# ============================================================

def log_messages(step: int, messages: List[Dict], title: str = "Agent → LLM 消息"):
    """
    打印发送给 LLM 的完整消息列表

    参数:
        step:     当前的 ReAct 循环步数
        messages: 消息列表
        title:    自定义标题
    """
    if not _DEBUG:
        return

    print(_section(f"第 {step} 步 | {title}"))

    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        name = msg.get("name", "")
        content = msg.get("content", "")

        # 角色标签
        label = _format_role(role)
        if name:
            label += f" (name={name})"

        print(f"{_prefix()}   [{i}] {label}")
        print(f"{_prefix()}   {'─' * 40}")

        # 显示完整内容（每行加前缀）
        display = _truncate(content, 3000)
        for line in display.split("\n"):
            print(f"{_prefix()}   | {line}")

        print()  # 消息之间的空行

    print(f"{_prefix()}   ─── 共 {len(messages)} 条消息 ───\n")


def log_llm_response(step: int, response: str, title: str = "LLM → Agent 响应"):
    """
    打印 LLM 返回的原始响应

    参数:
        step:     步数
        response: LLM 返回的文本
        title:    自定义标题
    """
    if not _DEBUG:
        return

    print(_section(f"第 {step} 步 | {title}"))

    display = _truncate(response, 2000)
    for line in display.split("\n"):
        print(f"{_prefix()}   {line}")
    print()


def log_tool_call(step: int, tool_name: str, params: str):
    """
    打印工具调用信息

    参数:
        step:      步数
        tool_name: 工具名称
        params:    参数 JSON
    """
    if not _DEBUG:
        return

    print(_section(f"第 {step} 步 | 🛠️  工具调用 → {tool_name}", "─"))

    # 美化 JSON
    try:
        params_obj = json.loads(params)
        params_display = json.dumps(params_obj, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        params_display = params

    for line in params_display.split("\n"):
        print(f"{_prefix()}   {line}")
    print()


def log_tool_result(step: int, tool_name: str, result: str):
    """
    打印工具返回结果

    参数:
        step:      步数
        tool_name: 工具名称
        result:    返回数据
    """
    if not _DEBUG:
        return

    print(_section(f"第 {step} 步 | 📦 工具返回 ← {tool_name}", "─"))

    display = _truncate(result, 1500)
    for line in display.split("\n"):
        print(f"{_prefix()}   {line}")
    print()


def log_separator():
    """打印一条醒目的分隔线"""
    if not _DEBUG:
        return
    print(f"\n{_prefix()} {'█' * 50}\n")


def log_info(message: str):
    """打印一条普通调试信息"""
    if not _DEBUG:
        return
    print(f"{_prefix()} {message}")


# ============================================================
# 提示用户调试模式已开启
# ============================================================

def enable_with_agent(name: str = "Agent"):
    """告知用户调试模式已开启"""
    if _DEBUG:
        print(f"\n{_prefix()} 🐛 调试模式已开启 —— 将显示 {name} 与 LLM 的完整通信")
        print(f"{_prefix()} {'─' * 45}")
