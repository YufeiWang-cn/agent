"""验证计划、工具调用和消息卡片的呈现行为。"""

import json
import tkinter as tk
import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.planning import PlanStepStatus, TaskPlan
from deepseek_agent.ui.cards import (
    MarkdownCodeCard,
    PlanCard,
    ToolCallCard,
    UserMessageCard,
)
from deepseek_agent.ui.formatting import _confirmation_content_previews


class UiCardTests(unittest.TestCase):
    def test_plan_card_renders_progress_and_can_collapse(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            plan = TaskPlan.create(
                [
                    {"step": "读取代码", "status": "completed"},
                    {"step": "运行测试", "status": "in_progress"},
                ]
            )
            card = PlanCard(root)
            card.pack(fill="x")

            card.update_plan(plan)
            root.update_idletasks()

            self.assertEqual(card._progress.cget("text"), "1/2 已完成")
            self.assertIn("连续", card._toggle_button.cget("text"))
            self.assertTrue(card._body.winfo_manager())
            card.toggle()
            self.assertFalse(card._body.winfo_manager())

            proposal = TaskPlan.create(
                [
                    {"step": "确定目标", "status": "pending"},
                    {"step": "整理路线", "status": "pending"},
                ],
                kind="proposal",
            )
            card.update_plan(proposal)
            self.assertEqual(card._progress.cget("text"), "共 2 步")
            self.assertIn("规划方案", card._toggle_button.cget("text"))
            single_step = TaskPlan.create(
                [
                    {"step": "读取代码", "status": "completed"},
                    {"step": "运行测试", "status": "pending"},
                ],
                scope="single_step",
            )
            card.update_plan(single_step)
            self.assertIn("单步", card._toggle_button.cget("text"))
            self.assertEqual(
                PlanCard._STATUS_STYLES[PlanStepStatus.WAITING_USER][1],
                "等待输入",
            )
        finally:
            root.destroy()

    def test_confirmation_previews_extract_code_using_path_language(self) -> None:
        previews = _confirmation_content_previews(
            '{"path":"src/app.py","content":"def run():\\n    return 1"}'
        )

        self.assertEqual(
            previews,
            [
                (
                    "preview_content",
                    "内容预览",
                    "def run():\n    return 1",
                    "python",
                )
            ],
        )

    def test_confirmation_previews_ignore_invalid_arguments(self) -> None:
        self.assertEqual(_confirmation_content_previews("not json"), [])

    def test_confirmation_previews_render_patch_diff(self) -> None:
        previews = _confirmation_content_previews(
            json.dumps(
                {
                    "changes": [],
                    "diff": "--- a/app.py\n+++ b/app.py\n-old\n+new\n",
                }
            )
        )

        self.assertEqual(
            previews,
            [
                (
                    "preview_diff",
                    "修改差异",
                    "--- a/app.py\n+++ b/app.py\n-old\n+new\n",
                    "diff",
                )
            ],
        )

    def test_user_bubble_can_finalize_after_batched_layout(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            chat = tk.Text(root, width=80, height=20)
            chat.pack()
            card = UserMessageCard(chat, "您", "一段需要计算尺寸的消息")

            changed = card.set_max_width(560, defer=True)
            root.update_idletasks()
            card.finalize_size()

            self.assertTrue(changed)
            self.assertGreater(card._bubble_width, 0)
            self.assertGreater(card._bubble_height, 0)
        finally:
            root.destroy()

    def test_code_card_height_includes_every_line_padding_and_scrollbar(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            chat = tk.Text(root, width=80, height=20)
            chat.pack()
            content = "第一行\nemoji 👍🏽\n" + "x" * 300 + "\n最后一行"
            card = MarkdownCodeCard(chat, content, "python")
            chat.window_create("end", window=card.frame)
            card.set_width(420)
            root.update_idletasks()
            card._sync_height()
            root.update_idletasks()

            text = card._editor.text
            pixel_count = text.count("1.0", "end", "ypixels")
            self.assertIsNotNone(pixel_count)
            expected = 43 + pixel_count[0] + 2 * int(text.cget("pady"))
            self.assertTrue(card._editor._scroll_x.grid_info())
            if card._editor._scroll_x.grid_info():
                expected += card._editor._scroll_x.winfo_reqheight()
            self.assertEqual(int(card.frame.cget("height")), expected)
        finally:
            root.destroy()

    def test_code_card_destroy_cancels_pending_callbacks(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            chat = tk.Text(root, width=80, height=20)
            chat.pack()
            card = MarkdownCodeCard(chat, "print('hello')", "python")
            height_job = card._height_job
            card._copy()
            copy_job = card._copy_reset_job

            self.assertIsNotNone(height_job)
            self.assertIsNotNone(copy_job)
            self.assertIn(height_job, root.tk.call("after", "info"))
            self.assertIn(copy_job, root.tk.call("after", "info"))

            card.destroy()

            pending = root.tk.call("after", "info")
            self.assertNotIn(height_job, pending)
            self.assertNotIn(copy_job, pending)
            self.assertIsNone(card._height_job)
            self.assertIsNone(card._copy_reset_job)
            self.assertIsNone(card._editor._horizontal_job)
        finally:
            root.destroy()

    def test_tool_card_destroy_closes_fullscreen_viewer_callbacks(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            chat = tk.Text(root, width=80, height=20)
            chat.pack()
            card = ToolCallCard(chat, "demo_tool", "{}")
            card._open_fullscreen()
            viewer = card._viewer
            self.assertIsNotNone(viewer)
            fullscreen_job = viewer._fullscreen_job
            self.assertIsNotNone(fullscreen_job)
            self.assertIn(fullscreen_job, root.tk.call("after", "info"))

            card.destroy()

            self.assertNotIn(fullscreen_job, root.tk.call("after", "info"))
            self.assertFalse(viewer._window.winfo_exists())
            self.assertEqual(card._tabs._items, {})
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
