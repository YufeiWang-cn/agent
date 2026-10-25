"""维护发送给模型的完整消息历史和工具调用消息链。"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


# 消息字段除了字符串，还可能包含 ``tool_calls`` 列表，因此使用统一的类型别名。
Message = dict[str, Any]


@dataclass(slots=True)
class Conversation:
    """封装对话消息的创建、恢复和失败轮次回滚操作。"""

    system_prompt: str
    # 消息列表始终由类内部根据系统提示词初始化。
    messages: list[Message] = field(init=False)

    # 数据类完成 ``__init__`` 后会自动调用此方法，用于建立初始系统消息。
    def __post_init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def restore(
        self,
        messages: list[Message],
        *,
        copy: bool = True,
    ) -> None:
        """恢复历史消息；``copy=False`` 时接管独占的消息列表。

        接管模式只浅拷贝列表本身、共享消息对象，省去一次全量深拷贝；
        调用方必须保证该列表及其消息不会被其他所有者继续修改
        （消息进入会话后本来就约定不再原地改动）。可能被共享的
        来源（如列表缓存对象）必须保持默认的 ``copy=True``。
        """
        if not messages or messages[0].get("role") != "system":
            raise ValueError("会话记录缺少 system 消息")
        self.messages = deepcopy(messages) if copy else list(messages)
        # 恢复旧会话时仍使用当前系统提示词，避免永久沿用过期版本。
        self.messages[0] = {"role": "system", "content": self.system_prompt}

    def __len__(self) -> int:
        return len(self.messages)

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(
        self, content: str, *, reasoning_content: str | None = None,
    ) -> None:
        message: Message = {"role": "assistant", "content": content}
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        self.messages.append(message)

    def add_assistant_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        content: str | None = None,
        *,
        reasoning_content: str | None = None,
    ) -> None:
        # 一次构造完整消息再 append；先 append 再补字段会让"按对象身份缓存"
        # 的 token 估算读到半成品消息。
        message: Message = {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        }
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        self.messages.append(message)

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
