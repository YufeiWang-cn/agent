"""定义执行副作用工具前使用的确认协议。"""

from typing import Protocol

from ..tools import Tool


class ToolConfirmer(Protocol):
    """约束命令行和图形界面使用相同的工具确认接口。"""

    def confirm(self, tool: Tool, arguments: str) -> bool:
        """仅在用户允许执行工具时返回 ``True``。"""
        ...
