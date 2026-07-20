from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..conversation import Message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Session:
    id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[Message] = field(default_factory=list)

    @classmethod
    def create(cls, messages: list[Message], title: str = "新会话") -> "Session":
        now = _now()
        return cls(
            id=uuid4().hex,
            title=title,
            created_at=now,
            updated_at=now,
            messages=deepcopy(messages),
        )

    def update_messages(self, messages: list[Message]) -> None:
        self.messages = deepcopy(messages)
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": deepcopy(self.messages),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        required = ("id", "title", "created_at", "updated_at", "messages")
        if any(key not in data for key in required):
            raise ValueError("会话文件缺少必要字段")
        if not all(isinstance(data[key], str) for key in required[:-1]):
            raise ValueError("会话基本信息格式错误")
        messages = data["messages"]
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) and isinstance(message.get("role"), str)
            for message in messages
        ):
            raise ValueError("会话消息格式错误")
        return cls(
            id=data["id"],
            title=data["title"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            messages=deepcopy(messages),
        )
