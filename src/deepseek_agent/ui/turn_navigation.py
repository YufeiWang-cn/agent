"""维护对话轮次标记、侧边轨道和滚动位置同步。"""

import tkinter as tk
from typing import Callable

from .message_cards import ConversationRail, UserMessageCard


class TurnNavigationController:
    """集中维护轮次导航状态，避免对话视图同时承担滚动状态机。"""

    def __init__(
        self,
        root: tk.Misc,
        chat_view: tk.Text,
        rail: ConversationRail,
        bottom_button: tk.Button,
        focus_composer: Callable[[], None],
        user_cards: Callable[[], list[UserMessageCard]],
    ) -> None:
        self._root = root
        self._chat_view = chat_view
        self._rail = rail
        self._bottom_button = bottom_button
        self._focus_composer = focus_composer
        self._user_cards = user_cards
        self.marks: list[str] = []
        self.questions: list[str] = []
        self.answers: list[str] = []
        self.selected: int | None = None
        self.sync_job: str | None = None

    @property
    def count(self) -> int:
        return len(self.marks)

    @property
    def has_current_answer(self) -> bool:
        """返回最新轮次是否已经包含一段回答预览。"""
        return bool(self.answers and self.answers[-1])

    def previous(self) -> None:
        if self.marks:
            current = self.selected or len(self.marks)
            self.jump(max(1, current - 1))

    def next(self) -> None:
        if self.marks:
            current = self.selected or 1
            self.jump(min(len(self.marks), current + 1))

    def register(
        self,
        question: str,
        *,
        select: bool,
        rendering_history: bool,
    ) -> None:
        turn_number = len(self.marks) + 1
        mark_name = f"conversation_turn_{turn_number}"
        self._chat_view.mark_set(mark_name, "end-1c")
        self._chat_view.mark_gravity(mark_name, "left")
        self.marks.append(mark_name)
        self.questions.append(question)
        self.answers.append("")
        if not rendering_history:
            self.refresh(turn_number if select else self.selected)

    def append_answer(
        self,
        content: str,
        *,
        starts_new_message: bool,
        rendering_history: bool,
    ) -> None:
        if not self.answers or not content:
            return
        existing = self.answers[-1]
        separator = "  " if starts_new_message and existing else ""
        self.answers[-1] = (existing + separator + content)[:500]
        if not rendering_history:
            self._rail.update_answer(len(self.answers), self.answers[-1])

    def reset(self) -> None:
        if self.marks:
            self._chat_view.mark_unset(*self.marks)
        self._chat_view.tag_remove("turn_focus", "1.0", "end")
        self.marks.clear()
        self.questions.clear()
        self.answers.clear()
        self.selected = None
        self.refresh()

    def refresh(self, selected_turn: int | None = None) -> None:
        total = len(self.marks)
        if total == 0:
            self.selected = None
            self._rail.set_turns([], [], None)
            return
        if selected_turn is None:
            selected_turn = self.selected or total
        selected_turn = min(total, max(1, selected_turn))
        self.selected = selected_turn
        self._rail.set_turns(
            self.questions,
            self.answers,
            selected_turn,
        )
        for index, card in enumerate(self._user_cards(), start=1):
            card.set_active(index == selected_turn)

    def on_yview(self, first: str, last: str) -> None:
        """同步原生滚动条，并延迟计算当前视口所在的轮次。"""
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
        if not self.marks:
            return
        self.cancel_pending_sync()
        self.sync_job = self._root.after_idle(self._sync_to_view)

    def is_at_bottom(self) -> bool:
        return self._chat_view.yview()[1] >= 0.995

    def scroll_to_bottom(self) -> None:
        self._chat_view.see("end")
        self._bottom_button.place_forget()
        if self.marks:
            self.refresh(len(self.marks))
        self._focus_composer()

    def jump(self, turn_number: int) -> None:
        if not 1 <= turn_number <= len(self.marks):
            return
        self.refresh(turn_number)
        mark_name = self.marks[turn_number - 1]
        self._chat_view.tag_remove("turn_focus", "1.0", "end")
        self._chat_view.tag_add(
            "turn_focus",
            mark_name,
            f"{mark_name} lineend+1c",
        )
        self._chat_view.tag_raise("turn_focus")
        self._chat_view.yview(mark_name)

    def cancel_pending_sync(self) -> None:
        if self.sync_job is None:
            return
        try:
            self._root.after_cancel(self.sync_job)
        except tk.TclError:
            pass
        self.sync_job = None

    def _sync_to_view(self) -> None:
        self.sync_job = None
        if not self.marks:
            return
        _first, last = self._chat_view.yview()
        if last >= 0.999:
            visible_turn = len(self.marks)
        else:
            viewport_height = max(1, self._chat_view.winfo_height())
            anchor = self._chat_view.index(f"@0,{int(viewport_height * 0.28)}")
            visible_turn = 1
            for number, mark_name in enumerate(self.marks, start=1):
                if self._chat_view.compare(mark_name, "<=", anchor):
                    visible_turn = number
                else:
                    break
        if visible_turn != self.selected:
            self.refresh(visible_turn)
