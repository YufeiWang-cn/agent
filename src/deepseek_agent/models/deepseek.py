"""通过 OpenAI 兼容接口将 DeepSeek 流式响应转换为内部事件。"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from ..config import Settings
from ..conversation import Message
from .base import (
    ModelProtocolError,
    StreamEvent,
    TextDelta,
    ToolCallRequest,
    UsageUpdate,
)


DEEPSEEK_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
)


@dataclass(slots=True)
class _PendingToolCall:
    """按调用索引累计 SDK 分批返回的工具调用字段。"""

    call_id: str = ""
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)

    def append(self, tool_call: Any) -> None:
        """合并一个工具调用增量，并拒绝互相冲突的调用标识。"""
        if tool_call.id:
            if self.call_id and self.call_id != tool_call.id:
                raise ModelProtocolError("同一工具调用索引返回了不同的调用 ID。")
            self.call_id = tool_call.id
        if tool_call.function:
            if tool_call.function.name:
                self.name_parts.append(tool_call.function.name)
            if tool_call.function.arguments:
                self.argument_parts.append(tool_call.function.arguments)

    def build(self, index: int) -> ToolCallRequest:
        """生成完整工具调用，并确保模型已经返回工具名称。"""
        name = "".join(self.name_parts)
        if not name:
            raise ModelProtocolError(f"索引 {index} 的工具调用缺少工具名称。")
        return ToolCallRequest(
            id=self.call_id or f"tool_call_{index}",
            name=name,
            arguments="".join(self.argument_parts) or "{}",
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
        pending_tool_calls: dict[int, _PendingToolCall] = {}
        finish_reasons: set[str] = set()

        for chunk in response:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                completion_tokens = getattr(usage, "completion_tokens", None)
                total_tokens = getattr(usage, "total_tokens", None)
                if (
                    isinstance(prompt_tokens, int)
                    and not isinstance(prompt_tokens, bool)
                    and isinstance(completion_tokens, int)
                    and not isinstance(completion_tokens, bool)
                    and isinstance(total_tokens, int)
                    and not isinstance(total_tokens, bool)
                ):
                    yield UsageUpdate(
                        input_tokens=prompt_tokens,
                        output_tokens=completion_tokens,
                        total_tokens=total_tokens,
                    )
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reasons.add(choice.finish_reason)
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                yield TextDelta(delta.content)

            for tool_call in delta.tool_calls or ():
                index = tool_call.index
                if not isinstance(index, int) or index < 0:
                    raise ModelProtocolError("工具调用分片缺少有效的非负索引。")
                pending_tool_calls.setdefault(index, _PendingToolCall()).append(
                    tool_call
                )

        unsupported_reasons = finish_reasons - {"stop", "tool_calls"}
        if unsupported_reasons:
            reasons = "、".join(sorted(unsupported_reasons))
            raise ModelProtocolError(f"模型流式响应异常结束：{reasons}。")

        for index in sorted(pending_tool_calls):
            yield pending_tool_calls[index].build(index)
