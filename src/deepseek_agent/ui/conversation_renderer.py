"""渲染普通消息、Markdown 块和嵌入式对话卡片。"""

import tkinter as tk
from typing import Callable

from .cards import MarkdownCodeCard, MarkdownTableCard, UserMessageCard
from .formatting import _split_emoji_spans
from .markdown import MarkdownBlock, parse_inline, parse_markdown


class ConversationRenderer:
    """集中处理消息渲染，并维护渲染过程中创建的卡片资源。"""

    def __init__(
        self,
        chat_view: tk.Text,
        *,
        is_at_bottom: Callable[[], bool],
        selected_turn: Callable[[], int | None],
        rendering_history: Callable[[], bool],
        on_layout_changed: Callable[[], None],
    ) -> None:
        self._chat_view = chat_view
        self._is_at_bottom = is_at_bottom
        self._selected_turn = selected_turn
        self._rendering_history = rendering_history
        self._on_layout_changed = on_layout_changed
        self.code_cards: list[MarkdownCodeCard] = []
        self.table_cards: list[MarkdownTableCard] = []
        self.user_cards: list[UserMessageCard] = []
        self._markdown_cache: dict[str, tuple[MarkdownBlock, ...]] = {}

    def append(self, content: str, tag: str) -> None:
        was_at_bottom = self._is_at_bottom()
        self._chat_view.configure(state="normal")
        if tag == "assistant_body":
            self._insert_chat_text(content, (tag,))
        else:
            self._chat_view.insert("end", content, tag)
        self._chat_view.configure(state="disabled")
        if was_at_bottom:
            self._chat_view.see("end")

    def append_message(self, author: str, content: str, role: str) -> None:
        if role == "user":
            self._append_user_message(author, content)
            return
        self.append(f"{author}\n", f"{role}_header")
        self.append(f"{content}\n", f"{role}_body")

    def append_markdown_message(
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
        if not self._rendering_history():
            self._chat_view.see("end")

    def resize(self, width: int) -> None:
        """批量调整嵌入卡片，避免每张卡片单独刷新布局树。"""
        card_width = max(260, width - 70)
        for card in self.code_cards:
            card.set_width(card_width)
        pending_tables = [
            card
            for card in self.table_cards
            if card.set_width(card_width, defer=True)
        ]
        pending_users = [
            card
            for card in self.user_cards
            if card.set_max_width(width - 48, defer=True)
        ]
        if pending_tables or pending_users:
            # 所有换行宽度先一次性写入，再统一读取 Tk 请求尺寸。
            self._chat_view.update_idletasks()
            for card in pending_tables:
                card.finalize_width()
            for card in pending_users:
                card.finalize_size()

    def clear(self) -> None:
        """销毁全部嵌入式卡片，防止遗留 Tk 回调和窗口资源。"""
        for card in self.code_cards:
            card.destroy()
        for card in self.table_cards:
            card.destroy()
        for card in self.user_cards:
            card.destroy()
        self.code_cards.clear()
        self.table_cards.clear()
        self.user_cards.clear()

    def _append_user_message(self, author: str, content: str) -> None:
        self._chat_view.configure(state="normal")
        card = UserMessageCard(self._chat_view, author, content)
        start = self._chat_view.index("end-1c")
        self._chat_view.window_create("end", window=card.frame, padx=8, pady=4)
        finish = self._chat_view.index("end-1c")
        self._chat_view.tag_add("user_message_line", start, finish)
        self._chat_view.insert("end", "\n", "user_message_line")
        self._chat_view.configure(state="disabled")
        self.user_cards.append(card)
        card.set_active(len(self.user_cards) == self._selected_turn())
        if not self._rendering_history():
            self._on_layout_changed()
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
            style_allowed = allow_font_styles or span.style in {"code", "link"}
            if style_tag is not None and style_allowed:
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
        self.code_cards.append(card)
        if not self._rendering_history():
            self._on_layout_changed()

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
        self.table_cards.append(card)
        if not self._rendering_history():
            self._on_layout_changed()
