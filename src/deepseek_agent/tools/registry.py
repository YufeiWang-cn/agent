"""注册工具，并负责工具名称查找和基础参数解析。"""

import json
from collections.abc import Iterable

from .base import JsonObject, Tool, ToolExecutionError, ToolNotFoundError


class ToolRegistry:
    """集中管理工具实例、提供给模型的工具结构和调用分发。"""

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
        tool, arguments = self.prepare(name, raw_arguments)
        return tool.execute(arguments)

    def prepare(self, name: str, raw_arguments: str) -> tuple[Tool, JsonObject]:
        """解析一次工具调用，并返回目标工具及经过基础校验的参数。"""
        tool = self.get(name)

        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as error:
            raise ToolExecutionError(
                f"工具 {name} 的参数不是有效 JSON：{error.msg}"
            ) from error

        if not isinstance(arguments, dict):
            raise ToolExecutionError(f"工具 {name} 的参数必须是 JSON 对象。")

        return tool, arguments
