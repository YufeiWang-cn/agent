"""管理对话搜索面板、匹配高亮和结果跳转。"""

import tkinter as tk
from typing import Callable

from .theme import EDITOR_BORDER, TEXT_PRIMARY, TEXT_SECONDARY


class ConversationSearchController:
    """把搜索状态和延迟刷新从主对话视图中隔离出来。"""

    def __init__(
        self,
        root: tk.Misc,
        parent: tk.Misc,
        chat_view: tk.Text,
        focus_composer: Callable[[], None],
    ) -> None:
        self._root = root
        self._chat_view = chat_view
        self._focus_composer = focus_composer
        self.matches: list[tuple[str, str]] = []
        self.match_index = -1
        self.job: str | None = None
        self.panel = self._build_panel(parent)

    @property
    def is_open(self) -> bool:
        return bool(self.panel.place_info())

    def open(self, _event: tk.Event | None = None) -> str:
        if not self.is_open:
            self.panel.place(
                relx=1.0,
                x=-24,
                y=12,
                anchor="ne",
            )
            self.panel.lift()
            self.refresh()
        self.entry.focus_set()
        self.entry.selection_range(0, "end")
        return "break"

    def close(self, _event: tk.Event | None = None) -> str:
        self.cancel_pending_refresh()
        self._clear_highlights()
        self.matches.clear()
        self.match_index = -1
        self.panel.place_forget()
        self._focus_composer()
        return "break"

    def schedule_refresh(self, event: tk.Event | None = None) -> None:
        ignored_keys = {
            "Return",
            "Escape",
            "F3",
            "Shift_L",
            "Shift_R",
            "Up",
            "Down",
        }
        if event is not None and event.keysym in ignored_keys:
            return
        self.cancel_pending_refresh()
        self.job = self._root.after(120, self.refresh)

    def refresh(self) -> None:
        """重新建立匹配索引，并把第一个结果设为当前项。"""
        self.job = None
        self._clear_highlights()
        self.matches.clear()
        self.match_index = -1
        query = self.query.get().strip()
        if not query:
            self.status.set("输入关键词")
            return

        count = tk.IntVar(master=self._root)
        position = "1.0"
        while len(self.matches) < 500:
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
            self.matches.append((start, finish))
            self._chat_view.tag_add("search_match", start, finish)
            position = finish

        if not self.matches:
            self.status.set("无结果")
            return
        self._focus_match(0)

    def next(self, _event: tk.Event | None = None) -> str:
        if not self.is_open:
            return self.open()
        self._flush_pending_refresh()
        if self.matches:
            self._focus_match(self.match_index + 1)
        return "break"

    def previous(self, _event: tk.Event | None = None) -> str:
        if not self.is_open:
            return self.open()
        self._flush_pending_refresh()
        if self.matches:
            self._focus_match(self.match_index - 1)
        return "break"

    def cancel_pending_refresh(self) -> None:
        if self.job is None:
            return
        try:
            self._root.after_cancel(self.job)
        except tk.TclError:
            pass
        self.job = None

    def _build_panel(self, parent: tk.Misc) -> tk.Frame:
        panel = tk.Frame(
            parent,
            background="#FFFFFF",
            highlightbackground=EDITOR_BORDER,
            highlightthickness=1,
            borderwidth=0,
        )
        self.query = tk.StringVar()
        self.status = tk.StringVar(value="输入关键词")
        self.entry = tk.Entry(
            panel,
            textvariable=self.query,
            width=24,
            font=("Microsoft YaHei UI", 10),
            background="#F8FAFC",
            foreground=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
        )
        self.entry.pack(side="left", padx=(9, 6), pady=7, ipady=3)
        tk.Label(
            panel,
            textvariable=self.status,
            width=8,
            anchor="center",
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(0, 4))
        for label, command in (
            ("↑", self.previous),
            ("↓", self.next),
            ("×", self.close),
        ):
            tk.Button(
                panel,
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
        self.entry.bind("<KeyRelease>", self.schedule_refresh)
        self.entry.bind("<Return>", self.next)
        self.entry.bind("<Shift-Return>", self.previous)
        self.entry.bind("<Escape>", self.close)
        return panel

    def _clear_highlights(self) -> None:
        self._chat_view.tag_remove("search_match", "1.0", "end")
        self._chat_view.tag_remove("search_current", "1.0", "end")

    def _focus_match(self, index: int) -> None:
        if not self.matches:
            return
        self.match_index = index % len(self.matches)
        start, finish = self.matches[self.match_index]
        self._chat_view.tag_remove("search_current", "1.0", "end")
        self._chat_view.tag_add("search_current", start, finish)
        self._chat_view.tag_raise("search_current")
        self._chat_view.see(start)
        self.status.set(f"{self.match_index + 1} / {len(self.matches)}")

    def _flush_pending_refresh(self) -> None:
        if self.job is None:
            return
        self.cancel_pending_refresh()
        self.refresh()
