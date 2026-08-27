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

L6#9 链路优化：
- setup_logging 为进程级 once-guard（幂等），重复调用直接返回，
  避免多入口（agent.py / gateway.cli.py）重复叠加 handler 导致日志翻倍。
- 全链路文件日志统一使用 RotatingFileHandler（maxBytes=10MB, backupCount=5）。
- 脱敏规则统一委托给 core.sandbox.guard 的 SECRET_PATTERNS / sanitize_output，
  本模块不再复制正则实现，仅保持 redact_secrets 函数名兼容。
- 通过 set_run_context()/clear_run_context() 提供 run_id/turn_id 运行上下文，
  日志记录自动附带 [run_id/turn_id] 字段（能取到处）。
"""

import contextvars
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler


# 创建专用 logger（gateway/core 全链路统一挂在 jk_agent 命名空间下）
logger = logging.getLogger('jk_agent')

# ============================================================
# 进程级 once-guard：setup_logging 只生效一次，重复调用直接返回
# ============================================================
_LOGGING_INITIALIZED = False
_setup_lock = threading.Lock()

# RotatingFileHandler 策略（L6#9）：10MB × 5 备份，全链路日志统一
_FILE_MAX_BYTES = 10 * 1024 * 1024
_FILE_BACKUP_COUNT = 5

# 运行上下文（run_id / turn_id）：由调用方在每轮 / 每个 turn 处理入口设置。
# 使用 contextvars，自动随 async task / 线程上下文隔离。
_run_ctx: contextvars.ContextVar = contextvars.ContextVar(
    'jk_agent_run_ctx', default={})


class _RunContextFormatter(logging.Formatter):
    """在格式化结果末尾附加 [run_id/turn_id]（上下文已设置时）。

    未设置上下文时输出与原格式完全一致，不产生任何额外字段。
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        ctx = _run_ctx.get()
        rid = ctx.get('run_id', '') or ''
        tid = ctx.get('turn_id', '') or ''
        if rid or tid:
            text = f'{text} [{rid or "-"}/{tid or "-"}]'
        return text


def set_run_context(run_id: str = None, turn_id: str = None) -> None:
    """在当前执行上下文（async task / 线程）中设置 run_id/turn_id。

    设置后本进程内所有 jk_agent 日志（控制台 + 文件）自动附带 [run_id/turn_id]；
    新一轮处理前应调用 clear_run_context() 或重新调用本函数覆盖。
    """
    cur = dict(_run_ctx.get())
    if run_id is not None:
        cur['run_id'] = str(run_id)
    if turn_id is not None:
        cur['turn_id'] = str(turn_id)
    _run_ctx.set(cur)


def clear_run_context() -> None:
    """清空当前执行上下文的 run_id/turn_id"""
    _run_ctx.set({})


def redact_secrets(text: str) -> str:
    """把疑似密钥/令牌模式打码为 ****，返回新字符串（不改动原文）。

    L6#9：实现委托给 core.sandbox.guard.sanitize_output（SECRET_PATTERNS 为
    统一规则源），本模块不复制正则实现，仅保持函数名 / 签名兼容。
    匹配要求足够长的片段以降低误伤；仅用于日志输出脱敏。
    """
    if not text:
        return text
    from core.sandbox.guard import sanitize_output
    return sanitize_output(text)


def setup_logging(debug: bool = False, log_file: str = None,
                  console_format: str = None, datefmt: str = None):
    """
    配置日志系统（进程级 once-guard：已初始化后重复调用直接返回）

    参数:
        debug: 是否启用 DEBUG 级别日志（调试信息写入 log 文件，控制台保持 INFO）
        log_file: 可选的日志文件路径（不传时 debug 模式自动生成 log/debug-YYYY-MM-DD.log）
        console_format: 控制台 handler 格式串；不传使用默认简短格式。
            gateway/cli.py 传入 basicConfig 风格格式以保持原控制台外观。
        datefmt: 时间格式（配合 console_format 使用）
    """
    global _LOGGING_INITIALIZED

    if _LOGGING_INITIALIZED:
        # L6#9：once-guard —— 已配置过则直接返回，避免重复 handler / 日志翻倍
        logger.debug('setup_logging: 已初始化，跳过重复配置')
        return

    import os
    from datetime import date

    with _setup_lock:
        if _LOGGING_INITIALIZED:
            return

        # 控制台 handler：始终输出 INFO 及以上级别（debug 信息不刷屏）
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(_RunContextFormatter(
            console_format or '%(levelname)-8s %(message)s',
            datefmt=datefmt))
        logger.addHandler(console_handler)

        # 文件 handler（记录所有 DEBUG 级别详情；RotatingFileHandler 10MB × 5）
        file_handler = None
        if debug or log_file:
            if not log_file:
                # 自动生成 log/debug-YYYY-MM-DD.log
                today = date.today().strftime('%Y-%m-%d')
                log_dir = os.path.join(os.getcwd(), 'log')
                os.makedirs(log_dir, exist_ok=True)
                log_file = os.path.join(log_dir, f'debug-{today}.log')

            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=_FILE_MAX_BYTES,
                backupCount=_FILE_BACKUP_COUNT,
                encoding='utf-8')
            # 日志可能包含敏感上下文（如 LLM 响应），收紧文件权限为 0600
            try:
                os.chmod(log_file, 0o600)
            except OSError:
                pass
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(_RunContextFormatter(
                '[%(asctime)s] %(levelname)-8s %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'))
            logger.addHandler(file_handler)

        # 最终日志级别由最严格的 handler 决定
        logger.setLevel(logging.DEBUG if (debug or file_handler) else logging.INFO)

        # 防止日志向上传播导致重复输出
        logger.propagate = False

        _LOGGING_INITIALIZED = True

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


# 截断常量（如需完整内容，可临时调大这些值）
_MAX_LLM_CHARS = 6000    # LLM 响应最大显示字符数


def _truncate(text: str, max_chars: int, label: str = "内容") -> str:
    """截断文本并标注总长度"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...... ({label}过长，仅显示前 {max_chars} 字符，共 {len(text)} 字符)"


# ============================================================
# 调试日志函数
# ============================================================

def log_llm_response(step: int, response: str, title: str = 'LLM -> Agent 响应'):
    """打印 LLM 返回的原始响应（写入前对疑似密钥/令牌模式打码）"""
    if not logger.isEnabledFor(logging.DEBUG):
        return

    logger.debug(f'=== 第 {step} 步 | {title} ({len(response)} 字符) ===')
    display = _truncate(redact_secrets(response), _MAX_LLM_CHARS, "LLM 响应")
    for line in display.split('\n'):
        logger.debug(f'  {line}')


def log_info(message: str):
    """打印普通调试信息"""
    logger.debug(message)
