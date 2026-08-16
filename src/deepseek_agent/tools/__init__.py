"""导出工具公共接口，并组装 Agent 的默认工具集合。"""

from pathlib import Path

from ..workspace import WorkspaceGuard
from .base import (
    JsonObject,
    Tool,
    ToolEffect,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
)
from .calculator import CalculatorTool
from .datetime_tool import DateTimeTool
from .list_directory import ListDirectoryTool
from .read_text_file import ReadTextFileTool
from .replace_text import ReplaceTextTool
from .registry import ToolRegistry
from .search_text import SearchTextTool
from .write_text_file import WriteTextFileTool


def build_default_registry(
    workspace_root: Path | None = None,
    max_file_size: int = 100_000,
) -> ToolRegistry:
    """使用同一个工作区守卫构建默认工具注册表。"""
    guard = WorkspaceGuard(workspace_root or Path.cwd(), max_file_size)
    return ToolRegistry(
        [
            CalculatorTool(),
            DateTimeTool(),
            ListDirectoryTool(guard),
            ReadTextFileTool(guard),
            SearchTextTool(guard),
            ReplaceTextTool(guard),
            WriteTextFileTool(guard),
        ]
    )


__all__ = [
    "CalculatorTool",
    "DateTimeTool",
    "ListDirectoryTool",
    "ReadTextFileTool",
    "ReplaceTextTool",
    "SearchTextTool",
    "JsonObject",
    "Tool",
    "ToolEffect",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistry",
    "WriteTextFileTool",
    "build_default_registry",
]
