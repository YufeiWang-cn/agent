from pathlib import Path

from ..workspace import WorkspaceGuard
from .base import Tool, ToolError, ToolExecutionError, ToolNotFoundError
from .calculator import CalculatorTool
from .datetime_tool import DateTimeTool
from .list_directory import ListDirectoryTool
from .read_text_file import ReadTextFileTool
from .registry import ToolRegistry
from .write_text_file import WriteTextFileTool


def build_default_registry(
    workspace_root: Path | None = None,
    max_file_size: int = 100_000,
) -> ToolRegistry:
    guard = WorkspaceGuard(workspace_root or Path.cwd(), max_file_size)
    return ToolRegistry(
        [
            CalculatorTool(),
            DateTimeTool(),
            ListDirectoryTool(guard),
            ReadTextFileTool(guard),
            WriteTextFileTool(guard),
        ]
    )


__all__ = [
    "CalculatorTool",
    "DateTimeTool",
    "ListDirectoryTool",
    "ReadTextFileTool",
    "Tool",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistry",
    "WriteTextFileTool",
    "build_default_registry",
]
