"""定义模型适配器与 Agent 之间使用的流式事件协议。"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..conversation import Message


class ModelProtocolError(RuntimeError):
    """表示模型流式响应不符合 Agent 依赖的事件协议。"""


@dataclass(frozen=True, slots=True)
class TextDelta:
    """表示模型流式返回的一段文本。"""

    content: str


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """表示模型通过函数调用发起的一次工具请求。"""

    id: str
    name: str
    arguments: str

    def as_message_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }


StreamEvent = TextDelta | ToolCallRequest


class ChatModel(Protocol):
    """约束 Agent 可使用的流式聊天模型接口。"""

    @property
    def model_name(self) -> str:
        """返回当前模型名称。"""
        ...

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> Iterable[StreamEvent]:
        """按顺序生成文本增量或完整工具调用。"""
        ...
