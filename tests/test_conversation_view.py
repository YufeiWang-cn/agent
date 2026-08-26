"""验证会话视图的渲染范围、搜索和布局行为。"""

import tkinter as tk
import unittest
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from _ui_test_support import FakeUiAgent
from deepseek_agent.config import Settings
from deepseek_agent.planning import TaskPlan
from deepseek_agent.ui.conversation_view import (
    ConversationView,
    _messages_after_last_user,
)
from deepseek_agent.ui.tk_app import AgentApp


class ConversationViewTests(unittest.TestCase):
    def test_latest_turn_rendering_ignores_older_history(self) -> None:
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
            {"role": "tool", "content": "new tool result"},
        ]

        self.assertEqual(
            _messages_after_last_user(messages),
            messages[-2:],
        )

    def test_conversation_search_highlights_all_matches(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            view = ConversationView(
                root,
                on_prompt_suggestion=lambda _prompt: None,
                focus_composer=lambda: None,
            )
            view.pack(fill="both", expand=True)
            view.append("Alpha beta alpha", "assistant_body")
            view.open_search()
            view._search_var.set("alpha")

            view._refresh_search_matches()

            self.assertEqual(len(view._search_matches), 2)
            self.assertEqual(view._search_status.get(), "1 / 2")
            view.search_next()
            self.assertEqual(view._search_status.get(), "2 / 2")
        finally:
            root.destroy()

    def test_completed_turn_replaces_only_the_live_response_region(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            view = ConversationView(
                root,
                on_prompt_suggestion=lambda _prompt: None,
                focus_composer=lambda: None,
            )
            view.pack(fill="both", expand=True)
            view.append("保留的旧消息\n", "assistant_body")
            view.begin_live_response()
            view.append("流式原始内容", "assistant_body")

            self.assertTrue(
                view.render_completed_turn(
                    [
                        {"role": "user", "content": "当前问题"},
                        {"role": "assistant", "content": "**最终答案**"},
                    ]
                )
            )

            rendered = view.content()
            self.assertIn("保留的旧消息", rendered)
            self.assertIn("最终答案", rendered)
            self.assertNotIn("流式原始内容", rendered)
            assistant_range = view._chat_view.tag_nextrange(
                "assistant_header",
                "1.0",
                "end",
            )
            self.assertTrue(assistant_range)
            self.assertEqual(str(assistant_range[0]).split(".")[0], "2")
            self.assertNotIn(
                view._live_response_mark,
                view._chat_view.mark_names(),
            )
        finally:
            root.destroy()

    def test_empty_state_suggestion_notifies_the_composer(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            prompts: list[str] = []
            view = ConversationView(
                root,
                on_prompt_suggestion=prompts.append,
                focus_composer=lambda: None,
            )
            view.pack(fill="both", expand=True)

            view._show_empty_state()
            view._use_prompt_suggestion("请分析项目结构。")

            self.assertIsNotNone(view._empty_state_frame)
            self.assertEqual(prompts, ["请分析项目结构。"])
        finally:
            root.destroy()

    def test_prompt_suggestion_populates_the_composer(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            app = AgentApp.__new__(AgentApp)
            app._busy = False
            app._input = tk.Text(root, width=40, height=2)

            app._use_prompt_suggestion("请分析项目结构。")

            self.assertEqual(
                app._input.get("1.0", "end-1c"),
                "请分析项目结构。",
            )
        finally:
            root.destroy()

    def test_sending_next_turn_keeps_current_plan_visible(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        fake_agent = FakeUiAgent()
        fake_agent.current_plan = TaskPlan.create(
            [
                {"step": "收集基础数据", "status": "pending"},
                {"step": "制定训练安排", "status": "pending"},
            ],
            kind="proposal",
        )
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="fake-model",
            system_prompt="system",
        )
        try:
            with patch(
                "deepseek_agent.ui.tk_app.Agent",
                return_value=fake_agent,
            ):
                app = AgentApp(root, settings)
            app._input.insert("1.0", "从第一步开始")

            with patch("deepseek_agent.ui.tk_app.threading.Thread") as thread:
                app._send()

            self.assertEqual(
                app._conversation._plan_card.winfo_manager(),
                "grid",
            )
            thread.return_value.start.assert_called_once_with()
            app._conversation.show_plan(None)
            with patch.object(
                app._conversation,
                "render_completed_turn",
                return_value=True,
            ):
                self.assertTrue(app._render_completed_turn())
            self.assertEqual(
                app._conversation._plan_card.winfo_manager(),
                "grid",
            )
            app._finish_turn("就绪")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
