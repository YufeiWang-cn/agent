"""导出工具公共接口，并组装 Agent 的默认工具集合。"""

from pathlib import Path

from ..workspace import WorkspaceGuard
from .apply_patch import ApplyPatchTool
from .base import (
    JsonObject,
    Tool,
    ToolEffect,
    ToolError,
    ToolExecutionContext,
    ToolExecutionError,
    ToolNotFoundError,
)
from .calculator import CalculatorTool
from .command_executor import (
    CommandExecutor,
    DockerCommandExecutor,
    LocalCommandExecutor,
    build_command_executor,
)
from .datetime_tool import DateTimeTool
from .list_directory import ListDirectoryTool
from .read_text_file import ReadTextFileTool
from .replace_text import ReplaceTextTool
from .registry import ToolRegistry
from .run_command import RunCommandTool
from .search_text import SearchTextTool
from .update_plan import PlanUpdater, UpdatePlanTool
from .write_text_file import WriteTextFileTool


def build_default_registry(
    workspace_root: Path | None = None,
    max_file_size: int = 100_000,
    command_timeout: float = 120.0,
    max_command_output: int = 50_000,
    plan_updater: PlanUpdater | None = None,
    command_execution_mode: str = "local",
    command_container_image: str = "python:3.10-slim",
) -> ToolRegistry:
    """使用同一个工作区守卫构建默认工具注册表。"""
    guard = WorkspaceGuard(workspace_root or Path.cwd(), max_file_size)
    tools: list[Tool] = [
        CalculatorTool(),
        DateTimeTool(),
        ListDirectoryTool(guard),
        ReadTextFileTool(guard),
        SearchTextTool(guard),
        RunCommandTool(
            guard,
            timeout_seconds=command_timeout,
            max_output_bytes=max_command_output,
            command_executor=build_command_executor(
                command_execution_mode,
                command_container_image,
            ),
        ),
        ApplyPatchTool(guard),
        ReplaceTextTool(guard),
        WriteTextFileTool(guard),
    ]
    if plan_updater is not None:
        # 计划工具依赖当前 Agent 的状态提交回调，因此只在回调存在时注册。
        tools.insert(0, UpdatePlanTool(plan_updater))
    return ToolRegistry(tools)


__all__ = [
    "ApplyPatchTool",
    "CalculatorTool",
    "CommandExecutor",
    "DateTimeTool",
    "DockerCommandExecutor",
    "ListDirectoryTool",
    "LocalCommandExecutor",
    "ReadTextFileTool",
    "ReplaceTextTool",
    "SearchTextTool",
    "RunCommandTool",
    "JsonObject",
    "Tool",
    "ToolEffect",
    "ToolError",
    "ToolExecutionContext",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistry",
    "UpdatePlanTool",
    "WriteTextFileTool",
    "build_default_registry",
    "build_command_executor",
]
