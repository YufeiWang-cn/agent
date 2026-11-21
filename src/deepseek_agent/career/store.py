"""兼容旧 CareerStore API；新代码依赖用例与存储协议。"""

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path

from ..infrastructure.sqlite_career import SqliteCareerRepository
from .domain import (
    CATEGORIES,
    MAX_FACT_HISTORY_PER_FACT,
    PROFILE_ID_PATTERN,
    CareerDataError,
    Json,
    text,
)
from .use_cases import CareerUseCases

__all__ = [
    "CareerStore",
    "CareerDataError",
    "Json",
    "text",
    "CATEGORIES",
    "MAX_FACT_HISTORY_PER_FACT",
    "PROFILE_ID_PATTERN",
]


class CareerStore(CareerUseCases):
    def __init__(self, path: Path) -> None:
        self._sqlite = SqliteCareerRepository(path)
        super().__init__(self._sqlite)

    @property
    def path(self) -> Path:
        return self._sqlite.path

    def connection(self) -> AbstractContextManager[sqlite3.Connection]:
        """兼容已有本地诊断脚本；业务用例不通过此接口访问 SQL。"""
        return self._sqlite.connection()
