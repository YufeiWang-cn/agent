"""负责对话渲染、搜索、轮次导航和嵌入式内容卡片。"""

import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable

from .cards import (
    ConversationRail,
    MarkdownCodeCard,
    MarkdownTableCard,
    ToolCallCard,
    UserMessageCard,
)
from .formatting import _split_emoji_spans
from .markdown import MarkdownBlock, parse_inline, parse_markdown
from .theme import (
    ACCENT,
    ASSISTANT_COLOR,
    CARD_BACKGROUND,
    DANGER,
    EDITOR_BORDER,
    EMOJI_FONT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TOOL_COLOR,
    USER_COLOR,
)


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
        self._markdown_code_widgets: list[MarkdownCodeCard] = []
        self._markdown_table_widgets: list[MarkdownTableCard] = []
        self._user_message_widgets: list[UserMessageCard] = []
        self._turn_marks: list[str] = []
        self._turn_questions: list[str] = []
        self._turn_answers: list[str] = []
        self._markdown_cache: dict[str, tuple[MarkdownBlock, ...]] = {}
        self._selected_turn_index: int | None = None
        self._turn_sync_job: str | None = None
        self._resize_job: str | None = None
        self._pending_chat_width = 0
        self._rendering_history = False
        self._live_response_mark = "live_response_start"
        self._live_tool_start: int | None = None
        self._search_matches: list[tuple[str, str]] = []
        self._search_match_index = -1
        self._search_job: str | None = None
        self._empty_state_frame: tk.Frame | None = None
        self._disposed = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._build_chat(self)

    @property
    def selected_turn_index(self) -> int | None:
        return self._selected_turn_index

    @property
    def turn_count(self) -> int:
        return len(self._turn_marks)

    @property
    def search_open(self) -> bool:
        return bool(self._search_panel.place_info())

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
        if self._turn_marks:
            current = self._selected_turn_index or len(self._turn_marks)
            self._jump_to_turn(max(1, current - 1))

    def next_turn(self) -> None:
        if self._turn_marks:
            current = self._selected_turn_index or 1
            self._jump_to_turn(min(len(self._turn_marks), current + 1))

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

    def render_completed_turn(self, messages: list[dict[str, Any]]) -> bool:
        return self._render_completed_turn(messages)

    def render_history(self, messages: list[dict[str, Any]]) -> None:
        self._render_history(messages)

    def cancel_pending_callbacks(self) -> None:
        for attribute in ("_search_job", "_turn_sync_job", "_resize_job"):
            job = getattr(self, attribute)
            if job is None:
                continue
            try:
                self._root.after_cancel(job)
            except tk.TclError:
                pass
            setattr(self, attribute, None)

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
        card.grid(row=0, column=0, sticky="nsew")
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
        self._turn_rail = ConversationRail(
            card,
            self._chat_view,
            self._jump_to_turn,
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
        self._chat_view.configure(yscrollcommand=self._on_chat_yview)
        self._chat_view.tag_configure(
            "user_header",
            foreground=USER_COLOR,
            font=("Microsoft YaHei UI", 10, "bold"),
            spacing1=7,
        )
        self._chat_view.tag_configure(
            "user_message_line",
            justify="right",
            rmargin=12,
            spacing1=7,
            spacing3=5,
        )
        self._chat_view.tag_configure(
            "user_body",
            foreground=TEXT_PRIMARY,
            lmargin1=12,
            lmargin2=12,
            rmargin=20,
            spacing3=5,
        )
        self._chat_view.tag_configure(
            "assistant_header",
            foreground=ASSISTANT_COLOR,
            font=("Microsoft YaHei UI", 10, "bold"),
            spacing1=7,
        )
        self._chat_view.tag_configure(
            "assistant_body",
            foreground=TEXT_PRIMARY,
            lmargin1=12,
            lmargin2=12,
            rmargin=20,
            spacing3=5,
        )
        self._chat_view.tag_configure(
            "tool_header",
            foreground=TOOL_COLOR,
            font=("Microsoft YaHei UI", 10, "bold"),
            spacing1=8,
        )
        self._chat_view.tag_configure(
            "tool_body",
            foreground="#6B4B25",
            background="#FFF8E8",
            font=("Consolas", 10),
            lmargin1=12,
            lmargin2=12,
            rmargin=20,
            spacing3=8,
        )
        self._chat_view.tag_configure("error", foreground=DANGER, spacing1=8)
        self._chat_view.tag_configure("muted", foreground=TEXT_SECONDARY)
        self._chat_view.tag_configure(
            "md_h1",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 18, "bold"),
            spacing1=10,
            spacing3=5,
        )
        self._chat_view.tag_configure(
            "md_h2",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 16, "bold"),
            spacing1=9,
            spacing3=4,
        )
        self._chat_view.tag_configure(
            "md_h3",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 14, "bold"),
            spacing1=8,
            spacing3=3,
        )
        for level in range(4, 7):
            self._chat_view.tag_configure(
                f"md_h{level}",
                foreground=TEXT_PRIMARY,
                font=("Microsoft YaHei UI", 12, "bold"),
                spacing1=7,
                spacing3=3,
            )
        self._chat_view.tag_configure(
            "md_bold",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self._chat_view.tag_configure(
            "md_italic",
            font=("Microsoft YaHei UI", 11, "italic"),
        )
        self._chat_view.tag_configure(
            "md_inline_code",
            background="#EEF2F7",
            foreground="#9D174D",
            font=("Cascadia Mono", 10),
        )
        self._chat_view.tag_configure(
            "md_link",
            foreground=ACCENT,
            underline=True,
        )
        self._chat_view.tag_configure(
            "md_list",
            lmargin1=30,
            lmargin2=30,
            rmargin=20,
        )
        self._chat_view.tag_configure(
            "md_list_marker",
            foreground=ACCENT,
            font=("Microsoft YaHei UI", 11, "bold"),
            lmargin1=16,
        )
        self._chat_view.tag_configure(
            "md_quote",
            foreground=TEXT_SECONDARY,
            background="#F8FAFC",
            lmargin1=25,
            lmargin2=25,
            rmargin=24,
        )
        self._chat_view.tag_configure(
            "md_rule",
            foreground="#CBD5E1",
            spacing1=7,
            spacing3=7,
        )
        self._chat_view.tag_configure(
            "md_blank",
            font=("Microsoft YaHei UI", 3),
            spacing1=0,
            spacing3=0,
        )
        self._chat_view.tag_configure(
            "md_emoji",
            font=(EMOJI_FONT, 11),
        )
        emoji_heading_sizes = ((1, 18), (2, 16), (3, 14), (4, 12), (5, 12), (6, 12))
        for level, font_size in emoji_heading_sizes:
            self._chat_view.tag_configure(
                f"md_emoji_h{level}",
                font=(EMOJI_FONT, font_size),
            )
        self._chat_view.tag_configure(
            "turn_focus",
            background="#EEF2FF",
        )
        self._chat_view.tag_configure(
            "search_match",
            background="#FFF3A3",
            foreground=TEXT_PRIMARY,
        )
        self._chat_view.tag_configure(
            "search_current",
            background="#F4C44E",
            foreground="#172033",
        )
        self._chat_view.bind("<Configure>", self._resize_tool_cards)
        self._build_search_panel(card)

    def _build_search_panel(self, parent: tk.Misc) -> None:
        self._search_panel = tk.Frame(
            parent,
            background="#FFFFFF",
            highlightbackground=EDITOR_BORDER,
            highlightthickness=1,
            borderwidth=0,
        )
        self._search_var = tk.StringVar()
        self._search_status = tk.StringVar(value="输入关键词")
        self._search_entry = tk.Entry(
            self._search_panel,
            textvariable=self._search_var,
            width=24,
            font=("Microsoft YaHei UI", 10),
            background="#F8FAFC",
            foreground=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
        )
        self._search_entry.pack(side="left", padx=(9, 6), pady=7, ipady=3)
        tk.Label(
            self._search_panel,
            textvariable=self._search_status,
            width=8,
            anchor="center",
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(0, 4))
        for label, command in (
            ("↑", self._search_previous),
            ("↓", self._search_next),
            ("×", self._close_search),
        ):
            tk.Button(
                self._search_panel,
                text=label,
                command=command,
                background="#FFFFFF",
                activebackground="#EEF2F7",
                foreground=TEXT_SECONDARY,
                activeforeground=TEXT_PRIMARY,
                relief="flat",
                borderwidth=0,
                cursor="hand2",
                font=("Microsoft YaHei UI", 10, "bold"),
                padx=7,
                pady=4,
            ).pack(side="left", padx=(0, 2), pady=4)
        self._search_entry.bind("<KeyRelease>", self._schedule_search_refresh)
        self._search_entry.bind("<Return>", self._search_next)
        self._search_entry.bind("<Shift-Return>", self._search_previous)
        self._search_entry.bind("<Escape>", self._close_search)

    def _open_search(self, _event: tk.Event | None = None) -> str:
        if not self._search_panel.place_info():
            self._search_panel.place(
                relx=1.0,
                x=-24,
                y=12,
                anchor="ne",
            )
            self._search_panel.lift()
            self._refresh_search_matches()
        self._search_entry.focus_set()
        self._search_entry.selection_range(0, "end")
        return "break"

    def _close_search(self, _event: tk.Event | None = None) -> str:
        if self._search_job is not None:
            self._root.after_cancel(self._search_job)
            self._search_job = None
        self._chat_view.tag_remove("search_match", "1.0", "end")
        self._chat_view.tag_remove("search_current", "1.0", "end")
        self._search_matches.clear()
        self._search_match_index = -1
        self._search_panel.place_forget()
        self._focus_composer()
        return "break"

    def _schedule_search_refresh(self, _event: tk.Event | None = None) -> None:
        if _event is not None and _event.keysym in {
            "Return",
            "Escape",
            "F3",
            "Shift_L",
            "Shift_R",
            "Up",
            "Down",
        }:
            return
        if self._search_job is not None:
            self._root.after_cancel(self._search_job)
        self._search_job = self._root.after(120, self._refresh_search_matches)

    def _refresh_search_matches(self) -> None:
        self._search_job = None
        self._chat_view.tag_remove("search_match", "1.0", "end")
        self._chat_view.tag_remove("search_current", "1.0", "end")
        self._search_matches.clear()
        self._search_match_index = -1
        query = self._search_var.get().strip()
        if not query:
            self._search_status.set("输入关键词")
            return

        count = tk.IntVar(master=self._root)
        position = "1.0"
        while len(self._search_matches) < 500:
            start = self._chat_view.search(
                query,
                position,
                stopindex="end",
                nocase=True,
                count=count,
            )
            if not start or count.get() <= 0:
                break
            finish = f"{start}+{count.get()}c"
            self._search_matches.append((start, finish))
            self._chat_view.tag_add("search_match", start, finish)
            position = finish

        if not self._search_matches:
            self._search_status.set("无结果")
            return
        self._focus_search_match(0)

    def _focus_search_match(self, index: int) -> None:
        if not self._search_matches:
            return
        self._search_match_index = index % len(self._search_matches)
        start, finish = self._search_matches[self._search_match_index]
        self._chat_view.tag_remove("search_current", "1.0", "end")
        self._chat_view.tag_add("search_current", start, finish)
        self._chat_view.tag_raise("search_current")
        self._chat_view.see(start)
        self._search_status.set(
            f"{self._search_match_index + 1} / {len(self._search_matches)}"
        )

    def _search_next(self, _event: tk.Event | None = None) -> str:
        if not self._search_panel.place_info():
            return self._open_search()
        if self._search_job is not None:
            self._root.after_cancel(self._search_job)
            self._refresh_search_matches()
        if self._search_matches:
            self._focus_search_match(self._search_match_index + 1)
        return "break"

    def _search_previous(self, _event: tk.Event | None = None) -> str:
        if not self._search_panel.place_info():
            return self._open_search()
        if self._search_job is not None:
            self._root.after_cancel(self._search_job)
            self._refresh_search_matches()
        if self._search_matches:
            self._focus_search_match(self._search_match_index - 1)
        return "break"

    def _begin_live_response(self) -> None:
        self._clear_live_response_tracking()
        self._chat_view.mark_set(self._live_response_mark, "end-1c")
        self._chat_view.mark_gravity(self._live_response_mark, "left")
        self._live_tool_start = len(self._tool_card_widgets)

    def _append(self, content: str, tag: str) -> None:
        was_at_bottom = self._is_chat_at_bottom()
        self._chat_view.configure(state="normal")
        if tag == "assistant_body":
            self._insert_chat_text(content, (tag,))
        else:
            self._chat_view.insert("end", content, tag)
        self._chat_view.configure(state="disabled")
        if was_at_bottom:
            self._chat_view.see("end")

    def _append_message(self, author: str, content: str, role: str) -> None:
        if role == "user":
            self._append_user_message(author, content)
            return
        self._append(f"{author}\n", f"{role}_header")
        self._append(f"{content}\n", f"{role}_body")

    def _append_turn_answer_preview(
        self,
        content: str,
        *,
        starts_new_message: bool = False,
    ) -> None:
        if not self._turn_answers or not content:
            return
        existing = self._turn_answers[-1]
        separator = "  " if starts_new_message and existing else ""
        self._turn_answers[-1] = (existing + separator + content)[:500]
        if not self._rendering_history:
            self._turn_rail.update_answer(
                len(self._turn_answers),
                self._turn_answers[-1],
            )

    def _append_user_message(self, author: str, content: str) -> None:
        self._chat_view.configure(state="normal")
        card = UserMessageCard(self._chat_view, author, content)
        start = self._chat_view.index("end-1c")
        self._chat_view.window_create("end", window=card.frame, padx=8, pady=4)
        finish = self._chat_view.index("end-1c")
        self._chat_view.tag_add("user_message_line", start, finish)
        self._chat_view.insert("end", "\n", "user_message_line")
        self._chat_view.configure(state="disabled")
        self._user_message_widgets.append(card)
        card.set_active(len(self._user_message_widgets) == self._selected_turn_index)
        if not self._rendering_history:
            self._resize_tool_cards()
            self._chat_view.see("end")

    def _append_markdown_message(
        self,
        author: str,
        content: str,
        role: str,
    ) -> None:
        self._chat_view.configure(state="normal")
        self._chat_view.insert("end", f"{author}\n", f"{role}_header")
        body_tag = f"{role}_body"
        for block in self._cached_markdown_blocks(content):
            self._insert_markdown_block(block, body_tag)
        self._chat_view.configure(state="disabled")
        if not self._rendering_history:
            self._chat_view.see("end")

    def _cached_markdown_blocks(self, content: str) -> tuple[MarkdownBlock, ...]:
        cached = self._markdown_cache.get(content)
        if cached is not None:
            return cached
        blocks = tuple(parse_markdown(content))
        if len(self._markdown_cache) >= 256:
            self._markdown_cache.pop(next(iter(self._markdown_cache)))
        self._markdown_cache[content] = blocks
        return blocks

    def _insert_markdown_block(
        self,
        block: MarkdownBlock,
        body_tag: str,
    ) -> None:
        if block.kind == "blank":
            self._chat_view.insert("end", "\n", (body_tag, "md_blank"))
            return
        if block.kind == "heading":
            heading_tag = f"md_h{min(6, max(1, block.level))}"
            self._insert_inline_markdown(
                block.text,
                (body_tag, heading_tag),
                allow_font_styles=False,
            )
            self._chat_view.insert("end", "\n", (body_tag, heading_tag))
            return
        if block.kind == "rule":
            self._chat_view.insert("end", "─" * 52 + "\n", "md_rule")
            return
        if block.kind == "quote":
            self._insert_inline_markdown(block.text, (body_tag, "md_quote"))
            self._chat_view.insert("end", "\n", (body_tag, "md_quote"))
            return
        if block.kind == "list_item":
            self._chat_view.insert(
                "end",
                f"{block.marker} ",
                (body_tag, "md_list_marker"),
            )
            self._insert_inline_markdown(block.text, (body_tag, "md_list"))
            self._chat_view.insert("end", "\n", (body_tag, "md_list"))
            return
        if block.kind == "code":
            self._insert_markdown_code(block.text, block.language)
            return
        if block.kind == "table":
            self._insert_markdown_table(block.headers, block.rows)
            return
        self._insert_inline_markdown(block.text, (body_tag,))
        self._chat_view.insert("end", "\n", body_tag)

    def _insert_inline_markdown(
        self,
        content: str,
        base_tags: tuple[str, ...],
        *,
        allow_font_styles: bool = True,
    ) -> None:
        style_tags = {
            "bold": "md_bold",
            "italic": "md_italic",
            "code": "md_inline_code",
            "link": "md_link",
        }
        for span in parse_inline(content):
            tags = base_tags
            style_tag = style_tags.get(span.style)
            if style_tag is not None and (
                allow_font_styles or span.style in {"code", "link"}
            ):
                tags = (*base_tags, style_tag)
            self._insert_chat_text(span.text, tags)

    def _insert_chat_text(
        self,
        content: str,
        tags: tuple[str, ...],
    ) -> None:
        emoji_tag = "md_emoji"
        for level in range(1, 7):
            if f"md_h{level}" in tags:
                emoji_tag = f"md_emoji_h{level}"
                break
        for segment, is_emoji in _split_emoji_spans(content):
            segment_tags = (*tags, emoji_tag) if is_emoji else tags
            self._chat_view.insert("end", segment, segment_tags)

    def _insert_markdown_code(self, content: str, language: str) -> None:
        card = MarkdownCodeCard(self._chat_view, content, language)
        self._chat_view.window_create(
            "end",
            window=card.frame,
            padx=8,
            pady=6,
        )
        self._chat_view.insert("end", "\n")
        self._markdown_code_widgets.append(card)
        if not self._rendering_history:
            self._resize_tool_cards()

    def _insert_markdown_table(
        self,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
    ) -> None:
        card = MarkdownTableCard(self._chat_view, headers, rows)
        self._chat_view.window_create(
            "end",
            window=card.frame,
            padx=8,
            pady=6,
        )
        self._chat_view.insert("end", "\n")
        self._markdown_table_widgets.append(card)
        if not self._rendering_history:
            self._resize_tool_cards()

    def _register_turn(self, question: str, *, select: bool) -> None:
        turn_number = len(self._turn_marks) + 1
        mark_name = f"conversation_turn_{turn_number}"
        self._chat_view.mark_set(mark_name, "end-1c")
        self._chat_view.mark_gravity(mark_name, "left")
        self._turn_marks.append(mark_name)
        self._turn_questions.append(question)
        self._turn_answers.append("")
        if not self._rendering_history:
            self._refresh_turn_navigation(
                turn_number if select else self._selected_turn_index
            )

    def _reset_turn_navigation(self) -> None:
        if self._turn_marks:
            self._chat_view.mark_unset(*self._turn_marks)
        self._chat_view.tag_remove("turn_focus", "1.0", "end")
        self._turn_marks.clear()
        self._turn_questions.clear()
        self._turn_answers.clear()
        self._selected_turn_index = None
        self._refresh_turn_navigation()

    def _refresh_turn_navigation(
        self,
        selected_turn: int | None = None,
    ) -> None:
        total = len(self._turn_marks)
        if total == 0:
            self._selected_turn_index = None
            self._turn_rail.set_turns([], [], None)
            return

        if selected_turn is None:
            selected_turn = self._selected_turn_index or total
        selected_turn = min(total, max(1, selected_turn))
        self._selected_turn_index = selected_turn
        self._turn_rail.set_turns(
            self._turn_questions,
            self._turn_answers,
            selected_turn,
        )
        for index, card in enumerate(self._user_message_widgets, start=1):
            card.set_active(index == selected_turn)

    def _on_chat_yview(self, first: str, last: str) -> None:
        self._chat_view.vbar.set(first, last)
        if float(last) < 0.995:
            if not self._bottom_button.winfo_ismapped():
                self._bottom_button.place(
                    relx=1.0,
                    rely=1.0,
                    x=-30,
                    y=-20,
                    anchor="se",
                )
                self._bottom_button.lift()
        else:
            self._bottom_button.place_forget()
        if not self._turn_marks:
            return
        if self._turn_sync_job is not None:
            self._root.after_cancel(self._turn_sync_job)
        self._turn_sync_job = self._root.after_idle(
            self._sync_turn_navigation_to_view
        )

    def _is_chat_at_bottom(self) -> bool:
        return self._chat_view.yview()[1] >= 0.995

    def _scroll_to_bottom(self) -> None:
        self._chat_view.see("end")
        self._bottom_button.place_forget()
        if self._turn_marks:
            self._refresh_turn_navigation(len(self._turn_marks))
        self._focus_composer()

    def _sync_turn_navigation_to_view(self) -> None:
        self._turn_sync_job = None
        if not self._turn_marks:
            return

        _first, last = self._chat_view.yview()
        if last >= 0.999:
            visible_turn = len(self._turn_marks)
        else:
            viewport_height = max(1, self._chat_view.winfo_height())
            anchor = self._chat_view.index(f"@0,{int(viewport_height * 0.28)}")
            visible_turn = 1
            for number, mark_name in enumerate(self._turn_marks, start=1):
                if self._chat_view.compare(mark_name, "<=", anchor):
                    visible_turn = number
                else:
                    break

        if visible_turn != self._selected_turn_index:
            self._refresh_turn_navigation(visible_turn)

    def _jump_to_turn(self, turn_number: int) -> None:
        if not 1 <= turn_number <= len(self._turn_marks):
            return
        self._refresh_turn_navigation(turn_number)
        mark_name = self._turn_marks[turn_number - 1]
        self._chat_view.tag_remove("turn_focus", "1.0", "end")
        self._chat_view.tag_add(
            "turn_focus",
            mark_name,
            f"{mark_name} lineend+1c",
        )
        self._chat_view.tag_raise("turn_focus")
        self._chat_view.yview(mark_name)

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
        for card in self._markdown_code_widgets:
            card.set_width(card_width)
        pending_tables: list[MarkdownTableCard] = []
        for card in self._markdown_table_widgets:
            if card.set_width(card_width, defer=True):
                pending_tables.append(card)
        pending_users: list[UserMessageCard] = []
        for card in self._user_message_widgets:
            if card.set_max_width(width - 48, defer=True):
                pending_users.append(card)
        if pending_tables or pending_users:
            # 所有 wraplength 先一次性写入，避免每张卡片单独刷新整个 Tk 布局树。
            self._root.update_idletasks()
            for card in pending_tables:
                card.finalize_width()
            for card in pending_users:
                card.finalize_size()

    def _clear_tool_cards(self) -> None:
        if self._empty_state_frame is not None:
            self._empty_state_frame.destroy()
            self._empty_state_frame = None
        for card in self._tool_card_widgets:
            card.destroy()
        for card in self._markdown_code_widgets:
            card.destroy()
        for card in self._markdown_table_widgets:
            card.destroy()
        for card in self._user_message_widgets:
            card.destroy()
        self._tool_cards.clear()
        self._tool_card_widgets.clear()
        self._markdown_code_widgets.clear()
        self._markdown_table_widgets.clear()
        self._user_message_widgets.clear()

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
        if (
            self._live_tool_start is None
            or self._live_response_mark not in self._chat_view.mark_names()
        ):
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
                            starts_new_message=bool(
                                self._turn_answers and self._turn_answers[-1]
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
        if not self._turn_marks:
            self._show_empty_state()
        self._resize_tool_cards()
        self._chat_view.see("end")
        self._refresh_turn_navigation()
