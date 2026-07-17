from .base import Tool, ToolError, ToolExecutionError, ToolNotFoundError
from .calculator import CalculatorTool
from .datetime_tool import DateTimeTool
from .registry import ToolRegistry


def build_default_registry() -> ToolRegistry:
    return ToolRegistry([CalculatorTool(), DateTimeTool()])


__all__ = [
    "CalculatorTool",
    "DateTimeTool",
    "Tool",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistry",
    "build_default_registry",
]
