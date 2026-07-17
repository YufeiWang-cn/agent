import json
from collections.abc import Iterable

from .base import JsonObject, Tool, ToolExecutionError, ToolNotFoundError


class ToolRegistry:
    """Register tools, expose model schemas, and dispatch tool calls."""

    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具已注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolNotFoundError(f"不存在的工具：{name}") from error

    @property
    def schemas(self) -> list[JsonObject]:
        return [tool.schema for tool in self._tools.values()]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def execute(self, name: str, raw_arguments: str) -> str:
        tool = self.get(name)

        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as error:
            raise ToolExecutionError(
                f"工具 {name} 的参数不是有效 JSON：{error.msg}"
            ) from error

        if not isinstance(arguments, dict):
            raise ToolExecutionError(f"工具 {name} 的参数必须是 JSON 对象。")

        return tool.execute(arguments)
