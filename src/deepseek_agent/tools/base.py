from abc import ABC, abstractmethod
from enum import Enum
from typing import Any


JsonObject = dict[str, Any]


class ToolError(Exception):
    """Base exception for tool registration and execution errors."""


class ToolNotFoundError(ToolError):
    """Raised when the model requests an unknown tool."""


class ToolExecutionError(ToolError):
    """表示工具参数无效或工具以可预期方式执行失败。"""

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
    """Common contract implemented by every Agent tool."""

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
        """
        把工具转换成 DeepSeek/OpenAI Function Calling 所需的格式
        """
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
        """Execute the tool and return a text result for the model."""
