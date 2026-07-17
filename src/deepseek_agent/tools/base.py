from abc import ABC, abstractmethod
from typing import Any


JsonObject = dict[str, Any]


class ToolError(Exception):
    """Base exception for tool registration and execution errors."""


class ToolNotFoundError(ToolError):
    """Raised when the model requests an unknown tool."""


class ToolExecutionError(ToolError):
    """Raised when tool arguments are invalid or execution fails."""


class Tool(ABC):
    """Common contract implemented by every Agent tool."""

    name: str
    description: str
    parameters: JsonObject
    requires_confirmation: bool = False

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
