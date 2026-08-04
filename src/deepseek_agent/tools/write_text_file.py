import json

from ..workspace import WorkspaceAccessError, WorkspaceGuard
from .base import JsonObject, Tool, ToolExecutionError


class WriteTextFileTool(Tool):
    name = "write_text_file"
    description = "在工作目录内创建或完整覆盖一个 UTF-8 文本文件。"
    requires_confirmation = True
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作目录的目标文件路径。",
            },
            "content": {
                "type": "string",
                "description": "要写入文件的完整文本内容。",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def execute(self, arguments: JsonObject) -> str:
        user_path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(user_path, str):
            raise ToolExecutionError("write_text_file 的 path 必须是字符串。")
        if not isinstance(content, str):
            raise ToolExecutionError("write_text_file 的 content 必须是字符串。")
        try:
            path, created, written_bytes = self._guard.write_text(
                user_path,
                content,
            )
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error
        return json.dumps(
            {
                "path": self._guard.relative_path(path),
                "created": created,
                "bytes": written_bytes,
            },
            ensure_ascii=False,
        )
