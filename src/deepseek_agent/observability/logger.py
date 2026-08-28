"""创建带容量限制和轮转策略的 UTF-8 文件日志记录器。"""

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..timekeeping import CHINA_TIMEZONE


class ChinaTimeFormatter(logging.Formatter):
    """无论主机系统时区如何，都使用北京时间格式化日志。"""

    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        value = datetime.fromtimestamp(record.created, CHINA_TIMEZONE)
        if datefmt:
            return value.strftime(datefmt)
        return value.isoformat(timespec="milliseconds")

def build_file_logger(path: Path, level: str = "INFO") -> logging.Logger:
    """构建可复用的文件日志记录器，并避免重复添加日志处理器。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"deepseek_agent.file.{path.resolve()}")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if not logger.handlers:
        handler = RotatingFileHandler(
            path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            ChinaTimeFormatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S%z",
            )
        )
        logger.addHandler(handler)
    return logger
