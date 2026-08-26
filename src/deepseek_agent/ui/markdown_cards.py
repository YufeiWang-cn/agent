"""实现 Markdown 预览、代码块和表格卡片。"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from .editor import SyntaxText
from .formatting import (
    _format_editor_content,
    _split_emoji_spans,
)
from .markdown import parse_inline, parse_markdown
from .theme import (
    EDITOR_BACKGROUND,
    EDITOR_BORDER,
    EDITOR_MUTED,
    EDITOR_TEXT,
    EDITOR_TOOLBAR,
    EMOJI_FONT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
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
