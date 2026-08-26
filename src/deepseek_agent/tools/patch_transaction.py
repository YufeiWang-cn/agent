"""准备文件补丁，并以补偿事务提交已经验证的修改。"""

from dataclasses import dataclass
from difflib import SequenceMatcher, unified_diff
from pathlib import Path
from typing import Any

from ..workspace import WorkspaceAccessError, WorkspaceGuard
from .base import JsonObject, ToolExecutionError


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


class PatchPreparer:
    """验证整批修改，并生成不触碰磁盘的候选补丁。"""

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def prepare(self, arguments: JsonObject) -> list[PreparedFilePatch]:
        """先验证全部修改，确保任一错误都不会留下部分写入。"""
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
                    "同一个文件不能在一次补丁中重复出现："
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
            updated_content = self._apply_edit(
                updated_content,
                edit,
                change_index=index,
                edit_index=edit_index,
            )

        try:
            prepared_path, created, _encoded = self._guard.prepare_text_write(
                user_path,
                updated_content,
            )
        except WorkspaceAccessError as error:
            raise ToolExecutionError(str(error)) from error
        if created or prepared_path != path:
            raise ToolExecutionError(f"更新目标在验证期间发生变化：{user_path}。")
        return self._build_patch(
            path,
            "update",
            original_content,
            updated_content,
            replacements=len(edits),
        )

    @staticmethod
    def _apply_edit(
        content: str,
        edit: object,
        *,
        change_index: int,
        edit_index: int,
    ) -> str:
        if not isinstance(edit, dict):
            raise ToolExecutionError(
                f"第 {change_index} 个 change 的第 {edit_index} 个 edit 必须是对象。"
            )
        unexpected_fields = set(edit) - {"old_text", "new_text"}
        if unexpected_fields:
            names = "、".join(sorted(unexpected_fields))
            raise ToolExecutionError(
                f"第 {change_index} 个 change 的第 {edit_index} 个 edit "
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

        match_count = content.count(old_text)
        if match_count == 0:
            raise ToolExecutionError(
                f"第 {change_index} 个 change 的第 {edit_index} 个 old_text "
                "未找到，所有文件均未修改。"
            )
        if match_count > 1:
            raise ToolExecutionError(
                f"第 {change_index} 个 change 的第 {edit_index} 个 old_text "
                f"出现 {match_count} 次，请提供更完整的上下文。"
            )
        return content.replace(old_text, new_text, 1)

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
        return self._build_patch(
            path,
            "create",
            None,
            content,
            replacements=0,
        )

    def _build_patch(
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
        return PreparedFilePatch(
            path=path,
            relative_path=relative_path,
            operation=operation,
            original_content=original_content,
            updated_content=updated_content,
            replacements=replacements,
            added_lines=added_lines,
            deleted_lines=deleted_lines,
            diff=self._unified_diff(
                relative_path,
                original_content,
                updated_content,
            ),
        )

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


class PatchTransaction:
    """在写入前检查快照，并在部分失败时执行反向补偿。"""

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def apply(self, changes: list[PreparedFilePatch]) -> None:
        """按顺序提交整批补丁，并回滚已经完成的写入。"""
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
                    "补丁写入失败，且部分文件无法回滚；请立即检查工作区状态。",
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


__all__ = [
    "MAX_EDITS_PER_FILE",
    "MAX_PATCH_FILES",
    "PatchPreparer",
    "PatchTransaction",
    "PreparedFilePatch",
]
