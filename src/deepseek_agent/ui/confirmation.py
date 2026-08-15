"""Thread-safe tool confirmation bridge and dialog."""

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any

from ..tools import Tool
from .cards import RenderedMarkdownPreview
from .editor import EditorTabs
from .formatting import _confirmation_content_previews
from .theme import APP_BACKGROUND, EDITOR_BACKGROUND, EDITOR_BORDER


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


