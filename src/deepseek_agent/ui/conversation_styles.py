"""集中配置对话文本控件使用的语义标签。"""

import tkinter as tk

from .theme import (
    ACCENT,
    ASSISTANT_COLOR,
    DANGER,
    EMOJI_FONT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TOOL_COLOR,
    USER_COLOR,
)


def configure_conversation_tags(chat_view: tk.Text) -> None:
    """配置消息、Markdown、搜索和轮次高亮所需的全部标签。"""
    chat_view.tag_configure(
        "user_header",
        foreground=USER_COLOR,
        font=("Microsoft YaHei UI", 10, "bold"),
        spacing1=7,
    )
    chat_view.tag_configure(
        "user_message_line",
        justify="right",
        rmargin=12,
        spacing1=7,
        spacing3=5,
    )
    chat_view.tag_configure(
        "user_body",
        foreground=TEXT_PRIMARY,
        lmargin1=12,
        lmargin2=12,
        rmargin=20,
        spacing3=5,
    )
    chat_view.tag_configure(
        "assistant_header",
        foreground=ASSISTANT_COLOR,
        font=("Microsoft YaHei UI", 10, "bold"),
        spacing1=7,
    )
    chat_view.tag_configure(
        "assistant_body",
        foreground=TEXT_PRIMARY,
        lmargin1=12,
        lmargin2=12,
        rmargin=20,
        spacing3=5,
    )
    chat_view.tag_configure(
        "tool_header",
        foreground=TOOL_COLOR,
        font=("Microsoft YaHei UI", 10, "bold"),
        spacing1=8,
    )
    chat_view.tag_configure(
        "tool_body",
        foreground="#6B4B25",
        background="#FFF8E8",
        font=("Consolas", 10),
        lmargin1=12,
        lmargin2=12,
        rmargin=20,
        spacing3=8,
    )
    chat_view.tag_configure("error", foreground=DANGER, spacing1=8)
    chat_view.tag_configure("muted", foreground=TEXT_SECONDARY)
    chat_view.tag_configure(
        "md_h1",
        foreground=TEXT_PRIMARY,
        font=("Microsoft YaHei UI", 18, "bold"),
        spacing1=10,
        spacing3=5,
    )
    chat_view.tag_configure(
        "md_h2",
        foreground=TEXT_PRIMARY,
        font=("Microsoft YaHei UI", 16, "bold"),
        spacing1=9,
        spacing3=4,
    )
    chat_view.tag_configure(
        "md_h3",
        foreground=TEXT_PRIMARY,
        font=("Microsoft YaHei UI", 14, "bold"),
        spacing1=8,
        spacing3=3,
    )
    for level in range(4, 7):
        chat_view.tag_configure(
            f"md_h{level}",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 12, "bold"),
            spacing1=7,
            spacing3=3,
        )
    chat_view.tag_configure(
        "md_bold",
        font=("Microsoft YaHei UI", 11, "bold"),
    )
    chat_view.tag_configure(
        "md_italic",
        font=("Microsoft YaHei UI", 11, "italic"),
    )
    chat_view.tag_configure(
        "md_inline_code",
        background="#EEF2F7",
        foreground="#9D174D",
        font=("Cascadia Mono", 10),
    )
    chat_view.tag_configure(
        "md_link",
        foreground=ACCENT,
        underline=True,
    )
    chat_view.tag_configure(
        "md_list",
        lmargin1=30,
        lmargin2=30,
        rmargin=20,
    )
    chat_view.tag_configure(
        "md_list_marker",
        foreground=ACCENT,
        font=("Microsoft YaHei UI", 11, "bold"),
        lmargin1=16,
    )
    chat_view.tag_configure(
        "md_quote",
        foreground=TEXT_SECONDARY,
        background="#F8FAFC",
        lmargin1=25,
        lmargin2=25,
        rmargin=24,
    )
    chat_view.tag_configure(
        "md_rule",
        foreground="#CBD5E1",
        spacing1=7,
        spacing3=7,
    )
    chat_view.tag_configure(
        "md_blank",
        font=("Microsoft YaHei UI", 3),
        spacing1=0,
        spacing3=0,
    )
    chat_view.tag_configure(
        "md_emoji",
        font=(EMOJI_FONT, 11),
    )
    emoji_heading_sizes = ((1, 18), (2, 16), (3, 14), (4, 12), (5, 12), (6, 12))
    for level, font_size in emoji_heading_sizes:
        chat_view.tag_configure(
            f"md_emoji_h{level}",
            font=(EMOJI_FONT, font_size),
        )
    chat_view.tag_configure(
        "turn_focus",
        background="#EEF2FF",
    )
    chat_view.tag_configure(
        "search_match",
        background="#FFF3A3",
        foreground=TEXT_PRIMARY,
    )
    chat_view.tag_configure(
        "search_current",
        background="#F4C44E",
        foreground="#172033",
    )

