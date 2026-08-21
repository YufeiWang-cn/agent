"""验证补丁工具的批量校验、差异预览和失败回滚。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.models import ToolCallRequest
from deepseek_agent.tool_execution import ToolExecutionStatus, ToolExecutor
from deepseek_agent.tools import ApplyPatchTool, ToolExecutionError, ToolRegistry
from deepseek_agent.workspace import WorkspaceAccessError, WorkspaceGuard


class RecordingConfirmer:
    """记录确认参数，并按照测试设置返回确认结果。"""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.arguments: list[str] = []

    def confirm(self, tool, arguments: str) -> bool:
        self.arguments.append(arguments)
        return self.allowed


class ApplyPatchToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text(
            "name = 'Agent'\nprint(name)\n",
            encoding="utf-8",
        )
        (self.workspace / "src" / "settings.py").write_text(
            "DEBUG = True\n",
            encoding="utf-8",
        )
        (self.workspace / ".env").write_text(
            "SECRET=value\n",
            encoding="utf-8",
        )
        self.guard = WorkspaceGuard(self.workspace, max_file_size=2_000)
        self.tool = ApplyPatchTool(self.guard)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _valid_changes() -> dict[str, object]:
        return {
            "changes": [
                {
                    "operation": "update",
                    "path": "src/app.py",
                    "edits": [
                        {
                            "old_text": "name = 'Agent'",
                            "new_text": "name = 'DeepSeek Agent'",
                        },
                        {
                            "old_text": "print(name)",
                            "new_text": "print(name.upper())",
                        },
                    ],
                },
                {
                    "operation": "update",
                    "path": "src/settings.py",
                    "edits": [
                        {
                            "old_text": "DEBUG = True",
                            "new_text": "DEBUG = False",
                        }
                    ],
                },
                {
                    "operation": "create",
                    "path": "src/new_module.py",
                    "content": "VALUE = 1\n",
                },
            ]
        }

    def test_applies_multiple_files_and_edits_as_one_batch(self) -> None:
        result = json.loads(self.tool.execute(self._valid_changes()))

        self.assertEqual(result["files_changed"], 3)
        self.assertEqual(result["files_created"], 1)
        self.assertEqual(result["replacements"], 3)
        self.assertEqual(
            (self.workspace / "src" / "app.py").read_text(encoding="utf-8"),
            "name = 'DeepSeek Agent'\nprint(name.upper())\n",
        )
        self.assertEqual(
            (self.workspace / "src" / "settings.py").read_text(
                encoding="utf-8"
            ),
            "DEBUG = False\n",
        )
        self.assertEqual(
            (self.workspace / "src" / "new_module.py").read_text(
                encoding="utf-8"
            ),
            "VALUE = 1\n",
        )

    def test_validation_failure_leaves_every_file_unchanged(self) -> None:
        app_before = (self.workspace / "src" / "app.py").read_bytes()
        settings_before = (self.workspace / "src" / "settings.py").read_bytes()
        arguments = self._valid_changes()
        arguments["changes"][1]["edits"][0]["old_text"] = "MISSING"

        with self.assertRaisesRegex(ToolExecutionError, "未找到"):
            self.tool.execute(arguments)

        self.assertEqual(
            (self.workspace / "src" / "app.py").read_bytes(),
            app_before,
        )
        self.assertEqual(
            (self.workspace / "src" / "settings.py").read_bytes(),
            settings_before,
        )
        self.assertFalse((self.workspace / "src" / "new_module.py").exists())

    def test_rejects_duplicate_ambiguous_and_invalid_operations(self) -> None:
        duplicate = {
            "changes": [
                {
                    "operation": "update",
                    "path": "src/app.py",
                    "edits": [{"old_text": "Agent", "new_text": "One"}],
                },
                {
                    "operation": "update",
                    "path": "src/./app.py",
                    "edits": [{"old_text": "Agent", "new_text": "Two"}],
                },
            ]
        }
        ambiguous = {
            "changes": [
                {
                    "operation": "update",
                    "path": "src/app.py",
                    "edits": [{"old_text": "n", "new_text": "x"}],
                }
            ]
        }
        existing_create = {
            "changes": [
                {
                    "operation": "create",
                    "path": "src/app.py",
                    "content": "replacement",
                }
            ]
        }

        with self.assertRaisesRegex(ToolExecutionError, "重复出现"):
            self.tool.execute(duplicate)
        with self.assertRaisesRegex(ToolExecutionError, "出现"):
            self.tool.execute(ambiguous)
        with self.assertRaisesRegex(ToolExecutionError, "已经存在"):
            self.tool.execute(existing_create)

    def test_rejects_protected_outside_and_missing_parent_paths(self) -> None:
        invalid_paths = [".env", "../outside.py", "missing/new.py"]
        for invalid_path in invalid_paths:
            with self.subTest(path=invalid_path):
                with self.assertRaises(ToolExecutionError):
                    self.tool.execute(
                        {
                            "changes": [
                                {
                                    "operation": "create",
                                    "path": invalid_path,
                                    "content": "value = 1\n",
                                }
                            ]
                        }
                    )

    def test_symlink_cannot_redirect_patch_outside_workspace(self) -> None:
        outside_file = Path(self.temporary_directory.name) / "outside.py"
        outside_file.write_text("value = 1\n", encoding="utf-8")
        link = self.workspace / "src" / "outside_link.py"
        try:
            link.symlink_to(outside_file)
        except (OSError, NotImplementedError):
            self.skipTest("当前 Windows 配置不允许创建符号链接")

        with self.assertRaises(ToolExecutionError):
            self.tool.execute(
                {
                    "changes": [
                        {
                            "operation": "update",
                            "path": "src/outside_link.py",
                            "edits": [
                                {
                                    "old_text": "value = 1",
                                    "new_text": "value = 2",
                                }
                            ],
                        }
                    ]
                }
            )
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "value = 1\n")

    def test_confirmation_payload_contains_unified_diff(self) -> None:
        confirmer = RecordingConfirmer()
        executor = ToolExecutor(ToolRegistry([self.tool]), confirmer)
        raw_arguments = json.dumps(self._valid_changes(), ensure_ascii=False)

        record = executor.execute(
            ToolCallRequest(
                id="call_patch",
                name="apply_patch",
                arguments=raw_arguments,
            )
        )

        self.assertEqual(record.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(len(confirmer.arguments), 1)
        confirmation_payload = json.loads(confirmer.arguments[0])
        self.assertIn("--- a/src/app.py", confirmation_payload["diff"])
        self.assertIn("+++ b/src/new_module.py", confirmation_payload["diff"])
        self.assertIn("+VALUE = 1", confirmation_payload["diff"])

    def test_file_change_after_confirmation_is_not_overwritten(self) -> None:
        class MutatingConfirmer(RecordingConfirmer):
            def confirm(inner_self, tool, arguments: str) -> bool:
                (self.workspace / "src" / "app.py").write_text(
                    "changed externally\n",
                    encoding="utf-8",
                )
                return super().confirm(tool, arguments)

        executor = ToolExecutor(
            ToolRegistry([self.tool]),
            MutatingConfirmer(),
        )
        record = executor.execute(
            ToolCallRequest(
                id="call_stale",
                name="apply_patch",
                arguments=json.dumps(self._valid_changes(), ensure_ascii=False),
            )
        )

        self.assertEqual(record.status, ToolExecutionStatus.FAILED)
        self.assertIn("未找到", record.model_result)
        self.assertEqual(
            (self.workspace / "src" / "app.py").read_text(encoding="utf-8"),
            "changed externally\n",
        )
        self.assertFalse((self.workspace / "src" / "new_module.py").exists())

    def test_write_failure_rolls_back_previous_files(self) -> None:
        app_before = (self.workspace / "src" / "app.py").read_text(
            encoding="utf-8"
        )
        settings_before = (self.workspace / "src" / "settings.py").read_text(
            encoding="utf-8"
        )
        original_write = self.guard.write_text
        call_count = 0

        def fail_third_write(path: str, content: str):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise WorkspaceAccessError("模拟第三个文件写入失败")
            return original_write(path, content)

        arguments = self._valid_changes()
        changes = arguments["changes"]
        arguments["changes"] = [changes[2], changes[0], changes[1]]

        with patch.object(
            self.guard,
            "write_text",
            side_effect=fail_third_write,
        ):
            with self.assertRaisesRegex(ToolExecutionError, "已回滚") as raised:
                self.tool.execute(arguments)

        self.assertFalse(raised.exception.side_effect_possible)
        self.assertEqual(
            (self.workspace / "src" / "app.py").read_text(encoding="utf-8"),
            app_before,
        )
        self.assertEqual(
            (self.workspace / "src" / "settings.py").read_text(
                encoding="utf-8"
            ),
            settings_before,
        )
        self.assertFalse((self.workspace / "src" / "new_module.py").exists())

    def test_rollback_failure_marks_result_as_unknown(self) -> None:
        original_write = self.guard.write_text
        call_count = 0

        def fail_write_and_rollback(path: str, content: str):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise WorkspaceAccessError("模拟写入或回滚失败")
            return original_write(path, content)

        with patch.object(
            self.guard,
            "write_text",
            side_effect=fail_write_and_rollback,
        ):
            with self.assertRaises(ToolExecutionError) as raised:
                self.tool.execute(self._valid_changes())

        self.assertTrue(raised.exception.side_effect_possible)
        self.assertIn("无法回滚", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
