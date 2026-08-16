"""递归搜索安全工作区内可读取的 UTF-8 文本文件。"""

import json
from collections import deque
from pathlib import Path

from ..workspace import WorkspaceAccessError, WorkspaceGuard
from .base import JsonObject, Tool, ToolExecutionError


MAX_SEARCH_RESULTS = 100
MAX_RESULT_LINE_LENGTH = 500


class SearchTextTool(Tool):
    """以广度优先方式搜索文本，并限制结果数量和单行长度。"""

    name = "search_text"
    description = "递归搜索工作目录内 UTF-8 文本文件中的文本，并返回匹配位置。"
    retryable = True
    idempotent = True
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要搜索的非空文本。",
            },
            "path": {
                "type": "string",
                "description": "相对于工作目录的搜索目录，默认为当前工作目录。",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "是否区分大小写，默认为 true。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def execute(self, arguments: JsonObject) -> str:
        query = arguments.get("query")
        user_path = arguments.get("path", ".")
        case_sensitive = arguments.get("case_sensitive", True)

        if not isinstance(query, str) or not query:
            raise ToolExecutionError("search_text 的 query 必须是非空字符串。")
        if not isinstance(user_path, str):
            raise ToolExecutionError("search_text 的 path 必须是字符串。")
        if not isinstance(case_sensitive, bool):
            raise ToolExecutionError("search_text 的 case_sensitive 必须是布尔值。")

        try:
            directory = self._guard.resolve_directory(user_path)
            result = self._search(directory, query, case_sensitive)
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error

        return json.dumps(
            {
                "path": self._guard.relative_path(directory),
                "query": query,
                **result,
            },
            ensure_ascii=False,
        )

    def _search(
        self,
        directory: Path,
        query: str,
        case_sensitive: bool,
    ) -> JsonObject:
        matches: list[JsonObject] = []
        pending_directories = deque([directory])
        visited_directories: set[Path] = set()
        files_scanned = 0
        skipped_files = 0
        truncated = False
        needle = query if case_sensitive else query.casefold()

        while pending_directories and not truncated:
            current = pending_directories.popleft()
            try:
                resolved_current = current.resolve(strict=True)
                if resolved_current in visited_directories:
                    continue
                visited_directories.add(resolved_current)
                children = sorted(current.iterdir(), key=lambda path: path.name.lower())
            except OSError:
                continue

            for child in children:
                if not self._guard.is_accessible(child):
                    continue
                try:
                    if child.is_dir():
                        pending_directories.append(child)
                        continue
                    if not child.is_file():
                        continue
                    relative_path = self._guard.relative_path(child.resolve(strict=True))
                    _path, content = self._guard.read_text(relative_path)
                except (OSError, WorkspaceAccessError):
                    skipped_files += 1
                    continue

                files_scanned += 1
                for line_number, line in enumerate(content.splitlines(), start=1):
                    haystack = line if case_sensitive else line.casefold()
                    if needle not in haystack:
                        continue
                    matches.append(
                        {
                            "path": relative_path,
                            "line": line_number,
                            "text": line[:MAX_RESULT_LINE_LENGTH],
                            "line_truncated": len(line) > MAX_RESULT_LINE_LENGTH,
                        }
                    )
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        truncated = True
                        break
                if truncated:
                    break

        return {
            "matches": matches,
            "truncated": truncated,
            "files_scanned": files_scanned,
            "skipped_files": skipped_files,
        }
