"""验证工作区文件工具的路径、编码和大小限制。"""

import json
import tempfile
import unittest
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.tools import (
    ListDirectoryTool,
    ReadTextFileTool,
    ReplaceTextTool,
    SearchTextTool,
    ToolExecutionError,
    WriteTextFileTool,
    build_default_registry,
)
from deepseek_agent.workspace import WorkspaceGuard
from deepseek_agent.workspace.guard import DEFAULT_BLOCKED_PREFIXES


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_bytes(
            "print('你好')\n".encode("utf-8")
        )
        (self.workspace / ".env").write_text("SECRET=value", encoding="utf-8")
        (self.workspace / ".git").mkdir()
        (self.workspace / "logs").mkdir()
        (self.workspace / "data" / "sessions").mkdir(parents=True)
        (self.workspace / "data" / "runs").mkdir()
        (self.workspace / "data" / "projects.json").write_text(
            '{"version": 1}',
            encoding="utf-8",
        )
        (self.workspace / "data" / "dataset.json").write_text(
            '{"items": []}',
            encoding="utf-8",
        )
        self.outside_file = self.base / "outside.txt"
        self.outside_file.write_text("outside", encoding="utf-8")

        self.guard = WorkspaceGuard(self.workspace, max_file_size=100)
        self.list_tool = ListDirectoryTool(self.guard)
        self.read_tool = ReadTextFileTool(self.guard)
        self.search_tool = SearchTextTool(self.guard)
        self.replace_tool = ReplaceTextTool(self.guard)
        self.write_tool = WriteTextFileTool(self.guard)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_list_directory_hides_sensitive_entries(self) -> None:
        result = json.loads(self.list_tool.execute({"path": "."}))
        names = [entry["name"] for entry in result["entries"]]

        self.assertIn("src", names)
        self.assertIn("data", names)
        self.assertNotIn(".env", names)
        self.assertNotIn(".git", names)
        self.assertNotIn("logs", names)

        data_result = json.loads(self.list_tool.execute({"path": "data"}))
        self.assertEqual(
            [entry["name"] for entry in data_result["entries"]],
            ["dataset.json"],
        )

    def test_read_text_file_returns_utf8_content(self) -> None:
        result = self.read_tool.execute({"path": "src/app.py"})
        self.assertEqual(result, "print('你好')\n")

    def test_sensitive_and_outside_paths_are_rejected(self) -> None:
        blocked_paths = [
            ".env",
            ".git/config",
            "logs/agent.log",
            "data/sessions/session.json",
            "data/runs/run.jsonl",
            "data/projects.json",
            "../outside.txt",
            str(self.outside_file),
        ]

        for path in blocked_paths:
            with self.subTest(path=path):
                with self.assertRaises(ToolExecutionError):
                    self.read_tool.execute({"path": path})

        absolute_inside_path = self.workspace / "src" / "app.py"
        with self.assertRaises(ToolExecutionError):
            self.read_tool.execute({"path": str(absolute_inside_path)})

    def test_regular_data_files_remain_accessible(self) -> None:
        content = self.read_tool.execute({"path": "data/dataset.json"})

        self.assertEqual(content, '{"items": []}')

    def test_internal_state_paths_reject_writes(self) -> None:
        protected_paths = (
            "data/runs/new-run.jsonl",
            "data/projects.json",
        )

        for path in protected_paths:
            with self.subTest(path=path):
                with self.assertRaises(ToolExecutionError):
                    self.write_tool.execute(
                        {"path": path, "content": "should not be written"}
                    )

        self.assertFalse((self.workspace / "data" / "runs" / "new-run.jsonl").exists())
        self.assertEqual(
            (self.workspace / "data" / "projects.json").read_text(
                encoding="utf-8"
            ),
            '{"version": 1}',
        )

    def test_gitignore_covers_every_internal_state_path(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        patterns = {
            line.strip()
            for line in (project_root / ".gitignore").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        expected_patterns = {
            path.as_posix() if path.suffix else f"{path.as_posix()}/"
            for path in DEFAULT_BLOCKED_PREFIXES
        }

        self.assertTrue(expected_patterns.issubset(patterns))
        self.assertNotIn("data/", patterns)

    def test_symlink_cannot_escape_workspace(self) -> None:
        link = self.workspace / "outside-link.txt"
        try:
            link.symlink_to(self.outside_file)
        except (OSError, NotImplementedError):
            self.skipTest("当前 Windows 配置不允许创建符号链接")

        with self.assertRaises(ToolExecutionError):
            self.read_tool.execute({"path": "outside-link.txt"})

        listing = json.loads(self.list_tool.execute({"path": "."}))
        self.assertNotIn(
            "outside-link.txt",
            [entry["name"] for entry in listing["entries"]],
        )

    def test_non_utf8_and_oversized_files_are_rejected(self) -> None:
        (self.workspace / "binary.bin").write_bytes(b"\xff\xfe")
        (self.workspace / "large.txt").write_text("x" * 101, encoding="utf-8")

        with self.assertRaises(ToolExecutionError):
            self.read_tool.execute({"path": "binary.bin"})
        with self.assertRaises(ToolExecutionError):
            self.read_tool.execute({"path": "large.txt"})

    def test_write_text_file_creates_and_overwrites_atomically(self) -> None:
        created = json.loads(
            self.write_tool.execute(
                {"path": "src/new.txt", "content": "第一版"}
            )
        )
        overwritten = json.loads(
            self.write_tool.execute(
                {"path": "src/new.txt", "content": "第二版"}
            )
        )

        self.assertTrue(created["created"])
        self.assertFalse(overwritten["created"])
        self.assertEqual(created["bytes"], len("第一版".encode("utf-8")))
        self.assertEqual(
            (self.workspace / "src" / "new.txt").read_text(encoding="utf-8"),
            "第二版",
        )
        self.assertEqual(list((self.workspace / "src").glob("*.tmp")), [])

    def test_write_rejects_missing_parent_and_oversized_content(self) -> None:
        with self.assertRaises(ToolExecutionError):
            self.write_tool.execute(
                {"path": "missing/new.txt", "content": "content"}
            )
        with self.assertRaises(ToolExecutionError):
            self.write_tool.execute(
                {"path": "src/large.txt", "content": "中" * 34}
            )

    def test_default_registry_contains_workspace_tools(self) -> None:
        registry = build_default_registry(self.workspace, max_file_size=100)

        self.assertEqual(
            registry.names,
            (
                "calculator",
                "get_current_time",
                "list_directory",
                "read_text_file",
                "search_text",
                "replace_text",
                "write_text_file",
            ),
        )
        self.assertFalse(registry.get("list_directory").requires_confirmation)
        self.assertFalse(registry.get("read_text_file").requires_confirmation)
        self.assertFalse(registry.get("search_text").requires_confirmation)
        self.assertTrue(registry.get("replace_text").requires_confirmation)
        self.assertTrue(registry.get("write_text_file").requires_confirmation)


if __name__ == "__main__":
    unittest.main()
