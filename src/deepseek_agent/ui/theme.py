"""集中定义 Tk 桌面界面共用的颜色、字体和 ttk 样式。"""

import tkinter as tk
from tkinter import ttk

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


def configure_app_styles(root: tk.Misc) -> None:
    """配置桌面应用共享的 ttk 主题、颜色和控件状态。"""
    style = ttk.Style(root)
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
