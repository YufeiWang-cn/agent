import json

from ..workspace import WorkspaceAccessError, WorkspaceGuard
from .base import JsonObject, Tool, ToolExecutionError


class ReplaceTextTool(Tool):
    name = "replace_text"
    description = "在工作目录内的 UTF-8 文本文件中精确替换唯一一处文本。"
    requires_confirmation = True
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作目录的目标文件路径。",
            },
            "old_text": {
                "type": "string",
                "description": "文件中必须恰好出现一次的原文本。",
            },
            "new_text": {
                "type": "string",
                "description": "用于替换原文本的新文本。",
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def execute(self, arguments: JsonObject) -> str:
        user_path = arguments.get("path")
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")

        if not isinstance(user_path, str):
            raise ToolExecutionError("replace_text 的 path 必须是字符串。")
        if not isinstance(old_text, str) or not old_text:
            raise ToolExecutionError("replace_text 的 old_text 必须是非空字符串。")
        if not isinstance(new_text, str):
            raise ToolExecutionError("replace_text 的 new_text 必须是字符串。")

        try:
            path, content = self._guard.read_text(user_path)
            match_count = content.count(old_text)
            if match_count == 0:
                raise ToolExecutionError("未找到要替换的 old_text，文件没有修改。")
            if match_count > 1:
                raise ToolExecutionError(
                    f"old_text 在文件中出现 {match_count} 次；请提供更完整的上下文，使其只匹配一次。"
                )

            updated_content = content.replace(old_text, new_text, 1)
            _path, _created, written_bytes = self._guard.write_text(
                user_path,
                updated_content,
            )
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error

        return json.dumps(
            {
                "path": self._guard.relative_path(path),
                "replacements": 1,
                "bytes": written_bytes,
            },
            ensure_ascii=False,
        )
