from typing import Protocol

from ..tools import Tool


class ToolConfirmer(Protocol):
    def confirm(self, tool: Tool, arguments: str) -> bool:
        """Return True only when the tool call is allowed to execute."""
        ...
