from dataclasses import dataclass, field
from typing import Any


# 定义一个类型别名。加入工具调用后，消息中除了字符串还会包含 tool_calls 列表。
Message = dict[str, Any]


@dataclass(slots=True)
class Conversation:
    system_prompt: str
    messages: list[Message] = field(init=False)  # init=False：messages由类内部初始化

    # dataclass 完成 __init__ 后自动调用，用于建立初始系统消息。
    def __post_init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def __len__(self) -> int:
        return len(self.messages)

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def add_assistant_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        content: str | None = None,
    ) -> None:
        self.messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }
        )

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": content,
            }
        )

    def truncate(self, length: int) -> None:
        del self.messages[length:]

    def visible_history(self) -> list[Message]:
        return self.messages[1:]
