"""定义工具协议、影响等级和统一异常类型。"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Any


JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """向工具传递协作式取消信号和单次执行期限。"""

    should_cancel: Callable[[], bool] | None = None
    deadline: float | None = None
    clock: Callable[[], float] = monotonic

    @property
    def cancellation_requested(self) -> bool:
        """返回调用方是否明确请求取消当前执行。"""
        return self.should_cancel is not None and self.should_cancel()

    @property
    def timed_out(self) -> bool:
        """返回当前执行是否已经超过统一的单调时钟期限。"""
        return self.deadline is not None and self.clock() >= self.deadline

    @property
    def cancelled(self) -> bool:
        """返回当前执行是否因主动取消或超时而应当停止。"""
        return self.cancellation_requested or self.timed_out


class ToolError(Exception):
    """作为所有工具注册和执行错误的基类。"""


class ToolNotFoundError(ToolError):
    """表示模型请求了尚未注册的工具。"""


class ToolExecutionError(ToolError):
    """表示工具参数无效，或执行过程中出现了可预期的失败。"""

    def __init__(
        self,
        message: str,
        *,
        side_effect_possible: bool = False,
    ) -> None:
        super().__init__(message)
        self.side_effect_possible = side_effect_possible


class ToolEffect(str, Enum):
    """描述工具成功执行后可能产生的外部影响。"""

    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    IRREVERSIBLE_WRITE = "irreversible_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


class Tool(ABC):
    """定义所有 Agent 工具必须实现的统一接口和安全属性。"""

    name: str
    description: str
    confirmation_description: str | None = None
    parameters: JsonObject
    requires_confirmation: bool = False
    effect: ToolEffect = ToolEffect.READ_ONLY
    retryable: bool = False
    idempotent: bool = False
    supports_rollback: bool = False
    timeout_seconds: float | None = None

    def begin_turn(self) -> None:
        """在新一轮用户消息开始时重置工具的临时状态。"""

    def requires_confirmation_for(self, arguments: JsonObject) -> bool:
        """返回当前参数是否需要确认；动态风险工具可以覆盖此方法。"""
        # 普通工具沿用类级静态策略，只有 run_command 这类风险随参数变化的工具需要覆盖此方法。
        return self.requires_confirmation

    def effect_for(self, arguments: JsonObject) -> ToolEffect:
        """返回当前参数对应的实际影响等级。"""
        return self.effect

    def confirmation_arguments_for(
        self,
        arguments: JsonObject,
        raw_arguments: str,
    ) -> str:
        """返回确认界面需要展示的参数；工具可以附加安全预览。"""
        return raw_arguments

    def execute_with_context(
        self,
        arguments: JsonObject,
        context: ToolExecutionContext,
    ) -> str:
        """执行支持运行边界的工具；普通工具继续使用旧执行入口。"""
        # 此兼容入口保留了原有工具签名，避免为新增取消能力而改写全部工具。
        return self.execute(arguments)

    @property
    def schema(self) -> JsonObject:
        """把工具转换成 DeepSeek/OpenAI 函数调用所需的结构。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def execute(self, arguments: JsonObject) -> str:
        """执行工具，并向模型返回文本结果。"""
