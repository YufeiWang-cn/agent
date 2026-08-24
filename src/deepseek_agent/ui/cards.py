"""实现对话消息、Markdown 内容和工具详情卡片。"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable

from ..planning import PlanStepStatus, TaskPlan
from .editor import EditorTabs, SyntaxText
from .formatting import (
    _content_language_hint,
    _conversation_preview,
    _format_editor_content,
    _split_emoji_spans,
)
from .markdown import parse_inline, parse_markdown
from .theme import (
    ACCENT,
    ASSISTANT_COLOR,
    CARD_BACKGROUND,
    DANGER,
    EDITOR_BACKGROUND,
    EDITOR_BORDER,
    EDITOR_MUTED,
    EDITOR_TEXT,
    EDITOR_TOOLBAR,
    EMOJI_FONT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    USER_BUBBLE_BACKGROUND,
    USER_COLOR,
)


class RenderedMarkdownPreview(tk.Frame):
    """在只读文本控件中渲染 Markdown 预览。"""

    def __init__(self, parent: tk.Misc, content: str) -> None:
        super().__init__(parent, background=EDITOR_BACKGROUND)
        self._table_cards: list[MarkdownTableCard] = []
        self.text = ScrolledText(
            self,
            wrap="word",
            state="normal",
            width=1,
            height=1,
            font=("Microsoft YaHei UI", 11),
            background="#FFFFFF",
            foreground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=14,
            spacing3=5,
        )
        self.text.pack(fill="both", expand=True)
        self._configure_tags()
        self._render(content)
        self.text.configure(state="disabled")
        self.text.bind("<Configure>", self._resize_tables)

    def _configure_tags(self) -> None:
        for level, size in ((1, 18), (2, 16), (3, 14), (4, 12), (5, 12), (6, 12)):
            self.text.tag_configure(
                f"heading_{level}",
                font=("Microsoft YaHei UI", size, "bold"),
                spacing1=9,
                spacing3=4,
            )
        self.text.tag_configure(
            "bold",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.text.tag_configure(
            "italic",
            font=("Microsoft YaHei UI", 11, "italic"),
        )
        self.text.tag_configure(
            "inline_code",
            font=("Cascadia Mono", 10),
            foreground="#9D174D",
            background="#EEF2F7",
        )
        self.text.tag_configure(
            "code",
            font=("Cascadia Mono", 10),
            foreground=EDITOR_TEXT,
            background=EDITOR_TOOLBAR,
            lmargin1=14,
            lmargin2=14,
            rmargin=14,
            spacing1=3,
            spacing3=3,
        )
        self.text.tag_configure(
            "code_language",
            font=("Cascadia Mono", 9, "bold"),
            foreground=USER_COLOR,
            background=EDITOR_TOOLBAR,
            lmargin1=14,
            lmargin2=14,
            rmargin=14,
            spacing1=7,
        )
        self.text.tag_configure(
            "quote",
            foreground=TEXT_SECONDARY,
            background="#F8FAFC",
            lmargin1=20,
            lmargin2=20,
            rmargin=18,
        )
        self.text.tag_configure("list", lmargin1=22, lmargin2=36)
        self.text.tag_configure("rule", foreground=EDITOR_BORDER)
        self.text.tag_configure("emoji", font=(EMOJI_FONT, 11))

    def _render(self, content: str) -> None:
        for block in parse_markdown(content):
            if block.kind == "blank":
                self.text.insert("end", "\n")
            elif block.kind == "heading":
                tag = f"heading_{min(6, max(1, block.level))}"
                self._insert_inline(block.text, (tag,))
                self.text.insert("end", "\n", tag)
            elif block.kind == "quote":
                self._insert_inline(block.text, ("quote",))
                self.text.insert("end", "\n", "quote")
            elif block.kind == "list_item":
                self._insert_inline(f"{block.marker} {block.text}", ("list",))
                self.text.insert("end", "\n", "list")
            elif block.kind == "rule":
                self.text.insert("end", "─" * 48 + "\n", "rule")
            elif block.kind == "code":
                self.text.insert(
                    "end",
                    f"{block.language.upper()}\n",
                    "code_language",
                )
                self.text.insert("end", block.text.rstrip("\n") + "\n", "code")
            elif block.kind == "table":
                card = MarkdownTableCard(self.text, block.headers, block.rows)
                self.text.window_create("end", window=card.frame, pady=6)
                self.text.insert("end", "\n")
                self._table_cards.append(card)
            else:
                self._insert_inline(block.text, ())
                self.text.insert("end", "\n")

    def _insert_inline(self, content: str, base_tags: tuple[str, ...]) -> None:
        style_tags = {
            "bold": "bold",
            "italic": "italic",
            "code": "inline_code",
        }
        for span in parse_inline(content):
            tags = base_tags
            if span.style in style_tags:
                tags = (*tags, style_tags[span.style])
            for segment, is_emoji in _split_emoji_spans(span.text):
                segment_tags = (*tags, "emoji") if is_emoji else tags
                self.text.insert("end", segment, segment_tags)

    def _resize_tables(self, event: tk.Event | None = None) -> None:
        width = event.width if event is not None else self.text.winfo_width()
        for card in self._table_cards:
            card.set_width(max(260, width - 44))


class MarkdownCodeCard:
    """展示带语言标签和复制功能的自适应代码块。"""

    def __init__(
        self,
        parent: tk.Text,
        content: str,
        language: str,
    ) -> None:
        self._chat_view = parent
        formatted, detected_language = _format_editor_content(
            content.rstrip("\n"),
            language,
        )
        self._content = formatted
        self._language = detected_language or "text"
        self._line_count = max(1, formatted.count("\n") + 1)
        self._height_job: str | None = None
        self._copy_reset_job: str | None = None

        self.frame = tk.Frame(
            parent,
            # 使用外层背景形成稳定的 1px 边框。
            # 这种方式避免 Windows DPI 缩放挤占内容区域或覆盖最后一行。
            background=EDITOR_BORDER,
            highlightthickness=0,
            borderwidth=0,
        )
        self.frame.pack_propagate(False)

        self._surface = tk.Frame(
            self.frame,
            background=EDITOR_BACKGROUND,
            borderwidth=0,
            highlightthickness=0,
        )
        self._surface.pack(fill="both", expand=True, padx=1, pady=1)
        self._surface.pack_propagate(False)

        toolbar = tk.Frame(self._surface, background=EDITOR_TOOLBAR, height=40)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        tk.Label(
            toolbar,
            text=self._language.upper(),
            background=EDITOR_TOOLBAR,
            foreground="#3157B7",
            font=("Cascadia Mono", 9, "bold"),
        ).pack(side="left", padx=(14, 9), pady=10)
        tk.Label(
            toolbar,
            text=f"{self._line_count} 行",
            background=EDITOR_TOOLBAR,
            foreground=EDITOR_MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", pady=9)
        self._copy_button = tk.Button(
            toolbar,
            text="复制",
            command=self._copy,
            background="#FFFFFF",
            activebackground="#E7EBF1",
            foreground=TEXT_SECONDARY,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9),
            padx=9,
            pady=3,
        )
        self._copy_button.pack(side="right", padx=9, pady=6)

        tk.Frame(self._surface, background=EDITOR_BORDER, height=1).pack(fill="x")
        self._editor = SyntaxText(
            self._surface,
            font_size=11,
            vertical_scrollbar=False,
            auto_hide_horizontal=True,
            padding_y=5,
        )
        self._editor.pack(fill="both", expand=True)
        self._editor.set_content(formatted, self._language)
        self._editor.text.configure(height=self._line_count)
        self._schedule_height_sync()
        self._bind_mousewheel(self.frame)

    def set_width(self, width: int) -> None:
        visible_width = max(260, width)
        if getattr(self, "_last_width", None) == visible_width:
            return
        self._last_width = visible_width
        self.frame.configure(width=visible_width)
        self._schedule_height_sync()

    def destroy(self) -> None:
        for attribute in ("_height_job", "_copy_reset_job"):
            job = getattr(self, attribute)
            if job is None:
                continue
            try:
                self.frame.after_cancel(job)
            except tk.TclError:
                pass
            setattr(self, attribute, None)
        self._editor.destroy()
        self.frame.destroy()

    def _copy(self) -> None:
        self.frame.clipboard_clear()
        self.frame.clipboard_append(self._content)
        self._copy_button.configure(text="已复制")
        if self._copy_reset_job is not None:
            self.frame.after_cancel(self._copy_reset_job)
        self._copy_reset_job = self.frame.after(1200, self._reset_copy_label)

    def _reset_copy_label(self) -> None:
        self._copy_reset_job = None
        if self._copy_button.winfo_exists():
            self._copy_button.configure(text="复制")

    def _schedule_height_sync(self) -> None:
        if self._height_job is not None or not self.frame.winfo_exists():
            return
        self._height_job = self.frame.after_idle(self._sync_height)

    def _sync_height(self) -> None:
        self._height_job = None
        if not self.frame.winfo_exists():
            return
        self.frame.update_idletasks()

        # 宽度变化可能会让横向滚动条出现或消失，先刷新它，再计算高度。
        self._editor._refresh_horizontal_scrollbar()
        self.frame.update_idletasks()

        border_height = 2
        toolbar_and_separator_height = 41
        text = self._editor.text
        # dlineinfo 只保证返回可见行的信息。
        # 代码块位于视口外或布局未完成时会返回 None，导致高度不稳定。
        # Text.count(..., "ypixels") 会计算所有显示行的真实像素高度。
        # 该结果包含行内字体度量，补上上下内边距即可得到完整高度。
        pixel_count = text.count("1.0", "end", "ypixels")
        content_height = pixel_count[0] if pixel_count else 0
        line_height = tkfont.Font(font=text.cget("font")).metrics("linespace")
        content_height = max(content_height, self._line_count * line_height)
        vertical_padding = 2 * int(text.cget("pady"))
        text_height = content_height + vertical_padding

        scrollbar_height = (
            self._editor._scroll_x.winfo_reqheight()
            if self._editor._scroll_x.grid_info()
            else 0
        )
        editor_height = text_height + scrollbar_height
        self.frame.configure(
            height=border_height + toolbar_and_separator_height + editor_height
        )

    def _bind_mousewheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._handle_mousewheel)
        widget.bind("<Button-4>", self._handle_mousewheel)
        widget.bind("<Button-5>", self._handle_mousewheel)
        for child in widget.winfo_children():
            self._bind_mousewheel(child)

    def _handle_mousewheel(self, event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            direction = -1 if delta > 0 else 1
            units = direction * max(1, abs(delta) // 120)
        else:
            units = -1 if getattr(event, "num", 0) == 4 else 1
        if int(getattr(event, "state", 0)) & 0x0001:
            self._editor.text.xview_scroll(units, "units")
        else:
            self._chat_view.yview_scroll(units, "units")
        return "break"


class MarkdownTableCard:
    """使用网格布局展示可自适应宽度的 Markdown 表格。"""

    def __init__(
        self,
        parent: tk.Text,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
    ) -> None:
        self._chat_view = parent
        self._headers = headers
        self._rows = rows
        self._labels: list[list[tk.Label]] = []
        self._body_font = tkfont.Font(
            root=parent,
            family="Microsoft YaHei UI",
            size=10,
        )
        self._header_font = tkfont.Font(
            root=parent,
            family="Microsoft YaHei UI",
            size=10,
            weight="bold",
        )

        self.frame = tk.Frame(
            parent,
            background=EDITOR_BORDER,
            borderwidth=0,
        )
        self.frame.pack_propagate(False)
        self._canvas = tk.Canvas(
            self.frame,
            background="#FFFFFF",
            borderwidth=0,
            highlightthickness=0,
        )
        self._scroll_x = ttk.Scrollbar(
            self.frame,
            orient="horizontal",
            command=self._canvas.xview,
        )
        self._canvas.configure(xscrollcommand=self._scroll_x.set)
        self._canvas.pack(side="top", fill="both", expand=True)

        self._table = tk.Frame(self._canvas, background=EDITOR_BORDER)
        self._table_window = self._canvas.create_window(
            0,
            0,
            window=self._table,
            anchor="nw",
        )
        self._build_cells()
        self._bind_mousewheel(self.frame)

    def set_width(self, width: int, *, defer: bool = False) -> bool:
        visible_width = max(260, width)
        if getattr(self, "_last_width", None) == visible_width:
            return False
        self._last_width = visible_width
        column_widths = self._column_widths()
        natural_width = sum(column_widths) + 1
        if natural_width < visible_width:
            column_widths[-1] += visible_width - natural_width
        content_width = max(visible_width, sum(column_widths) + 1)

        for column, column_width in enumerate(column_widths):
            self._table.grid_columnconfigure(column, minsize=column_width)
            for row_labels in self._labels:
                row_labels[column].configure(wraplength=max(60, column_width - 22))

        self._pending_size = (visible_width, content_width)
        if defer:
            return True
        self._table.update_idletasks()
        self.finalize_width()
        return True

    def finalize_width(self) -> None:
        pending = getattr(self, "_pending_size", None)
        if pending is None:
            return
        visible_width, content_width = pending
        self._pending_size = None
        content_height = self._table.winfo_reqheight()
        overflow = content_width > visible_width
        if overflow:
            if not self._scroll_x.winfo_ismapped():
                self._scroll_x.pack(side="bottom", fill="x")
        else:
            self._scroll_x.pack_forget()
            self._canvas.xview_moveto(0)

        scrollbar_height = self._scroll_x.winfo_reqheight() if overflow else 0
        self._canvas.itemconfigure(
            self._table_window,
            width=content_width,
            height=content_height,
        )
        self._canvas.configure(
            scrollregion=(0, 0, content_width, content_height),
        )
        self.frame.configure(
            width=visible_width,
            height=content_height + scrollbar_height,
        )

    def destroy(self) -> None:
        self.frame.destroy()

    def _build_cells(self) -> None:
        values = (self._headers, *self._rows)
        for row, cells in enumerate(values):
            row_labels: list[tk.Label] = []
            background = EDITOR_TOOLBAR if row == 0 else (
                "#FFFFFF" if row % 2 else "#FAFBFD"
            )
            for column, value in enumerate(cells):
                plain_text = "".join(span.text for span in parse_inline(value))
                contains_emoji = any(
                    is_emoji
                    for _segment, is_emoji in _split_emoji_spans(plain_text)
                )
                if contains_emoji:
                    cell_font: tkfont.Font | tuple[str, int, str] = (
                        EMOJI_FONT,
                        10,
                        "bold" if row == 0 else "normal",
                    )
                else:
                    cell_font = self._header_font if row == 0 else self._body_font
                label = tk.Label(
                    self._table,
                    text=plain_text,
                    background=background,
                    foreground=TEXT_PRIMARY,
                    font=cell_font,
                    justify="left",
                    anchor="nw",
                    padx=10,
                    pady=7,
                )
                label.grid(
                    row=row,
                    column=column,
                    sticky="nsew",
                    padx=(1 if column == 0 else 0, 1),
                    pady=(1 if row == 0 else 0, 1),
                )
                row_labels.append(label)
            self._labels.append(row_labels)

    def _column_widths(self) -> list[int]:
        widths: list[int] = []
        values = (self._headers, *self._rows)
        for column in range(len(self._headers)):
            measured = 0
            for row, cells in enumerate(values):
                plain_text = "".join(
                    span.text for span in parse_inline(cells[column])
                )
                font = self._header_font if row == 0 else self._body_font
                measured = max(
                    measured,
                    *(font.measure(line) for line in plain_text.splitlines() or [""]),
                )
            widths.append(min(420, max(84, measured + 24)))
        return widths

    def _bind_mousewheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._handle_mousewheel)
        widget.bind("<Button-4>", self._handle_mousewheel)
        widget.bind("<Button-5>", self._handle_mousewheel)
        for child in widget.winfo_children():
            self._bind_mousewheel(child)

    def _handle_mousewheel(self, event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            direction = -1 if delta > 0 else 1
            units = direction * max(1, abs(delta) // 120)
        else:
            units = -1 if getattr(event, "num", 0) == 4 else 1
        if int(getattr(event, "state", 0)) & 0x0001:
            self._canvas.xview_scroll(units, "units")
        else:
            self._chat_view.yview_scroll(units, "units")
        return "break"


class UserMessageCard:
    """展示根据内容确定宽度并右对齐的用户消息气泡。"""

    def __init__(self, parent: tk.Text, author: str, content: str) -> None:
        self._chat_view = parent
        self._content = content
        self._active = False
        self._bubble_width = 0
        self._bubble_height = 0
        self.frame = tk.Frame(parent, background=CARD_BACKGROUND)

        tk.Label(
            self.frame,
            text=author,
            background=CARD_BACKGROUND,
            foreground=USER_COLOR,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="e", padx=5, pady=(0, 4))

        self._canvas = tk.Canvas(
            self.frame,
            background=CARD_BACKGROUND,
            borderwidth=0,
            highlightthickness=0,
        )
        self._canvas.pack(anchor="e")
        self._label = tk.Label(
            self._canvas,
            text=content,
            background=USER_BUBBLE_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=(
                EMOJI_FONT
                if any(is_emoji for _text, is_emoji in _split_emoji_spans(content))
                else "Microsoft YaHei UI",
                11,
            ),
            justify="left",
            anchor="w",
        )
        self._label_window = self._canvas.create_window(
            0,
            0,
            window=self._label,
            anchor="center",
        )
        self.set_max_width(parent.winfo_width())
        self._bind_mousewheel(self.frame)

    def set_max_width(
        self,
        available_width: int,
        *,
        defer: bool = False,
    ) -> bool:
        if getattr(self, "_last_available_width", None) == available_width:
            return False
        self._last_available_width = available_width
        content_limit = max(150, int(max(320, available_width) * 0.64) - 32)
        self._label.configure(wraplength=content_limit)
        self._pending_content_limit = content_limit
        if defer:
            return True
        self.frame.update_idletasks()
        self.finalize_size()
        return True

    def finalize_size(self) -> None:
        content_limit = getattr(self, "_pending_content_limit", None)
        if content_limit is None:
            return
        self._pending_content_limit = None
        bubble_width = min(content_limit, self._label.winfo_reqwidth()) + 28
        bubble_height = self._label.winfo_reqheight() + 18
        self._bubble_width = bubble_width
        self._bubble_height = bubble_height
        self._canvas.configure(width=bubble_width, height=bubble_height)
        self._canvas.coords(
            self._label_window,
            bubble_width / 2,
            bubble_height / 2,
        )
        self._draw_bubble(bubble_width, bubble_height)

    def set_active(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        if self._bubble_width and self._bubble_height:
            self._draw_bubble(self._bubble_width, self._bubble_height)

    def destroy(self) -> None:
        self.frame.destroy()

    def _draw_bubble(self, width: int, height: int) -> None:
        self._canvas.delete("bubble")
        radius = min(14, height // 2)
        points = (
            radius,
            0,
            width - radius,
            0,
            width,
            0,
            width,
            radius,
            width,
            height - radius,
            width,
            height,
            width - radius,
            height,
            radius,
            height,
            0,
            height,
            0,
            height - radius,
            0,
            radius,
            0,
            0,
        )
        self._canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=12,
            fill=USER_BUBBLE_BACKGROUND,
            outline=ACCENT if self._active else "",
            width=2 if self._active else 1,
            tags="bubble",
        )
        self._canvas.tag_lower("bubble")

    def _bind_mousewheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._handle_mousewheel)
        widget.bind("<Button-4>", self._handle_mousewheel)
        widget.bind("<Button-5>", self._handle_mousewheel)
        for child in widget.winfo_children():
            self._bind_mousewheel(child)

    def _handle_mousewheel(self, event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            direction = -1 if delta > 0 else 1
            units = direction * max(1, abs(delta) // 120)
        else:
            units = -1 if getattr(event, "num", 0) == 4 else 1
        self._chat_view.yview_scroll(units, "units")
        return "break"


class ConversationRail:
    """提供按对话轮次跳转的紧凑侧边导航。"""

    def __init__(
        self,
        parent: tk.Misc,
        chat_view: tk.Text,
        on_jump: Callable[[int], None],
    ) -> None:
        self._parent = parent
        self._chat_view = chat_view
        self._on_jump = on_jump
        self._questions: list[str] = []
        self._answers: list[str] = []
        self._selected_turn: int | None = None
        self._hovered_turn: int | None = None
        self._tick_positions: list[float] = []

        self.canvas = tk.Canvas(
            parent,
            width=30,
            background=CARD_BACKGROUND,
            borderwidth=0,
            highlightthickness=0,
            cursor="arrow",
        )
        self.canvas.place(x=7, y=12, relheight=1.0, height=-24)
        self.canvas.bind("<Configure>", self._redraw)
        self.canvas.bind("<Motion>", self._handle_motion)
        self.canvas.bind("<Leave>", self._hide_preview)
        self.canvas.bind("<Button-1>", self._handle_click)
        self.canvas.bind("<MouseWheel>", self._handle_mousewheel)
        self.canvas.bind("<Button-4>", self._handle_mousewheel)
        self.canvas.bind("<Button-5>", self._handle_mousewheel)

        self._preview = tk.Frame(
            parent,
            background="#FFFFFF",
            highlightbackground="#D8DEE8",
            highlightthickness=1,
            borderwidth=0,
        )
        self._preview_title = tk.Label(
            self._preview,
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        )
        self._preview_title.pack(fill="x", padx=13, pady=(11, 4))
        self._preview_question = tk.Label(
            self._preview,
            background="#FFFFFF",
            foreground=TEXT_PRIMARY,
            font=(EMOJI_FONT, 10, "bold"),
            justify="left",
            anchor="w",
            wraplength=304,
        )
        self._preview_question.pack(fill="x", padx=13)
        self._preview_answer = tk.Label(
            self._preview,
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=(EMOJI_FONT, 9),
            justify="left",
            anchor="w",
            wraplength=304,
        )
        self._preview_answer.pack(fill="x", padx=13, pady=(6, 12))

    def set_turns(
        self,
        questions: list[str],
        answers: list[str],
        selected_turn: int | None,
    ) -> None:
        content_changed = questions != self._questions or answers != self._answers
        selection_changed = selected_turn != self._selected_turn
        if not content_changed and not selection_changed:
            return
        if content_changed:
            self._questions = list(questions)
            self._answers = list(answers)
            self._hovered_turn = None
            self._hide_preview()
        self._selected_turn = selected_turn
        self._redraw()

    def update_answer(self, turn_number: int, answer: str) -> None:
        if not 1 <= turn_number <= len(self._answers):
            return
        self._answers[turn_number - 1] = answer
        if self._hovered_turn == turn_number:
            self._show_preview(turn_number)

    def _redraw(self, _event: tk.Event | None = None) -> None:
        self.canvas.delete("all")
        total = len(self._questions)
        self._tick_positions = []
        if total == 0:
            return

        height = max(1, self.canvas.winfo_height())
        top = 12
        bottom = max(top, height - 12)
        step = 0 if total == 1 else (bottom - top) / (total - 1)
        for index in range(total):
            turn_number = index + 1
            y = top + step * index
            self._tick_positions.append(y)
            active = turn_number == self._selected_turn
            hovered = turn_number == self._hovered_turn
            self.canvas.create_line(
                9,
                y,
                27 if active or hovered else 20,
                y,
                fill=ACCENT if active else "#AAB2BF" if hovered else "#C8CDD5",
                width=3 if active else 2,
                capstyle="round",
            )

    def _turn_at(self, y: int) -> int | None:
        if not self._tick_positions:
            return None
        nearest = min(
            range(len(self._tick_positions)),
            key=lambda index: abs(self._tick_positions[index] - y),
        )
        if abs(self._tick_positions[nearest] - y) > 8:
            return None
        return nearest + 1

    def _handle_motion(self, event: tk.Event) -> None:
        turn_number = self._turn_at(event.y)
        if turn_number == self._hovered_turn:
            return
        self._hovered_turn = turn_number
        self.canvas.configure(cursor="hand2" if turn_number else "arrow")
        self._redraw()
        if turn_number is None:
            self._hide_preview()
        else:
            self._show_preview(turn_number)

    def _handle_click(self, event: tk.Event) -> str | None:
        turn_number = self._turn_at(event.y)
        if turn_number is None:
            return None
        self._on_jump(turn_number)
        return "break"

    def _show_preview(self, turn_number: int) -> None:
        question = self._questions[turn_number - 1]
        answer = self._answers[turn_number - 1]
        self._preview_title.configure(
            text=f"第 {turn_number} / {len(self._questions)} 轮"
        )
        self._preview_question.configure(
            text=_conversation_preview(question, limit=68)
        )
        self._preview_answer.configure(
            text=_conversation_preview(answer, limit=130)
            if answer.strip()
            else "尚未生成回答"
        )
        self._preview.update_idletasks()
        preview_height = self._preview.winfo_reqheight()
        parent_height = max(1, self._parent.winfo_height())
        tick_y = self.canvas.winfo_y() + self._tick_positions[turn_number - 1]
        preview_y = int(tick_y - preview_height / 2)
        preview_y = max(8, min(preview_y, parent_height - preview_height - 8))
        self._preview.place(x=42, y=preview_y, width=332)
        self._preview.lift()

    def _hide_preview(self, _event: tk.Event | None = None) -> None:
        self._preview.place_forget()

    def _handle_mousewheel(self, event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            direction = -1 if delta > 0 else 1
            units = direction * max(1, abs(delta) // 120)
        else:
            units = -1 if getattr(event, "num", 0) == 4 else 1
        self._chat_view.yview_scroll(units, "units")
        return "break"


class FullscreenToolViewer:
    """在独立窗口中展示较长的工具参数和结果。"""

    def __init__(
        self,
        parent: tk.Misc,
        name: str,
        arguments: str,
        result: str | None,
        result_language: str,
    ) -> None:
        self._window = tk.Toplevel(parent)
        self._window.title(f"工具详情 · {name}")
        self._window.configure(background="#F5F7FA")
        self._fullscreen = True
        self._fullscreen_job: str | None = None

        toolbar = tk.Frame(self._window, background="#FFFFFF", height=58)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        tk.Label(
            toolbar,
            text=name,
            background="#FFFFFF",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(side="left", padx=(18, 8), pady=14)
        tk.Label(
            toolbar,
            text="工具调用详情",
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", pady=16)
        tk.Button(
            toolbar,
            text="关闭  Esc",
            command=self._close,
            background="#EEF2F6",
            activebackground="#DDE3EB",
            foreground=TEXT_SECONDARY,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=5,
        ).pack(side="right", padx=(5, 12), pady=8)
        self._fullscreen_button = tk.Button(
            toolbar,
            text="退出全屏  F11",
            command=self._toggle_fullscreen,
            background="#EEF2F6",
            activebackground="#DDE3EB",
            foreground=TEXT_SECONDARY,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=5,
        )
        self._fullscreen_button.pack(side="right", padx=5, pady=8)
        tk.Button(
            toolbar,
            text="复制全部",
            command=self._copy_current,
            background="#EEF2F6",
            activebackground="#DDE3EB",
            foreground=TEXT_SECONDARY,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=5,
        ).pack(side="right", padx=5, pady=8)

        self._status = tk.StringVar()
        self._tabs = EditorTabs(
            self._window,
            on_change=self._update_status,
            editor_font_size=14,
            tab_font_size=10,
        )
        self._tabs.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        self._tabs.add_or_update("arguments", "参数", arguments, "json")
        if result is not None:
            self._tabs.add_or_update(
                "result",
                "结果",
                result,
                result_language,
                select=True,
            )

        status_bar = tk.Frame(self._window, background="#FFFFFF", height=30)
        status_bar.pack(fill="x")
        status_bar.pack_propagate(False)
        tk.Label(
            status_bar,
            textvariable=self._status,
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Cascadia Mono", 9),
        ).pack(side="left", padx=12, pady=4)
        tk.Label(
            status_bar,
            text="滚轮浏览  ·  F11 切换全屏  ·  Esc 退出",
            background="#FFFFFF",
            foreground=EDITOR_MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right", padx=12, pady=4)

        self._window.bind("<F11>", self._toggle_fullscreen)
        self._window.bind("<Escape>", self._handle_escape)
        self._window.protocol("WM_DELETE_WINDOW", self._close)
        self._fullscreen_job = self._window.after_idle(self._enter_fullscreen)

    def _enter_fullscreen(self) -> None:
        self._fullscreen_job = None
        if self._window.winfo_exists():
            self._window.attributes("-fullscreen", True)

    def _close(self) -> None:
        if not self._window.winfo_exists():
            return
        if self._fullscreen_job is not None:
            try:
                self._window.after_cancel(self._fullscreen_job)
            except tk.TclError:
                pass
            self._fullscreen_job = None
        self._tabs.destroy()
        self._window.destroy()

    def close(self) -> None:
        self._close()

    def _copy_current(self) -> None:
        self._window.clipboard_clear()
        self._window.clipboard_append(self._tabs.current_editor().content)

    def _update_status(self, editor: SyntaxText) -> None:
        line_count = editor.content.count("\n") + 1
        self._status.set(
            f"{editor.language.upper()}   {line_count} 行   "
            f"{len(editor.content)} 字符"
        )

    def _toggle_fullscreen(self, _event: tk.Event | None = None) -> str:
        self._fullscreen = not self._fullscreen
        self._window.attributes("-fullscreen", self._fullscreen)
        self._fullscreen_button.configure(
            text="退出全屏  F11" if self._fullscreen else "进入全屏  F11"
        )
        if not self._fullscreen:
            try:
                self._window.state("zoomed")
            except tk.TclError:
                pass
        return "break"

    def _handle_escape(self, _event: tk.Event) -> str:
        if self._fullscreen:
            self._toggle_fullscreen()
        else:
            self._close()
        return "break"


class PlanCard(tk.Frame):
    """以紧凑可折叠卡片展示当前任务计划和完成进度。"""

    _STATUS_STYLES = {
        PlanStepStatus.PENDING: ("○", "待处理", TEXT_SECONDARY, "#FFFFFF"),
        PlanStepStatus.IN_PROGRESS: ("●", "进行中", ACCENT, "#EEF3FF"),
        PlanStepStatus.COMPLETED: ("✓", "已完成", ASSISTANT_COLOR, "#F0FDF4"),
        PlanStepStatus.FAILED: ("!", "失败", DANGER, "#FEF2F2"),
        PlanStepStatus.SKIPPED: ("–", "已跳过", TEXT_SECONDARY, "#F8FAFC"),
    }

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(
            parent,
            background=CARD_BACKGROUND,
            highlightbackground=EDITOR_BORDER,
            highlightthickness=1,
            borderwidth=0,
        )
        self._expanded = True
        self._plan: TaskPlan | None = None
        self._step_labels: list[tk.Label] = []

        header = tk.Frame(self, background="#F8FAFC")
        header.pack(fill="x")
        self._toggle_button = tk.Button(
            header,
            text="▼  任务计划",
            command=self.toggle,
            anchor="w",
            background="#F8FAFC",
            activebackground="#F1F5F9",
            foreground=TEXT_PRIMARY,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=12,
            pady=7,
        )
        self._toggle_button.pack(side="left", fill="x", expand=True)
        self._progress = tk.Label(
            header,
            text="",
            background="#E9EEF5",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=8,
            pady=3,
        )
        self._progress.pack(side="right", padx=9, pady=7)

        self._body = tk.Frame(self, background=CARD_BACKGROUND)
        self._body.pack(fill="x", padx=10, pady=(7, 9))
        self.bind("<Configure>", self._resize_text)

    def update_plan(self, plan: TaskPlan) -> None:
        """使用最新不可变快照重新绘制步骤列表。"""
        self._plan = plan
        self._progress.configure(
            text=f"{plan.completed_count}/{len(plan.steps)} 已完成"
        )
        for child in self._body.winfo_children():
            child.destroy()
        self._step_labels.clear()

        if plan.explanation:
            explanation = tk.Label(
                self._body,
                text=plan.explanation,
                anchor="w",
                justify="left",
                background="#F8FAFC",
                foreground=TEXT_SECONDARY,
                font=("Microsoft YaHei UI", 9),
                padx=9,
                pady=5,
            )
            explanation.pack(fill="x", pady=(0, 5))
            self._step_labels.append(explanation)

        for index, step in enumerate(plan.steps, start=1):
            symbol, status_text, color, background = self._STATUS_STYLES[
                step.status
            ]
            row = tk.Frame(self._body, background=background)
            row.pack(fill="x", pady=1)
            tk.Label(
                row,
                text=symbol,
                background=background,
                foreground=color,
                font=("Microsoft YaHei UI", 11, "bold"),
                width=2,
            ).pack(side="left", padx=(7, 2), pady=5)
            label = tk.Label(
                row,
                text=f"{index}. {step.step}",
                anchor="w",
                justify="left",
                background=background,
                foreground=TEXT_PRIMARY,
                font=("Microsoft YaHei UI", 9),
            )
            label.pack(side="left", fill="x", expand=True, pady=5)
            self._step_labels.append(label)
            tk.Label(
                row,
                text=status_text,
                background=background,
                foreground=color,
                font=("Microsoft YaHei UI", 9),
            ).pack(side="right", padx=(8, 9), pady=5)
        self._resize_text()

    def toggle(self) -> None:
        """切换步骤详情的展开状态。"""
        self._expanded = not self._expanded
        self._toggle_button.configure(
            text=("▼  任务计划" if self._expanded else "▶  任务计划")
        )
        if self._expanded:
            self._body.pack(fill="x", padx=10, pady=(7, 9))
        else:
            self._body.pack_forget()

    def _resize_text(self, _event: tk.Event | None = None) -> None:
        """根据卡片宽度调整步骤文本的换行范围。"""
        wraplength = max(220, self.winfo_width() - 180)
        for label in self._step_labels:
            label.configure(wraplength=wraplength)


class ToolCallCard:
    """展示工具调用状态，并支持查看完整参数和执行结果。"""

    def __init__(self, parent: tk.Text, name: str, arguments: str) -> None:
        self._chat_view = parent
        self._name = name
        self._arguments = arguments
        self._result: str | None = None
        self._result_language = _content_language_hint(arguments)
        self._expanded = False
        self._viewer: FullscreenToolViewer | None = None

        self.frame = tk.Frame(
            parent,
            background=CARD_BACKGROUND,
            highlightbackground=EDITOR_BORDER,
            highlightthickness=1,
            borderwidth=0,
        )
        self.frame.pack_propagate(False)
        header = tk.Frame(self.frame, background="#F8FAFC", height=42)
        header.pack(fill="x")
        header.pack_propagate(False)
        self._toggle_button = tk.Button(
            header,
            text=f"▶  {name}",
            command=self.toggle,
            anchor="w",
            background="#F8FAFC",
            activebackground="#F1F5F9",
            foreground=TEXT_PRIMARY,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=12,
            pady=8,
        )
        self._toggle_button.pack(side="left", fill="x", expand=True)
        self._status = tk.Label(
            header,
            text="运行中",
            background="#FFF7ED",
            foreground="#B45309",
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=8,
            pady=3,
        )
        self._status.pack(side="right", padx=(7, 5), pady=8)
        tk.Button(
            header,
            text="查看",
            command=self._open_fullscreen,
            background="#E9EEF5",
            activebackground="#E2E8F0",
            foreground=TEXT_SECONDARY,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9),
            padx=8,
            pady=3,
        ).pack(side="right", padx=(5, 0), pady=8)

        self._summary = tk.Label(
            self.frame,
            text=self._make_summary(arguments),
            anchor="w",
            justify="left",
            background=CARD_BACKGROUND,
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        )
        self._summary.pack(fill="x", padx=14, pady=(8, 9))

        self._details_separator = tk.Frame(
            self.frame,
            background="#E2E8F0",
            height=1,
        )

        self._details_frame = tk.Frame(
            self.frame,
            background=EDITOR_BACKGROUND,
        )
        self._tabs = EditorTabs(
            self._details_frame,
            editor_font_size=11,
            tab_font_size=9,
        )
        self._tabs.pack(fill="both", expand=True)
        self._argument_editor = self._tabs.add_or_update(
            "arguments",
            "参数",
            arguments,
            "json",
        )
        self._result_editor: SyntaxText | None = None
        self._sync_size()
        self._bind_mousewheel(self.frame)

    def set_result(self, result: str) -> None:
        self._result = result
        is_failure = result.startswith(("工具执行失败", "工具发生", "用户拒绝"))
        self._status.configure(
            text="失败" if is_failure else "已完成",
            foreground=DANGER if is_failure else ASSISTANT_COLOR,
            background="#FEF2F2" if is_failure else "#ECFDF5",
        )
        self._summary.configure(text=self._make_summary(result))
        self._result_editor = self._tabs.add_or_update(
            "result",
            "结果",
            result,
            self._result_language,
            select=self._expanded,
        )
        self._bind_mousewheel(self.frame)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        arrow = "▼" if self._expanded else "▶"
        self._toggle_button.configure(text=f"{arrow}  {self._name}")
        if self._expanded:
            self._details_separator.pack(fill="x")
            self._details_frame.pack(
                fill="both",
                expand=True,
            )
            if self._result_editor is not None:
                self._tabs.select("result")
        else:
            self._details_separator.pack_forget()
            self._details_frame.pack_forget()
        self._sync_size()

    def set_width(self, width: int) -> None:
        visible_width = max(260, width)
        if getattr(self, "_last_width", None) == visible_width:
            return
        self._last_width = visible_width
        self.frame.configure(width=visible_width)
        self._summary.configure(wraplength=max(200, visible_width - 36))

    def destroy(self) -> None:
        if self._viewer is not None:
            self._viewer.close()
            self._viewer = None
        self._tabs.destroy()
        self.frame.destroy()

    def _open_fullscreen(self) -> None:
        if self._viewer is not None and self._viewer._window.winfo_exists():
            self._viewer._window.lift()
            self._viewer._window.focus_force()
            return
        self._viewer = FullscreenToolViewer(
            self.frame.winfo_toplevel(),
            self._name,
            self._arguments,
            self._result,
            self._result_language,
        )

    def _sync_size(self) -> None:
        self.frame.configure(height=360 if self._expanded else 76)

    def _bind_mousewheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._handle_mousewheel)
        widget.bind("<Button-4>", self._handle_mousewheel)
        widget.bind("<Button-5>", self._handle_mousewheel)
        for child in widget.winfo_children():
            self._bind_mousewheel(child)

    def _handle_mousewheel(self, event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            direction = -1 if delta > 0 else 1
            units = direction * max(1, abs(delta) // 120)
        else:
            units = -1 if getattr(event, "num", 0) == 4 else 1

        if self._expanded:
            target = self._tabs.current_editor().text
        else:
            target = self._chat_view
        target.yview_scroll(units, "units")
        return "break"

    @staticmethod
    def _make_summary(value: str) -> str:
        compact = " ".join(value.split())
        if not compact:
            return "暂无详细内容"
        return compact if len(compact) <= 100 else compact[:100] + "…"


