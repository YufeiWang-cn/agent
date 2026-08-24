"""构建 Tk 桌面应用，并协调后台 Agent 线程和主线程界面事件。"""

import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from ..agent import Agent, AgentCancelledError
from ..config import Settings
from ..models import ToolCallRequest
from ..runtime import AgentEvent, AgentEventType, RunStatus, TurnOutcome
from .confirmation import (
    ConfirmationRequest,
    TkToolConfirmer,
    ToolConfirmationDialog,
)
from .conversation_view import ConversationView
from .formatting import _conversation_preview
from .sidebar import ProjectSessionSidebar
from .theme import (
    ACCENT,
    ACCENT_HOVER,
    APP_BACKGROUND,
    CARD_BACKGROUND,
    DANGER,
    EDITOR_BORDER,
    SIDEBAR_BACKGROUND,
    SIDEBAR_PANEL,
    SIDEBAR_TEXT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


def _coalesce_ui_events(
    events: list[tuple[str, Any]],
) -> list[tuple[str, Any]]:
    """合并相邻文本事件，降低流式输出对 Tk 布局的刷新压力。"""
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
    """每次按数量上限取出一批事件，防止快速流式输出阻塞 Tk 事件循环。"""
    events: list[tuple[str, Any]] = []
    for _index in range(max(1, limit)):
        try:
            events.append(event_queue.get_nowait())
        except queue.Empty:
            break
    return events


def _format_activity_status(phase: str, elapsed: float, characters: int) -> str:
    details = [phase, f"{max(0.0, elapsed):.1f}s"]
    if characters:
        details.append(f"{characters} 字符")
    return "  ·  ".join(details)


class AgentApp:
    """管理桌面应用布局、后台任务、流式事件和安全关闭流程。"""

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
        self._turn_started_at: float | None = None
        self._stream_character_count = 0
        self._activity_phase = ""
        self._activity_job: str | None = None
        self._drain_job: str | None = None
        self._layout_job: str | None = None
        self._restore_turn_job: str | None = None
        self._pending_restore_turn: int | None = None
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
        self._sidebar.refresh()
        self._update_header()
        self._input.focus_set()
        self._show_recovery_issues()
        self._schedule_event_drain(50)

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

        self._sidebar = ProjectSessionSidebar(
            self._paned,
            self._agent,
            is_busy=self._is_busy,
            render_history=self._render_history,
            update_header=self._update_header,
            set_status=lambda message: self._status.set(message),
        )
        self._main_panel = ttk.Frame(
            self._paned,
            style="App.TFrame",
            padding=(20, 16, 20, 14),
        )
        self._paned.add(self._sidebar, minsize=190, width=240, stretch="never")
        self._paned.add(self._main_panel, minsize=520, stretch="always")
        self._sidebar_visible = True

        self._main_panel.columnconfigure(0, weight=1)
        self._main_panel.rowconfigure(1, weight=1)
        self._build_header(self._main_panel)
        self._conversation = ConversationView(
            self._main_panel,
            on_prompt_suggestion=self._use_prompt_suggestion,
            focus_composer=lambda: self._input.focus_set(),
        )
        self._conversation.grid(row=1, column=0, sticky="nsew")
        self._build_input(self._main_panel)
        self._schedule_sidebar_layout()

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
        return self._sidebar.focus_filter(_event)

    def _open_search(self, event: tk.Event | None = None) -> str:
        return self._conversation.open_search(event)

    def _close_search(self, event: tk.Event | None = None) -> str:
        return self._conversation.close_search(event)

    def _search_next(self, event: tk.Event | None = None) -> str:
        return self._conversation.search_next(event)

    def _search_previous(self, event: tk.Event | None = None) -> str:
        return self._conversation.search_previous(event)

    def _shortcut_scroll_to_bottom(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        self._conversation.scroll_to_bottom()
        return "break"

    def _shortcut_new_session(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        if not self._is_busy():
            self._sidebar.new_session()
        return "break"

    def _shortcut_previous_turn(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        self._conversation.previous_turn()
        return "break"

    def _shortcut_next_turn(
        self,
        _event: tk.Event | None = None,
    ) -> str:
        self._conversation.next_turn()
        return "break"

    def _handle_escape(self, _event: tk.Event | None = None) -> str:
        if self._conversation.search_open:
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
        if self._conversation.search_open:
            self._close_search()
        self._conversation.remove_empty_state()

        self._input.delete("1.0", "end")
        self._active_prompt = prompt
        self._conversation.register_turn(prompt, select=True)
        self._conversation.append_message("您", prompt, "user")
        self._conversation.begin_live_response()
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
                on_event=self._queue_runtime_event,
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
            elif outcome.status is RunStatus.WAITING_USER:
                self._events.put(("waiting_user", outcome))
            else:
                self._events.put(("done", outcome))

    def _queue_runtime_event(self, event: AgentEvent) -> None:
        """把需要 GUI 展示的统一运行事件转发到 Tk 主线程。"""
        if event.type is AgentEventType.PLAN_UPDATED:
            self._events.put(("plan_updated", event.plan))

    def _stop(self) -> None:
        if self._is_busy():
            self._cancel.set()
            self._set_activity_phase("正在停止")
            self._stop_button.configure(state="disabled")

    def _drain_events(self) -> None:
        self._drain_job = None
        if self._closed:
            return
        pending_events = _dequeue_ui_events(self._events)
        for event_name, payload in _coalesce_ui_events(pending_events):
            self._handle_event(event_name, payload)
        if self._close_pending and not self._busy:
            if self._worker is not None and self._worker.is_alive():
                self._schedule_event_drain(20)
            else:
                self._finalize_close()
                if not self._closed:
                    self._schedule_event_drain(50)
            return
        # 流式输出期间提高事件刷新频率。
        # 批量上限可避免事件占满主线程，确保界面操作仍能及时响应。
        next_delay = 8 if not self._events.empty() else 20 if self._busy else 50
        self._schedule_event_drain(next_delay)

    def _schedule_event_drain(self, delay: int) -> None:
        if self._closed or self._drain_job is not None:
            return
        self._drain_job = self._root.after(delay, self._drain_events)

    def _handle_event(self, event_name: str, payload: Any) -> None:
        if event_name == "text":
            starts_new_message = not self._assistant_open
            if not self._assistant_open:
                self._conversation.append("DeepSeek\n", "assistant_header")
                self._assistant_open = True
            content = str(payload)
            self._stream_character_count += len(content)
            self._conversation.append_turn_answer_preview(
                content,
                starts_new_message=starts_new_message,
            )
            self._conversation.append(content, "assistant_body")
            return

        if event_name == "tool_call":
            request: ToolCallRequest = payload
            self._finish_assistant_line()
            self._conversation.create_tool_card(
                request.id,
                request.name,
                request.arguments,
            )
            self._set_activity_phase(f"执行工具 {request.name}")
            return

        if event_name == "tool_result":
            request, result = payload
            self._conversation.complete_tool_card(
                request.id,
                request.name,
                request.arguments,
                result,
            )
            self._set_activity_phase("继续生成")
            return

        if event_name == "plan_updated":
            self._conversation.show_plan(payload)
            return

        if event_name == "confirmation":
            self._set_activity_phase("等待确认")
            self._show_confirmation(payload)
            if self._is_busy():
                self._set_activity_phase("继续生成")
            return

        if event_name == "done":
            restore_turn = (
                self._conversation.selected_turn_index
                if not self._conversation.is_at_bottom()
                else None
            )
            self._finish_turn("就绪")
            if not self._render_completed_turn():
                self._render_history()
            self._sidebar.refresh(select_session_id=self._agent.session_id)
            self._update_header()
            self._active_prompt = None
            if restore_turn is not None:
                self._schedule_turn_restore(restore_turn)
            return

        if event_name == "step_limit_reached":
            outcome: TurnOutcome = payload
            self._finish_assistant_line()
            self._finish_turn("未完成")
            if not self._render_completed_turn():
                self._render_history()
            self._sidebar.refresh(select_session_id=self._agent.session_id)
            self._update_header()
            self._active_prompt = None
            self._status.set(
                f"已达到执行步数上限，共执行 {outcome.steps_completed} 步。"
            )
            return

        if event_name == "waiting_user":
            self._finish_assistant_line()
            self._finish_turn("等待输入")
            if not self._render_completed_turn():
                self._render_history()
            self._sidebar.refresh(select_session_id=self._agent.session_id)
            self._update_header()
            self._active_prompt = None
            self._status.set("执行计划已暂停，请根据 Agent 的问题继续回复。")
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
        self._sidebar.refresh(select_session_id=self._agent.session_id)
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
            self._conversation.append("\n", "assistant_body")
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
        self._sidebar.set_interaction_locked(interaction_locked)

    def _is_busy(self) -> bool:
        return self._busy

    def _render_completed_turn(self) -> bool:
        rendered = self._conversation.render_completed_turn(self._agent.history())
        if rendered:
            # 增量渲染不会经过完整历史恢复流程，因此需要显式同步顶部计划卡片。
            self._conversation.show_plan(
                getattr(self._agent, "current_plan", None)
            )
            self._assistant_open = False
        return rendered

    def _render_history(self) -> None:
        self._conversation.render_history(self._agent.history())
        # 自定义旧 Agent 可以尚未提供计划属性，此时保持计划区域隐藏。
        self._conversation.show_plan(getattr(self._agent, "current_plan", None))
        self._assistant_open = False

    def _use_prompt_suggestion(self, prompt: str) -> None:
        if self._is_busy():
            return
        self._input.delete("1.0", "end")
        self._input.insert("1.0", prompt)
        self._input.edit_modified(True)
        self._resize_input()
        self._input.mark_set("insert", "end-1c")
        self._input.focus_set()

    def _schedule_turn_restore(self, turn_number: int) -> None:
        self._cancel_job("_restore_turn_job")
        self._pending_restore_turn = turn_number
        self._restore_turn_job = self._root.after_idle(
            self._restore_selected_turn
        )

    def _restore_selected_turn(self) -> None:
        self._restore_turn_job = None
        turn_number = self._pending_restore_turn
        self._pending_restore_turn = None
        if self._closed or turn_number is None:
            return
        self._conversation.jump_to_turn(turn_number)

    def _schedule_sidebar_layout(self) -> None:
        self._cancel_job("_layout_job")
        self._layout_job = self._root.after_idle(self._place_sidebar_sash)

    def _place_sidebar_sash(self) -> None:
        self._layout_job = None
        if self._closed or not self._sidebar_visible:
            return
        self._paned.sash_place(0, 240, 0)

    def _cancel_job(self, attribute: str) -> None:
        job = getattr(self, attribute)
        if job is None:
            return
        try:
            self._root.after_cancel(job)
        except tk.TclError:
            pass
        setattr(self, attribute, None)

    def _cancel_ui_jobs(self) -> None:
        for attribute in (
            "_drain_job",
            "_layout_job",
            "_restore_turn_job",
        ):
            self._cancel_job(attribute)
        self._pending_restore_turn = None

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
        self._schedule_sidebar_layout()

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
        self._sidebar.refresh(select_session_id=self._agent.session_id)
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
        self._cancel_ui_jobs()
        self._sidebar.cancel_pending_callbacks()
        self._conversation.dispose()
        self._root.destroy()


def run_gui(
    settings: Settings,
    logger: logging.Logger | None = None,
) -> None:
    """创建 Tk 根窗口并运行桌面应用主循环。"""
    root = tk.Tk()
    AgentApp(root, settings, logger=logger)
    root.mainloop()
