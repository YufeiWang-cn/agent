"""定义工具协议、影响等级和统一异常类型。"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any


JsonObject = dict[str, Any]


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
