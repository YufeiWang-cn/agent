import json

from ..workspace import WorkspaceAccessError, WorkspaceGuard
from .base import JsonObject, Tool, ToolExecutionError


MAX_DIRECTORY_ENTRIES = 200


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "列出工作目录内指定目录的直接子项，不递归访问。"
    retryable = True
    idempotent = True
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作目录的路径，默认为当前工作目录。",
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def execute(self, arguments: JsonObject) -> str:
        user_path = arguments.get("path", ".")
        if not isinstance(user_path, str):
            raise ToolExecutionError("list_directory 的 path 必须是字符串。")
        try:
            directory = self._guard.resolve_directory(user_path)
            children = sorted(
                (
                    path
                    for path in directory.iterdir()
                    if self._guard.is_accessible(path)
                ),
                key=lambda path: path.name.lower(),
            )
        except (WorkspaceAccessError, OSError) as error:
            raise ToolExecutionError(str(error)) from error

        visible_children = children[:MAX_DIRECTORY_ENTRIES]
        entries = []
        for child in visible_children:
            try:
                is_directory = child.is_dir()
                is_file = child.is_file()
                size = child.stat().st_size if is_file else None
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": self._guard.relative_path(child),
                    "type": "directory" if is_directory else "file",
                    "size": size,
                }
            )
        return json.dumps(
            {
                "path": self._guard.relative_path(directory),
                "entries": entries,
                "truncated": len(children) > MAX_DIRECTORY_ENTRIES,
            },
            ensure_ascii=False,
        )
