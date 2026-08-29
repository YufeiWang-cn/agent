"""实现工具调用详情卡片和全屏查看器。"""

import json
import tkinter as tk

from .editor import EditorTabs, SyntaxText
from .formatting import _content_language_hint
from .theme import (
    ASSISTANT_COLOR,
    CARD_BACKGROUND,
    DANGER,
    EDITOR_BACKGROUND,
    EDITOR_BORDER,
    EDITOR_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


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


class ToolCallCard:
    """展示工具调用状态，并支持查看完整参数和执行结果。"""

    def __init__(self, parent: tk.Text, name: str, arguments: str) -> None:
        self._chat_view = parent
        self._name = name
        self._display_name = self._make_display_name(name, arguments)
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
            text=f"▶  {self._display_name}",
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
            text=self._make_argument_summary(name, arguments),
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
        is_partial = self._is_partial_search_result(result)
        self._status.configure(
            text="失败" if is_failure else "部分完成" if is_partial else "已完成",
            foreground=DANGER if is_failure else "#B45309" if is_partial else ASSISTANT_COLOR,
            background="#FEF2F2" if is_failure else "#FFF7ED" if is_partial else "#ECFDF5",
        )
        self._summary.configure(text=self._make_result_summary(self._name, result))
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
        self._toggle_button.configure(text=f"{arrow}  {self._display_name}")
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

    @staticmethod
    def _make_display_name(name: str, arguments: str) -> str:
        if name != "web_search":
            return name
        try:
            payload = json.loads(arguments)
        except json.JSONDecodeError:
            return "联网搜索"
        if not isinstance(payload, dict):
            return "联网搜索"
        labels = {
            "balanced": "联网搜索 · 国内 + 国际",
            "domestic": "联网搜索 · 国内",
            "international": "联网搜索 · 国际",
            "unrestricted": "联网搜索 · 不限来源",
        }
        return labels.get(payload.get("scope"), "联网搜索")

    @classmethod
    def _make_argument_summary(cls, name: str, arguments: str) -> str:
        if name != "web_search":
            return cls._make_summary(arguments)
        try:
            payload = json.loads(arguments)
        except json.JSONDecodeError:
            return cls._make_summary(arguments)
        if not isinstance(payload, dict):
            return cls._make_summary(arguments)
        query = payload.get("query")
        international_query = payload.get("international_query")
        if isinstance(query, str) and isinstance(international_query, str):
            return cls._make_summary(f"国内：{query}  ·  国际：{international_query}")
        if isinstance(query, str):
            return cls._make_summary(query)
        return cls._make_summary(arguments)

    @classmethod
    def _make_result_summary(cls, name: str, result: str) -> str:
        if name != "web_search":
            return cls._make_summary(result)
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return cls._make_summary(result)
        if not isinstance(payload, dict):
            return cls._make_summary(result)
        if payload.get("requested_scope") == "balanced":
            groups = payload.get("groups")
            if isinstance(groups, dict):
                domestic = groups.get("domestic")
                international = groups.get("international")
                domestic_count = (
                    domestic.get("result_count", 0)
                    if isinstance(domestic, dict)
                    else 0
                )
                international_count = (
                    international.get("result_count", 0)
                    if isinstance(international, dict)
                    else 0
                )
                suffix = " · 一路搜索失败" if payload.get("partial_failure") else ""
                return (
                    f"国内 {domestic_count} 条 · 国际 {international_count} 条{suffix}"
                )
        count = payload.get("result_count")
        if isinstance(count, int) and not isinstance(count, bool):
            return f"找到 {count} 条来源"
        return cls._make_summary(result)

    @staticmethod
    def _is_partial_search_result(result: str) -> bool:
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get("partial_failure") is True
