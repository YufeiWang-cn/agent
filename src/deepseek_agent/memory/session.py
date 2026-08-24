"""定义可序列化的会话实体及其消息快照操作。"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..conversation import Message
from ..planning import TaskPlan


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Session:
    """保存会话标识、项目归属、时间信息和完整消息历史。"""

    id: str
    title: str
    created_at: str
    updated_at: str
    project_id: str | None = None
    messages: list[Message] = field(default_factory=list)
    plan: TaskPlan | None = None

    @classmethod
    def create(
        cls,
        messages: list[Message],
        title: str = "新会话",
        project_id: str | None = None,
    ) -> "Session":
        now = _now()
        return cls(
            id=uuid4().hex,
            title=title,
            created_at=now,
            updated_at=now,
            project_id=project_id,
            messages=deepcopy(messages),
        )

    def update_messages(self, messages: list[Message]) -> None:
        self.messages = deepcopy(messages)
        self.updated_at = _now()

    def update_plan(self, plan: TaskPlan | None) -> None:
        """替换当前会话保存的最新任务计划快照。"""
        self.plan = plan
        self.updated_at = _now()

    def rename(self, title: str) -> None:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("会话名称不能为空")
        if len(normalized_title) > 80:
            raise ValueError("会话名称不能超过 80 个字符")
        self.title = normalized_title
        self.updated_at = _now()

    def move_to_project(self, project_id: str | None) -> None:
        self.project_id = project_id
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "project_id": self.project_id,
            "messages": deepcopy(self.messages),
            "plan": self.plan.to_dict() if self.plan is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        required = ("id", "title", "created_at", "updated_at", "messages")
        if any(key not in data for key in required):
            raise ValueError("会话文件缺少必要字段")
        if not all(isinstance(data[key], str) for key in required[:-1]):
            raise ValueError("会话基本信息格式错误")
        messages = data["messages"]
        project_id = data.get("project_id")
        raw_plan = data.get("plan")
        if project_id is not None and not isinstance(project_id, str):
            raise ValueError("会话所属项目格式错误")
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) and isinstance(message.get("role"), str)
            for message in messages
        ):
            raise ValueError("会话消息格式错误")
        if raw_plan is not None and not isinstance(raw_plan, dict):
            raise ValueError("会话计划格式错误")
        plan = TaskPlan.from_dict(raw_plan) if raw_plan is not None else None
        return cls(
            id=data["id"],
            title=data["title"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            project_id=project_id,
            messages=deepcopy(messages),
            plan=plan,
        )
