"""实现用户消息气泡和对话轮次导航组件。"""

import tkinter as tk
from typing import Callable

from .formatting import (
    _conversation_preview,
    _split_emoji_spans,
)
from .theme import (
    ACCENT,
    CARD_BACKGROUND,
    EMOJI_FONT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    USER_BUBBLE_BACKGROUND,
    USER_COLOR,
)


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
