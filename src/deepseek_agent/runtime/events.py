from dataclasses import dataclass
from enum import Enum

from ..models import ToolCallRequest


class AgentEventType(str, Enum):
    """表示 Agent 在一轮执行过程中产生的事件类型。"""

    TURN_STARTED = "turn_started"
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """通过统一结构向界面或其他调用方发送 Agent 运行事件。"""

    type: AgentEventType
    content: str | None = None
    tool_call: ToolCallRequest | None = None
    tool_result: str | None = None

    @classmethod
    def turn_started(cls) -> "AgentEvent":
        """创建表示一轮执行已经开始的事件。"""
        return cls(type=AgentEventType.TURN_STARTED)

    @classmethod
    def text_delta(cls, content: str) -> "AgentEvent":
        """创建包含一段流式文本的事件。"""
        return cls(type=AgentEventType.TEXT_DELTA, content=content)

    @classmethod
    def tool_call_started(cls, request: ToolCallRequest) -> "AgentEvent":
        """创建表示工具即将执行的事件。"""
        return cls(type=AgentEventType.TOOL_CALL, tool_call=request)

    @classmethod
    def tool_call_completed(
        cls,
        request: ToolCallRequest,
        result: str,
    ) -> "AgentEvent":
        """创建包含工具真实执行结果的事件。"""
        return cls(
            type=AgentEventType.TOOL_RESULT,
            tool_call=request,
            tool_result=result,
        )
