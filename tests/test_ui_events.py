"""验证界面事件的合并、分发和生命周期处理。"""

import queue
import tkinter as tk
import unittest
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from _ui_test_support import FakeUiAgent
from deepseek_agent.config import Settings
from deepseek_agent.memory import Session
from deepseek_agent.ui.sidebar import _filter_sessions_by_title
from deepseek_agent.ui.tk_app import (
    AgentApp,
    _coalesce_ui_events,
    _dequeue_ui_events,
    _format_activity_status,
)


class UiEventTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
