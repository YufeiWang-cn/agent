"""通过 OpenAI 兼容接口将 DeepSeek 流式响应转换为内部事件。"""

from collections.abc import Iterable, Sequence
from typing import Any

from openai import OpenAI

from ..config import Settings
from ..conversation import Message
from .base import StreamEvent, TextDelta, ToolCallRequest


DEEPSEEK_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
)


class DeepSeekModel:
    """封装 DeepSeek 客户端、模型切换和流式工具调用片段拼接。"""

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.model
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
            # 由项目自己的可靠性层统一重试，避免与 SDK 内置重试叠加。
            max_retries=0,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def available_models(self) -> tuple[str, ...]:
        if self._model_name in DEEPSEEK_MODELS:
            return DEEPSEEK_MODELS
        return (self._model_name, *DEEPSEEK_MODELS)

    def select_model(self, model_name: str) -> None:
        normalized_name = model_name.strip()
        if normalized_name not in self.available_models:
            raise ValueError(f"不支持的模型：{normalized_name}")
        self._model_name = normalized_name

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
        # SDK 会把同一个工具调用拆成多个增量，因此需要按索引重新拼接。
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
