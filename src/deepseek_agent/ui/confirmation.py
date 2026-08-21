"""在线程安全的桥接层与对话框之间传递工具确认请求。"""

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any

from ..tools import Tool
from .cards import RenderedMarkdownPreview
from .diff_viewer import DiffViewer
from .editor import EditorTabs
from .formatting import _confirmation_content_previews
from .theme import APP_BACKGROUND, EDITOR_BACKGROUND, EDITOR_BORDER


@dataclass(slots=True)
class ConfirmationRequest:
    """保存工作线程与 Tk 主线程之间共享的确认状态。"""

    tool: Tool
    arguments: str
    completed: threading.Event
    allowed: bool = False


class TkToolConfirmer:
    """把工作线程中的同步确认请求转发到 Tk 事件队列。"""

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
    """展示工具说明和完整参数，并按需提供内容预览。"""

    def __init__(self, parent: tk.Tk, request: ConfirmationRequest) -> None:
        self.allowed = False
        self._fullscreen = False
        self._window = tk.Toplevel(parent)
        self._window.title("确认工具调用")
        self._window.geometry("720x540")
        self._window.minsize(600, 440)
        self._window.configure(background=APP_BACKGROUND)
        self._window.transient(parent)
        self._window.grab_set()
        self._window.protocol("WM_DELETE_WINDOW", self._deny)
        self._window.bind("<F11>", self._toggle_fullscreen)
        self._window.bind("<Escape>", self._handle_escape)

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
        self._fullscreen_button = ttk.Button(
            actions,
            text="全屏查看  F11",
            command=self._toggle_fullscreen,
            style="Secondary.TButton",
        )
        self._fullscreen_button.pack(side="left")
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
        self._arguments_view = EditorTabs(
            editor_frame,
            editor_font_size=11,
            tab_font_size=9,
        )
        self._arguments_view.pack(fill="both", expand=True)
        self._arguments_view.add_or_update(
            "arguments",
            "完整参数",
            request.arguments,
            "json",
        )
        for key, title, value, language in _confirmation_content_previews(
            request.arguments
        ):
            if language == "diff":
                self._arguments_view.add_widget(
                    key,
                    title,
                    "DIFF",
                    lambda parent, diff=value: DiffViewer(
                        parent,
                        diff,
                        font_size=11,
                    ),
                    select=True,
                )
                continue

            self._arguments_view.add_or_update(
                key,
                title.replace("预览", "源码")
                if language == "markdown"
                else title,
                value,
                language,
            )
            if language == "markdown":
                self._arguments_view.add_widget(
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

    def _toggle_fullscreen(self, _event: tk.Event | None = None) -> str:
        self._fullscreen = not self._fullscreen
        self._window.attributes("-fullscreen", self._fullscreen)
        self._fullscreen_button.configure(
            text=(
                "退出全屏  F11"
                if self._fullscreen
                else "全屏查看  F11"
            )
        )
        return "break"

    def _handle_escape(self, _event: tk.Event) -> str:
        if self._fullscreen:
            self._toggle_fullscreen()
        else:
            self._deny()
        return "break"


