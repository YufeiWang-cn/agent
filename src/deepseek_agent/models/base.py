from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..conversation import Message


@dataclass(frozen=True, slots=True)
class TextDelta:
    content: str


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
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
    @property
    def model_name(self) -> str: ...  # ...表示这里只声明接口，不提供实现

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> Iterable[StreamEvent]: ...
