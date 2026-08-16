"""定义不可变的项目实体和项目名称校验规则。"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Project:
    """表示用于归类会话的轻量级项目。"""

    id: str
    name: str
    created_at: str

    @classmethod
    def create(cls, name: str) -> "Project":
        normalized_name = cls.normalize_name(name)
        return cls(
            id=uuid4().hex,
            name=normalized_name,
            created_at=_now(),
        )

    @staticmethod
    def normalize_name(name: str) -> str:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("项目名称不能为空")
        if len(normalized_name) > 50:
            raise ValueError("项目名称不能超过 50 个字符")
        return normalized_name

    def renamed(self, name: str) -> "Project":
        return Project(
            id=self.id,
            name=self.normalize_name(name),
            created_at=self.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        required = ("id", "name", "created_at")
        if any(not isinstance(data.get(key), str) for key in required):
            raise ValueError("项目数据格式错误")
        return cls(
            id=data["id"],
            name=data["name"],
            created_at=data["created_at"],
        )
