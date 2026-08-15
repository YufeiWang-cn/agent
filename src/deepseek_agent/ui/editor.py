"""Reusable syntax-aware text and tabbed editor widgets."""

import io
import keyword
import re
import tkinter as tk
import tokenize
from typing import Any, Callable

from .formatting import _format_editor_content, _split_emoji_spans
from .theme import (
    EDITOR_BACKGROUND,
    EDITOR_BORDER,
    EDITOR_MUTED,
    EDITOR_TEXT,
    EDITOR_TOOLBAR,
    EMOJI_FONT,
    TEXT_PRIMARY,
)


class SyntaxText(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        font_size: int = 10,
        *,
        vertical_scrollbar: bool = True,
        auto_hide_horizontal: bool = False,
        padding_x: int = 16,
        padding_y: int = 13,
    ) -> None:
        super().__init__(parent, background=EDITOR_BACKGROUND)
        self._content = ""
        self._language = "text"
        self._font_size = font_size
        self._auto_hide_horizontal = auto_hide_horizontal
        self.text = tk.Text(
            self,
            wrap="none",
            width=1,
            height=1,
            font=("Cascadia Mono", font_size),
            background=EDITOR_BACKGROUND,
            foreground=EDITOR_TEXT,
            insertbackground=EDITOR_TEXT,
            selectbackground="#DCE7FF",
            selectforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            padx=padding_x,
            pady=padding_y,
        )
        self._scroll_y = tk.Scrollbar(
            self,
            orient="vertical",
            command=self.text.yview,
            background="#D8DEE8",
            activebackground="#B8C1CE",
            troughcolor="#F1F3F6",
            relief="flat",
            borderwidth=0,
            width=12,
        )
        self._scroll_x = tk.Scrollbar(
            self,
            orient="horizontal",
            command=self.text.xview,
            background="#D8DEE8",
            activebackground="#B8C1CE",
            troughcolor="#F1F3F6",
            relief="flat",
            borderwidth=0,
            width=12,
        )
        self.text.configure(
            yscrollcommand=self._scroll_y.set,
            xscrollcommand=self._handle_horizontal_scroll,
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        if vertical_scrollbar:
            self._scroll_y.grid(row=0, column=1, sticky="ns")
        self._scroll_x.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._configure_tags()
        self.text.bind("<Configure>", self._schedule_horizontal_refresh)
        if auto_hide_horizontal:
            self._scroll_x.grid_remove()

    @property
    def content(self) -> str:
        return self._content

    @property
    def language(self) -> str:
        return self._language

    def set_content(self, value: str, language_hint: str = "text") -> None:
        content, language = _format_editor_content(value, language_hint)
        self._content = content
        self._language = language
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for segment, is_emoji in _split_emoji_spans(content):
            self.text.insert("end", segment, "emoji" if is_emoji else ())
        self._highlight(content, language)
        self.text.configure(state="disabled")
        self.after_idle(self._refresh_horizontal_scrollbar)

    def _handle_horizontal_scroll(self, first: str, last: str) -> None:
        self._scroll_x.set(first, last)
        if self._auto_hide_horizontal:
            self._set_horizontal_scrollbar(float(first) > 0 or float(last) < 1)

    def _schedule_horizontal_refresh(self, _event: tk.Event) -> None:
        if self._auto_hide_horizontal:
            self.after_idle(self._refresh_horizontal_scrollbar)

    def _refresh_horizontal_scrollbar(self) -> None:
        if not self.winfo_exists() or not self._auto_hide_horizontal:
            return
        first, last = self.text.xview()
        self._scroll_x.set(first, last)
        self._set_horizontal_scrollbar(first > 0 or last < 1)

    def _set_horizontal_scrollbar(self, visible: bool) -> None:
        if visible:
            # winfo_ismapped() 还取决于祖先窗口当前是否可见；代码块位于
            # 聊天视口外时会返回 False。这里关心的是滚动条是否参与布局。
            if not self._scroll_x.grid_info():
                self._scroll_x.grid()
        else:
            self._scroll_x.grid_remove()

    def _configure_tags(self) -> None:
        self.text.tag_configure("key", foreground="#0550AE")
        self.text.tag_configure("string", foreground="#0A7B4F")
        self.text.tag_configure("number", foreground="#953800")
        self.text.tag_configure("keyword", foreground="#8250DF")
        self.text.tag_configure("comment", foreground="#6E7781")
        self.text.tag_configure("operator", foreground="#CF222E")
        self.text.tag_configure(
            "heading",
            foreground="#0550AE",
            font=("Cascadia Mono", self._font_size, "bold"),
        )
        self.text.tag_configure(
            "emoji",
            font=(EMOJI_FONT, self._font_size),
        )

    def _highlight(self, content: str, language: str) -> None:
        for tag in ("key", "string", "number", "keyword", "comment", "operator", "heading"):
            self.text.tag_remove(tag, "1.0", "end")
        if language == "json":
            self._highlight_json(content)
        elif language == "python":
            self._highlight_python(content)
        elif language == "markdown":
            self._highlight_markdown(content)
        elif language != "text":
            self._highlight_generic_code(content)

    def _tag_matches(self, tag: str, pattern: str, content: str, flags: int = 0) -> None:
        for match in re.finditer(pattern, content, flags):
            self.text.tag_add(
                tag,
                f"1.0+{match.start()}c",
                f"1.0+{match.end()}c",
            )

    def _highlight_json(self, content: str) -> None:
        string_pattern = r'"(?:\\.|[^"\\])*"'
        self._tag_matches("string", string_pattern, content)
        self._tag_matches("key", string_pattern + r"(?=\s*:)", content)
        self._tag_matches("number", r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", content)
        self._tag_matches("keyword", r"\b(?:true|false|null)\b", content)
        self._tag_matches("operator", r"[{}\[\],:]", content)
        self.text.tag_raise("key")

    def _highlight_python(self, content: str) -> None:
        try:
            tokens = tokenize.generate_tokens(io.StringIO(content).readline)
            for token in tokens:
                tag = None
                if token.type == tokenize.STRING:
                    tag = "string"
                elif token.type == tokenize.NUMBER:
                    tag = "number"
                elif token.type == tokenize.COMMENT:
                    tag = "comment"
                elif token.type == tokenize.NAME and keyword.iskeyword(token.string):
                    tag = "keyword"
                elif token.type == tokenize.OP:
                    tag = "operator"
                if tag is not None:
                    self.text.tag_add(
                        tag,
                        f"{token.start[0]}.{token.start[1]}",
                        f"{token.end[0]}.{token.end[1]}",
                    )
        except (IndentationError, tokenize.TokenError):
            self._highlight_generic_code(content)

    def _highlight_markdown(self, content: str) -> None:
        self._tag_matches("heading", r"^#{1,6} .+$", content, re.MULTILINE)
        self._tag_matches("string", r"`[^`]+`", content)
        self._tag_matches("comment", r"^>.*$", content, re.MULTILINE)

    def _highlight_generic_code(self, content: str) -> None:
        self._tag_matches("string", r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')', content)
        self._tag_matches("comment", r"(?m)(?://|#).*$", content)
        self._tag_matches("number", r"\b\d+(?:\.\d+)?\b", content)
        common_keywords = (
            "class|def|function|return|if|else|elif|for|while|try|catch|except|"
            "finally|import|from|const|let|var|new|async|await|true|false|null|"
            "SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|CREATE|TABLE|JOIN"
        )
        self._tag_matches("keyword", rf"\b(?:{common_keywords})\b", content)


class EditorTabs(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        on_change: Any | None = None,
        editor_font_size: int = 10,
        tab_font_size: int = 9,
    ) -> None:
        super().__init__(parent, background=EDITOR_BACKGROUND, borderwidth=0)
        self._on_change = on_change
        self._editor_font_size = editor_font_size
        self._tab_font_size = tab_font_size
        self._items: dict[str, tuple[str, tk.Widget, tk.Button]] = {}
        self._selected_key: str | None = None

        self._tab_bar = tk.Frame(self, background=EDITOR_TOOLBAR, height=40)
        self._tab_bar.pack(fill="x")
        self._tab_bar.pack_propagate(False)
        tk.Frame(self, background=EDITOR_BORDER, height=1).pack(fill="x")
        self._editor_area = tk.Frame(self, background=EDITOR_BACKGROUND)
        self._editor_area.pack(fill="both", expand=True)

    def add_or_update(
        self,
        key: str,
        title: str,
        value: str,
        language_hint: str,
        *,
        select: bool = False,
    ) -> SyntaxText:
        if key in self._items:
            _old_title, editor, button = self._items[key]
            if not isinstance(editor, SyntaxText):
                raise TypeError(f"标签 {key} 不是代码编辑器")
        else:
            editor = SyntaxText(
                self._editor_area,
                font_size=self._editor_font_size,
            )
            button = self._create_tab_button(key)
        editor.set_content(value, language_hint)
        button.configure(text=f"{title}   {editor.language.upper()}")
        self._items[key] = (title, editor, button)
        if self._selected_key is None or select:
            self.select(key)
        return editor

    def add_widget(
        self,
        key: str,
        title: str,
        badge: str,
        factory: Callable[[tk.Misc], tk.Widget],
        *,
        select: bool = False,
    ) -> tk.Widget:
        if key in self._items:
            _old_title, widget, button = self._items[key]
        else:
            widget = factory(self._editor_area)
            button = self._create_tab_button(key)
        button.configure(text=f"{title}   {badge.upper()}")
        self._items[key] = (title, widget, button)
        if self._selected_key is None or select:
            self.select(key)
        return widget

    def _create_tab_button(self, key: str) -> tk.Button:
        button = tk.Button(
            self._tab_bar,
            command=lambda tab_key=key: self.select(tab_key),
            background=EDITOR_TOOLBAR,
            activebackground="#E7EBF1",
            foreground=EDITOR_MUTED,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            padx=15,
            pady=8,
            font=("Microsoft YaHei UI", self._tab_font_size),
            cursor="hand2",
        )
        button.pack(side="left", fill="y")
        return button

    def select(self, key: str) -> None:
        if key not in self._items:
            return
        for item_key, (_title, editor, button) in self._items.items():
            editor.pack_forget()
            is_selected = item_key == key
            button.configure(
                background="#FFFFFF" if is_selected else EDITOR_TOOLBAR,
                foreground=TEXT_PRIMARY if is_selected else EDITOR_MUTED,
                font=(
                    "Microsoft YaHei UI",
                    self._tab_font_size,
                    "bold" if is_selected else "normal",
                ),
            )
        self._selected_key = key
        editor = self._items[key][1]
        editor.pack(fill="both", expand=True)
        if self._on_change is not None:
            self._on_change(editor)

    def current_editor(self) -> SyntaxText:
        if self._selected_key is None:
            raise RuntimeError("编辑器还没有可用标签")
        editor = self._items[self._selected_key][1]
        if not isinstance(editor, SyntaxText):
            raise RuntimeError("当前标签不是代码编辑器")
        return editor


