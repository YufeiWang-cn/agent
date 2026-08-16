"""按完整对话轮次裁剪上下文，避免破坏工具调用消息链。"""

from copy import deepcopy
from dataclasses import dataclass

from ..conversation import Message
from .estimator import estimate_messages_tokens


@dataclass(frozen=True, slots=True)
class ContextWindow:
    """描述实际发送给模型的上下文及其裁剪统计。"""

    messages: list[Message]
    estimated_tokens: int
    total_estimated_tokens: int
    omitted_messages: int

    @property
    def was_truncated(self) -> bool:
        return self.omitted_messages > 0


class ContextManager:
    """在 Token 预算内保留系统消息，并尽量保留最近的完整对话轮次。"""

    def __init__(self, max_tokens: int) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须大于 0")
        self._max_tokens = max_tokens

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def prepare(self, messages: list[Message]) -> ContextWindow:
        """生成上下文快照，并始终保留可能超出预算的最新轮次。"""
        if not messages:
            return ContextWindow([], 0, 0, 0)

        system_messages, turns = self._split_into_turns(messages)
        selected_turns: list[list[Message]] = []
        selected_tokens = estimate_messages_tokens(system_messages)

        for turn in reversed(turns):
            turn_tokens = estimate_messages_tokens(turn)
            is_latest_turn = not selected_turns
            if not is_latest_turn and selected_tokens + turn_tokens > self._max_tokens:
                break
            selected_turns.append(turn)
            selected_tokens += turn_tokens

        selected_turns.reverse()
        selected_messages = system_messages + [
            message
            for turn in selected_turns
            for message in turn
        ]
        total_tokens = estimate_messages_tokens(messages)
        return ContextWindow(
            messages=deepcopy(selected_messages),
            estimated_tokens=selected_tokens,
            total_estimated_tokens=total_tokens,
            omitted_messages=len(messages) - len(selected_messages),
        )

    @staticmethod
    def _split_into_turns(
        messages: list[Message],
    ) -> tuple[list[Message], list[list[Message]]]:
        if messages[0].get("role") == "system":
            system_messages = [messages[0]]
            remaining_messages = messages[1:]
        else:
            system_messages = []
            remaining_messages = messages

        turns: list[list[Message]] = []
        for message in remaining_messages:
            if message.get("role") == "user" or not turns:
                turns.append([message])
            else:
                turns[-1].append(message)
        return system_messages, turns
