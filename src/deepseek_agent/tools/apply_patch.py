"""以事务方式在安全工作区内创建或更新多个文本文件。"""

import json

from ..workspace import WorkspaceGuard
from .base import JsonObject, Tool, ToolEffect
from .patch_transaction import (
    MAX_EDITS_PER_FILE,
    MAX_PATCH_FILES,
    PatchPreparer,
    PatchTransaction,
)


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
        self._preparer = PatchPreparer(guard)
        self._transaction = PatchTransaction(guard)

    def confirmation_arguments_for(
        self,
        arguments: JsonObject,
        raw_arguments: str,
    ) -> str:
        """在原始补丁参数后附加根据当前文件生成的统一差异。"""
        prepared = self._preparer.prepare(arguments)
        return json.dumps(
            {
                "changes": arguments.get("changes"),
                "diff": "".join(change.diff for change in prepared),
            },
            ensure_ascii=False,
            indent=2,
        )

    def execute(self, arguments: JsonObject) -> str:
        prepared = self._preparer.prepare(arguments)
        self._transaction.apply(prepared)

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

__all__ = ["ApplyPatchTool"]
