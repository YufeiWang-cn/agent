"""验证界面格式化、Markdown 渲染和布局计算。"""

import json
import queue
import tkinter as tk
import unittest
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.config import Settings
from deepseek_agent.memory import Session
from deepseek_agent.planning import TaskPlan
from deepseek_agent.ui.cards import (
    MarkdownCodeCard,
    PlanCard,
    ToolCallCard,
    UserMessageCard,
)
from deepseek_agent.ui.conversation_view import (
    ConversationView,
    _messages_after_last_user,
)
from deepseek_agent.ui.formatting import (
    _confirmation_content_previews,
    _content_language_hint,
    _conversation_preview,
    _format_editor_content,
    _split_emoji_spans,
)
from deepseek_agent.ui.markdown import parse_inline, parse_markdown
from deepseek_agent.ui.sidebar import _filter_sessions_by_title
from deepseek_agent.ui.tk_app import (
    AgentApp,
    _coalesce_ui_events,
    _dequeue_ui_events,
    _format_activity_status,
)


class FakeUiAgent:
    model_name = "fake-model"
    available_models = ("fake-model",)

    def __init__(self) -> None:
        self._session = Session.create(
            [{"role": "system", "content": "system"}]
        )
        self.saved = 0

    @property
    def session_id(self) -> str:
        return self._session.id

    @property
    def session_title(self) -> str:
        return self._session.title

    def history(self):
        return []

    def list_sessions(self):
        return [self._session]

    def list_projects(self):
        return []

    def save_session(self) -> None:
        self.saved += 1


class UiFormattingTests(unittest.TestCase):
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
            self.assertTrue(card._body.winfo_manager())
            card.toggle()
            self.assertFalse(card._body.winfo_manager())
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

    def test_adjacent_stream_events_are_coalesced_without_reordering(self) -> None:
        marker = object()
        events = _coalesce_ui_events(
            [
                ("text", "你"),
                ("text", "好"),
                ("tool_call", marker),
                ("text", "结果"),
                ("done", None),
            ]
        )

        self.assertEqual(events[0], ("text", "你好"))
        self.assertIs(events[1][1], marker)
        self.assertEqual(events[2:], [("text", "结果"), ("done", None)])

    def test_ui_event_batches_are_bounded_and_keep_order(self) -> None:
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        for index in range(5):
            events.put(("text", str(index)))

        first_batch = _dequeue_ui_events(events, limit=3)

        self.assertEqual(
            first_batch,
            [("text", "0"), ("text", "1"), ("text", "2")],
        )
        self.assertEqual(
            _dequeue_ui_events(events, limit=3),
            [("text", "3"), ("text", "4")],
        )

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

    def test_activity_status_reports_elapsed_time_and_stream_size(self) -> None:
        self.assertEqual(
            _format_activity_status("正在生成", 1.26, 128),
            "正在生成  ·  1.3s  ·  128 字符",
        )
        self.assertEqual(
            _format_activity_status("等待确认", -1.0, 0),
            "等待确认  ·  0.0s",
        )

    def test_session_filter_is_trimmed_and_case_insensitive(self) -> None:
        sessions = [
            Session.create([], title="Python 性能优化"),
            Session.create([], title="Go 并发设计"),
        ]

        self.assertEqual(
            _filter_sessions_by_title(sessions, "  PYTHON "),
            sessions[:1],
        )
        self.assertEqual(_filter_sessions_by_title(sessions, "不存在"), [])

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

    def test_composer_hides_shortcut_hint_at_narrow_width(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            app = AgentApp.__new__(AgentApp)
            app._shortcut_hint = tk.Label(root, text="快捷键")
            app._shortcut_hint.grid()
            event = tk.Event()
            event.width = 600

            app._update_composer_layout(event)

            self.assertEqual(app._shortcut_hint.winfo_manager(), "")
            event.width = 800
            app._update_composer_layout(event)
            self.assertEqual(app._shortcut_hint.winfo_manager(), "grid")
        finally:
            root.destroy()

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

    def test_cancelled_unsaved_turn_is_removed_and_prompt_is_restored(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        fake_agent = FakeUiAgent()
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
            app._active_prompt = "请重试这个问题"
            app._conversation.register_turn(app._active_prompt, select=True)
            app._conversation.append_message("您", app._active_prompt, "user")
            app._conversation.begin_live_response()
            app._handle_event("text", "部分回答")

            app._handle_event(
                "cancelled",
                ("本轮对话已停止。", False),
            )

            self.assertEqual(app._conversation.turn_count, 0)
            self.assertNotIn(
                "部分回答",
                app._conversation.content(),
            )
            self.assertEqual(
                app._input.get("1.0", "end-1c"),
                "请重试这个问题",
            )
            self.assertEqual(app._status.get(), "已停止，问题已放回输入框")
        finally:
            root.destroy()

    def test_close_waits_for_worker_before_destroying_window(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        fake_agent = FakeUiAgent()
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="fake-model",
            system_prompt="system",
        )

        class WorkerState:
            alive = True

            def is_alive(self) -> bool:
                return self.alive

        worker = WorkerState()
        try:
            with patch(
                "deepseek_agent.ui.tk_app.Agent",
                return_value=fake_agent,
            ):
                app = AgentApp(root, settings)
            app._sidebar._schedule_filter()
            app._conversation._schedule_search_refresh()
            resize_event = tk.Event()
            resize_event.width = 640
            app._conversation._resize_tool_cards(resize_event)
            self.assertIsNotNone(app._drain_job)
            self.assertIsNotNone(app._layout_job)
            self.assertIsNotNone(app._sidebar._filter_job)
            self.assertIsNotNone(app._conversation._search_job)
            self.assertIsNotNone(app._conversation._resize_job)
            app._worker = worker
            app._busy = True
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app._on_close()

            self.assertTrue(app._close_pending)
            self.assertFalse(app._closed)
            self.assertTrue(root.winfo_exists())

            worker.alive = False
            app._busy = False
            app._finalize_close()

            self.assertTrue(app._closed)
            self.assertEqual(fake_agent.saved, 1)
            self.assertIsNone(app._drain_job)
            self.assertIsNone(app._layout_job)
            self.assertIsNone(app._sidebar._filter_job)
            self.assertIsNone(app._conversation._search_job)
            self.assertIsNone(app._conversation._resize_job)
            self.assertTrue(app._conversation._disposed)
        finally:
            if not getattr(app, "_closed", False):
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

    def test_json_is_pretty_printed_for_editor(self) -> None:
        content, language = _format_editor_content(
            '{"path":"src/app.py","enabled":true}'
        )

        self.assertEqual(language, "json")
        self.assertIn('\n  "path": "src/app.py"', content)
        self.assertIn('\n  "enabled": true', content)

    def test_result_language_comes_from_file_extension(self) -> None:
        self.assertEqual(
            _content_language_hint('{"path":"src/app.py"}'),
            "python",
        )
        self.assertEqual(
            _content_language_hint('{"path":"README.md"}'),
            "markdown",
        )

    def test_invalid_arguments_fall_back_to_plain_text(self) -> None:
        self.assertEqual(_content_language_hint("not json"), "text")
        content, language = _format_editor_content("plain text", "text")
        self.assertEqual((content, language), ("plain text", "text"))

    def test_conversation_preview_is_compact_and_descriptive(self) -> None:
        self.assertEqual(
            _conversation_preview("  如何\n切换模型？  "),
            "如何 切换模型？",
        )
        self.assertEqual(_conversation_preview("123456", limit=5), "1234…")

    def test_emoji_sequences_remain_complete_spans(self) -> None:
        spans = _split_emoji_spans("开始 👍🏽 🇨🇳 👨‍👩‍👧‍👦 1️⃣ 结束")

        self.assertEqual(
            [text for text, is_emoji in spans if is_emoji],
            ["👍🏽", "🇨🇳", "👨‍👩‍👧‍👦", "1️⃣"],
        )
        self.assertEqual(
            "".join(text for text, _is_emoji in spans),
            "开始 👍🏽 🇨🇳 👨‍👩‍👧‍👦 1️⃣ 结束",
        )

    def test_markdown_blocks_include_headings_lists_and_code(self) -> None:
        blocks = parse_markdown(
            "# 标题\n\n- 项目\n\n```python\nprint('hello')\n```"
        )

        self.assertEqual(
            [block.kind for block in blocks],
            ["heading", "blank", "list_item", "blank", "code"],
        )
        self.assertEqual(blocks[0].level, 1)
        self.assertEqual(blocks[-1].language, "python")

    def test_markdown_table_keeps_rows_and_inline_pipes(self) -> None:
        blocks = parse_markdown(
            "| 文件 | 行号 | 内容 |\n"
            "| --- | ---: | --- |\n"
            "| src/app.py | 13 | `value = left | right` |\n"
            "| tests/test_app.py | 7 | `assert value` |"
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "table")
        self.assertEqual(blocks[0].headers, ("文件", "行号", "内容"))
        self.assertEqual(
            blocks[0].rows[0],
            ("src/app.py", "13", "`value = left | right`"),
        )

    def test_pure_json_becomes_pretty_code_block(self) -> None:
        blocks = parse_markdown('{"enabled":true,"count":2}')

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "code")
        self.assertEqual(blocks[0].language, "json")
        self.assertIn('\n  "enabled": true', blocks[0].text)

    def test_inline_markdown_removes_delimiters_and_keeps_styles(self) -> None:
        spans = parse_inline("普通 **加粗** `code` *斜体*")

        self.assertEqual(
            [(span.text, span.style) for span in spans],
            [
                ("普通 ", "plain"),
                ("加粗", "bold"),
                (" ", "plain"),
                ("code", "code"),
                (" ", "plain"),
                ("斜体", "italic"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
