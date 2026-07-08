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
        debug: 是否启用 DEBUG 级别日志
        log_file: 可选的日志文件路径
    """
    # 设置日志级别
    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)
    
    # 清除已有的 handler
    logger.handlers.clear()
    
    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    
    # 日志格式
    if debug:
        fmt = '[%(asctime)s] %(levelname)-8s %(message)s'
    else:
        fmt = '%(levelname)-8s %(message)s'
    
    formatter = logging.Formatter(fmt, datefmt='%Y-%m-%d %H:%M:%S')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件 handler（可选）
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '[%(asctime)s] %(levelname)-8s %(name)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(file_handler)
    
    # 防止日志向上传播导致重复输出
    logger.propagate = False


def set_debug(enabled: bool):
    """开启或关闭调试日志（向后兼容）"""
    if enabled:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
        for handler in logger.handlers:
            handler.setLevel(logging.INFO)


def is_debug() -> bool:
    """当前是否开启调试模式"""
    return logger.level == logging.DEBUG


# ============================================================
# 向后兼容的日志函数
# ============================================================

def log_messages(step: int, messages: List[Dict], title: str = 'Agent -> LLM 消息'):
    """打印发送给 LLM 的完整消息列表"""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    
    logger.debug(f'=== 第 {step} 步 | {title} ===')
    for i, msg in enumerate(messages):
        role = msg.get('role', '?')
        name = msg.get('name', '')
        content = msg.get('content', '')
        
        label = f'{role.upper()}'
        if name:
            label += f' (name={name})'
        
        logger.debug(f'  [{i}] {label}')
        # 截断过长的内容
        display = content if len(content) <= 1000 else content[:1000] + f'\n... (截断，共 {len(content)} 字符)'
        for line in display.split('\n'):
            logger.debug(f'    | {line}')
    
    logger.debug(f'=== 共 {len(messages)} 条消息 ===')


def log_llm_response(step: int, response: str, title: str = 'LLM -> Agent 响应'):
    """打印 LLM 返回的原始响应"""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    
    logger.debug(f'=== 第 {step} 步 | {title} ===')
    display = response if len(response) <= 2000 else response[:2000] + f'\n... (截断，共 {len(response)} 字符)'
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
    
    logger.debug(f'=== 第 {step} 步 | 工具返回 <- {tool_name} ===')
    display = result if len(result) <= 1500 else result[:1500] + f'\n... (截断，共 {len(result)} 字符)'
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
