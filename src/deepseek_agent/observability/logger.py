"""创建带容量限制和轮转策略的 UTF-8 文件日志记录器。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


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
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
    return logger
