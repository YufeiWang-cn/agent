import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from ..agent import Agent, AgentCancelledError
from ..config import Settings
from ..memory import Project, Session
from ..models import ToolCallRequest
from ..runtime import RunStatus, TurnOutcome
from .cards import (
    ConversationRail,
    MarkdownCodeCard,
    MarkdownTableCard,
    ToolCallCard,
    UserMessageCard,
)
from .confirmation import (
    ConfirmationRequest,
    TkToolConfirmer,
    ToolConfirmationDialog,
)
from .formatting import _conversation_preview, _split_emoji_spans
from .markdown import MarkdownBlock, parse_inline, parse_markdown
from .theme import (
    ACCENT,
    ACCENT_HOVER,
    APP_BACKGROUND,
    ASSISTANT_COLOR,
    CARD_BACKGROUND,
    DANGER,
    EDITOR_BORDER,
    EMOJI_FONT,
    SIDEBAR_BACKGROUND,
    SIDEBAR_PANEL,
    SIDEBAR_TEXT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TOOL_COLOR,
    USER_COLOR,
)


ALL_PROJECTS = "__all_projects__"
UNASSIGNED_PROJECT = "__unassigned_project__"


def _coalesce_ui_events(
    events: list[tuple[str, Any]],
) -> list[tuple[str, Any]]:
    combined: list[tuple[str, Any]] = []
    for event_name, payload in events:
        if event_name == "text" and combined and combined[-1][0] == "text":
            previous_name, previous_payload = combined[-1]
            combined[-1] = (
                previous_name,
                str(previous_payload) + str(payload),
            )
        else:
            combined.append((event_name, payload))
    return combined


def _dequeue_ui_events(
    event_queue: queue.Queue[tuple[str, Any]],
    limit: int = 256,
) -> list[tuple[str, Any]]:
    """Take a bounded batch so a fast stream cannot starve Tk's event loop."""
    events: list[tuple[str, Any]] = []
    for _index in range(max(1, limit)):
        try:
            events.append(event_queue.get_nowait())
        except queue.Empty:
            break
    return events


def _messages_after_last_user(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only the model/tool messages belonging to the latest turn."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return messages[index + 1 :]
    return messages


def _format_activity_status(phase: str, elapsed: float, characters: int) -> str:
    details = [phase, f"{max(0.0, elapsed):.1f}s"]
    if characters:
        details.append(f"{characters} 字符")
    return "  ·  ".join(details)


def _filter_sessions_by_title(
    sessions: list[Session],
    query: str,
) -> list[Session]:
    normalized = query.strip().casefold()
    if not normalized:
        return sessions
    return [session for session in sessions if normalized in session.title.casefold()]


class AgentApp:
    def __init__(
        self,
        root: tk.Tk,
        settings: Settings,
        logger: logging.Logger | None = None,
    ) -> None:
        self._root = root
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._closing = threading.Event()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._busy = False
        self._assistant_open = False
        self._projects: list[Project] = []
        self._project_options: list[str] = []
        self._sessions: list[Session] = []
        self._tool_cards: dict[str, ToolCallCard] = {}
        self._tool_card_widgets: list[ToolCallCard] = []
        self._markdown_code_widgets: list[MarkdownCodeCard] = []
        self._markdown_table_widgets: list[MarkdownTableCard] = []
        self._user_message_widgets: list[UserMessageCard] = []
        self._turn_marks: list[str] = []
        self._turn_questions: list[str] = []
        self._turn_answers: list[str] = []
        self._markdown_cache: dict[str, tuple[MarkdownBlock, ...]] = {}
        self._selected_turn_index: int | None = None
        self._turn_sync_job: str | None = None
        self._resize_job: str | None = None
        self._pending_chat_width = 0
        self._rendering_history = False
        self._live_response_mark = "live_response_start"
        self._live_tool_start: int | None = None
        self._search_matches: list[tuple[str, str]] = []
        self._search_match_index = -1
        self._search_job: str | None = None
        self._turn_started_at: float | None = None
        self._stream_character_count = 0
        self._activity_phase = ""
        self._activity_job: str | None = None
        self._empty_state_frame: tk.Frame | None = None
        self._session_filter_job: str | None = None
        self._active_prompt: str | None = None
        self._close_pending = False
        self._closed = False

        confirmer = TkToolConfirmer(self._events, self._closing)
        self._agent = Agent(settings, confirmer=confirmer, logger=logger)

        self._configure_window()
        self._configure_styles()
        self._build_layout()
        self._bind_shortcuts()
        self._render_history()
        self._refresh_navigation()
        self._update_header()
        self._input.focus_set()
        self._show_recovery_issues()
        self._root.after(50, self._drain_events)

    def _show_recovery_issues(self) -> None:
        """在界面完成初始化后提示用户核对上次未明确结束的工具。"""
        # 界面仍兼容没有实现运行日志接口的自定义 Agent。
        issues = getattr(self._agent, "recovery_issues", ())
        if not issues:
            return
        details = "\n\n".join(issue.message for issue in issues[:5])
        if len(issues) > 5:
            details += f"\n\n另外还有 {len(issues) - 5} 条结果未知记录。"
        messagebox.showwarning(
            "需要检查上次运行",
            details,
            parent=self._root,
        )

    def _configure_window(self) -> None:
        self._root.title("DeepSeek Agent")
        self._root.geometry("1180x780")
        self._root.minsize(820, 600)
        self._root.configure(background=APP_BACKGROUND)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self._root)
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

    def _build_layout(self) -> None:
        shell = ttk.Frame(self._root, style="App.TFrame")
        shell.pack(fill="both", expand=True)

        self._paned = tk.PanedWindow(
            shell,
            orient="horizontal",
            background="#D9DCE2",
            borderwidth=0,
            sashwidth=5,
            sashrelief="flat",
            showhandle=False,
        )
        self._paned.pack(fill="both", expand=True)

        self._sidebar = ttk.Frame(self._paned, style="Sidebar.TFrame")
        self._main_panel = ttk.Frame(
            self._paned,
            style="App.TFrame",
            padding=(20, 16, 20, 14),
        )
        self._paned.add(self._sidebar, minsize=190, width=240, stretch="never")
        self._paned.add(self._main_panel, minsize=520, stretch="always")
        self._sidebar_visible = True

        self._build_sidebar(self._sidebar)
        self._main_panel.columnconfigure(0, weight=1)
        self._main_panel.rowconfigure(1, weight=1)
        self._build_header(self._main_panel)
        self._build_chat(self._main_panel)
        self._build_input(self._main_panel)
        self._root.after_idle(lambda: self._paned.sash_place(0, 240, 0))

    def _build_sidebar(self, sidebar: ttk.Frame) -> None:
        brand = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(16, 16, 16, 8))
        brand.pack(fill="x")
        ttk.Label(
            brand,
            text="DeepSeek Agent",
            style="SidebarTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            brand,
            text="Agent 工作区",
            style="SidebarMuted.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        self._new_session_button = ttk.Button(
            sidebar,
            text="＋  新建会话",
            command=self._new_session,
            style="Sidebar.TButton",
        )
        self._new_session_button.pack(fill="x", padx=12, pady=(4, 12))

        project_header = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(16, 4))
        project_header.pack(fill="x")
        ttk.Label(
            project_header,
            text="项目",
            style="SidebarMuted.TLabel",
        ).pack(side="left")
        self._create_project_button = ttk.Button(
            project_header,
            text="＋",
            command=self._create_project,
            style="Sidebar.TButton",
            width=3,
        )
        self._create_project_button.pack(side="right")

        project_frame = ttk.Frame(sidebar, style="Sidebar.TFrame")
        project_frame.pack(fill="x", padx=(10, 5), pady=(0, 10))
        self._project_tree = ttk.Treeview(
            project_frame,
            show="tree",
            selectmode="browse",
            style="Projects.Treeview",
            height=5,
        )
        project_scroll_y = ttk.Scrollbar(
            project_frame,
            orient="vertical",
            command=self._project_tree.yview,
        )
        self._project_tree.configure(
            yscrollcommand=project_scroll_y.set,
        )
        self._project_tree.column("#0", width=180, minwidth=100, stretch=True)
        self._project_tree.grid(row=0, column=0, sticky="nsew")
        project_scroll_y.grid(row=0, column=1, sticky="ns")
        project_frame.columnconfigure(0, weight=1)
        self._project_tree.bind("<<TreeviewSelect>>", self._on_project_selected)
        self._project_tree.bind("<Button-3>", self._show_project_menu)

        ttk.Separator(sidebar).pack(fill="x", padx=14, pady=(0, 9))
        ttk.Label(
            sidebar,
            text="会话  ·  右键管理",
            style="SidebarMuted.TLabel",
        ).pack(anchor="w", padx=16, pady=(0, 6))

        session_filter = tk.Frame(sidebar, background=SIDEBAR_BACKGROUND)
        session_filter.pack(fill="x", padx=(14, 9), pady=(0, 8))
        self._session_filter_var = tk.StringVar()
        tk.Label(
            session_filter,
            text="筛选",
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(8, 3))
        self._session_filter_entry = tk.Entry(
            session_filter,
            textvariable=self._session_filter_var,
            font=("Microsoft YaHei UI", 9),
            background="#FFFFFF",
            foreground=SIDEBAR_TEXT,
            insertbackground=SIDEBAR_TEXT,
            relief="flat",
            borderwidth=0,
        )
        self._session_filter_entry.pack(
            side="left",
            fill="x",
            expand=True,
            ipady=6,
            padx=(0, 0),
        )
        self._session_filter_entry.insert(0, "")
        self._session_filter_entry.bind(
            "<KeyRelease>",
            self._schedule_session_filter,
        )
        tk.Button(
            session_filter,
            text="×",
            command=self._clear_session_filter,
            background="#FFFFFF",
            activebackground="#EEF2F7",
            foreground=TEXT_SECONDARY,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10),
            padx=7,
            pady=3,
        ).pack(side="right")

        session_frame = ttk.Frame(sidebar, style="Sidebar.TFrame")
        session_frame.pack(fill="both", expand=True, padx=(16, 8))
        self._session_tree = ttk.Treeview(
            session_frame,
            show="tree",
            selectmode="browse",
            style="Sessions.Treeview",
            height=1,
        )
        session_scroll_y = ttk.Scrollbar(
            session_frame,
            orient="vertical",
            command=self._session_tree.yview,
        )
        self._session_tree.configure(
            yscrollcommand=session_scroll_y.set,
        )
        self._session_tree.column("#0", width=180, minwidth=100, stretch=True)
        self._session_tree.grid(row=0, column=0, sticky="nsew")
        session_scroll_y.grid(row=0, column=1, sticky="ns")
        session_frame.rowconfigure(0, weight=1)
        session_frame.columnconfigure(0, weight=1)
        self._session_tree.bind("<Double-Button-1>", self._load_selected_session)
        self._session_tree.bind("<Return>", self._load_selected_session)
        self._session_tree.bind("<Button-3>", self._show_session_menu)
        self._build_context_menus()

    def _build_header(self, main: ttk.Frame) -> None:
        header = ttk.Frame(main, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        header.columnconfigure(1, weight=1)

        self._session_title = tk.StringVar()
        self._session_project = tk.StringVar()
        ttk.Button(
            header,
            text="☰",
            command=self._toggle_sidebar,
            style="Header.TButton",
            width=3,
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))

        identity = ttk.Frame(header, style="App.TFrame")
        identity.grid(row=0, column=1, sticky="w")
        ttk.Label(
            identity,
            textvariable=self._session_title,
            style="Title.TLabel",
            wraplength=260,
        ).pack(side="left")
        ttk.Label(
            identity,
            textvariable=self._session_project,
            style="Body.TLabel",
        ).pack(side="left", padx=(8, 0))

        info = ttk.Frame(header, style="App.TFrame")
        info.grid(row=0, column=2, sticky="e")

        ttk.Button(
            info,
            text="查找",
            command=self._open_search,
            style="Header.TButton",
        ).pack(side="left", padx=(0, 9))

        model_picker = ttk.Frame(info, style="App.TFrame")
        model_picker.pack(side="left")
        ttk.Label(
            model_picker,
            text="模型",
            style="Body.TLabel",
        ).pack(side="left", padx=(0, 5))
        self._model_selector_var = tk.StringVar(value=self._agent.model_name)
        self._model_selector = ttk.Combobox(
            model_picker,
            textvariable=self._model_selector_var,
            values=self._agent.available_models,
            state="readonly",
            width=18,
        )
        self._model_selector.pack(side="left")
        self._model_selector.bind(
            "<<ComboboxSelected>>",
            self._change_model,
        )

    def _build_chat(self, main: ttk.Frame) -> None:
        card = ttk.Frame(main, style="Card.TFrame", padding=1)
        self._chat_card = card
        card.grid(row=1, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)
        self._chat_view = ScrolledText(
            card,
            wrap="word",
            state="disabled",
            width=1,
            height=1,
            font=("Microsoft YaHei UI", 11),
            background=CARD_BACKGROUND,
            foreground=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            padx=44,
            pady=18,
            spacing1=2,
            spacing3=7,
        )
        self._chat_view.grid(row=0, column=0, sticky="nsew")
        self._turn_rail = ConversationRail(
            card,
            self._chat_view,
            self._jump_to_turn,
        )
        self._bottom_button = tk.Button(
            card,
            text="↓  回到底部",
            command=self._scroll_to_bottom,
            background="#FFFFFF",
            activebackground="#EEF2F7",
            foreground=TEXT_PRIMARY,
            activeforeground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 9, "bold"),
            relief="flat",
            borderwidth=0,
            highlightbackground=EDITOR_BORDER,
            highlightcolor=EDITOR_BORDER,
            highlightthickness=1,
            cursor="hand2",
            padx=11,
            pady=5,
        )
        self._bottom_button.bind(
            "<Enter>",
            lambda _event: self._bottom_button.configure(background="#F8FAFC"),
        )
        self._bottom_button.bind(
            "<Leave>",
            lambda _event: self._bottom_button.configure(background="#FFFFFF"),
        )
        self._chat_view.configure(yscrollcommand=self._on_chat_yview)
        self._chat_view.tag_configure(
            "user_header",
            foreground=USER_COLOR,
            font=("Microsoft YaHei UI", 10, "bold"),
            spacing1=7,
        )
        self._chat_view.tag_configure(
            "user_message_line",
            justify="right",
            rmargin=12,
            spacing1=7,
            spacing3=5,
        )
        self._chat_view.tag_configure(
            "user_body",
            foreground=TEXT_PRIMARY,
            lmargin1=12,
            lmargin2=12,
            rmargin=20,
            spacing3=5,
        )
        self._chat_view.tag_configure(
            "assistant_header",
            foreground=ASSISTANT_COLOR,
            font=("Microsoft YaHei UI", 10, "bold"),
            spacing1=7,
        )
        self._chat_view.tag_configure(
            "assistant_body",
            foreground=TEXT_PRIMARY,
            lmargin1=12,
            lmargin2=12,
            rmargin=20,
            spacing3=5,
        )
        self._chat_view.tag_configure(
            "tool_header",
            foreground=TOOL_COLOR,
            font=("Microsoft YaHei UI", 10, "bold"),
            spacing1=8,
        )
        self._chat_view.tag_configure(
            "tool_body",
            foreground="#6B4B25",
            background="#FFF8E8",
            font=("Consolas", 10),
            lmargin1=12,
            lmargin2=12,
            rmargin=20,
            spacing3=8,
        )
        self._chat_view.tag_configure("error", foreground=DANGER, spacing1=8)
        self._chat_view.tag_configure("muted", foreground=TEXT_SECONDARY)
        self._chat_view.tag_configure(
            "md_h1",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 18, "bold"),
            spacing1=10,
            spacing3=5,
        )
        self._chat_view.tag_configure(
            "md_h2",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 16, "bold"),
            spacing1=9,
            spacing3=4,
        )
        self._chat_view.tag_configure(
            "md_h3",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 14, "bold"),
            spacing1=8,
            spacing3=3,
        )
        for level in range(4, 7):
            self._chat_view.tag_configure(
                f"md_h{level}",
                foreground=TEXT_PRIMARY,
                font=("Microsoft YaHei UI", 12, "bold"),
                spacing1=7,
                spacing3=3,
            )
        self._chat_view.tag_configure(
            "md_bold",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self._chat_view.tag_configure(
            "md_italic",
            font=("Microsoft YaHei UI", 11, "italic"),
        )
        self._chat_view.tag_configure(
            "md_inline_code",
            background="#EEF2F7",
            foreground="#9D174D",
            font=("Cascadia Mono", 10),
        )
        self._chat_view.tag_configure(
            "md_link",
            foreground=ACCENT,
            underline=True,
        )
        self._chat_view.tag_configure(
            "md_list",
            lmargin1=30,
            lmargin2=30,
            rmargin=20,
        )
        self._chat_view.tag_configure(
            "md_list_marker",
            foreground=ACCENT,
            font=("Microsoft YaHei UI", 11, "bold"),
            lmargin1=16,
        )
        self._chat_view.tag_configure(
            "md_quote",
            foreground=TEXT_SECONDARY,
            background="#F8FAFC",
            lmargin1=25,
            lmargin2=25,
            rmargin=24,
        )
        self._chat_view.tag_configure(
            "md_rule",
            foreground="#CBD5E1",
            spacing1=7,
            spacing3=7,
        )
        self._chat_view.tag_configure(
            "md_blank",
            font=("Microsoft YaHei UI", 3),
            spacing1=0,
            spacing3=0,
        )
        self._chat_view.tag_configure(
            "md_emoji",
            font=(EMOJI_FONT, 11),
        )
        emoji_heading_sizes = ((1, 18), (2, 16), (3, 14), (4, 12), (5, 12), (6, 12))
        for level, font_size in emoji_heading_sizes:
            self._chat_view.tag_configure(
                f"md_emoji_h{level}",
                font=(EMOJI_FONT, font_size),
            )
        self._chat_view.tag_configure(
            "turn_focus",
            background="#EEF2FF",
        )
        self._chat_view.tag_configure(
            "search_match",
            background="#FFF3A3",
            foreground=TEXT_PRIMARY,
        )
        self._chat_view.tag_configure(
            "search_current",
            background="#F4C44E",
            foreground="#172033",
        )
        self._chat_view.bind("<Configure>", self._resize_tool_cards)
        self._build_search_panel(card)

    def _build_search_panel(self, parent: tk.Misc) -> None:
        self._search_panel = tk.Frame(
            parent,
            background="#FFFFFF",
            highlightbackground=EDITOR_BORDER,
            highlightthickness=1,
            borderwidth=0,
        )
        self._search_var = tk.StringVar()
        self._search_status = tk.StringVar(value="输入关键词")
        self._search_entry = tk.Entry(
            self._search_panel,
            textvariable=self._search_var,
            width=24,
            font=("Microsoft YaHei UI", 10),
            background="#F8FAFC",
            foreground=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
        )
        self._search_entry.pack(side="left", padx=(9, 6), pady=7, ipady=3)
        tk.Label(
            self._search_panel,
            textvariable=self._search_status,
            width=8,
            anchor="center",
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(0, 4))
        for label, command in (
            ("↑", self._search_previous),
            ("↓", self._search_next),
            ("×", self._close_search),
        ):
            tk.Button(
                self._search_panel,
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
        self._search_entry.bind("<KeyRelease>", self._schedule_search_refresh)
        self._search_entry.bind("<Return>", self._search_next)
        self._search_entry.bind("<Shift-Return>", self._search_previous)
        self._search_entry.bind("<Escape>", self._close_search)

    def _build_input(self, main: ttk.Frame) -> None:
        composer = ttk.Frame(main, style="Card.TFrame", padding=(12, 9))
        self._composer = composer
        composer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        composer.columnconfigure(0, weight=1)
        composer.bind("<Configure>", self._update_composer_layout)

        self._input = tk.Text(
            composer,
            width=1,
            height=2,
            wrap="word",
            font=("Microsoft YaHei UI", 11),
            background="#F8FAFC",
            foreground=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            highlightbackground=EDITOR_BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            padx=10,
            pady=7,
            undo=True,
        )
        self._input.grid(row=0, column=0, columnspan=3, sticky="ew")
        self._input.bind("<Return>", self._handle_return)
        self._input.bind("<KP_Enter>", self._handle_return)
        self._input.bind("<<Modified>>", self._resize_input)
        self._input.edit_modified(False)

        self._status = tk.StringVar(value="就绪")
        ttk.Label(
            composer,
            textvariable=self._status,
            style="CardMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self._shortcut_hint = ttk.Label(
            composer,
            text="Enter 发送  ·  Shift+Enter 换行  ·  Ctrl+F 查找",
            style="CardMuted.TLabel",
        )
        self._shortcut_hint.grid(
            row=1,
            column=1,
            sticky="e",
            padx=12,
            pady=(10, 0),
        )

        self._stop_button = ttk.Button(
            composer,
            text="停止生成",
            command=self._stop,
            state="disabled",
            style="Danger.TButton",
        )
        self._stop_button.grid(row=1, column=2, sticky="e", pady=(8, 0))
        self._send_button = ttk.Button(
            composer,
            text="发送",
            command=self._send,
            style="Accent.TButton",
        )
        self._send_button.grid(row=1, column=3, sticky="e", padx=(8, 0), pady=(8, 0))
        self._clear_button = ttk.Button(
            composer,
            text="清空对话",
            command=self._clear_conversation,
            style="Secondary.TButton",
        )
        self._clear_button.grid(row=0, column=3, sticky="ne", padx=(10, 0))

    def _update_composer_layout(self, event: tk.Event) -> None:
        if event.width < 720:
            self._shortcut_hint.grid_remove()
        elif not self._shortcut_hint.winfo_manager():
            self._shortcut_hint.grid()

    def _bind_shortcuts(self) -> None:
        self._root.bind("<Control-l>", self._focus_composer)
        self._root.bind("<Control-f>", self._open_search)
        self._root.bind("<Control-Shift-F>", self._focus_session_filter)
        self._root.bind("<Control-Down>", self._shortcut_scroll_to_bottom)
        self._root.bind("<Control-n>", self._shortcut_new_session)
        self._root.bind("<F3>", self._search_next)
        self._root.bind("<Shift-F3>", self._search_previous)
        self._root.bind("<Alt-Up>", self._shortcut_previous_turn)
        self._root.bind("<Alt-Down>", self._shortcut_next_turn)
        self._root.bind("<Escape>", self._handle_escape)

    def _focus_composer(self, _event: tk.Event | None = None) -> str:
        self._input.focus_set()
        return "break"

    def _focus_session_filter(self, _event: tk.Event | None = None) -> str:
        self._session_filter_entry.focus_set()
        self._session_filter_entry.selection_range(0, "end")
        return "break"

    def _schedule_session_filter(self, _event: tk.Event | None = None) -> None:
        if self._session_filter_job is not None:
            self._root.after_cancel(self._session_filter_job)
        self._session_filter_job = self._root.after(
            120,
            self._apply_session_filter,
        )

    def _apply_session_filter(self) -> None:
        self._session_filter_job = None
        self._refresh_sessions()

    def _clear_session_filter(self) -> None:
        if not self._session_filter_var.get():
            return
        self._session_filter_var.set("")
        self._refresh_sessions()

    def _open_search(self, _event: tk.Event | None = None) -> str:
        if not self._search_panel.place_info():
            self._search_panel.place(
                relx=1.0,
                x=-24,
                y=12,
                anchor="ne",
            )
            self._search_panel.lift()
            self._refresh_search_matches()
        self._search_entry.focus_set()
        self._search_entry.selection_range(0, "end")
        return "break"

    def _close_search(self, _event: tk.Event | None = None) -> str:
        if self._search_job is not None:
            self._root.after_cancel(self._search_job)
            self._search_job = None
        self._chat_view.tag_remove("search_match", "1.0", "end")
        self._chat_view.tag_remove("search_current", "1.0", "end")
        self._search_matches.clear()
        self._search_match_index = -1
        self._search_panel.place_forget()
        self._input.focus_set()
        return "break"

    def _schedule_search_refresh(self, _event: tk.Event | None = None) -> None:
        if _event is not None and _event.keysym in {
            "Return",
            "Escape",
            "F3",
            "Shift_L",
            "Shift_R",
            "Up",
            "Down",
        }:
            return
        if self._search_job is not None:
            self._root.after_cancel(self._search_job)
        self._search_job = self._root.after(120, self._refresh_search_matches)

    def _refresh_search_matches(self) -> None:
        self._search_job = None
        self._chat_view.tag_remove("search_match", "1.0", "end")
        self._chat_view.tag_remove("search_current", "1.0", "end")
        self._search_matches.clear()
        self._search_match_index = -1
        query = self._search_var.get().strip()
        if not query:
            self._search_status.set("输入关键词")
            return

        count = tk.IntVar(master=self._root)
        position = "1.0"
        while len(self._search_matches) < 500:
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
            self._search_matches.append((start, finish))
            self._chat_view.tag_add("search_match", start, finish)
            position = finish

        if not self._search_matches:
            self._search_status.set("无结果")
            return
        self._focus_search_match(0)

    def _focus_search_match(self, index: int) -> None:
        if not self._search_matches:
            return
        self._search_match_index = index % len(self._search_matches)
        start, finish = self._search_matches[self._search_match_index]
        self._chat_view.tag_remove("search_current", "1.0", "end")
        self._chat_view.tag_add("search_current", start, finish)
        self._chat_view.tag_raise("search_current")
        self._chat_view.see(start)
        self._search_status.set(
            f"{self._search_match_index + 1} / {len(self._search_matches)}"
        )

    def _search_next(self, _event: tk.Event | None = None) -> str:
        if not self._search_panel.place_info():
            return self._open_search()
        if self._search_job is not None:
            self._root.after_cancel(self._search_job)
            self._refresh_search_matches()
        if self._search_matches:
            self._focus_search_match(self._search_match_index + 1)
        return "break"

    def _search_previous(self, _event: tk.Event | None = None) -> str:
        if not self._search_panel.place_info():
            return self._open_search()
        if self._search_job is not None:
            self._root.after_cancel(self._search_job)
            self._refresh_search_matches()
        if self._search_matches:
            self._focus_search_match(self._search_match_index - 1)
        return "break"

    def _shortcut_scroll_to_bottom(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        self._scroll_to_bottom()
        return "break"

    def _shortcut_new_session(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        if not self._is_busy():
            self._new_session()
        return "break"

    def _shortcut_previous_turn(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        if self._turn_marks:
            current = self._selected_turn_index or len(self._turn_marks)
            self._jump_to_turn(max(1, current - 1))
        return "break"

    def _shortcut_next_turn(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        if self._turn_marks:
            current = self._selected_turn_index or 1
            self._jump_to_turn(min(len(self._turn_marks), current + 1))
        return "break"

    def _handle_escape(self, _event: tk.Event | None = None) -> str:
        if self._search_panel.place_info():
            return self._close_search()
        if self._is_busy():
            self._stop()
        else:
            self._input.focus_set()
        return "break"

    def _handle_return(self, event: tk.Event) -> str | None:
        if event.state & 0x0001:
            return None
        self._send()
        return "break"

    def _resize_input(self, _event: tk.Event | None = None) -> None:
        if not self._input.edit_modified():
            return
        display_count = self._input.count(
            "1.0",
            "end-1c",
            "displaylines",
        )
        line_count = display_count[0] if display_count else 1
        self._input.configure(height=min(6, max(2, line_count)))
        self._input.edit_modified(False)

    def _change_model(self, _event: tk.Event | None = None) -> None:
        previous_model = self._agent.model_name
        selected_model = self._model_selector_var.get().strip()
        if self._is_busy() or selected_model == previous_model:
            self._model_selector_var.set(previous_model)
            return
        try:
            self._agent.select_model(selected_model)
        except Exception as error:
            self._model_selector_var.set(previous_model)
            messagebox.showerror("切换失败", str(error), parent=self._root)
            return
        self._status.set(f"已切换到 {selected_model}，将在下一轮对话中使用")

    def _send(self) -> None:
        if self._is_busy():
            return
        prompt = self._input.get("1.0", "end-1c").strip()
        if not prompt:
            return
        if self._search_panel.place_info():
            self._close_search()
        self._remove_empty_state()

        self._input.delete("1.0", "end")
        self._active_prompt = prompt
        self._register_turn(prompt, select=True)
        self._append_message("您", prompt, "user")
        self._begin_live_response()
        self._assistant_open = False
        self._cancel.clear()
        self._set_busy(True)
        self._start_activity()

        self._worker = threading.Thread(
            target=self._run_chat,
            args=(prompt,),
            daemon=False,
        )
        self._worker.start()

    def _begin_live_response(self) -> None:
        self._clear_live_response_tracking()
        self._chat_view.mark_set(self._live_response_mark, "end-1c")
        self._chat_view.mark_gravity(self._live_response_mark, "left")
        self._live_tool_start = len(self._tool_card_widgets)

    def _run_chat(self, prompt: str) -> None:
        try:
            outcome = self._agent.chat(
                prompt,
                on_text=lambda content: self._events.put(("text", content)),
                on_tool_call=lambda request: self._events.put(
                    ("tool_call", request)
                ),
                on_tool_result=lambda request, result: self._events.put(
                    ("tool_result", (request, result))
                ),
                should_cancel=self._cancel.is_set,
            )
        except AgentCancelledError as error:
            outcome = error.outcome
            self._events.put(
                (
                    "cancelled",
                    (
                        str(error),
                        (
                            outcome.history_preserved
                            if outcome is not None
                            else error.tool_records_preserved
                        ),
                    ),
                )
            )
        except Exception as error:
            outcome = self._agent.last_turn_outcome
            self._events.put(
                (
                    "error",
                    (
                        str(error),
                        (
                            outcome.history_preserved
                            if outcome is not None
                            else self._agent.last_turn_history_preserved
                        ),
                    ),
                )
            )
        else:
            if outcome.status is RunStatus.STEP_LIMIT_REACHED:
                self._events.put(("step_limit_reached", outcome))
            else:
                self._events.put(("done", outcome))

    def _stop(self) -> None:
        if self._is_busy():
            self._cancel.set()
            self._set_activity_phase("正在停止")
            self._stop_button.configure(state="disabled")

    def _drain_events(self) -> None:
        if self._closed:
            return
        pending_events = _dequeue_ui_events(self._events)
        for event_name, payload in _coalesce_ui_events(pending_events):
            self._handle_event(event_name, payload)
        if self._close_pending and not self._busy:
            if self._worker is not None and self._worker.is_alive():
                self._root.after(20, self._drain_events)
            else:
                self._finalize_close()
                if not self._closed:
                    self._root.after(50, self._drain_events)
            return
        # 流式输出期间提高刷新频率；单次批量有上限，避免大量 token 或工具
        # 事件占满主线程，确保滚动、停止按钮和窗口拖动仍能及时响应。
        next_delay = 8 if not self._events.empty() else 20 if self._busy else 50
        self._root.after(next_delay, self._drain_events)

    def _handle_event(self, event_name: str, payload: Any) -> None:
        if event_name == "text":
            starts_new_message = not self._assistant_open
            if not self._assistant_open:
                self._append("DeepSeek\n", "assistant_header")
                self._assistant_open = True
            content = str(payload)
            self._stream_character_count += len(content)
            self._append_turn_answer_preview(
                content,
                starts_new_message=starts_new_message,
            )
            self._append(content, "assistant_body")
            return

        if event_name == "tool_call":
            request: ToolCallRequest = payload
            self._finish_assistant_line()
            self._create_tool_card(
                request.id,
                request.name,
                request.arguments,
            )
            self._set_activity_phase(f"执行工具 {request.name}")
            return

        if event_name == "tool_result":
            request, result = payload
            self._complete_tool_card(
                request.id,
                request.name,
                request.arguments,
                result,
            )
            self._set_activity_phase("继续生成")
            return

        if event_name == "confirmation":
            self._set_activity_phase("等待确认")
            self._show_confirmation(payload)
            if self._is_busy():
                self._set_activity_phase("继续生成")
            return

        if event_name == "done":
            restore_turn = (
                self._selected_turn_index
                if not self._is_chat_at_bottom()
                else None
            )
            self._finish_turn("就绪")
            if not self._render_completed_turn():
                self._render_history()
            self._refresh_navigation(select_session_id=self._agent.session_id)
            self._update_header()
            self._active_prompt = None
            if restore_turn is not None:
                self._root.after_idle(
                    lambda turn=restore_turn: self._jump_to_turn(turn)
                )
            return

        if event_name == "step_limit_reached":
            outcome: TurnOutcome = payload
            self._finish_assistant_line()
            self._finish_turn("未完成")
            if not self._render_completed_turn():
                self._render_history()
            self._refresh_navigation(select_session_id=self._agent.session_id)
            self._update_header()
            self._active_prompt = None
            self._status.set(
                f"已达到执行步数上限，共执行 {outcome.steps_completed} 步。"
            )
            return

        if event_name == "cancelled":
            message, tool_records_preserved = payload
            self._finish_assistant_line()
            self._finish_turn("已停止")
            self._reconcile_interrupted_turn(tool_records_preserved)
            if not tool_records_preserved:
                self._status.set("已停止，问题已放回输入框")
            return

        if event_name == "error":
            message, history_preserved = payload
            self._finish_assistant_line()
            self._finish_turn("调用失败")
            self._reconcile_interrupted_turn(history_preserved)
            summary = _conversation_preview(str(message), limit=52)
            self._status.set(f"调用失败：{summary}")

    def _reconcile_interrupted_turn(self, history_preserved: bool) -> None:
        prompt = self._active_prompt
        if history_preserved:
            if not self._render_completed_turn():
                self._render_history()
        else:
            self._render_history()
            self._restore_prompt_to_composer(prompt)
        self._refresh_navigation(select_session_id=self._agent.session_id)
        self._update_header()
        self._active_prompt = None

    def _restore_prompt_to_composer(self, prompt: str | None) -> None:
        if not prompt:
            return
        draft = self._input.get("1.0", "end-1c")
        if prompt in draft:
            return
        restored = prompt if not draft.strip() else f"{prompt}\n\n{draft}"
        self._input.delete("1.0", "end")
        self._input.insert("1.0", restored)
        self._input.edit_modified(True)
        self._resize_input()
        self._input.mark_set("insert", "end-1c")

    def _show_confirmation(self, request: ConfirmationRequest) -> None:
        dialog = ToolConfirmationDialog(self._root, request)
        request.allowed = dialog.allowed
        request.completed.set()

    def _finish_assistant_line(self) -> None:
        if self._assistant_open:
            self._append("\n", "assistant_body")
            self._assistant_open = False

    def _finish_turn(self, status: str) -> None:
        self._finish_assistant_line()
        self._stop_activity()
        self._set_busy(False)
        self._status.set(status)
        self._input.focus_set()

    def _start_activity(self) -> None:
        self._turn_started_at = time.perf_counter()
        self._stream_character_count = 0
        self._activity_phase = "正在生成"
        self._refresh_activity_status()

    def _set_activity_phase(self, phase: str) -> None:
        self._activity_phase = phase
        self._refresh_activity_status()

    def _refresh_activity_status(self) -> None:
        if self._activity_job is not None:
            self._root.after_cancel(self._activity_job)
            self._activity_job = None
        if self._turn_started_at is None or not self._busy:
            return
        elapsed = time.perf_counter() - self._turn_started_at
        self._status.set(
            _format_activity_status(
                self._activity_phase,
                elapsed,
                self._stream_character_count,
            )
        )
        self._activity_job = self._root.after(
            250,
            self._refresh_activity_status,
        )

    def _stop_activity(self) -> None:
        if self._activity_job is not None:
            self._root.after_cancel(self._activity_job)
            self._activity_job = None
        self._turn_started_at = None
        self._activity_phase = ""

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        interaction_locked = busy or self._close_pending
        normal_or_disabled = "disabled" if interaction_locked else "normal"
        self._send_button.configure(state=normal_or_disabled)
        self._stop_button.configure(
            state="normal" if busy and not self._close_pending else "disabled"
        )
        self._clear_button.configure(state=normal_or_disabled)
        self._model_selector.configure(
            state="disabled" if interaction_locked else "readonly"
        )
        for button in (self._create_project_button, self._new_session_button):
            button.configure(state=normal_or_disabled)
        self._project_tree.configure(
            selectmode="none" if interaction_locked else "browse"
        )
        self._session_tree.configure(
            selectmode="none" if interaction_locked else "browse"
        )
        self._session_filter_entry.configure(
            state="disabled" if interaction_locked else "normal"
        )

    def _is_busy(self) -> bool:
        return self._busy

    def _append(self, content: str, tag: str) -> None:
        was_at_bottom = self._is_chat_at_bottom()
        self._chat_view.configure(state="normal")
        if tag == "assistant_body":
            self._insert_chat_text(content, (tag,))
        else:
            self._chat_view.insert("end", content, tag)
        self._chat_view.configure(state="disabled")
        if was_at_bottom:
            self._chat_view.see("end")

    def _append_message(self, author: str, content: str, role: str) -> None:
        if role == "user":
            self._append_user_message(author, content)
            return
        self._append(f"{author}\n", f"{role}_header")
        self._append(f"{content}\n", f"{role}_body")

    def _append_turn_answer_preview(
        self,
        content: str,
        *,
        starts_new_message: bool = False,
    ) -> None:
        if not self._turn_answers or not content:
            return
        existing = self._turn_answers[-1]
        separator = "  " if starts_new_message and existing else ""
        self._turn_answers[-1] = (existing + separator + content)[:500]
        if not self._rendering_history:
            self._turn_rail.update_answer(
                len(self._turn_answers),
                self._turn_answers[-1],
            )

    def _append_user_message(self, author: str, content: str) -> None:
        self._chat_view.configure(state="normal")
        card = UserMessageCard(self._chat_view, author, content)
        start = self._chat_view.index("end-1c")
        self._chat_view.window_create("end", window=card.frame, padx=8, pady=4)
        finish = self._chat_view.index("end-1c")
        self._chat_view.tag_add("user_message_line", start, finish)
        self._chat_view.insert("end", "\n", "user_message_line")
        self._chat_view.configure(state="disabled")
        self._user_message_widgets.append(card)
        card.set_active(len(self._user_message_widgets) == self._selected_turn_index)
        if not self._rendering_history:
            self._resize_tool_cards()
            self._chat_view.see("end")

    def _append_markdown_message(
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
        if not self._rendering_history:
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
            if style_tag is not None and (
                allow_font_styles or span.style in {"code", "link"}
            ):
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
        self._markdown_code_widgets.append(card)
        if not self._rendering_history:
            self._resize_tool_cards()

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
        self._markdown_table_widgets.append(card)
        if not self._rendering_history:
            self._resize_tool_cards()

    def _register_turn(self, question: str, *, select: bool) -> None:
        turn_number = len(self._turn_marks) + 1
        mark_name = f"conversation_turn_{turn_number}"
        self._chat_view.mark_set(mark_name, "end-1c")
        self._chat_view.mark_gravity(mark_name, "left")
        self._turn_marks.append(mark_name)
        self._turn_questions.append(question)
        self._turn_answers.append("")
        if not self._rendering_history:
            self._refresh_turn_navigation(
                turn_number if select else self._selected_turn_index
            )

    def _reset_turn_navigation(self) -> None:
        if self._turn_marks:
            self._chat_view.mark_unset(*self._turn_marks)
        self._chat_view.tag_remove("turn_focus", "1.0", "end")
        self._turn_marks.clear()
        self._turn_questions.clear()
        self._turn_answers.clear()
        self._selected_turn_index = None
        self._refresh_turn_navigation()

    def _refresh_turn_navigation(
        self,
        selected_turn: int | None = None,
    ) -> None:
        total = len(self._turn_marks)
        if total == 0:
            self._selected_turn_index = None
            self._turn_rail.set_turns([], [], None)
            return

        if selected_turn is None:
            selected_turn = self._selected_turn_index or total
        selected_turn = min(total, max(1, selected_turn))
        self._selected_turn_index = selected_turn
        self._turn_rail.set_turns(
            self._turn_questions,
            self._turn_answers,
            selected_turn,
        )
        for index, card in enumerate(self._user_message_widgets, start=1):
            card.set_active(index == selected_turn)

    def _on_chat_yview(self, first: str, last: str) -> None:
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
        if not self._turn_marks:
            return
        if self._turn_sync_job is not None:
            self._root.after_cancel(self._turn_sync_job)
        self._turn_sync_job = self._root.after_idle(
            self._sync_turn_navigation_to_view
        )

    def _is_chat_at_bottom(self) -> bool:
        return self._chat_view.yview()[1] >= 0.995

    def _scroll_to_bottom(self) -> None:
        self._chat_view.see("end")
        self._bottom_button.place_forget()
        if self._turn_marks:
            self._refresh_turn_navigation(len(self._turn_marks))
        self._input.focus_set()

    def _sync_turn_navigation_to_view(self) -> None:
        self._turn_sync_job = None
        if not self._turn_marks:
            return

        _first, last = self._chat_view.yview()
        if last >= 0.999:
            visible_turn = len(self._turn_marks)
        else:
            viewport_height = max(1, self._chat_view.winfo_height())
            anchor = self._chat_view.index(f"@0,{int(viewport_height * 0.28)}")
            visible_turn = 1
            for number, mark_name in enumerate(self._turn_marks, start=1):
                if self._chat_view.compare(mark_name, "<=", anchor):
                    visible_turn = number
                else:
                    break

        if visible_turn != self._selected_turn_index:
            self._refresh_turn_navigation(visible_turn)

    def _jump_to_turn(self, turn_number: int) -> None:
        if not 1 <= turn_number <= len(self._turn_marks):
            return
        self._refresh_turn_navigation(turn_number)
        mark_name = self._turn_marks[turn_number - 1]
        self._chat_view.tag_remove("turn_focus", "1.0", "end")
        self._chat_view.tag_add(
            "turn_focus",
            mark_name,
            f"{mark_name} lineend+1c",
        )
        self._chat_view.tag_raise("turn_focus")
        self._chat_view.yview(mark_name)

    def _create_tool_card(
        self,
        tool_call_id: str,
        name: str,
        arguments: str,
    ) -> ToolCallCard:
        was_at_bottom = (
            not self._rendering_history and self._is_chat_at_bottom()
        )
        card = ToolCallCard(self._chat_view, name, arguments)
        self._chat_view.configure(state="normal")
        self._chat_view.window_create(
            "end",
            window=card.frame,
            padx=8,
            pady=6,
        )
        self._chat_view.insert("end", "\n")
        self._chat_view.configure(state="disabled")
        self._tool_cards[tool_call_id] = card
        self._tool_card_widgets.append(card)
        if not self._rendering_history:
            self._resize_tool_cards()
        if was_at_bottom:
            self._chat_view.see("end")
        return card

    def _complete_tool_card(
        self,
        tool_call_id: str,
        name: str,
        arguments: str,
        result: str,
    ) -> None:
        card = self._tool_cards.get(tool_call_id)
        if card is None:
            card = self._create_tool_card(tool_call_id, name, arguments)
        card.set_result(result)

    def _resize_tool_cards(self, event: tk.Event | None = None) -> None:
        if event is not None:
            self._pending_chat_width = event.width
            if self._resize_job is not None:
                self._root.after_cancel(self._resize_job)
            self._resize_job = self._root.after(40, self._flush_pending_resize)
            return
        self._apply_embedded_width(self._chat_view.winfo_width())

    def _flush_pending_resize(self) -> None:
        self._resize_job = None
        self._apply_embedded_width(
            self._pending_chat_width or self._chat_view.winfo_width()
        )

    def _apply_embedded_width(self, width: int) -> None:
        card_width = max(260, width - 70)
        for card in self._tool_card_widgets:
            card.set_width(card_width)
        for card in self._markdown_code_widgets:
            card.set_width(card_width)
        pending_tables: list[MarkdownTableCard] = []
        for card in self._markdown_table_widgets:
            if card.set_width(card_width, defer=True):
                pending_tables.append(card)
        pending_users: list[UserMessageCard] = []
        for card in self._user_message_widgets:
            if card.set_max_width(width - 48, defer=True):
                pending_users.append(card)
        if pending_tables or pending_users:
            # 所有 wraplength 先一次性写入，避免每张卡片单独刷新整个 Tk 布局树。
            self._root.update_idletasks()
            for card in pending_tables:
                card.finalize_width()
            for card in pending_users:
                card.finalize_size()

    def _clear_tool_cards(self) -> None:
        if self._empty_state_frame is not None:
            self._empty_state_frame.destroy()
            self._empty_state_frame = None
        for card in self._tool_card_widgets:
            card.destroy()
        for card in self._markdown_code_widgets:
            card.destroy()
        for card in self._markdown_table_widgets:
            card.destroy()
        for card in self._user_message_widgets:
            card.destroy()
        self._tool_cards.clear()
        self._tool_card_widgets.clear()
        self._markdown_code_widgets.clear()
        self._markdown_table_widgets.clear()
        self._user_message_widgets.clear()

    def _show_empty_state(self) -> None:
        if self._empty_state_frame is not None:
            return
        frame = tk.Frame(
            self._chat_view,
            background="#F8FAFC",
            highlightbackground="#E1E7F0",
            highlightthickness=1,
            borderwidth=0,
        )
        tk.Label(
            frame,
            text="从一个清晰目标开始",
            background="#F8FAFC",
            foreground=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(padx=34, pady=(24, 5))
        tk.Label(
            frame,
            text="描述你想完成的事情，也可以附上文件或项目路径。",
            background="#F8FAFC",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 10),
        ).pack(padx=34, pady=(0, 16))
        actions = tk.Frame(frame, background="#F8FAFC")
        actions.pack(padx=24, pady=(0, 22))
        suggestions = (
            ("分析项目", "请分析当前项目的结构，并指出最值得改进的地方。"),
            ("解释代码", "请解释下面代码的作用，并指出潜在问题：\n"),
            ("编写测试", "请为这个功能补充测试，并说明覆盖的边界情况。"),
        )
        for column, (label, prompt) in enumerate(suggestions):
            tk.Button(
                actions,
                text=label,
                command=lambda value=prompt: self._use_prompt_suggestion(value),
                background="#FFFFFF",
                activebackground="#EEF3FF",
                foreground=USER_COLOR,
                activeforeground=ACCENT,
                relief="flat",
                borderwidth=0,
                highlightbackground=EDITOR_BORDER,
                highlightthickness=1,
                cursor="hand2",
                font=("Microsoft YaHei UI", 10),
                padx=14,
                pady=7,
            ).grid(row=0, column=column, padx=5)

        self._chat_view.configure(state="normal")
        start = self._chat_view.index("end-1c")
        self._chat_view.window_create(
            "end",
            window=frame,
            padx=36,
            pady=52,
            align="center",
        )
        finish = self._chat_view.index("end-1c")
        self._chat_view.tag_add("empty_state_line", start, finish)
        self._chat_view.insert("end", "\n", "empty_state_line")
        self._chat_view.tag_configure("empty_state_line", justify="center")
        self._chat_view.configure(state="disabled")
        self._empty_state_frame = frame

    def _remove_empty_state(self) -> None:
        if self._empty_state_frame is None:
            return
        self._empty_state_frame.destroy()
        self._empty_state_frame = None
        self._chat_view.configure(state="normal")
        self._chat_view.delete("1.0", "end")
        self._chat_view.configure(state="disabled")

    def _use_prompt_suggestion(self, prompt: str) -> None:
        if self._is_busy():
            return
        self._input.delete("1.0", "end")
        self._input.insert("1.0", prompt)
        self._input.edit_modified(True)
        self._resize_input()
        self._input.mark_set("insert", "end-1c")
        self._input.focus_set()

    def _clear_live_response_tracking(self) -> None:
        if self._live_response_mark in self._chat_view.mark_names():
            self._chat_view.mark_unset(self._live_response_mark)
        self._live_tool_start = None

    def _render_completed_turn(self) -> bool:
        if (
            self._live_tool_start is None
            or self._live_response_mark not in self._chat_view.mark_names()
        ):
            return False

        tail = _messages_after_last_user(self._agent.history())
        if not tail:
            self._clear_live_response_tracking()
            return False

        was_at_bottom = self._is_chat_at_bottom()
        live_cards = self._tool_card_widgets[self._live_tool_start :]
        live_card_ids = {id(card) for card in live_cards}
        for card in live_cards:
            card.destroy()
        del self._tool_card_widgets[self._live_tool_start :]
        self._tool_cards = {
            tool_call_id: card
            for tool_call_id, card in self._tool_cards.items()
            if id(card) not in live_card_ids
        }

        self._chat_view.configure(state="normal")
        # 保留 Text 控件最后一个换行。若删除到 end，Tk 会把用户气泡后的
        # 分隔行一并折叠，最终的助手标题就会被插回气泡所在显示行。
        self._chat_view.delete(self._live_response_mark, "end-1c")
        self._chat_view.configure(state="disabled")
        self._rendering_history = True
        try:
            for message in tail:
                role = message.get("role")
                if role == "tool":
                    self._complete_tool_card(
                        str(message.get("tool_call_id", "")),
                        str(message.get("name", "未知工具")),
                        "",
                        str(message.get("content", "")),
                    )
                    continue
                if role == "user":
                    continue
                content = message.get("content")
                if content:
                    self._append_markdown_message(
                        "DeepSeek",
                        str(content),
                        "assistant",
                    )
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {})
                    self._create_tool_card(
                        str(call.get("id", "")),
                        str(function.get("name", "未知工具")),
                        str(function.get("arguments", "")),
                    )
        finally:
            self._rendering_history = False
            self._clear_live_response_tracking()

        self._resize_tool_cards()
        if was_at_bottom:
            self._chat_view.see("end")
        self._assistant_open = False
        return True

    def _render_history(self) -> None:
        if self._search_panel.place_info():
            self._close_search()
        self._clear_live_response_tracking()
        self._clear_tool_cards()
        self._chat_view.configure(state="normal")
        self._reset_turn_navigation()
        self._chat_view.delete("1.0", "end")
        self._chat_view.configure(state="disabled")
        self._rendering_history = True
        try:
            for message in self._agent.history():
                role = message.get("role")
                if role == "user":
                    question = str(message.get("content", ""))
                    self._register_turn(question, select=False)
                    self._append_message("您", question, "user")
                elif role == "tool":
                    self._complete_tool_card(
                        str(message.get("tool_call_id", "")),
                        str(message.get("name", "未知工具")),
                        "",
                        str(message.get("content", "")),
                    )
                else:
                    content = message.get("content")
                    if content:
                        self._append_turn_answer_preview(
                            str(content),
                            starts_new_message=bool(
                                self._turn_answers and self._turn_answers[-1]
                            ),
                        )
                        self._append_markdown_message(
                            "DeepSeek",
                            str(content),
                            "assistant",
                        )
                    for call in message.get("tool_calls") or []:
                        function = call.get("function", {})
                        self._create_tool_card(
                            str(call.get("id", "")),
                            str(function.get("name", "未知工具")),
                            str(function.get("arguments", "")),
                        )
        finally:
            self._rendering_history = False
        if not self._turn_marks:
            self._show_empty_state()
        self._resize_tool_cards()
        self._chat_view.see("end")
        self._refresh_turn_navigation()
        self._assistant_open = False

    def _refresh_navigation(
        self,
        *,
        select_project_id: str | None = None,
        select_session_id: str | None = None,
    ) -> None:
        previous_project = select_project_id or self._selected_project_id()
        self._projects = self._agent.list_projects()
        all_sessions = self._agent.list_sessions()

        counts: dict[str | None, int] = {None: 0}
        for session in all_sessions:
            counts[session.project_id] = counts.get(session.project_id, 0) + 1
        self._project_options = [
            ALL_PROJECTS,
            UNASSIGNED_PROJECT,
            *[project.id for project in self._projects],
        ]
        if previous_project not in self._project_options:
            previous_project = ALL_PROJECTS

        for item in self._project_tree.get_children():
            self._project_tree.delete(item)
        self._project_tree.insert(
            "",
            "end",
            iid=ALL_PROJECTS,
            text=f"全部会话  ({len(all_sessions)})",
        )
        self._project_tree.insert(
            "",
            "end",
            iid=UNASSIGNED_PROJECT,
            text=f"未分类  ({counts.get(None, 0)})",
        )
        for project in self._projects:
            self._project_tree.insert(
                "",
                "end",
                iid=project.id,
                text=f"{project.name}  ({counts.get(project.id, 0)})",
            )
        self._project_tree.selection_set(previous_project)
        self._project_tree.focus(previous_project)
        self._project_tree.see(previous_project)
        self._refresh_sessions(all_sessions, select_session_id)

    def _refresh_sessions(
        self,
        all_sessions: list[Session] | None = None,
        select_session_id: str | None = None,
    ) -> None:
        all_sessions = all_sessions or self._agent.list_sessions()
        project_id = self._selected_project_id()
        if project_id == ALL_PROJECTS:
            self._sessions = all_sessions
        elif project_id == UNASSIGNED_PROJECT:
            self._sessions = [item for item in all_sessions if item.project_id is None]
        else:
            self._sessions = [
                item for item in all_sessions if item.project_id == project_id
            ]
        self._sessions = _filter_sessions_by_title(
            self._sessions,
            self._session_filter_var.get(),
        )

        for item in self._session_tree.get_children():
            self._session_tree.delete(item)
        selected_id = select_session_id or self._agent.session_id
        for session in self._sessions:
            marker = "● " if session.id == self._agent.session_id else ""
            self._session_tree.insert(
                "",
                "end",
                iid=session.id,
                text=f"{marker}{session.title}",
            )
        if selected_id in {session.id for session in self._sessions}:
            self._session_tree.selection_set(selected_id)
            self._session_tree.focus(selected_id)
            self._session_tree.see(selected_id)

    def _selected_project_id(self) -> str:
        selected = self._project_tree.selection()
        return selected[0] if selected else ALL_PROJECTS

    def _selected_session(self) -> Session | None:
        selected = self._session_tree.selection()
        if not selected:
            return None
        session_id = selected[0]
        return next((item for item in self._sessions if item.id == session_id), None)

    def _on_project_selected(self, _event: tk.Event | None = None) -> None:
        self._refresh_sessions()

    def _build_context_menus(self) -> None:
        self._project_menu = tk.Menu(self._root, tearoff=False)
        self._project_menu.add_command(label="新建项目", command=self._create_project)
        self._project_menu.add_separator()
        self._project_menu.add_command(
            label="重命名项目",
            command=self._rename_selected_project,
        )
        self._project_menu.add_command(
            label="删除项目",
            command=self._delete_selected_project,
        )

        self._move_menu = tk.Menu(self._root, tearoff=False)
        self._session_menu = tk.Menu(self._root, tearoff=False)
        self._session_menu.add_command(label="新建会话", command=self._new_session)
        self._session_menu.add_separator()
        self._session_menu.add_command(
            label="打开会话",
            command=self._load_selected_session,
        )
        self._session_menu.add_command(
            label="重命名会话",
            command=self._rename_selected_session,
        )
        self._session_menu.add_cascade(label="移动到项目", menu=self._move_menu)
        self._session_menu.add_separator()
        self._session_menu.add_command(
            label="删除会话",
            command=self._delete_selected_session,
        )

    def _show_project_menu(self, event: tk.Event) -> None:
        if self._is_busy():
            return
        item = self._project_tree.identify_row(event.y)
        if item:
            self._project_tree.selection_set(item)
            self._project_tree.focus(item)
        project_id = self._selected_project_id()
        state = (
            "normal"
            if project_id not in {ALL_PROJECTS, UNASSIGNED_PROJECT}
            else "disabled"
        )
        self._project_menu.entryconfigure("重命名项目", state=state)
        self._project_menu.entryconfigure("删除项目", state=state)
        try:
            self._project_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._project_menu.grab_release()

    def _show_session_menu(self, event: tk.Event) -> None:
        if self._is_busy():
            return
        item = self._session_tree.identify_row(event.y)
        if item:
            self._session_tree.selection_set(item)
            self._session_tree.focus(item)
        session = self._selected_session()
        state = "normal" if session is not None else "disabled"
        for label in ("打开会话", "重命名会话", "移动到项目", "删除会话"):
            self._session_menu.entryconfigure(label, state=state)

        self._move_menu.delete(0, "end")
        self._move_menu.add_command(
            label="未分类",
            command=lambda: self._move_session_to_project(None),
        )
        if self._projects:
            self._move_menu.add_separator()
        for project in self._projects:
            self._move_menu.add_command(
                label=project.name,
                command=lambda project_id=project.id: self._move_session_to_project(
                    project_id
                ),
            )
        try:
            self._session_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._session_menu.grab_release()

    def _create_project(self) -> None:
        if self._is_busy():
            return
        name = simpledialog.askstring(
            "创建项目",
            "项目名称：",
            parent=self._root,
        )
        if name is None:
            return
        try:
            project = self._agent.create_project(name)
        except Exception as error:
            messagebox.showerror("创建失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_project_id=project.id)
        self._status.set(f"已创建项目：{project.name}")

    def _rename_selected_project(self) -> None:
        if self._is_busy():
            return
        project = self._selected_project()
        if project is None:
            return
        name = simpledialog.askstring(
            "重命名项目",
            "新的项目名称：",
            initialvalue=project.name,
            parent=self._root,
        )
        if name is None:
            return
        try:
            renamed = self._agent.rename_project(project.id, name)
        except Exception as error:
            messagebox.showerror("重命名失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_project_id=renamed.id)
        self._update_header()
        self._status.set(f"项目已重命名为：{renamed.name}")

    def _delete_selected_project(self) -> None:
        if self._is_busy():
            return
        project = self._selected_project()
        if project is None:
            return
        session_count = sum(
            1 for session in self._agent.list_sessions() if session.project_id == project.id
        )
        if not messagebox.askyesno(
            "删除项目",
            f"确定删除项目“{project.name}”吗？\n"
            f"其中的 {session_count} 个会话会移动到“未分类”，不会被删除。",
            parent=self._root,
        ):
            return
        try:
            self._agent.delete_project(project.id)
        except Exception as error:
            messagebox.showerror("删除失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_project_id=UNASSIGNED_PROJECT)
        self._update_header()
        self._status.set(f"项目已删除：{project.name}")

    def _selected_project(self) -> Project | None:
        project_id = self._selected_project_id()
        return next(
            (project for project in self._projects if project.id == project_id),
            None,
        )

    def _new_session(self) -> None:
        if self._is_busy():
            return
        try:
            project_id = self._selected_project_id()
            target_project_id = (
                project_id
                if project_id not in {ALL_PROJECTS, UNASSIGNED_PROJECT}
                else None
            )
            session = self._agent.start_new_session(target_project_id)
        except Exception as error:
            messagebox.showerror("创建失败", str(error), parent=self._root)
            return
        self._session_filter_var.set("")
        self._render_history()
        self._refresh_navigation(select_session_id=session.id)
        self._update_header()
        self._status.set("已创建新会话")

    def _load_selected_session(self, _event: tk.Event | None = None) -> None:
        if self._is_busy():
            return
        session = self._selected_session()
        if session is None or session.id == self._agent.session_id:
            return
        try:
            self._agent.load_session(session.id)
        except Exception as error:
            messagebox.showerror("加载失败", str(error), parent=self._root)
            return
        self._render_history()
        self._refresh_navigation(select_session_id=session.id)
        self._update_header()
        self._status.set(f"已加载会话：{session.title}")

    def _rename_selected_session(self) -> None:
        if self._is_busy():
            return
        session = self._selected_session()
        if session is None:
            messagebox.showinfo("重命名会话", "请先选择一个会话。", parent=self._root)
            return
        title = simpledialog.askstring(
            "重命名会话",
            "新的会话名称：",
            initialvalue=session.title,
            parent=self._root,
        )
        if title is None:
            return
        try:
            renamed = self._agent.rename_session(session.id, title)
        except Exception as error:
            messagebox.showerror("重命名失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_session_id=renamed.id)
        self._update_header()
        self._status.set(f"会话已重命名为：{renamed.title}")

    def _move_session_to_project(self, project_id: str | None) -> None:
        if self._is_busy():
            return
        session = self._selected_session()
        if session is None:
            return
        if project_id == session.project_id:
            return
        try:
            moved = self._agent.move_session(session.id, project_id)
        except Exception as error:
            messagebox.showerror("移动失败", str(error), parent=self._root)
            return
        self._refresh_navigation(select_session_id=moved.id)
        self._update_header()
        self._status.set("会话已移动")

    def _toggle_sidebar(self) -> None:
        if self._sidebar_visible:
            self._paned.forget(self._sidebar)
            self._sidebar_visible = False
            return
        self._paned.add(
            self._sidebar,
            before=self._main_panel,
            minsize=190,
            width=240,
            stretch="never",
        )
        self._sidebar_visible = True
        self._root.after_idle(lambda: self._paned.sash_place(0, 240, 0))

    def _delete_selected_session(self) -> None:
        if self._is_busy():
            return
        session = self._selected_session()
        if session is None:
            messagebox.showinfo("删除会话", "请先选择一个会话。", parent=self._root)
            return
        if not messagebox.askyesno(
            "删除会话",
            f"确定永久删除会话“{session.title}”吗？\n此操作无法撤销。",
            parent=self._root,
        ):
            return
        was_current = session.id == self._agent.session_id
        selected_project = self._selected_project_id()
        try:
            replacement_project_id = (
                selected_project
                if was_current
                and selected_project not in {ALL_PROJECTS, UNASSIGNED_PROJECT}
                else None
            )
            self._agent.delete_session(
                session.id,
                replacement_project_id=replacement_project_id,
            )
        except Exception as error:
            messagebox.showerror("删除失败", str(error), parent=self._root)
            return
        if was_current:
            self._render_history()
        self._refresh_navigation(select_session_id=self._agent.session_id)
        self._update_header()
        self._status.set("会话已删除")

    def _clear_conversation(self) -> None:
        if self._is_busy():
            return
        if not messagebox.askyesno(
            "清空对话",
            "确定清空当前会话的全部消息吗？\n会话本身和项目归属会保留。",
            parent=self._root,
        ):
            return
        try:
            self._agent.clear_conversation()
        except Exception as error:
            messagebox.showerror("清空失败", str(error), parent=self._root)
            return
        self._render_history()
        self._refresh_navigation(select_session_id=self._agent.session_id)
        self._status.set("当前对话已清空")

    def _update_header(self) -> None:
        sessions = self._agent.list_sessions()
        current = next(
            (session for session in sessions if session.id == self._agent.session_id),
            None,
        )
        title = current.title if current is not None else self._agent.session_title
        project_id = current.project_id if current is not None else None
        project_name = next(
            (
                project.name
                for project in self._agent.list_projects()
                if project.id == project_id
            ),
            "未分类",
        )
        self._session_title.set(title)
        self._session_project.set(project_name)

    def _on_close(self) -> None:
        if self._close_pending or self._closed:
            return
        if self._is_busy() and not messagebox.askyesno(
            "退出",
            "Agent 正在生成回答，确定停止并退出吗？",
            parent=self._root,
        ):
            return
        self._close_pending = True
        self._closing.set()
        self._cancel.set()
        self._set_busy(self._is_busy())
        if self._is_busy():
            self._set_activity_phase("正在安全退出")
            self._status.set("正在停止当前任务并保存会话…")
            return
        self._finalize_close()

    def _finalize_close(self) -> None:
        if self._closed:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        try:
            self._agent.save_session()
        except Exception as error:
            self._close_pending = False
            self._closing.clear()
            self._set_busy(False)
            self._status.set("退出失败：会话未能保存")
            messagebox.showerror(
                "退出失败",
                f"会话保存失败，窗口将保持打开：\n{error}",
                parent=self._root,
            )
            return
        self._closed = True
        self._stop_activity()
        self._root.destroy()


def run_gui(
    settings: Settings,
    logger: logging.Logger | None = None,
) -> None:
    root = tk.Tk()
    AgentApp(root, settings, logger=logger)
    root.mainloop()
