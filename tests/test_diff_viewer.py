"""验证统一差异解析、专用查看器和确认窗口全屏行为。"""

import json
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.tools import Tool
from deepseek_agent.tools.base import JsonObject
from deepseek_agent.ui.confirmation import (
    ConfirmationRequest,
    ToolConfirmationDialog,
)
from deepseek_agent.ui.diff_viewer import DiffViewer, parse_unified_diff


SAMPLE_DIFF = """--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
-old
+new
 unchanged
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1 @@
+created
"""


class ConfirmationTool(Tool):
    """提供确认窗口测试所需的最小工具定义。"""

    name = "apply_patch"
    description = "测试补丁确认窗口。"
    parameters: JsonObject = {"type": "object", "properties": {}}

    def execute(self, arguments: JsonObject) -> str:
        return "ok"


class DiffParserTests(unittest.TestCase):
    def test_parser_tracks_files_changes_and_line_numbers(self) -> None:
        parsed = parse_unified_diff(SAMPLE_DIFF)

        self.assertEqual(parsed.files, 2)
        self.assertEqual(parsed.additions, 2)
        self.assertEqual(parsed.deletions, 1)
        deleted = next(row for row in parsed.rows if row.kind == "deleted")
        added_rows = [row for row in parsed.rows if row.kind == "added"]
        context = next(row for row in parsed.rows if row.kind == "context")
        self.assertEqual((deleted.old_line, deleted.new_line), (1, None))
        self.assertEqual(
            [(row.old_line, row.new_line) for row in added_rows],
            [(None, 1), (None, 1)],
        )
        self.assertEqual((context.old_line, context.new_line), (2, 2))


class DiffViewerTests(unittest.TestCase):
    def _create_root(self) -> tk.Tk:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        return root

    def test_viewer_renders_semantic_colors_and_line_gutter(self) -> None:
        root = self._create_root()
        try:
            viewer = DiffViewer(root, SAMPLE_DIFF)
            viewer.pack(fill="both", expand=True)
            root.update_idletasks()

            self.assertEqual(viewer.content, SAMPLE_DIFF)
            self.assertEqual(viewer.language, "diff")
            self.assertEqual(viewer.parsed.files, 2)
            self.assertTrue(viewer.text.tag_ranges("added"))
            self.assertTrue(viewer.text.tag_ranges("deleted"))
            self.assertTrue(viewer.text.tag_ranges("hunk"))
            self.assertEqual(
                viewer.text.tag_cget("added", "background"),
                "#E6FFEC",
            )
            self.assertIn("1", viewer.gutter.get("1.0", "end"))
        finally:
            root.destroy()

    def test_confirmation_selects_diff_and_can_toggle_fullscreen(self) -> None:
        root = self._create_root()
        request = ConfirmationRequest(
            tool=ConfirmationTool(),
            arguments=json.dumps({"changes": [], "diff": SAMPLE_DIFF}),
            completed=threading.Event(),
        )
        dialog = None
        try:
            with patch.object(tk.Toplevel, "wait_window", return_value=None):
                dialog = ToolConfirmationDialog(root, request)

            self.assertEqual(
                dialog._arguments_view._selected_key,
                "preview_diff",
            )
            self.assertFalse(dialog._fullscreen)
            dialog._toggle_fullscreen()
            self.assertTrue(dialog._fullscreen)
            self.assertIn("退出全屏", dialog._fullscreen_button.cget("text"))

            dialog._handle_escape(None)
            self.assertFalse(dialog._fullscreen)
            self.assertTrue(dialog._window.winfo_exists())
        finally:
            if dialog is not None and dialog._window.winfo_exists():
                dialog._window.destroy()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
