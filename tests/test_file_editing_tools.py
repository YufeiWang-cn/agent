import json
import tempfile
import unittest
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.tools import (
    ReplaceTextTool,
    SearchTextTool,
    ToolExecutionError,
)
from deepseek_agent.workspace import WorkspaceGuard


class FileEditingToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text(
            "name = 'Agent'\nprint(name)\n",
            encoding="utf-8",
        )
        (self.workspace / "src" / "other.py").write_text(
            "NAME = 'agent'\n",
            encoding="utf-8",
        )
        (self.workspace / "repeat.txt").write_text(
            "same\nsame\n",
            encoding="utf-8",
        )
        (self.workspace / ".env").write_text(
            "SECRET=Agent",
            encoding="utf-8",
        )
        (self.workspace / "binary.bin").write_bytes(b"\xff\xfe")

        guard = WorkspaceGuard(self.workspace, max_file_size=1_000)
        self.search_tool = SearchTextTool(guard)
        self.replace_tool = ReplaceTextTool(guard)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_search_returns_paths_and_line_numbers(self) -> None:
        result = json.loads(
            self.search_tool.execute(
                {"path": "src", "query": "Agent"}
            )
        )

        self.assertEqual(result["path"], "src")
        self.assertEqual(result["files_scanned"], 2)
        self.assertEqual(
            result["matches"],
            [
                {
                    "path": "src/app.py",
                    "line": 1,
                    "text": "name = 'Agent'",
                    "line_truncated": False,
                }
            ],
        )
        self.assertFalse(result["truncated"])

    def test_search_can_ignore_case_and_skips_protected_files(self) -> None:
        result = json.loads(
            self.search_tool.execute(
                {"query": "agent", "case_sensitive": False}
            )
        )

        paths = [match["path"] for match in result["matches"]]
        self.assertEqual(paths, ["src/app.py", "src/other.py"])
        self.assertNotIn(".env", paths)
        self.assertEqual(result["skipped_files"], 1)

    def test_search_rejects_empty_query_and_outside_path(self) -> None:
        with self.assertRaises(ToolExecutionError):
            self.search_tool.execute({"query": ""})
        with self.assertRaises(ToolExecutionError):
            self.search_tool.execute({"query": "Agent", "path": ".."})

    def test_replace_updates_exactly_one_match(self) -> None:
        result = json.loads(
            self.replace_tool.execute(
                {
                    "path": "src/app.py",
                    "old_text": "name = 'Agent'",
                    "new_text": "name = 'DeepSeek Agent'",
                }
            )
        )

        self.assertEqual(result["path"], "src/app.py")
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(
            (self.workspace / "src" / "app.py").read_text(encoding="utf-8"),
            "name = 'DeepSeek Agent'\nprint(name)\n",
        )

    def test_replace_rejects_zero_or_multiple_matches_without_changes(self) -> None:
        app_before = (self.workspace / "src" / "app.py").read_bytes()
        repeat_before = (self.workspace / "repeat.txt").read_bytes()

        with self.assertRaisesRegex(ToolExecutionError, "未找到"):
            self.replace_tool.execute(
                {
                    "path": "src/app.py",
                    "old_text": "missing",
                    "new_text": "new",
                }
            )
        with self.assertRaisesRegex(ToolExecutionError, "出现 2 次"):
            self.replace_tool.execute(
                {
                    "path": "repeat.txt",
                    "old_text": "same",
                    "new_text": "changed",
                }
            )

        self.assertEqual((self.workspace / "src" / "app.py").read_bytes(), app_before)
        self.assertEqual((self.workspace / "repeat.txt").read_bytes(), repeat_before)

    def test_replace_requires_confirmation_and_respects_guard(self) -> None:
        self.assertTrue(self.replace_tool.requires_confirmation)
        with self.assertRaises(ToolExecutionError):
            self.replace_tool.execute(
                {
                    "path": ".env",
                    "old_text": "Agent",
                    "new_text": "changed",
                }
            )


if __name__ == "__main__":
    unittest.main()
