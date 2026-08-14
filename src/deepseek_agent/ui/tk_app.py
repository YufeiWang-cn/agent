import io
import json
import keyword
import logging
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import tokenize
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable

from ..agent import Agent, AgentCancelledError
from ..runtime import RunStatus, TurnOutcome
from ..config import Settings
from ..memory import Project, Session
from ..models import ToolCallRequest
from ..tools import Tool
from .markdown import MarkdownBlock, parse_inline, parse_markdown


APP_BACKGROUND = "#F1F5F9"
SIDEBAR_BACKGROUND = "#F7F7F8"
SIDEBAR_PANEL = "#ECEEF2"
CARD_BACKGROUND = "#FFFFFF"
TEXT_PRIMARY = "#172033"
TEXT_SECONDARY = "#64748B"
SIDEBAR_TEXT = "#25262A"
ACCENT = "#4F6BED"
ACCENT_HOVER = "#4059D0"
DANGER = "#DC4C64"
USER_COLOR = "#3157B7"
USER_BUBBLE_BACKGROUND = "#EEF3FF"
ASSISTANT_COLOR = "#16794F"
TOOL_COLOR = "#9A5B13"
EDITOR_BACKGROUND = "#FBFCFE"
EDITOR_TOOLBAR = "#F3F5F8"
EDITOR_BORDER = "#D8DEE8"
EDITOR_TEXT = "#25324A"
EDITOR_MUTED = "#6B7280"
EMOJI_FONT = "Segoe UI Emoji"

ALL_PROJECTS = "__all_projects__"
UNASSIGNED_PROJECT = "__unassigned_project__"


@dataclass(slots=True)
class ConfirmationRequest:
    tool: Tool
    arguments: str
    completed: threading.Event
    allowed: bool = False


class TkToolConfirmer:
    def __init__(
        self,
        event_queue: queue.Queue[tuple[str, Any]],
        closing: threading.Event,
    ) -> None:
        self._event_queue = event_queue
        self._closing = closing

    def confirm(self, tool: Tool, arguments: str) -> bool:
        request = ConfirmationRequest(tool, arguments, threading.Event())
        self._event_queue.put(("confirmation", request))
        while not request.completed.wait(0.1):
            if self._closing.is_set():
                return False
        return request.allowed


class ToolConfirmationDialog:
    def __init__(self, parent: tk.Tk, request: ConfirmationRequest) -> None:
        self.allowed = False
        self._window = tk.Toplevel(parent)
        self._window.title("确认工具调用")
        self._window.geometry("720x540")
        self._window.minsize(600, 440)
        self._window.configure(background=APP_BACKGROUND)
        self._window.transient(parent)
        self._window.grab_set()
        self._window.protocol("WM_DELETE_WINDOW", self._deny)
        self._window.bind("<Escape>", lambda _event: self._deny())

        container = ttk.Frame(
            self._window,
            style="App.TFrame",
            padding=(20, 20, 20, 72),
        )
        container.pack(fill="both", expand=True)

        actions = ttk.Frame(self._window, style="App.TFrame")
        deny_button = ttk.Button(
            actions,
            text="拒绝",
            command=self._deny,
            style="Secondary.TButton",
        )
        deny_button.pack(side="right")
        ttk.Button(
            actions,
            text="允许执行",
            command=self._allow,
            style="Accent.TButton",
        ).pack(side="right", padx=(0, 8))
        actions.place(
            relx=0,
            rely=1,
            x=20,
            y=-20,
            relwidth=1,
            width=-40,
            anchor="sw",
        )

        content = ttk.Frame(container, style="App.TFrame")
        content.pack(side="top", fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(3, weight=1)
        ttk.Label(
            content,
            text="此操作需要您的确认",
            style="DialogTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            content,
            text=f"{request.tool.name}  ·  {request.tool.description}",
            style="Body.TLabel",
            wraplength=660,
        ).grid(row=1, column=0, sticky="ew", pady=(6, 14))

        ttk.Label(content, text="调用详情", style="Section.TLabel").grid(
            row=2,
            column=0,
            sticky="w",
            pady=(0, 6),
        )
        editor_frame = tk.Frame(
            content,
            background=EDITOR_BACKGROUND,
            highlightbackground=EDITOR_BORDER,
            highlightthickness=1,
            borderwidth=0,
        )
        editor_frame.grid(row=3, column=0, sticky="nsew")
        arguments_view = EditorTabs(
            editor_frame,
            editor_font_size=11,
            tab_font_size=9,
        )
        arguments_view.pack(fill="both", expand=True)
        arguments_view.add_or_update(
            "arguments",
            "完整参数",
            request.arguments,
            "json",
        )
        for key, title, value, language in _confirmation_content_previews(
            request.arguments
        ):
            arguments_view.add_or_update(
                key,
                title.replace("预览", "源码")
                if language == "markdown"
                else title,
                value,
                language,
            )
            if language == "markdown":
                arguments_view.add_widget(
                    f"rendered_{key}",
                    "渲染预览",
                    "MARKDOWN",
                    lambda parent, markdown=value: RenderedMarkdownPreview(
                        parent,
                        markdown,
                    ),
                    select=True,
                )

        self._window.update_idletasks()
        x = parent.winfo_rootx() + max(
            0, (parent.winfo_width() - self._window.winfo_width()) // 2
        )
        y = parent.winfo_rooty() + max(
            0, (parent.winfo_height() - self._window.winfo_height()) // 2
        )
        self._window.geometry(f"+{x}+{y}")
        deny_button.focus_set()
        self._window.wait_window()

    def _allow(self) -> None:
        self.allowed = True
        self._window.destroy()

    def _deny(self) -> None:
        self.allowed = False
        self._window.destroy()


EXTENSION_LANGUAGES = {
    ".py": "python",
    ".json": "json",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
    ".sql": "sql",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "shell",
    ".ps1": "powershell",
}


def _content_language_hint(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return "text"
    if not isinstance(parsed, dict):
        return "text"
    path = parsed.get("path")
    if not isinstance(path, str):
        return "text"
    return EXTENSION_LANGUAGES.get(Path(path).suffix.lower(), "text")


def _confirmation_content_previews(
    arguments: str,
) -> list[tuple[str, str, str, str]]:
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []

    titles = {
        "content": "内容预览",
        "text": "文本预览",
        "old_text": "原文本",
        "new_text": "新文本",
        "replacement": "替换内容",
    }
    language_hint = _content_language_hint(arguments)
    previews: list[tuple[str, str, str, str]] = []
    for field, title in titles.items():
        value = parsed.get(field)
        if not isinstance(value, str) or not value:
            continue
        previews.append(
            (
                f"preview_{field}",
                title,
                value,
                language_hint if field != "text" else "text",
            )
        )
    return previews[:4]


def _format_editor_content(value: str, language_hint: str = "text") -> tuple[str, str]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value, language_hint
    if isinstance(parsed, (dict, list)):
        return json.dumps(parsed, ensure_ascii=False, indent=2), "json"
    return value, language_hint


def _conversation_preview(value: str, limit: int = 28) -> str:
    compact = " ".join(value.split()) or "（空问题）"
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)] + "…"


def _coalesce_ui_events(
    events: list[tuple[str, Any]],
) -> list[tuple[str, Any]]:
    combined: list[tuple[str, Any]] = []
    for event_name, payload in events:
        if event_name == "text" and combined and combined[-1][0] == "text":
            previous_name, previous_payload = combined[-1]
            combined[-1] = (
                previous_name,
                str(previous_payload) + str(payload),
            )
        else:
            combined.append((event_name, payload))
    return combined


def _dequeue_ui_events(
    event_queue: queue.Queue[tuple[str, Any]],
    limit: int = 256,
) -> list[tuple[str, Any]]:
    """Take a bounded batch so a fast stream cannot starve Tk's event loop."""
    events: list[tuple[str, Any]] = []
    for _index in range(max(1, limit)):
        try:
            events.append(event_queue.get_nowait())
        except queue.Empty:
            break
    return events


def _messages_after_last_user(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only the model/tool messages belonging to the latest turn."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return messages[index + 1 :]
    return messages


def _format_activity_status(phase: str, elapsed: float, characters: int) -> str:
    details = [phase, f"{max(0.0, elapsed):.1f}s"]
    if characters:
        details.append(f"{characters} 字符")
    return "  ·  ".join(details)


def _filter_sessions_by_title(
    sessions: list[Session],
    query: str,
) -> list[Session]:
    normalized = query.strip().casefold()
    if not normalized:
        return sessions
    return [session for session in sessions if normalized in session.title.casefold()]


def _split_emoji_spans(value: str) -> list[tuple[str, bool]]:
    """Split text while keeping joined emoji sequences in one span."""

    def is_emoji_base(character: str) -> bool:
        codepoint = ord(character)
        return (
            0x1F000 <= codepoint <= 0x1FAFF
            or 0x1FC00 <= codepoint <= 0x1FFFD
            or 0x2300 <= codepoint <= 0x23FF
            or 0x2600 <= codepoint <= 0x27BF
            or 0x2B00 <= codepoint <= 0x2BFF
            or codepoint
            in {0x00A9, 0x00AE, 0x2122, 0x3030, 0x303D, 0x3297, 0x3299}
        )

    def consume_suffix(position: int) -> int:
        while position < len(value) and (
            ord(value[position]) in {0xFE0E, 0xFE0F, 0x20E3}
            or 0x1F3FB <= ord(value[position]) <= 0x1F3FF
        ):
            position += 1
        return position

    spans: list[tuple[str, bool]] = []
    plain_start = 0
    index = 0
    while index < len(value):
        keycap_end = index + 1
        if value[index] in "#*0123456789":
            if keycap_end < len(value) and ord(value[keycap_end]) == 0xFE0F:
                keycap_end += 1
            is_keycap = (
                keycap_end < len(value) and ord(value[keycap_end]) == 0x20E3
            )
        else:
            is_keycap = False

        if not is_keycap and not is_emoji_base(value[index]):
            index += 1
            continue

        if plain_start < index:
            spans.append((value[plain_start:index], False))

        emoji_start = index
        if is_keycap:
            index = keycap_end + 1
        else:
            first_codepoint = ord(value[index])
            index = consume_suffix(index + 1)
            if (
                0x1F1E6 <= first_codepoint <= 0x1F1FF
                and index < len(value)
                and 0x1F1E6 <= ord(value[index]) <= 0x1F1FF
            ):
                index = consume_suffix(index + 1)
            while index + 1 < len(value) and ord(value[index]) == 0x200D:
                index = consume_suffix(index + 2)

        spans.append((value[emoji_start:index], True))
        plain_start = index

    if plain_start < len(value):
        spans.append((value[plain_start:], False))
    return spans or [(value, False)]


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


class RenderedMarkdownPreview(tk.Frame):
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

        self.frame = tk.Frame(
            parent,
            # 用外层背景形成稳定的 1px 边框，避免 highlightthickness 在
            # Windows DPI 缩放下挤占内容区域或覆盖最后一行。
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
        if self._height_job is not None:
            try:
                self.frame.after_cancel(self._height_job)
            except tk.TclError:
                pass
            self._height_job = None
        self.frame.destroy()

    def _copy(self) -> None:
        self.frame.clipboard_clear()
        self.frame.clipboard_append(self._content)
        self._copy_button.configure(text="已复制")
        self.frame.after(1200, self._reset_copy_label)

    def _reset_copy_label(self) -> None:
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
        # dlineinfo 只保证返回可见行的信息；代码块位于视口外或父容器尚未
        # 完成布局时，它会返回 None，旧逻辑因此会得到不稳定的高度。
        # Text.count(..., "ypixels") 会计算所有显示行的真实像素高度，且会
        # 纳入行内不同字体的度量，再显式补上上下 padding 即可得到完整高度。
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
    """A content-sized, right-aligned user message bubble."""

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
    """Compact side navigation for conversation turns."""

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
            command=self._window.destroy,
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
        self._window.after_idle(
            lambda: self._window.attributes("-fullscreen", True)
        )

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
            self._window.destroy()
        return "break"


class ToolCallCard:
    def __init__(self, parent: tk.Text, name: str, arguments: str) -> None:
        self._chat_view = parent
        self._name = name
        self._arguments = arguments
        self._result: str | None = None
        self._result_language = _content_language_hint(arguments)
        self._expanded = False

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
        self.frame.destroy()

    def _open_fullscreen(self) -> None:
        FullscreenToolViewer(
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


class AgentApp:
    def __init__(
        self,
        root: tk.Tk,
        settings: Settings,
        logger: logging.Logger | None = None,
    ) -> None:
        self._root = root
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._closing = threading.Event()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._busy = False
        self._assistant_open = False
        self._projects: list[Project] = []
        self._project_options: list[str] = []
        self._sessions: list[Session] = []
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
        self._turn_started_at: float | None = None
        self._stream_character_count = 0
        self._activity_phase = ""
        self._activity_job: str | None = None
        self._empty_state_frame: tk.Frame | None = None
        self._session_filter_job: str | None = None
        self._active_prompt: str | None = None
        self._close_pending = False
        self._closed = False

        confirmer = TkToolConfirmer(self._events, self._closing)
        self._agent = Agent(settings, confirmer=confirmer, logger=logger)

        self._configure_window()
        self._configure_styles()
        self._build_layout()
        self._bind_shortcuts()
        self._render_history()
        self._refresh_navigation()
        self._update_header()
        self._input.focus_set()
        self._show_recovery_issues()
        self._root.after(50, self._drain_events)

    def _show_recovery_issues(self) -> None:
        """在界面完成初始化后提示用户核对上次未明确结束的工具。"""
        # 界面仍兼容没有实现运行日志接口的自定义 Agent。
        issues = getattr(self._agent, "recovery_issues", ())
        if not issues:
            return
        details = "\n\n".join(issue.message for issue in issues[:5])
        if len(issues) > 5:
            details += f"\n\n另外还有 {len(issues) - 5} 条结果未知记录。"
        messagebox.showwarning(
            "需要检查上次运行",
            details,
            parent=self._root,
        )

    def _configure_window(self) -> None:
        self._root.title("DeepSeek Agent")
        self._root.geometry("1180x780")
        self._root.minsize(820, 600)
        self._root.configure(background=APP_BACKGROUND)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self._root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        default_font = ("Microsoft YaHei UI", 10)
        style.configure(".", font=default_font)
        style.configure("App.TFrame", background=APP_BACKGROUND)
        style.configure("Card.TFrame", background=CARD_BACKGROUND)
        style.configure("Sidebar.TFrame", background=SIDEBAR_BACKGROUND)
        style.configure(
            "Title.TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 15, "bold"),
        )
        style.configure(
            "DialogTitle.TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 15, "bold"),
        )
        style.configure(
            "Section.TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "Body.TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT_SECONDARY,
        )
        style.configure(
            "CardTitle.TLabel",
            background=CARD_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        style.configure(
            "CardMuted.TLabel",
            background=CARD_BACKGROUND,
            foreground=TEXT_SECONDARY,
        )
        style.configure(
            "SidebarTitle.TLabel",
            background=SIDEBAR_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 14, "bold"),
        )
        style.configure(
            "SidebarMuted.TLabel",
            background=SIDEBAR_BACKGROUND,
            foreground=TEXT_SECONDARY,
        )
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#FFFFFF",
            borderwidth=0,
            padding=(14, 8),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_HOVER), ("disabled", "#A5B4D8")],
            foreground=[("disabled", "#EEF2FF")],
        )
        style.configure(
            "Secondary.TButton",
            background="#E8EDF5",
            foreground=TEXT_PRIMARY,
            borderwidth=0,
            padding=(12, 7),
        )
        style.map("Secondary.TButton", background=[("active", "#DCE3EE")])
        style.configure(
            "Header.TButton",
            background="#E8EDF5",
            foreground=TEXT_PRIMARY,
            borderwidth=0,
            padding=(5, 3),
        )
        style.map("Header.TButton", background=[("active", "#DCE3EE")])
        style.configure(
            "Danger.TButton",
            background="#FCE8EC",
            foreground=DANGER,
            borderwidth=0,
            padding=(12, 7),
        )
        style.map("Danger.TButton", background=[("active", "#F8D4DC")])
        style.configure(
            "Sidebar.TButton",
            background=SIDEBAR_PANEL,
            foreground=SIDEBAR_TEXT,
            borderwidth=0,
            padding=(10, 7),
        )
        style.map(
            "Sidebar.TButton",
            background=[("active", "#DFE2E8"), ("disabled", "#F2F3F5")],
            foreground=[("disabled", "#A0A5AF")],
        )
        style.configure(
            "Sessions.Treeview",
            background=SIDEBAR_BACKGROUND,
            fieldbackground=SIDEBAR_BACKGROUND,
            foreground=SIDEBAR_TEXT,
            borderwidth=0,
            rowheight=34,
        )
        style.map(
            "Sessions.Treeview",
            background=[("selected", "#E3E6EC")],
            foreground=[("selected", TEXT_PRIMARY)],
        )
        style.configure(
            "Projects.Treeview",
            background=SIDEBAR_BACKGROUND,
            fieldbackground=SIDEBAR_BACKGROUND,
            foreground=SIDEBAR_TEXT,
            borderwidth=0,
            rowheight=30,
        )
        style.map(
            "Projects.Treeview",
            background=[("selected", "#E3E6EC")],
            foreground=[("selected", TEXT_PRIMARY)],
        )

    def _build_layout(self) -> None:
        shell = ttk.Frame(self._root, style="App.TFrame")
        shell.pack(fill="both", expand=True)

        self._paned = tk.PanedWindow(
            shell,
            orient="horizontal",
            background="#D9DCE2",
            borderwidth=0,
            sashwidth=5,
            sashrelief="flat",
            showhandle=False,
        )
        self._paned.pack(fill="both", expand=True)

        self._sidebar = ttk.Frame(self._paned, style="Sidebar.TFrame")
        self._main_panel = ttk.Frame(
            self._paned,
            style="App.TFrame",
            padding=(20, 16, 20, 14),
        )
        self._paned.add(self._sidebar, minsize=190, width=240, stretch="never")
        self._paned.add(self._main_panel, minsize=520, stretch="always")
        self._sidebar_visible = True

        self._build_sidebar(self._sidebar)
        self._main_panel.columnconfigure(0, weight=1)
        self._main_panel.rowconfigure(1, weight=1)
        self._build_header(self._main_panel)
        self._build_chat(self._main_panel)
        self._build_input(self._main_panel)
        self._root.after_idle(lambda: self._paned.sash_place(0, 240, 0))

    def _build_sidebar(self, sidebar: ttk.Frame) -> None:
        brand = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(16, 16, 16, 8))
        brand.pack(fill="x")
        ttk.Label(
            brand,
            text="DeepSeek Agent",
            style="SidebarTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            brand,
            text="Agent 工作区",
            style="SidebarMuted.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        self._new_session_button = ttk.Button(
            sidebar,
            text="＋  新建会话",
            command=self._new_session,
            style="Sidebar.TButton",
        )
        self._new_session_button.pack(fill="x", padx=12, pady=(4, 12))

        project_header = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(16, 4))
        project_header.pack(fill="x")
        ttk.Label(
            project_header,
            text="项目",
            style="SidebarMuted.TLabel",
        ).pack(side="left")
        self._create_project_button = ttk.Button(
            project_header,
            text="＋",
            command=self._create_project,
            style="Sidebar.TButton",
            width=3,
        )
        self._create_project_button.pack(side="right")

        project_frame = ttk.Frame(sidebar, style="Sidebar.TFrame")
        project_frame.pack(fill="x", padx=(10, 5), pady=(0, 10))
        self._project_tree = ttk.Treeview(
            project_frame,
            show="tree",
            selectmode="browse",
            style="Projects.Treeview",
            height=5,
        )
        project_scroll_y = ttk.Scrollbar(
            project_frame,
            orient="vertical",
            command=self._project_tree.yview,
        )
        self._project_tree.configure(
            yscrollcommand=project_scroll_y.set,
        )
        self._project_tree.column("#0", width=180, minwidth=100, stretch=True)
        self._project_tree.grid(row=0, column=0, sticky="nsew")
        project_scroll_y.grid(row=0, column=1, sticky="ns")
        project_frame.columnconfigure(0, weight=1)
        self._project_tree.bind("<<TreeviewSelect>>", self._on_project_selected)
        self._project_tree.bind("<Button-3>", self._show_project_menu)

        ttk.Separator(sidebar).pack(fill="x", padx=14, pady=(0, 9))
        ttk.Label(
            sidebar,
            text="会话  ·  右键管理",
            style="SidebarMuted.TLabel",
        ).pack(anchor="w", padx=16, pady=(0, 6))

        session_filter = tk.Frame(sidebar, background=SIDEBAR_BACKGROUND)
        session_filter.pack(fill="x", padx=(14, 9), pady=(0, 8))
        self._session_filter_var = tk.StringVar()
        tk.Label(
            session_filter,
            text="筛选",
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(8, 3))
        self._session_filter_entry = tk.Entry(
            session_filter,
            textvariable=self._session_filter_var,
            font=("Microsoft YaHei UI", 9),
            background="#FFFFFF",
            foreground=SIDEBAR_TEXT,
            insertbackground=SIDEBAR_TEXT,
            relief="flat",
            borderwidth=0,
        )
        self._session_filter_entry.pack(
            side="left",
            fill="x",
            expand=True,
            ipady=6,
            padx=(0, 0),
        )
        self._session_filter_entry.insert(0, "")
        self._session_filter_entry.bind(
            "<KeyRelease>",
            self._schedule_session_filter,
        )
        tk.Button(
            session_filter,
            text="×",
            command=self._clear_session_filter,
            background="#FFFFFF",
            activebackground="#EEF2F7",
            foreground=TEXT_SECONDARY,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10),
            padx=7,
            pady=3,
        ).pack(side="right")

        session_frame = ttk.Frame(sidebar, style="Sidebar.TFrame")
        session_frame.pack(fill="both", expand=True, padx=(16, 8))
        self._session_tree = ttk.Treeview(
            session_frame,
            show="tree",
            selectmode="browse",
            style="Sessions.Treeview",
            height=1,
        )
        session_scroll_y = ttk.Scrollbar(
            session_frame,
            orient="vertical",
            command=self._session_tree.yview,
        )
        self._session_tree.configure(
            yscrollcommand=session_scroll_y.set,
        )
        self._session_tree.column("#0", width=180, minwidth=100, stretch=True)
        self._session_tree.grid(row=0, column=0, sticky="nsew")
        session_scroll_y.grid(row=0, column=1, sticky="ns")
        session_frame.rowconfigure(0, weight=1)
        session_frame.columnconfigure(0, weight=1)
        self._session_tree.bind("<Double-Button-1>", self._load_selected_session)
        self._session_tree.bind("<Return>", self._load_selected_session)
        self._session_tree.bind("<Button-3>", self._show_session_menu)
        self._build_context_menus()

    def _build_header(self, main: ttk.Frame) -> None:
        header = ttk.Frame(main, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        header.columnconfigure(1, weight=1)

        self._session_title = tk.StringVar()
        self._session_project = tk.StringVar()
        ttk.Button(
            header,
            text="☰",
            command=self._toggle_sidebar,
            style="Header.TButton",
            width=3,
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))

        identity = ttk.Frame(header, style="App.TFrame")
        identity.grid(row=0, column=1, sticky="w")
        ttk.Label(
            identity,
            textvariable=self._session_title,
            style="Title.TLabel",
            wraplength=260,
        ).pack(side="left")
        ttk.Label(
            identity,
            textvariable=self._session_project,
            style="Body.TLabel",
        ).pack(side="left", padx=(8, 0))

        info = ttk.Frame(header, style="App.TFrame")
        info.grid(row=0, column=2, sticky="e")

        ttk.Button(
            info,
            text="查找",
            command=self._open_search,
            style="Header.TButton",
        ).pack(side="left", padx=(0, 9))

        model_picker = ttk.Frame(info, style="App.TFrame")
        model_picker.pack(side="left")
        ttk.Label(
            model_picker,
            text="模型",
            style="Body.TLabel",
        ).pack(side="left", padx=(0, 5))
        self._model_selector_var = tk.StringVar(value=self._agent.model_name)
        self._model_selector = ttk.Combobox(
            model_picker,
            textvariable=self._model_selector_var,
            values=self._agent.available_models,
            state="readonly",
            width=18,
        )
        self._model_selector.pack(side="left")
        self._model_selector.bind(
            "<<ComboboxSelected>>",
            self._change_model,
        )

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

    def _build_input(self, main: ttk.Frame) -> None:
        composer = ttk.Frame(main, style="Card.TFrame", padding=(12, 9))
        self._composer = composer
        composer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        composer.columnconfigure(0, weight=1)
        composer.bind("<Configure>", self._update_composer_layout)

        self._input = tk.Text(
            composer,
            width=1,
            height=2,
            wrap="word",
            font=("Microsoft YaHei UI", 11),
            background="#F8FAFC",
            foreground=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            highlightbackground=EDITOR_BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            padx=10,
            pady=7,
            undo=True,
        )
        self._input.grid(row=0, column=0, columnspan=3, sticky="ew")
        self._input.bind("<Return>", self._handle_return)
        self._input.bind("<KP_Enter>", self._handle_return)
        self._input.bind("<<Modified>>", self._resize_input)
        self._input.edit_modified(False)

        self._status = tk.StringVar(value="就绪")
        ttk.Label(
            composer,
            textvariable=self._status,
            style="CardMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self._shortcut_hint = ttk.Label(
            composer,
            text="Enter 发送  ·  Shift+Enter 换行  ·  Ctrl+F 查找",
            style="CardMuted.TLabel",
        )
        self._shortcut_hint.grid(
            row=1,
            column=1,
            sticky="e",
            padx=12,
            pady=(10, 0),
        )

        self._stop_button = ttk.Button(
            composer,
            text="停止生成",
            command=self._stop,
            state="disabled",
            style="Danger.TButton",
        )
        self._stop_button.grid(row=1, column=2, sticky="e", pady=(8, 0))
        self._send_button = ttk.Button(
            composer,
            text="发送",
            command=self._send,
            style="Accent.TButton",
        )
        self._send_button.grid(row=1, column=3, sticky="e", padx=(8, 0), pady=(8, 0))
        self._clear_button = ttk.Button(
            composer,
            text="清空对话",
            command=self._clear_conversation,
            style="Secondary.TButton",
        )
        self._clear_button.grid(row=0, column=3, sticky="ne", padx=(10, 0))

    def _update_composer_layout(self, event: tk.Event) -> None:
        if event.width < 720:
            self._shortcut_hint.grid_remove()
        elif not self._shortcut_hint.winfo_manager():
            self._shortcut_hint.grid()

    def _bind_shortcuts(self) -> None:
        self._root.bind("<Control-l>", self._focus_composer)
        self._root.bind("<Control-f>", self._open_search)
        self._root.bind("<Control-Shift-F>", self._focus_session_filter)
        self._root.bind("<Control-Down>", self._shortcut_scroll_to_bottom)
        self._root.bind("<Control-n>", self._shortcut_new_session)
        self._root.bind("<F3>", self._search_next)
        self._root.bind("<Shift-F3>", self._search_previous)
        self._root.bind("<Alt-Up>", self._shortcut_previous_turn)
        self._root.bind("<Alt-Down>", self._shortcut_next_turn)
        self._root.bind("<Escape>", self._handle_escape)

    def _focus_composer(self, _event: tk.Event | None = None) -> str:
        self._input.focus_set()
        return "break"

    def _focus_session_filter(self, _event: tk.Event | None = None) -> str:
        self._session_filter_entry.focus_set()
        self._session_filter_entry.selection_range(0, "end")
        return "break"

    def _schedule_session_filter(self, _event: tk.Event | None = None) -> None:
        if self._session_filter_job is not None:
            self._root.after_cancel(self._session_filter_job)
        self._session_filter_job = self._root.after(
            120,
            self._apply_session_filter,
        )

    def _apply_session_filter(self) -> None:
        self._session_filter_job = None
        self._refresh_sessions()

    def _clear_session_filter(self) -> None:
        if not self._session_filter_var.get():
            return
        self._session_filter_var.set("")
        self._refresh_sessions()

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
        self._input.focus_set()
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

    def _shortcut_scroll_to_bottom(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        self._scroll_to_bottom()
        return "break"

    def _shortcut_new_session(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        if not self._is_busy():
            self._new_session()
        return "break"

    def _shortcut_previous_turn(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        if self._turn_marks:
            current = self._selected_turn_index or len(self._turn_marks)
            self._jump_to_turn(max(1, current - 1))
        return "break"

    def _shortcut_next_turn(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        if self._turn_marks:
            current = self._selected_turn_index or 1
            self._jump_to_turn(min(len(self._turn_marks), current + 1))
        return "break"

    def _handle_escape(self, _event: tk.Event | None = None) -> str:
        if self._search_panel.place_info():
            return self._close_search()
        if self._is_busy():
            self._stop()
        else:
            self._input.focus_set()
        return "break"

    def _handle_return(self, event: tk.Event) -> str | None:
        if event.state & 0x0001:
            return None
        self._send()
        return "break"

    def _resize_input(self, _event: tk.Event | None = None) -> None:
        if not self._input.edit_modified():
            return
        display_count = self._input.count(
            "1.0",
            "end-1c",
            "displaylines",
        )
        line_count = display_count[0] if display_count else 1
        self._input.configure(height=min(6, max(2, line_count)))
        self._input.edit_modified(False)

    def _change_model(self, _event: tk.Event | None = None) -> None:
        previous_model = self._agent.model_name
        selected_model = self._model_selector_var.get().strip()
        if self._is_busy() or selected_model == previous_model:
            self._model_selector_var.set(previous_model)
            return
        try:
            self._agent.select_model(selected_model)
        except Exception as error:
            self._model_selector_var.set(previous_model)
            messagebox.showerror("切换失败", str(error), parent=self._root)
            return
        self._status.set(f"已切换到 {selected_model}，将在下一轮对话中使用")

    def _send(self) -> None:
        if self._is_busy():
            return
        prompt = self._input.get("1.0", "end-1c").strip()
        if not prompt:
            return
        if self._search_panel.place_info():
            self._close_search()
        self._remove_empty_state()

        self._input.delete("1.0", "end")
        self._active_prompt = prompt
        self._register_turn(prompt, select=True)
        self._append_message("您", prompt, "user")
        self._begin_live_response()
        self._assistant_open = False
        self._cancel.clear()
        self._set_busy(True)
        self._start_activity()

        self._worker = threading.Thread(
            target=self._run_chat,
            args=(prompt,),
            daemon=False,
        )
        self._worker.start()

    def _begin_live_response(self) -> None:
        self._clear_live_response_tracking()
        self._chat_view.mark_set(self._live_response_mark, "end-1c")
        self._chat_view.mark_gravity(self._live_response_mark, "left")
        self._live_tool_start = len(self._tool_card_widgets)

    def _run_chat(self, prompt: str) -> None:
        try:
            outcome = self._agent.chat(
                prompt,
                on_text=lambda content: self._events.put(("text", content)),
                on_tool_call=lambda request: self._events.put(
                    ("tool_call", request)
                ),
                on_tool_result=lambda request, result: self._events.put(
                    ("tool_result", (request, result))
                ),
                should_cancel=self._cancel.is_set,
            )
        except AgentCancelledError as error:
            outcome = error.outcome
            self._events.put(
                (
                    "cancelled",
                    (
                        str(error),
                        (
                            outcome.history_preserved
                            if outcome is not None
                            else error.tool_records_preserved
                        ),
                    ),
                )
            )
        except Exception as error:
            outcome = self._agent.last_turn_outcome
            self._events.put(
                (
                    "error",
                    (
                        str(error),
                        (
                            outcome.history_preserved
                            if outcome is not None
                            else self._agent.last_turn_history_preserved
                        ),
                    ),
                )
            )
        else:
            if outcome.status is RunStatus.STEP_LIMIT_REACHED:
                self._events.put(("step_limit_reached", outcome))
            else:
                self._events.put(("done", outcome))

    def _stop(self) -> None:
        if self._is_busy():
            self._cancel.set()
            self._set_activity_phase("正在停止")
            self._stop_button.configure(state="disabled")

    def _drain_events(self) -> None:
        if self._closed:
            return
        pending_events = _dequeue_ui_events(self._events)
        for event_name, payload in _coalesce_ui_events(pending_events):
            self._handle_event(event_name, payload)
        if self._close_pending and not self._busy:
            if self._worker is not None and self._worker.is_alive():
                self._root.after(20, self._drain_events)
            else:
                self._finalize_close()
                if not self._closed:
                    self._root.after(50, self._drain_events)
            return
        # 流式输出期间提高刷新频率；单次批量有上限，避免大量 token 或工具
        # 事件占满主线程，确保滚动、停止按钮和窗口拖动仍能及时响应。
        next_delay = 8 if not self._events.empty() else 20 if self._busy else 50
        self._root.after(next_delay, self._drain_events)

    def _handle_event(self, event_name: str, payload: Any) -> None:
        if event_name == "text":
            starts_new_message = not self._assistant_open
            if not self._assistant_open:
                self._append("DeepSeek\n", "assistant_header")
                self._assistant_open = True
            content = str(payload)
            self._stream_character_count += len(content)
            self._append_turn_answer_preview(
                content,
                starts_new_message=starts_new_message,
            )
            self._append(content, "assistant_body")
            return

        if event_name == "tool_call":
            request: ToolCallRequest = payload
            self._finish_assistant_line()
            self._create_tool_card(
                request.id,
                request.name,
                request.arguments,
            )
            self._set_activity_phase(f"执行工具 {request.name}")
            return

        if event_name == "tool_result":
            request, result = payload
            self._complete_tool_card(
                request.id,
                request.name,
                request.arguments,
                result,
            )
            self._set_activity_phase("继续生成")
            return

        if event_name == "confirmation":
            self._set_activity_phase("等待确认")
            self._show_confirmation(payload)
            if self._is_busy():
                self._set_activity_phase("继续生成")
            return

        if event_name == "done":
            restore_turn = (
                self._selected_turn_index
                if not self._is_chat_at_bottom()
                else None
            )
            self._finish_turn("就绪")
            if not self._render_completed_turn():
                self._render_history()
            self._refresh_navigation(select_session_id=self._agent.session_id)
            self._update_header()
            self._active_prompt = None
            if restore_turn is not None:
                self._root.after_idle(
                    lambda turn=restore_turn: self._jump_to_turn(turn)
                )
            return

        if event_name == "step_limit_reached":
            outcome: TurnOutcome = payload
            self._finish_assistant_line()
            self._finish_turn("未完成")
            if not self._render_completed_turn():
                self._render_history()
            self._refresh_navigation(select_session_id=self._agent.session_id)
            self._update_header()
            self._active_prompt = None
            self._status.set(
                f"已达到执行步数上限，共执行 {outcome.steps_completed} 步。"
            )
            return

        if event_name == "cancelled":
            message, tool_records_preserved = payload
            self._finish_assistant_line()
            self._finish_turn("已停止")
            self._reconcile_interrupted_turn(tool_records_preserved)
            if not tool_records_preserved:
                self._status.set("已停止，问题已放回输入框")
            return

        if event_name == "error":
            message, history_preserved = payload
            self._finish_assistant_line()
            self._finish_turn("调用失败")
            self._reconcile_interrupted_turn(history_preserved)
            summary = _conversation_preview(str(message), limit=52)
            self._status.set(f"调用失败：{summary}")

    def _reconcile_interrupted_turn(self, history_preserved: bool) -> None:
        prompt = self._active_prompt
        if history_preserved:
            if not self._render_completed_turn():
                self._render_history()
        else:
            self._render_history()
            self._restore_prompt_to_composer(prompt)
        self._refresh_navigation(select_session_id=self._agent.session_id)
        self._update_header()
        self._active_prompt = None

    def _restore_prompt_to_composer(self, prompt: str | None) -> None:
        if not prompt:
            return
        draft = self._input.get("1.0", "end-1c")
        if prompt in draft:
            return
        restored = prompt if not draft.strip() else f"{prompt}\n\n{draft}"
        self._input.delete("1.0", "end")
        self._input.insert("1.0", restored)
        self._input.edit_modified(True)
        self._resize_input()
        self._input.mark_set("insert", "end-1c")

    def _show_confirmation(self, request: ConfirmationRequest) -> None:
        dialog = ToolConfirmationDialog(self._root, request)
        request.allowed = dialog.allowed
        request.completed.set()

    def _finish_assistant_line(self) -> None:
        if self._assistant_open:
            self._append("\n", "assistant_body")
            self._assistant_open = False

    def _finish_turn(self, status: str) -> None:
        self._finish_assistant_line()
        self._stop_activity()
        self._set_busy(False)
        self._status.set(status)
        self._input.focus_set()

    def _start_activity(self) -> None:
        self._turn_started_at = time.perf_counter()
        self._stream_character_count = 0
        self._activity_phase = "正在生成"
        self._refresh_activity_status()

    def _set_activity_phase(self, phase: str) -> None:
        self._activity_phase = phase
        self._refresh_activity_status()

    def _refresh_activity_status(self) -> None:
        if self._activity_job is not None:
            self._root.after_cancel(self._activity_job)
            self._activity_job = None
        if self._turn_started_at is None or not self._busy:
            return
        elapsed = time.perf_counter() - self._turn_started_at
        self._status.set(
            _format_activity_status(
                self._activity_phase,
                elapsed,
                self._stream_character_count,
            )
        )
        self._activity_job = self._root.after(
            250,
            self._refresh_activity_status,
        )

    def _stop_activity(self) -> None:
        if self._activity_job is not None:
            self._root.after_cancel(self._activity_job)
            self._activity_job = None
        self._turn_started_at = None
        self._activity_phase = ""

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        interaction_locked = busy or self._close_pending
        normal_or_disabled = "disabled" if interaction_locked else "normal"
        self._send_button.configure(state=normal_or_disabled)
        self._stop_button.configure(
            state="normal" if busy and not self._close_pending else "disabled"
        )
        self._clear_button.configure(state=normal_or_disabled)
        self._model_selector.configure(
            state="disabled" if interaction_locked else "readonly"
        )
        for button in (self._create_project_button, self._new_session_button):
            button.configure(state=normal_or_disabled)
        self._project_tree.configure(
            selectmode="none" if interaction_locked else "browse"
        )
        self._session_tree.configure(
            selectmode="none" if interaction_locked else "browse"
        )
        self._session_filter_entry.configure(
            state="disabled" if interaction_locked else "normal"
        )

    def _is_busy(self) -> bool:
        return self._busy

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
        self._input.focus_set()

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
        if self._is_busy():
            return
        self._input.delete("1.0", "end")
        self._input.insert("1.0", prompt)
        self._input.edit_modified(True)
        self._resize_input()
        self._input.mark_set("insert", "end-1c")
        self._input.focus_set()

    def _clear_live_response_tracking(self) -> None:
        if self._live_response_mark in self._chat_view.mark_names():
            self._chat_view.mark_unset(self._live_response_mark)
        self._live_tool_start = None

    def _render_completed_turn(self) -> bool:
        if (
            self._live_tool_start is None
            or self._live_response_mark not in self._chat_view.mark_names()
        ):
            return False

        tail = _messages_after_last_user(self._agent.history())
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
        # 保留 Text 控件最后一个换行。若删除到 end，Tk 会把用户气泡后的
        # 分隔行一并折叠，最终的助手标题就会被插回气泡所在显示行。
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
        self._assistant_open = False
        return True

    def _render_history(self) -> None:
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
            for message in self._agent.history():
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
        self._assistant_open = False

    def _refresh_navigation(
        self,
        *,
        select_project_id: str | None = None,
        select_session_id: str | None = None,
    ) -> None:
        previous_project = select_project_id or self._selected_project_id()
        self._projects = self._agent.list_projects()
        all_sessions = self._agent.list_sessions()

        counts: dict[str | None, int] = {None: 0}
        for session in all_sessions:
            counts[session.project_id] = counts.get(session.project_id, 0) + 1
        self._project_options = [
            ALL_PROJECTS,
            UNASSIGNED_PROJECT,
            *[project.id for project in self._projects],
        ]
        if previous_project not in self._project_options:
            previous_project = ALL_PROJECTS

        for item in self._project_tree.get_children():
            self._project_tree.delete(item)
        self._project_tree.insert(
            "",
            "end",
            iid=ALL_PROJECTS,
            text=f"全部会话  ({len(all_sessions)})",
        )
        self._project_tree.insert(
            "",
            "end",
            iid=UNASSIGNED_PROJECT,
            text=f"未分类  ({counts.get(None, 0)})",
        )
        for project in self._projects:
            self._project_tree.insert(
                "",
                "end",
                iid=project.id,
                text=f"{project.name}  ({counts.get(project.id, 0)})",
            )
        self._project_tree.selection_set(previous_project)
        self._project_tree.focus(previous_project)
        self._project_tree.see(previous_project)
        self._refresh_sessions(all_sessions, select_session_id)

    def _refresh_sessions(
        self,
        all_sessions: list[Session] | None = None,
        select_session_id: str | None = None,
    ) -> None:
        all_sessions = all_sessions or self._agent.list_sessions()
        project_id = self._selected_project_id()
        if project_id == ALL_PROJECTS:
            self._sessions = all_sessions
        elif project_id == UNASSIGNED_PROJECT:
            self._sessions = [item for item in all_sessions if item.project_id is None]
        else:
            self._sessions = [
                item for item in all_sessions if item.project_id == project_id
            ]
        self._sessions = _filter_sessions_by_title(
            self._sessions,
            self._session_filter_var.get(),
        )

        for item in self._session_tree.get_children():
            self._session_tree.delete(item)
        selected_id = select_session_id or self._agent.session_id
        for session in self._sessions:
            marker = "● " if session.id == self._agent.session_id else ""
            self._session_tree.insert(
                "",
                "end",
                iid=session.id,
                text=f"{marker}{session.title}",
            )
        if selected_id in {session.id for session in self._sessions}:
            self._session_tree.selection_set(selected_id)
            self._session_tree.focus(selected_id)
            self._session_tree.see(selected_id)

    def _selected_project_id(self) -> str:
        selected = self._project_tree.selection()
        return selected[0] if selected else ALL_PROJECTS

    def _selected_session(self) -> Session | None:
        selected = self._session_tree.selection()
        if not selected:
            return None
        session_id = selected[0]
        return next((item for item in self._sessions if item.id == session_id), None)

    def _on_project_selected(self, _event: tk.Event | None = None) -> None:
        self._refresh_sessions()

    def _build_context_menus(self) -> None:
        self._project_menu = tk.Menu(self._root, tearoff=False)
        self._project_menu.add_command(label="新建项目", command=self._create_project)
        self._project_menu.add_separator()
        self._project_menu.add_command(
            label="重命名项目",
            command=self._rename_selected_project,
        )
        self._project_menu.add_command(
            label="删除项目",
            command=self._delete_selected_project,
        )

        self._move_menu = tk.Menu(self._root, tearoff=False)
        self._session_menu = tk.Menu(self._root, tearoff=False)
        self._session_menu.add_command(label="新建会话", command=self._new_session)
        self._session_menu.add_separator()
        self._session_menu.add_command(
            label="打开会话",
            command=self._load_selected_session,
        )
        self._session_menu.add_command(
            label="重命名会话",
            command=self._rename_selected_session,
        )
        self._session_menu.add_cascade(label="移动到项目", menu=self._move_menu)
        self._session_menu.add_separator()
        self._session_menu.add_command(
            label="删除会话",
            command=self._delete_selected_session,
        )

    def _show_project_menu(self, event: tk.Event) -> None:
        if self._is_busy():
            return
        item = self._project_tree.identify_row(event.y)
        if item:
            self._project_tree.selection_set(item)
            self._project_tree.focus(item)
        project_id = self._selected_project_id()
        state = (
            "normal"
            if project_id not in {ALL_PROJECTS, UNASSIGNED_PROJECT}
            else "disabled"
        )
        self._project_menu.entryconfigure("重命名项目", state=state)
        self._project_menu.entryconfigure("删除项目", state=state)
        try:
            self._project_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._project_menu.grab_release()

    def _show_session_menu(self, event: tk.Event) -> None:
        if self._is_busy():
            return
        item = self._session_tree.identify_row(event.y)
        if item:
            self._session_tree.selection_set(item)
            self._session_tree.focus(item)
        session = self._selected_session()
        state = "normal" if session is not None else "disabled"
        for label in ("打开会话", "重命名会话", "移动到项目", "删除会话"):
            self._session_menu.entryconfigure(label, state=state)

        self._move_menu.delete(0, "end")
        self._move_menu.add_command(
            label="未分类",
            command=lambda: self._move_session_to_project(None),
        )
        if self._projects:
            self._move_menu.add_separator()
        for project in self._projects:
            self._move_menu.add_command(
                label=project.name,
                command=lambda project_id=project.id: self._move_session_to_project(
                    project_id
                ),
            )
        try:
            self._session_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._session_menu.grab_release()

    def _create_project(self) -> None:
        if self._is_busy():
            return
        name = simpledialog.askstring(
            "创建项目",
            "项目名称：",
            parent=self._root,
        )
        if name is None:
            return
        try:
            project = self._agent.create_project(name)
        except Exception as error:
            messagebox.showerror("创建失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_project_id=project.id)
        self._status.set(f"已创建项目：{project.name}")

    def _rename_selected_project(self) -> None:
        if self._is_busy():
            return
        project = self._selected_project()
        if project is None:
            return
        name = simpledialog.askstring(
            "重命名项目",
            "新的项目名称：",
            initialvalue=project.name,
            parent=self._root,
        )
        if name is None:
            return
        try:
            renamed = self._agent.rename_project(project.id, name)
        except Exception as error:
            messagebox.showerror("重命名失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_project_id=renamed.id)
        self._update_header()
        self._status.set(f"项目已重命名为：{renamed.name}")

    def _delete_selected_project(self) -> None:
        if self._is_busy():
            return
        project = self._selected_project()
        if project is None:
            return
        session_count = sum(
            1 for session in self._agent.list_sessions() if session.project_id == project.id
        )
        if not messagebox.askyesno(
            "删除项目",
            f"确定删除项目“{project.name}”吗？\n"
            f"其中的 {session_count} 个会话会移动到“未分类”，不会被删除。",
            parent=self._root,
        ):
            return
        try:
            self._agent.delete_project(project.id)
        except Exception as error:
            messagebox.showerror("删除失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_project_id=UNASSIGNED_PROJECT)
        self._update_header()
        self._status.set(f"项目已删除：{project.name}")

    def _selected_project(self) -> Project | None:
        project_id = self._selected_project_id()
        return next(
            (project for project in self._projects if project.id == project_id),
            None,
        )

    def _new_session(self) -> None:
        if self._is_busy():
            return
        try:
            project_id = self._selected_project_id()
            target_project_id = (
                project_id
                if project_id not in {ALL_PROJECTS, UNASSIGNED_PROJECT}
                else None
            )
            session = self._agent.start_new_session(target_project_id)
        except Exception as error:
            messagebox.showerror("创建失败", str(error), parent=self._root)
            return
        self._session_filter_var.set("")
        self._render_history()
        self._refresh_navigation(select_session_id=session.id)
        self._update_header()
        self._status.set("已创建新会话")

    def _load_selected_session(self, _event: tk.Event | None = None) -> None:
        if self._is_busy():
            return
        session = self._selected_session()
        if session is None or session.id == self._agent.session_id:
            return
        try:
            self._agent.load_session(session.id)
        except Exception as error:
            messagebox.showerror("加载失败", str(error), parent=self._root)
            return
        self._render_history()
        self._refresh_navigation(select_session_id=session.id)
        self._update_header()
        self._status.set(f"已加载会话：{session.title}")

    def _rename_selected_session(self) -> None:
        if self._is_busy():
            return
        session = self._selected_session()
        if session is None:
            messagebox.showinfo("重命名会话", "请先选择一个会话。", parent=self._root)
            return
        title = simpledialog.askstring(
            "重命名会话",
            "新的会话名称：",
            initialvalue=session.title,
            parent=self._root,
        )
        if title is None:
            return
        try:
            renamed = self._agent.rename_session(session.id, title)
        except Exception as error:
            messagebox.showerror("重命名失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_session_id=renamed.id)
        self._update_header()
        self._status.set(f"会话已重命名为：{renamed.title}")

    def _move_session_to_project(self, project_id: str | None) -> None:
        if self._is_busy():
            return
        session = self._selected_session()
        if session is None:
            return
        if project_id == session.project_id:
            return
        try:
            moved = self._agent.move_session(session.id, project_id)
        except Exception as error:
            messagebox.showerror("移动失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_session_id=moved.id)
        self._update_header()
        self._status.set("会话已移动")

    def _toggle_sidebar(self) -> None:
        if self._sidebar_visible:
            self._paned.forget(self._sidebar)
            self._sidebar_visible = False
            return
        self._paned.add(
            self._sidebar,
            before=self._main_panel,
            minsize=190,
            width=240,
            stretch="never",
        )
        self._sidebar_visible = True
        self._root.after_idle(lambda: self._paned.sash_place(0, 240, 0))

    def _delete_selected_session(self) -> None:
        if self._is_busy():
            return
        session = self._selected_session()
        if session is None:
            messagebox.showinfo("删除会话", "请先选择一个会话。", parent=self._root)
            return
        if not messagebox.askyesno(
            "删除会话",
            f"确定永久删除会话“{session.title}”吗？\n此操作无法撤销。",
            parent=self._root,
        ):
            return
        was_current = session.id == self._agent.session_id
        selected_project = self._selected_project_id()
        try:
            replacement_project_id = (
                selected_project
                if was_current
                and selected_project not in {ALL_PROJECTS, UNASSIGNED_PROJECT}
                else None
            )
            self._agent.delete_session(
                session.id,
                replacement_project_id=replacement_project_id,
            )
        except Exception as error:
            messagebox.showerror("删除失败", str(error), parent=self._root)
            return
        if was_current:
            self._render_history()
        self._refresh_navigation(select_session_id=self._agent.session_id)
        self._update_header()
        self._status.set("会话已删除")

    def _clear_conversation(self) -> None:
        if self._is_busy():
            return
        if not messagebox.askyesno(
            "清空对话",
            "确定清空当前会话的全部消息吗？\n会话本身和项目归属会保留。",
            parent=self._root,
        ):
            return
        try:
            self._agent.clear_conversation()
        except Exception as error:
            messagebox.showerror("清空失败", str(error), parent=self._root)
            return
        self._render_history()
        self._refresh_navigation(select_session_id=self._agent.session_id)
        self._status.set("当前对话已清空")

    def _update_header(self) -> None:
        sessions = self._agent.list_sessions()
        current = next(
            (session for session in sessions if session.id == self._agent.session_id),
            None,
        )
        title = current.title if current is not None else self._agent.session_title
        project_id = current.project_id if current is not None else None
        project_name = next(
            (
                project.name
                for project in self._agent.list_projects()
                if project.id == project_id
            ),
            "未分类",
        )
        self._session_title.set(title)
        self._session_project.set(project_name)

    def _on_close(self) -> None:
        if self._close_pending or self._closed:
            return
        if self._is_busy() and not messagebox.askyesno(
            "退出",
            "Agent 正在生成回答，确定停止并退出吗？",
            parent=self._root,
        ):
            return
        self._close_pending = True
        self._closing.set()
        self._cancel.set()
        self._set_busy(self._is_busy())
        if self._is_busy():
            self._set_activity_phase("正在安全退出")
            self._status.set("正在停止当前任务并保存会话…")
            return
        self._finalize_close()

    def _finalize_close(self) -> None:
        if self._closed:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        try:
            self._agent.save_session()
        except Exception as error:
            self._close_pending = False
            self._closing.clear()
            self._set_busy(False)
            self._status.set("退出失败：会话未能保存")
            messagebox.showerror(
                "退出失败",
                f"会话保存失败，窗口将保持打开：\n{error}",
                parent=self._root,
            )
            return
        self._closed = True
        self._stop_activity()
        self._root.destroy()


def run_gui(
    settings: Settings,
    logger: logging.Logger | None = None,
) -> None:
    root = tk.Tk()
    AgentApp(root, settings, logger=logger)
    root.mainloop()
