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
        dialog_width, dialog_height = self._preferred_size(
            parent_width=parent.winfo_width(),
            parent_height=parent.winfo_height(),
            screen_width=self._window.winfo_screenwidth(),
            screen_height=self._window.winfo_screenheight(),
        )
        self._window.geometry(f"{dialog_width}x{dialog_height}")
        self._window.minsize(
            min(600, dialog_width),
            min(440, dialog_height),
        )
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
        self._description_label = ttk.Label(
            content,
            text=(
                f"{request.tool.name}  ·  "
                f"{request.tool.confirmation_description or request.tool.description}"
            ),
            style="Body.TLabel",
            wraplength=max(320, dialog_width - 40),
        )
        self._description_label.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(6, 14),
        )
        content.bind("<Configure>", self._resize_description)

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
        x, y = self._center_position(
            parent_x=parent.winfo_rootx(),
            parent_y=parent.winfo_rooty(),
            parent_width=parent.winfo_width(),
            parent_height=parent.winfo_height(),
            dialog_width=dialog_width,
            dialog_height=dialog_height,
            screen_width=self._window.winfo_screenwidth(),
            screen_height=self._window.winfo_screenheight(),
        )
        self._window.geometry(f"{dialog_width}x{dialog_height}+{x}+{y}")
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

    def _resize_description(self, event: tk.Event) -> None:
        """随内容区域宽度调整说明文字换行，避免固定像素导致拥挤。"""
        width = int(getattr(event, "width", 0))
        if width > 0:
            self._description_label.configure(wraplength=max(320, width))

    @staticmethod
    def _preferred_size(
        *,
        parent_width: int,
        parent_height: int,
        screen_width: int,
        screen_height: int,
    ) -> tuple[int, int]:
        """根据主窗口和屏幕尺寸计算适合当前显示器的初始大小。"""
        usable_width = max(480, screen_width - 80)
        usable_height = max(400, screen_height - 100)
        width = (
            max(720, round(parent_width * 0.82))
            if parent_width >= 640
            else 720
        )
        height = (
            max(540, round(parent_height * 0.82))
            if parent_height >= 480
            else 540
        )
        width = min(usable_width, 1_040, width)
        height = min(usable_height, 760, height)
        return width, height

    @staticmethod
    def _center_position(
        *,
        parent_x: int,
        parent_y: int,
        parent_width: int,
        parent_height: int,
        dialog_width: int,
        dialog_height: int,
        screen_width: int,
        screen_height: int,
    ) -> tuple[int, int]:
        """优先相对主窗口居中，主窗口不可见时退回屏幕中央。"""
        if parent_width >= 320 and parent_height >= 240:
            return (
                parent_x + (parent_width - dialog_width) // 2,
                parent_y + (parent_height - dialog_height) // 2,
            )
        return (
            max(0, (screen_width - dialog_width) // 2),
            max(0, (screen_height - dialog_height) // 2),
        )


