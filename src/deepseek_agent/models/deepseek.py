from collections.abc import Iterable, Sequence
from typing import Any

from openai import OpenAI

from ..config import Settings
from ..conversation import Message
from .base import StreamEvent, TextDelta, ToolCallRequest


class DeepSeekModel:
    def __init__(self, settings: Settings) -> None:
        # 属性名前面的_表示内部实现，不建议外部直接访问
        self._model_name = settings.model
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> Iterable[StreamEvent]:
        request: dict[str, Any] = {
            "model": self._model_name,
            "messages": list(messages),
            "stream": True,
        }
        if tools:
            request["tools"] = list(tools)

        response = self._client.chat.completions.create(**request)
        pending_tool_calls: dict[int, dict[str, str]] = {}

        for chunk in response:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            if delta.content:
                yield TextDelta(delta.content)

            for tool_call in delta.tool_calls or ():
                index = tool_call.index
                pending = pending_tool_calls.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if tool_call.id:
                    pending["id"] = tool_call.id
                if tool_call.function:
                    if tool_call.function.name:
                        pending["name"] += tool_call.function.name
                    if tool_call.function.arguments:
                        pending["arguments"] += tool_call.function.arguments

        for index in sorted(pending_tool_calls):
            call = pending_tool_calls[index]
            yield ToolCallRequest(
                id=call["id"] or f"tool_call_{index}",
                name=call["name"],
                arguments=call["arguments"] or "{}",
            )
