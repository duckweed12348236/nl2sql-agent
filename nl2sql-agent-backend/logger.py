"""日志配置：装配 loguru 出口并接管标准库 logging。

logger 对象由各模块直接从 loguru 导入，本模块只负责配置。
日志参数（级别、文件、滚动、格式）见 config 的 LOG_* 常量：
setup_logger 由应用生命周期启动时调用，teardown_logger 在关闭时刷盘。
"""

import atexit
import inspect
import logging
import os
import sys

from loguru import logger

_configured = False
# worker 日志的装配状态记的是进程号：prefork 下子进程会继承父进程的内存状态，
# 只记布尔值会让子进程误以为已经配好，从而丢掉自己的出口（见 setup_celery_logger）
_celery_configured_pid = None


class InterceptHandler(logging.Handler):
    """把标准库 logging 的记录转发到 loguru。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 跳过 logging 内部的调用帧，定位到真正的日志调用处
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def teardown_logger() -> None:
    """等待 enqueue 队列中的日志全部写出，避免进程退出时丢失。

    文件出口使用 enqueue=True 异步落盘，日志进入队列即返回，若直接退出进程，
    队列中未处理的日志（如关闭阶段的日志）会被丢弃，故需在退出前显式 flush。
    """
    logger.complete()


def setup_logger() -> None:
    """按 config 的 LOG_* 参数初始化 loguru 出口，重复调用只生效一次。"""
    global _configured
    if _configured:
        return
    _configured = True

    # 延迟导入：config 依赖 models，models 又依赖本模块，故在调用时再取配置
    from config import LOG_LEVEL, LOG_FILE, LOG_ROTATION, LOG_RETENTION, LOG_CONSOLE_FORMAT, LOG_FILE_FORMAT

    logger.remove()

    # 控制台：带颜色，便于本地开发
    logger.add(
        sys.stderr,
        level=LOG_LEVEL,
        colorize=True,
        backtrace=True,
        diagnose=False,
        format=LOG_CONSOLE_FORMAT
    )

    # 文件：按 LOG_ROTATION 滚动，按 LOG_RETENTION 保留
    logger.add(
        LOG_FILE,
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        encoding="utf-8",
        level=LOG_LEVEL,
        enqueue=True,
        backtrace=True,
        diagnose=False,
        format=LOG_FILE_FORMAT
    )
    # 兜底：进程正常或异常退出时也刷新队列，确保日志落盘
    atexit.register(teardown_logger)

    # 接管标准库 logging：根 logger 统一转发，uvicorn 需显式替换 handler
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging_logger = logging.getLogger(name)
        logging_logger.handlers = [InterceptHandler()]
        logging_logger.propagate = False


def setup_celery_logger() -> None:
    """按 config 的 LOG_* 参数装配 Celery worker 进程的日志，重复调用只生效一次（按进程算）。

    与 setup_logger 的差别只有一个出口：日志写进 LOG_CELERY_FILE（``<日期>.celery`` 日志文件），
    不写应用日志文件，worker 的日志因此单独成文件、和应用日志互不混淆；控制台出口照旧保留。
    接管标准库 logging 的方式相同：celery 自己的 logger（celery / celery.worker / celery.task…）
    都往上冒泡到根 logger，由 InterceptHandler 转成 loguru 记录。
    """
    global _celery_configured_pid
    if _celery_configured_pid == os.getpid():
        return
    _celery_configured_pid = os.getpid()

    # 延迟导入：config 依赖 models，models 又依赖本模块，故在调用时再取配置
    from config import LOG_CELERY_FILE, LOG_CONSOLE_FORMAT, LOG_FILE_FORMAT, LOG_LEVEL, LOG_RETENTION, LOG_ROTATION

    logger.remove()

    logger.add(
        sys.stderr,
        level=LOG_LEVEL,
        colorize=True,
        backtrace=True,
        diagnose=False,
        format=LOG_CONSOLE_FORMAT
    )

    logger.add(
        LOG_CELERY_FILE,
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        encoding="utf-8",
        level=LOG_LEVEL,
        enqueue=True,
        backtrace=True,
        diagnose=False,
        format=LOG_FILE_FORMAT
    )
    atexit.register(teardown_logger)

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)


__all__ = ["setup_logger", "setup_celery_logger", "teardown_logger", "InterceptHandler"]
