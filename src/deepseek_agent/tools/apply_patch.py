"""以事务方式在安全工作区内创建或更新多个文本文件。"""

import json
from dataclasses import dataclass
from difflib import SequenceMatcher, unified_diff
from pathlib import Path
from typing import Any

from ..workspace import WorkspaceAccessError, WorkspaceGuard
from .base import JsonObject, Tool, ToolEffect, ToolExecutionError


MAX_PATCH_FILES = 20
MAX_EDITS_PER_FILE = 50


@dataclass(frozen=True, slots=True)
class PreparedFilePatch:
    """保存一个已经完成校验但尚未写入的文件补丁。"""

    path: Path
    relative_path: str
    operation: str
    original_content: str | None
    updated_content: str
    replacements: int
    added_lines: int
    deleted_lines: int
    diff: str


class ApplyPatchTool(Tool):
    """先验证全部补丁，再以可回滚的批次修改多个文本文件。"""

    name = "apply_patch"
    description = (
        "在工作目录内以补丁方式创建或更新多个 UTF-8 文本文件；"
        "所有修改会先验证，写入失败时会尝试回滚整个批次。"
    )
    requires_confirmation = True
    effect = ToolEffect.IRREVERSIBLE_WRITE
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PATCH_FILES,
                "description": "需要统一验证并写入的文件修改列表。",
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["update", "create"],
                            "description": "update 修改现有文件，create 创建新文件。",
                        },
                        "path": {
                            "type": "string",
                            "description": "相对于工作目录的目标文件路径。",
                        },
                        "edits": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_EDITS_PER_FILE,
                            "description": "update 操作按顺序执行的精确替换列表。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_text": {
                                        "type": "string",
                                        "description": "当前文件中必须恰好出现一次的原文本。",
                                    },
                                    "new_text": {
                                        "type": "string",
                                        "description": "用于替换原文本的新文本。",
                                    },
                                },
                                "required": ["old_text", "new_text"],
                                "additionalProperties": False,
                            },
                        },
                        "content": {
                            "type": "string",
                            "description": "create 操作写入新文件的完整内容。",
                        },
                    },
                    "required": ["operation", "path"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["changes"],
        "additionalProperties": False,
    }

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def confirmation_arguments_for(
        self,
        arguments: JsonObject,
        raw_arguments: str,
    ) -> str:
        """在原始补丁参数后附加根据当前文件生成的统一差异。"""
        prepared = self._prepare_changes(arguments)
        return json.dumps(
            {
                "changes": arguments.get("changes"),
                "diff": "".join(change.diff for change in prepared),
            },
            ensure_ascii=False,
            indent=2,
        )

    def execute(self, arguments: JsonObject) -> str:
        prepared = self._prepare_changes(arguments)
        self._apply_atomically(prepared)

        files = [
            {
                "path": change.relative_path,
                "operation": change.operation,
                "replacements": change.replacements,
                "added_lines": change.added_lines,
                "deleted_lines": change.deleted_lines,
                "bytes": len(change.updated_content.encode("utf-8")),
            }
            for change in prepared
        ]
        return json.dumps(
            {
                "files_changed": len(files),
                "files_created": sum(
                    file["operation"] == "create" for file in files
                ),
                "replacements": sum(file["replacements"] for file in files),
                "added_lines": sum(file["added_lines"] for file in files),
                "deleted_lines": sum(file["deleted_lines"] for file in files),
                "files": files,
            },
            ensure_ascii=False,
        )

    def _prepare_changes(
        self,
        arguments: JsonObject,
    ) -> list[PreparedFilePatch]:
        unexpected_arguments = set(arguments) - {"changes"}
        if unexpected_arguments:
            names = "、".join(sorted(unexpected_arguments))
            raise ToolExecutionError(f"apply_patch 包含不支持的参数：{names}。")

        changes = arguments.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ToolExecutionError("apply_patch 的 changes 必须是非空数组。")
        if len(changes) > MAX_PATCH_FILES:
            raise ToolExecutionError(
                f"apply_patch 每次最多修改 {MAX_PATCH_FILES} 个文件。"
            )

        prepared: list[PreparedFilePatch] = []
        seen_paths: set[Path] = set()
        for index, change in enumerate(changes, start=1):
            if not isinstance(change, dict):
                raise ToolExecutionError(
                    f"apply_patch 的第 {index} 个 change 必须是对象。"
                )
            prepared_change = self._prepare_change(change, index)
            if prepared_change.path in seen_paths:
                raise ToolExecutionError(
                    f"同一个文件不能在一次补丁中重复出现："
                    f"{prepared_change.relative_path}。"
                )
            seen_paths.add(prepared_change.path)
            prepared.append(prepared_change)
        return prepared

    def _prepare_change(
        self,
        change: dict[str, Any],
        index: int,
    ) -> PreparedFilePatch:
        allowed_fields = {"operation", "path", "edits", "content"}
        unexpected_fields = set(change) - allowed_fields
        if unexpected_fields:
            names = "、".join(sorted(unexpected_fields))
            raise ToolExecutionError(
                f"第 {index} 个 change 包含不支持的字段：{names}。"
            )

        operation = change.get("operation")
        user_path = change.get("path")
        if operation not in {"update", "create"}:
            raise ToolExecutionError(
                f"第 {index} 个 change 的 operation 必须是 update 或 create。"
            )
        if not isinstance(user_path, str):
            raise ToolExecutionError(
                f"第 {index} 个 change 的 path 必须是字符串。"
            )

        if operation == "update":
            return self._prepare_update(change, index, user_path)
        return self._prepare_create(change, index, user_path)

    def _prepare_update(
        self,
        change: dict[str, Any],
        index: int,
        user_path: str,
    ) -> PreparedFilePatch:
        if "content" in change:
            raise ToolExecutionError(
                f"第 {index} 个 update change 不能包含 content。"
            )
        edits = change.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ToolExecutionError(
                f"第 {index} 个 update change 的 edits 必须是非空数组。"
            )
        if len(edits) > MAX_EDITS_PER_FILE:
            raise ToolExecutionError(
                f"每个文件最多包含 {MAX_EDITS_PER_FILE} 个修改区块。"
            )

        try:
            path, original_content = self._guard.read_text(user_path)
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error

        updated_content = original_content
        for edit_index, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                raise ToolExecutionError(
                    f"第 {index} 个 change 的第 {edit_index} 个 edit 必须是对象。"
                )
            unexpected_fields = set(edit) - {"old_text", "new_text"}
            if unexpected_fields:
                names = "、".join(sorted(unexpected_fields))
                raise ToolExecutionError(
                    f"第 {index} 个 change 的第 {edit_index} 个 edit "
                    f"包含不支持的字段：{names}。"
                )
            old_text = edit.get("old_text")
            new_text = edit.get("new_text")
            if not isinstance(old_text, str) or not old_text:
                raise ToolExecutionError("每个 old_text 都必须是非空字符串。")
            if not isinstance(new_text, str):
                raise ToolExecutionError("每个 new_text 都必须是字符串。")
            if old_text == new_text:
                raise ToolExecutionError("old_text 和 new_text 不能完全相同。")

            match_count = updated_content.count(old_text)
            if match_count == 0:
                raise ToolExecutionError(
                    f"第 {index} 个 change 的第 {edit_index} 个 old_text "
                    "未找到，所有文件均未修改。"
                )
            if match_count > 1:
                raise ToolExecutionError(
                    f"第 {index} 个 change 的第 {edit_index} 个 old_text "
                    f"出现 {match_count} 次，请提供更完整的上下文。"
                )
            updated_content = updated_content.replace(old_text, new_text, 1)

        try:
            prepared_path, created, _encoded = self._guard.prepare_text_write(
                user_path,
                updated_content,
            )
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error
        if created or prepared_path != path:
            raise ToolExecutionError(f"更新目标在验证期间发生变化：{user_path}。")

        return self._build_prepared_patch(
            path,
            "update",
            original_content,
            updated_content,
            replacements=len(edits),
        )

    def _prepare_create(
        self,
        change: dict[str, Any],
        index: int,
        user_path: str,
    ) -> PreparedFilePatch:
        if "edits" in change:
            raise ToolExecutionError(
                f"第 {index} 个 create change 不能包含 edits。"
            )
        content = change.get("content")
        if not isinstance(content, str):
            raise ToolExecutionError(
                f"第 {index} 个 create change 的 content 必须是字符串。"
            )
        try:
            path, created, _encoded = self._guard.prepare_text_write(
                user_path,
                content,
            )
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error
        if not created:
            raise ToolExecutionError(f"create 目标已经存在：{user_path}。")

        return self._build_prepared_patch(
            path,
            "create",
            None,
            content,
            replacements=0,
        )

    def _build_prepared_patch(
        self,
        path: Path,
        operation: str,
        original_content: str | None,
        updated_content: str,
        *,
        replacements: int,
    ) -> PreparedFilePatch:
        relative_path = self._guard.relative_path(path)
        before = original_content or ""
        added_lines, deleted_lines = self._line_changes(before, updated_content)
        diff = self._unified_diff(
            relative_path,
            original_content,
            updated_content,
        )
        return PreparedFilePatch(
            path=path,
            relative_path=relative_path,
            operation=operation,
            original_content=original_content,
            updated_content=updated_content,
            replacements=replacements,
            added_lines=added_lines,
            deleted_lines=deleted_lines,
            diff=diff,
        )

    def _apply_atomically(self, changes: list[PreparedFilePatch]) -> None:
        committed: list[PreparedFilePatch] = []
        try:
            for change in changes:
                self._ensure_source_is_current(change)
                self._guard.write_text(
                    change.relative_path,
                    change.updated_content,
                )
                committed.append(change)
        except Exception as error:
            rollback_errors = self._rollback(committed)
            if rollback_errors:
                raise ToolExecutionError(
                    "补丁写入失败，并且部分文件无法回滚；请立即检查工作区状态。",
                    side_effect_possible=True,
                ) from error
            raise ToolExecutionError(
                f"补丁写入失败，已回滚所有已写入文件：{error}"
            ) from error

    def _ensure_source_is_current(self, change: PreparedFilePatch) -> None:
        if change.original_content is None:
            if change.path.exists():
                raise WorkspaceAccessError(
                    f"创建目标在验证后已经出现：{change.relative_path}"
                )
            return
        _path, current_content = self._guard.read_text(change.relative_path)
        if current_content != change.original_content:
            raise WorkspaceAccessError(
                f"文件在验证后发生变化：{change.relative_path}"
            )

    def _rollback(self, changes: list[PreparedFilePatch]) -> list[str]:
        errors: list[str] = []
        for change in reversed(changes):
            try:
                if change.original_content is None:
                    change.path.unlink(missing_ok=True)
                else:
                    self._guard.write_text(
                        change.relative_path,
                        change.original_content,
                    )
            except Exception as error:
                errors.append(f"{change.relative_path}: {error}")
        return errors

    @staticmethod
    def _line_changes(before: str, after: str) -> tuple[int, int]:
        matcher = SequenceMatcher(
            None,
            before.splitlines(),
            after.splitlines(),
            autojunk=False,
        )
        added = 0
        deleted = 0
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag in {"replace", "delete"}:
                deleted += old_end - old_start
            if tag in {"replace", "insert"}:
                added += new_end - new_start
        return added, deleted

    @staticmethod
    def _unified_diff(
        relative_path: str,
        original_content: str | None,
        updated_content: str,
    ) -> str:
        before_lines = (original_content or "").splitlines()
        after_lines = updated_content.splitlines()
        lines = list(
            unified_diff(
                before_lines,
                after_lines,
                fromfile=(
                    f"a/{relative_path}"
                    if original_content is not None
                    else "/dev/null"
                ),
                tofile=f"b/{relative_path}",
                lineterm="",
            )
        )
        if not lines:
            return f"--- /dev/null\n+++ b/{relative_path}\n"
        return "\n".join(lines) + "\n"


__all__ = ["ApplyPatchTool"]
