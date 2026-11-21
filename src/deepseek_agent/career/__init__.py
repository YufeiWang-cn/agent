"""面向学生的 Agent 与后端求职业务。"""
from typing import Any
from .domain import CareerDataError

__all__ = ["CareerDataError", "CareerStore"]


def __getattr__(name: str) -> Any:
    if name == "CareerStore":
        from .store import CareerStore
        return CareerStore
    raise AttributeError(name)
