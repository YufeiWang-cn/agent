"""定义工具协议、影响等级和统一异常类型。"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """向需要协作式取消的工具传递本轮执行状态。"""

    should_cancel: Callable[[], bool] | None = None

    @property
    def cancelled(self) -> bool:
        return self.should_cancel is not None and self.should_cancel()


class ToolError(Exception):
    """作为所有工具注册和执行错误的基类。"""


class ToolNotFoundError(ToolError):
    """表示模型请求了尚未注册的工具。"""


class ToolExecutionError(ToolError):
    """表示工具参数无效或发生了可预期的执行失败。"""

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
    parameters: JsonObject
    requires_confirmation: bool = False
    effect: ToolEffect = ToolEffect.READ_ONLY
    retryable: bool = False
    idempotent: bool = False
    supports_rollback: bool = False
    timeout_seconds: float | None = None

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
        """执行支持取消上下文的工具；普通工具继续使用旧执行入口。"""
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
