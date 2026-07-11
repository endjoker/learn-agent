# -*- coding: utf-8 -*-
"""
调试日志模块 - 使用标准 logging 模块

通过配置 logging 级别来控制日志输出：
- DEBUG: 显示所有详细信息（消息、LLM 响应、工具调用）
- INFO: 显示关键信息
- WARNING/ERROR: 显示警告和错误

用法：
    from core.debug import logger, setup_logging

    setup_logging(debug=True)
    logger.debug('调试信息')
"""

import logging
import sys
from typing import List, Dict


# 创建专用 logger
logger = logging.getLogger('hello_agent')


def setup_logging(debug: bool = False, log_file: str = None):
    """
    配置日志系统

    参数:
        debug: 是否启用 DEBUG 级别日志（调试信息写入 log 文件，控制台保持 INFO）
        log_file: 可选的日志文件路径（不传时 debug 模式自动生成 log/debug-YYYY-MM-DD.log）
    """
    import os
    from datetime import date

    # 清除已有的 handler
    logger.handlers.clear()

    # 控制台 handler：始终输出 INFO 及以上级别（debug 信息不刷屏）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(levelname)-8s %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件 handler（记录所有 DEBUG 级别详情）
    file_handler = None
    if debug or log_file:
        if not log_file:
            # 自动生成 log/debug-YYYY-MM-DD.log
            today = date.today().strftime('%Y-%m-%d')
            log_dir = os.path.join(os.getcwd(), 'log')
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f'debug-{today}.log')

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '[%(asctime)s] %(levelname)-8s %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(file_handler)

    # 最终日志级别由最严格的 handler 决定
    logger.setLevel(logging.DEBUG if (debug or file_handler) else logging.INFO)

    # 防止日志向上传播导致重复输出
    logger.propagate = False

    if file_handler:
        logger.debug(f'调试日志已写入: {log_file}')


def set_debug(enabled: bool):
    """开启或关闭调试日志（仅影响文件 handler，控制台保持 INFO）"""
    if enabled:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(logging.INFO)


def is_debug() -> bool:
    """当前是否开启调试模式"""
    return logger.level == logging.DEBUG


# 截断常量（如需完整内容，可临时调大这些值）
_MAX_MSG_CHARS = 5000    # 单条消息最大显示字符数
_MAX_LLM_CHARS = 6000    # LLM 响应最大显示字符数
_MAX_TOOL_CHARS = 4000   # 工具返回结果最大显示字符数


def _truncate(text: str, max_chars: int, label: str = "内容") -> str:
    """截断文本并标注总长度"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...... ({label}过长，仅显示前 {max_chars} 字符，共 {len(text)} 字符)"


# ============================================================
# 调试日志函数
# ============================================================

def log_messages(step: int, messages: List[Dict], title: str = 'Agent -> LLM 消息'):
    """打印发送给 LLM 的完整消息列表"""
    if not logger.isEnabledFor(logging.DEBUG):
        return

    total_chars = 0
    logger.debug(f'=== 第 {step} 步 | {title} ===')

    for i, msg in enumerate(messages):
        role = msg.get('role', '?')
        name = msg.get('name', '')
        content = msg.get('content', '')
        content_len = len(content)
        total_chars += content_len

        label = f'  [{i}] {role.upper()}'
        if name:
            label += f' (name={name})'
        logger.debug(f'{label} ({content_len} 字符)')

        display = _truncate(content, _MAX_MSG_CHARS, "消息")
        for line in display.split('\n'):
            logger.debug(f'    | {line}')

    logger.debug(f'=== 共 {len(messages)} 条消息，合计 {total_chars} 字符 ===')


def log_llm_response(step: int, response: str, title: str = 'LLM -> Agent 响应'):
    """打印 LLM 返回的原始响应"""
    if not logger.isEnabledFor(logging.DEBUG):
        return

    logger.debug(f'=== 第 {step} 步 | {title} ({len(response)} 字符) ===')
    display = _truncate(response, _MAX_LLM_CHARS, "LLM 响应")
    for line in display.split('\n'):
        logger.debug(f'  {line}')


def log_tool_call(step: int, tool_name: str, params: str):
    """打印工具调用信息"""
    if not logger.isEnabledFor(logging.DEBUG):
        return

    logger.debug(f'=== 第 {step} 步 | 工具调用 -> {tool_name} ===')
    logger.debug(f'  参数: {params}')


def log_tool_result(step: int, tool_name: str, result: str):
    """打印工具返回结果"""
    if not logger.isEnabledFor(logging.DEBUG):
        return

    logger.debug(f'=== 第 {step} 步 | 工具返回 <- {tool_name} ({len(result)} 字符) ===')
    display = _truncate(result, _MAX_TOOL_CHARS, "工具返回")
    for line in display.split('\n'):
        logger.debug(f'  {line}')


def log_separator():
    """打印分隔线"""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug('=' * 60)


def log_info(message: str):
    """打印普通调试信息"""
    logger.debug(message)


def enable_with_agent(name: str = 'Agent'):
    """告知用户调试模式已开启"""
    if is_debug():
        logger.debug(f'调试模式已开启 - 将显示 {name} 与 LLM 的完整通信')
