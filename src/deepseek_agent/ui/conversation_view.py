"""负责对话渲染、搜索、轮次导航和嵌入式内容卡片。"""

import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable

from ..planning import TaskPlan
from .cards import (
    ConversationRail,
    PlanCard,
    ToolCallCard,
)
from .conversation_renderer import ConversationRenderer
from .conversation_search import ConversationSearchController
from .conversation_styles import configure_conversation_tags
from .theme import (
    ACCENT,
    CARD_BACKGROUND,
    EDITOR_BORDER,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    USER_COLOR,
)
from .turn_navigation import TurnNavigationController


def _messages_after_last_user(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """仅返回属于最新一轮的模型消息和工具消息。"""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return messages[index + 1 :]
    return messages


class ConversationView(ttk.Frame):
    """渲染和导航对话，但不负责执行 Agent。"""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_prompt_suggestion: Callable[[str], None],
        focus_composer: Callable[[], None],
    ) -> None:
        super().__init__(parent, style="App.TFrame")
        self._root = self.winfo_toplevel()
        self._on_prompt_suggestion = on_prompt_suggestion
        self._focus_composer = focus_composer
        self._tool_cards: dict[str, ToolCallCard] = {}
        self._tool_card_widgets: list[ToolCallCard] = []
        self._resize_job: str | None = None
        self._pending_chat_width = 0
        self._rendering_history = False
        self._live_response_mark = "live_response_start"
        self._live_tool_start: int | None = None
        self._empty_state_frame: tk.Frame | None = None
        self._disposed = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._plan_card = PlanCard(self)
        self._plan_card.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        self._plan_card.grid_remove()
        self._build_chat(self)

    @property
    def selected_turn_index(self) -> int | None:
        return self._turn_navigation.selected

    @property
    def turn_count(self) -> int:
        return self._turn_navigation.count

    @property
    def search_open(self) -> bool:
        return self._search.is_open

    def content(self) -> str:
        return self._chat_view.get("1.0", "end-1c")

    def open_search(self, event: tk.Event | None = None) -> str:
        return self._open_search(event)

    def close_search(self, event: tk.Event | None = None) -> str:
        return self._close_search(event)

    def search_next(self, event: tk.Event | None = None) -> str:
        return self._search_next(event)

    def search_previous(self, event: tk.Event | None = None) -> str:
        return self._search_previous(event)

    def scroll_to_bottom(self) -> None:
        self._scroll_to_bottom()

    def previous_turn(self) -> None:
        self._turn_navigation.previous()

    def next_turn(self) -> None:
        self._turn_navigation.next()

    def jump_to_turn(self, turn_number: int) -> None:
        self._jump_to_turn(turn_number)

    def is_at_bottom(self) -> bool:
        return self._is_chat_at_bottom()

    def remove_empty_state(self) -> None:
        self._remove_empty_state()

    def append(self, content: str, tag: str) -> None:
        self._append(content, tag)

    def append_message(self, author: str, content: str, role: str) -> None:
        self._append_message(author, content, role)

    def append_turn_answer_preview(
        self,
        content: str,
        *,
        starts_new_message: bool,
    ) -> None:
        self._append_turn_answer_preview(
            content,
            starts_new_message=starts_new_message,
        )

    def register_turn(self, question: str, *, select: bool) -> None:
        self._register_turn(question, select=select)

    def begin_live_response(self) -> None:
        self._begin_live_response()

    def create_tool_card(
        self,
        tool_call_id: str,
        name: str,
        arguments: str,
    ) -> ToolCallCard:
        return self._create_tool_card(tool_call_id, name, arguments)

    def complete_tool_card(
        self,
        tool_call_id: str,
        name: str,
        arguments: str,
        result: str,
    ) -> None:
        self._complete_tool_card(tool_call_id, name, arguments, result)

    def show_plan(self, plan: TaskPlan | None) -> None:
        """显示当前计划快照，传入空值时隐藏计划区域。"""
        if plan is None:
            self._plan_card.grid_remove()
            return
        self._plan_card.update_plan(plan)
        self._plan_card.grid()

    def render_completed_turn(self, messages: list[dict[str, Any]]) -> bool:
        return self._render_completed_turn(messages)

    def render_history(self, messages: list[dict[str, Any]]) -> None:
        self._render_history(messages)

    def cancel_pending_callbacks(self) -> None:
        self._search.cancel_pending_refresh()
        self._turn_navigation.cancel_pending_sync()
        if self._resize_job is not None:
            try:
                self._root.after_cancel(self._resize_job)
            except tk.TclError:
                pass
            self._resize_job = None

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self.cancel_pending_callbacks()
        self._clear_tool_cards()

    def destroy(self) -> None:
        self.dispose()
        super().destroy()

    def _build_chat(self, main: ttk.Frame) -> None:
        card = ttk.Frame(main, style="Card.TFrame", padding=1)
        self._chat_card = card
        card.grid(row=1, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)
        self._chat_view = ScrolledText(
            card,
            wrap="word",
            state="disabled",
            width=1,
            height=1,
            font=("Microsoft YaHei UI", 11),
            background=CARD_BACKGROUND,
            foreground=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            padx=44,
            pady=18,
            spacing1=2,
            spacing3=7,
        )
        self._chat_view.grid(row=0, column=0, sticky="nsew")
        self._renderer = ConversationRenderer(
            self._chat_view,
            is_at_bottom=self._is_chat_at_bottom,
            selected_turn=lambda: self._turn_navigation.selected,
            rendering_history=lambda: self._rendering_history,
            on_layout_changed=self._resize_tool_cards,
        )
        self._turn_rail = ConversationRail(
            card,
            self._chat_view,
            lambda turn_number: self._turn_navigation.jump(turn_number),
        )
        self._bottom_button = tk.Button(
            card,
            text="↓  回到底部",
            command=self._scroll_to_bottom,
            background="#FFFFFF",
            activebackground="#EEF2F7",
            foreground=TEXT_PRIMARY,
            activeforeground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 9, "bold"),
            relief="flat",
            borderwidth=0,
            highlightbackground=EDITOR_BORDER,
            highlightcolor=EDITOR_BORDER,
            highlightthickness=1,
            cursor="hand2",
            padx=11,
            pady=5,
        )
        self._bottom_button.bind(
            "<Enter>",
            lambda _event: self._bottom_button.configure(background="#F8FAFC"),
        )
        self._bottom_button.bind(
            "<Leave>",
            lambda _event: self._bottom_button.configure(background="#FFFFFF"),
        )
        self._turn_navigation = TurnNavigationController(
            self._root,
            self._chat_view,
            self._turn_rail,
            self._bottom_button,
            self._focus_composer,
            lambda: self._renderer.user_cards,
        )
        self._chat_view.configure(yscrollcommand=self._on_chat_yview)
        configure_conversation_tags(self._chat_view)
        self._chat_view.bind("<Configure>", self._resize_tool_cards)
        self._search = ConversationSearchController(
            self._root,
            card,
            self._chat_view,
            self._focus_composer,
        )

    # 下列兼容属性保留既有测试和内部扩展入口，实际状态由搜索控制器维护。
    @property
    def _search_var(self) -> tk.StringVar:
        return self._search.query

    @property
    def _search_status(self) -> tk.StringVar:
        return self._search.status

    @property
    def _search_matches(self) -> list[tuple[str, str]]:
        return self._search.matches

    @property
    def _search_job(self) -> str | None:
        return self._search.job

    @property
    def _search_panel(self) -> tk.Frame:
        return self._search.panel

    def _open_search(self, event: tk.Event | None = None) -> str:
        return self._search.open(event)

    def _close_search(self, event: tk.Event | None = None) -> str:
        return self._search.close(event)

    def _schedule_search_refresh(self, event: tk.Event | None = None) -> None:
        self._search.schedule_refresh(event)

    def _refresh_search_matches(self) -> None:
        self._search.refresh()

    def _search_next(self, event: tk.Event | None = None) -> str:
        return self._search.next(event)

    def _search_previous(self, event: tk.Event | None = None) -> str:
        return self._search.previous(event)

    def _begin_live_response(self) -> None:
        self._clear_live_response_tracking()
        self._chat_view.mark_set(self._live_response_mark, "end-1c")
        self._chat_view.mark_gravity(self._live_response_mark, "left")
        self._live_tool_start = len(self._tool_card_widgets)

    def _append(self, content: str, tag: str) -> None:
        self._renderer.append(content, tag)

    def _append_message(self, author: str, content: str, role: str) -> None:
        self._renderer.append_message(author, content, role)

    def _append_turn_answer_preview(
        self,
        content: str,
        *,
        starts_new_message: bool = False,
    ) -> None:
        self._turn_navigation.append_answer(
            content,
            starts_new_message=starts_new_message,
            rendering_history=self._rendering_history,
        )

    def _append_markdown_message(
        self,
        author: str,
        content: str,
        role: str,
    ) -> None:
        self._renderer.append_markdown_message(author, content, role)

    def _register_turn(self, question: str, *, select: bool) -> None:
        self._turn_navigation.register(
            question,
            select=select,
            rendering_history=self._rendering_history,
        )

    def _reset_turn_navigation(self) -> None:
        self._turn_navigation.reset()

    def _refresh_turn_navigation(
        self,
        selected_turn: int | None = None,
    ) -> None:
        self._turn_navigation.refresh(selected_turn)

    def _on_chat_yview(self, first: str, last: str) -> None:
        self._turn_navigation.on_yview(first, last)

    def _is_chat_at_bottom(self) -> bool:
        return self._turn_navigation.is_at_bottom()

    def _scroll_to_bottom(self) -> None:
        self._turn_navigation.scroll_to_bottom()

    def _jump_to_turn(self, turn_number: int) -> None:
        self._turn_navigation.jump(turn_number)

    def _create_tool_card(
        self,
        tool_call_id: str,
        name: str,
        arguments: str,
    ) -> ToolCallCard:
        was_at_bottom = (
            not self._rendering_history and self._is_chat_at_bottom()
        )
        card = ToolCallCard(self._chat_view, name, arguments)
        self._chat_view.configure(state="normal")
        self._chat_view.window_create(
            "end",
            window=card.frame,
            padx=8,
            pady=6,
        )
        self._chat_view.insert("end", "\n")
        self._chat_view.configure(state="disabled")
        self._tool_cards[tool_call_id] = card
        self._tool_card_widgets.append(card)
        if not self._rendering_history:
            self._resize_tool_cards()
        if was_at_bottom:
            self._chat_view.see("end")
        return card

    def _complete_tool_card(
        self,
        tool_call_id: str,
        name: str,
        arguments: str,
        result: str,
    ) -> None:
        card = self._tool_cards.get(tool_call_id)
        if card is None:
            card = self._create_tool_card(tool_call_id, name, arguments)
        card.set_result(result)

    def _resize_tool_cards(self, event: tk.Event | None = None) -> None:
        if event is not None:
            self._pending_chat_width = event.width
            if self._resize_job is not None:
                self._root.after_cancel(self._resize_job)
            self._resize_job = self._root.after(40, self._flush_pending_resize)
            return
        self._apply_embedded_width(self._chat_view.winfo_width())

    def _flush_pending_resize(self) -> None:
        self._resize_job = None
        self._apply_embedded_width(
            self._pending_chat_width or self._chat_view.winfo_width()
        )

    def _apply_embedded_width(self, width: int) -> None:
        card_width = max(260, width - 70)
        for card in self._tool_card_widgets:
            card.set_width(card_width)
        self._renderer.resize(width)

    def _clear_tool_cards(self) -> None:
        if self._empty_state_frame is not None:
            self._empty_state_frame.destroy()
            self._empty_state_frame = None
        for card in self._tool_card_widgets:
            card.destroy()
        self._tool_cards.clear()
        self._tool_card_widgets.clear()
        self._renderer.clear()

    def _show_empty_state(self) -> None:
        if self._empty_state_frame is not None:
            return
        frame = tk.Frame(
            self._chat_view,
            background="#F8FAFC",
            highlightbackground="#E1E7F0",
            highlightthickness=1,
            borderwidth=0,
        )
        tk.Label(
            frame,
            text="从一个清晰目标开始",
            background="#F8FAFC",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(padx=34, pady=(24, 5))
        tk.Label(
            frame,
            text="描述你想完成的事情，也可以附上文件或项目路径。",
            background="#F8FAFC",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 10),
        ).pack(padx=34, pady=(0, 16))
        actions = tk.Frame(frame, background="#F8FAFC")
        actions.pack(padx=24, pady=(0, 22))
        suggestions = (
            ("分析项目", "请分析当前项目的结构，并指出最值得改进的地方。"),
            ("解释代码", "请解释下面代码的作用，并指出潜在问题：\n"),
            ("编写测试", "请为这个功能补充测试，并说明覆盖的边界情况。"),
        )
        for column, (label, prompt) in enumerate(suggestions):
            tk.Button(
                actions,
                text=label,
                command=lambda value=prompt: self._use_prompt_suggestion(value),
                background="#FFFFFF",
                activebackground="#EEF3FF",
                foreground=USER_COLOR,
                activeforeground=ACCENT,
                relief="flat",
                borderwidth=0,
                highlightbackground=EDITOR_BORDER,
                highlightthickness=1,
                cursor="hand2",
                font=("Microsoft YaHei UI", 10),
                padx=14,
                pady=7,
            ).grid(row=0, column=column, padx=5)

        self._chat_view.configure(state="normal")
        start = self._chat_view.index("end-1c")
        self._chat_view.window_create(
            "end",
            window=frame,
            padx=36,
            pady=52,
            align="center",
        )
        finish = self._chat_view.index("end-1c")
        self._chat_view.tag_add("empty_state_line", start, finish)
        self._chat_view.insert("end", "\n", "empty_state_line")
        self._chat_view.tag_configure("empty_state_line", justify="center")
        self._chat_view.configure(state="disabled")
        self._empty_state_frame = frame

    def _remove_empty_state(self) -> None:
        if self._empty_state_frame is None:
            return
        self._empty_state_frame.destroy()
        self._empty_state_frame = None
        self._chat_view.configure(state="normal")
        self._chat_view.delete("1.0", "end")
        self._chat_view.configure(state="disabled")

    def _use_prompt_suggestion(self, prompt: str) -> None:
        self._on_prompt_suggestion(prompt)

    def _clear_live_response_tracking(self) -> None:
        if self._live_response_mark in self._chat_view.mark_names():
            self._chat_view.mark_unset(self._live_response_mark)
        self._live_tool_start = None

    def _render_completed_turn(self, messages: list[dict[str, Any]]) -> bool:
        has_live_response = self._live_response_mark in self._chat_view.mark_names()
        if self._live_tool_start is None or not has_live_response:
            return False

        tail = _messages_after_last_user(messages)
        if not tail:
            self._clear_live_response_tracking()
            return False

        was_at_bottom = self._is_chat_at_bottom()
        live_cards = self._tool_card_widgets[self._live_tool_start :]
        live_card_ids = {id(card) for card in live_cards}
        for card in live_cards:
            card.destroy()
        del self._tool_card_widgets[self._live_tool_start :]
        self._tool_cards = {
            tool_call_id: card
            for tool_call_id, card in self._tool_cards.items()
            if id(card) not in live_card_ids
        }

        self._chat_view.configure(state="normal")
        # 保留 Text 控件最后一个换行。
        # 删除到 end 会折叠气泡后的分隔行，使助手标题插入错误位置。
        self._chat_view.delete(self._live_response_mark, "end-1c")
        self._chat_view.configure(state="disabled")
        self._rendering_history = True
        try:
            for message in tail:
                role = message.get("role")
                if role == "tool":
                    self._complete_tool_card(
                        str(message.get("tool_call_id", "")),
                        str(message.get("name", "未知工具")),
                        "",
                        str(message.get("content", "")),
                    )
                    continue
                if role == "user":
                    continue
                content = message.get("content")
                if content:
                    self._append_markdown_message(
                        "DeepSeek",
                        str(content),
                        "assistant",
                    )
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {})
                    self._create_tool_card(
                        str(call.get("id", "")),
                        str(function.get("name", "未知工具")),
                        str(function.get("arguments", "")),
                    )
        finally:
            self._rendering_history = False
            self._clear_live_response_tracking()

        self._resize_tool_cards()
        if was_at_bottom:
            self._chat_view.see("end")
        return True

    def _render_history(self, messages: list[dict[str, Any]]) -> None:
        if self._search_panel.place_info():
            self._close_search()
        self._clear_live_response_tracking()
        self._clear_tool_cards()
        self._chat_view.configure(state="normal")
        self._reset_turn_navigation()
        self._chat_view.delete("1.0", "end")
        self._chat_view.configure(state="disabled")
        self._rendering_history = True
        try:
            for message in messages:
                role = message.get("role")
                if role == "user":
                    question = str(message.get("content", ""))
                    self._register_turn(question, select=False)
                    self._append_message("您", question, "user")
                elif role == "tool":
                    self._complete_tool_card(
                        str(message.get("tool_call_id", "")),
                        str(message.get("name", "未知工具")),
                        "",
                        str(message.get("content", "")),
                    )
                else:
                    content = message.get("content")
                    if content:
                        self._append_turn_answer_preview(
                            str(content),
                            starts_new_message=(
                                self._turn_navigation.has_current_answer
                            ),
                        )
                        self._append_markdown_message(
                            "DeepSeek",
                            str(content),
                            "assistant",
                        )
                    for call in message.get("tool_calls") or []:
                        function = call.get("function", {})
                        self._create_tool_card(
                            str(call.get("id", "")),
                            str(function.get("name", "未知工具")),
                            str(function.get("arguments", "")),
                        )
        finally:
            self._rendering_history = False
        if self._turn_navigation.count == 0:
            self._show_empty_state()
        self._resize_tool_cards()
        self._chat_view.see("end")
        self._refresh_turn_navigation()
