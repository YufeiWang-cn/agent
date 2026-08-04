from ..workspace import WorkspaceAccessError, WorkspaceGuard
from .base import JsonObject, Tool, ToolExecutionError


class ReadTextFileTool(Tool):
    name = "read_text_file"
    description = "读取工作目录内不超过大小限制的 UTF-8 文本文件。"
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作目录的文本文件路径。",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def execute(self, arguments: JsonObject) -> str:
        user_path = arguments.get("path")
        if not isinstance(user_path, str):
            raise ToolExecutionError("read_text_file 的 path 必须是字符串。")
        try:
            _path, content = self._guard.read_text(user_path)
            return content
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error
