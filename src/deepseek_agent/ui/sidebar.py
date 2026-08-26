"""实现桌面应用中的项目与会话导航和管理操作。"""

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Callable, Protocol

from ..memory import Project, Session
from .theme import SIDEBAR_BACKGROUND, SIDEBAR_PANEL, SIDEBAR_TEXT, TEXT_SECONDARY


ALL_PROJECTS = "__all_projects__"
UNASSIGNED_PROJECT = "__unassigned_project__"


class NavigationAgent(Protocol):
    """约束侧边栏所需的项目和会话管理接口。"""

    @property
    def session_id(self) -> str: ...

    def list_projects(self) -> list[Project]: ...

    def list_sessions(self) -> list[Session]: ...

    def create_project(self, name: str) -> Project: ...

    def rename_project(self, project_id: str, name: str) -> Project: ...

    def delete_project(self, project_id: str) -> Project: ...

    def start_new_session(self, project_id: str | None = None) -> Session: ...

    def load_session(self, session_id: str) -> Session: ...

    def rename_session(self, session_id: str, title: str) -> Session: ...

    def move_session(self, session_id: str, project_id: str | None) -> Session: ...

    def delete_session(
        self,
        session_id: str,
        replacement_project_id: str | None = None,
    ) -> str: ...


def _filter_sessions_by_title(
    sessions: list[Session],
    query: str,
) -> list[Session]:
    """使用不区分大小写的关键字筛选会话标题。"""
    normalized = query.strip().casefold()
    if not normalized:
        return sessions
    return [session for session in sessions if normalized in session.title.casefold()]


class ProjectSessionSidebar(ttk.Frame):
    """管理项目树、会话树、上下文菜单和相关操作。"""

    def __init__(
        self,
        parent: tk.Misc,
        agent: NavigationAgent,
        *,
        is_busy: Callable[[], bool],
        render_history: Callable[[], None],
        update_header: Callable[[], None],
        set_status: Callable[[str], None],
    ) -> None:
        super().__init__(parent, style="Sidebar.TFrame")
        self._root = self.winfo_toplevel()
        self._agent = agent
        self._is_busy = is_busy
        self._render_history = render_history
        self._update_header = update_header
        self._set_status = set_status
        self._projects: list[Project] = []
        self._sessions: list[Session] = []
        self._filter_job: str | None = None

        self._build()

    def _build(self) -> None:
        """按视觉区域构建侧栏，保持控件创建顺序清晰可查。"""
        self._build_brand()
        self._build_project_tree()
        self._build_session_filter()
        self._build_session_tree()
        self._build_context_menus()

    def _build_brand(self) -> None:
        brand = ttk.Frame(self, style="Sidebar.TFrame", padding=(16, 16, 16, 8))
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
            self,
            text="＋  新建会话",
            command=self.new_session,
            style="Sidebar.TButton",
        )
        self._new_session_button.pack(fill="x", padx=12, pady=(4, 12))

    def _build_project_tree(self) -> None:
        project_header = ttk.Frame(self, style="Sidebar.TFrame", padding=(16, 4))
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

        project_frame = ttk.Frame(self, style="Sidebar.TFrame")
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
        self._project_tree.configure(yscrollcommand=project_scroll_y.set)
        self._project_tree.column("#0", width=180, minwidth=100, stretch=True)
        self._project_tree.grid(row=0, column=0, sticky="nsew")
        project_scroll_y.grid(row=0, column=1, sticky="ns")
        project_frame.columnconfigure(0, weight=1)
        self._project_tree.bind("<<TreeviewSelect>>", self._on_project_selected)
        self._project_tree.bind("<Button-3>", self._show_project_menu)

    def _build_session_filter(self) -> None:
        ttk.Separator(self).pack(fill="x", padx=14, pady=(0, 9))
        ttk.Label(
            self,
            text="会话  ·  右键管理",
            style="SidebarMuted.TLabel",
        ).pack(anchor="w", padx=16, pady=(0, 6))
        session_filter = tk.Frame(self, background=SIDEBAR_BACKGROUND)
        session_filter.pack(fill="x", padx=(14, 9), pady=(0, 8))
        self._filter_var = tk.StringVar()
        tk.Label(
            session_filter,
            text="筛选",
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(8, 3))
        self._filter_entry = tk.Entry(
            session_filter,
            textvariable=self._filter_var,
            font=("Microsoft YaHei UI", 9),
            background="#FFFFFF",
            foreground=SIDEBAR_TEXT,
            insertbackground=SIDEBAR_TEXT,
            relief="flat",
            borderwidth=0,
        )
        self._filter_entry.pack(
            side="left",
            fill="x",
            expand=True,
            ipady=6,
        )
        self._filter_entry.bind("<KeyRelease>", self._schedule_filter)
        tk.Button(
            session_filter,
            text="×",
            command=self._clear_filter,
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

    def _build_session_tree(self) -> None:
        session_frame = ttk.Frame(self, style="Sidebar.TFrame")
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
        self._session_tree.configure(yscrollcommand=session_scroll_y.set)
        self._session_tree.column("#0", width=180, minwidth=100, stretch=True)
        self._session_tree.grid(row=0, column=0, sticky="nsew")
        session_scroll_y.grid(row=0, column=1, sticky="ns")
        session_frame.rowconfigure(0, weight=1)
        session_frame.columnconfigure(0, weight=1)
        self._session_tree.bind("<Double-Button-1>", self._load_selected_session)
        self._session_tree.bind("<Return>", self._load_selected_session)
        self._session_tree.bind("<Button-3>", self._show_session_menu)

    def focus_filter(self, _event: tk.Event | None = None) -> str:
        self._filter_entry.focus_set()
        self._filter_entry.selection_range(0, "end")
        return "break"

    def set_interaction_locked(self, locked: bool) -> None:
        state = "disabled" if locked else "normal"
        self._create_project_button.configure(state=state)
        self._new_session_button.configure(state=state)
        self._project_tree.configure(selectmode="none" if locked else "browse")
        self._session_tree.configure(selectmode="none" if locked else "browse")
        self._filter_entry.configure(state=state)

    def cancel_pending_callbacks(self) -> None:
        if self._filter_job is None:
            return
        try:
            self._root.after_cancel(self._filter_job)
        except tk.TclError:
            pass
        self._filter_job = None

    def refresh(
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
        project_options = [
            ALL_PROJECTS,
            UNASSIGNED_PROJECT,
            *[project.id for project in self._projects],
        ]
        if previous_project not in project_options:
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
        if all_sessions is None:
            all_sessions = self._agent.list_sessions()
        project_id = self._selected_project_id()
        if project_id == ALL_PROJECTS:
            sessions = all_sessions
        elif project_id == UNASSIGNED_PROJECT:
            sessions = [item for item in all_sessions if item.project_id is None]
        else:
            sessions = [
                item for item in all_sessions if item.project_id == project_id
            ]
        self._sessions = _filter_sessions_by_title(sessions, self._filter_var.get())

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

    def _schedule_filter(self, _event: tk.Event | None = None) -> None:
        if self._filter_job is not None:
            self._root.after_cancel(self._filter_job)
        self._filter_job = self._root.after(120, self._apply_filter)

    def _apply_filter(self) -> None:
        self._filter_job = None
        self._refresh_sessions()

    def _clear_filter(self) -> None:
        if not self._filter_var.get():
            return
        self._filter_var.set("")
        self._refresh_sessions()

    def _selected_project_id(self) -> str:
        selected = self._project_tree.selection()
        return selected[0] if selected else ALL_PROJECTS

    def _selected_project(self) -> Project | None:
        project_id = self._selected_project_id()
        return next(
            (project for project in self._projects if project.id == project_id),
            None,
        )

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
        self._session_menu.add_command(label="新建会话", command=self.new_session)
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
        name = simpledialog.askstring("创建项目", "项目名称：", parent=self._root)
        if name is None:
            return
        try:
            project = self._agent.create_project(name)
        except Exception as error:
            messagebox.showerror("创建失败", str(error), parent=self._root)
            return
        self.refresh(select_project_id=project.id)
        self._set_status(f"已创建项目：{project.name}")

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
        self.refresh(select_project_id=renamed.id)
        self._update_header()
        self._set_status(f"项目已重命名为：{renamed.name}")

    def _delete_selected_project(self) -> None:
        if self._is_busy():
            return
        project = self._selected_project()
        if project is None:
            return
        session_count = sum(
            1
            for session in self._agent.list_sessions()
            if session.project_id == project.id
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
        self.refresh(select_project_id=UNASSIGNED_PROJECT)
        self._update_header()
        self._set_status(f"项目已删除：{project.name}")

    def new_session(self) -> None:
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
        self._filter_var.set("")
        self._render_history()
        self.refresh(select_session_id=session.id)
        self._update_header()
        self._set_status("已创建新会话")

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
        self.refresh(select_session_id=session.id)
        self._update_header()
        self._set_status(f"已加载会话：{session.title}")

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
        self.refresh(select_session_id=renamed.id)
        self._update_header()
        self._set_status(f"会话已重命名为：{renamed.title}")

    def _move_session_to_project(self, project_id: str | None) -> None:
        if self._is_busy():
            return
        session = self._selected_session()
        if session is None or project_id == session.project_id:
            return
        try:
            moved = self._agent.move_session(session.id, project_id)
        except Exception as error:
            messagebox.showerror("移动失败", str(error), parent=self._root)
            return
        self.refresh(select_session_id=moved.id)
        self._update_header()
        self._set_status("会话已移动")

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
            has_selected_project = selected_project not in {
                ALL_PROJECTS,
                UNASSIGNED_PROJECT,
            }
            can_keep_selected_project = was_current and has_selected_project
            replacement_project_id = (
                selected_project if can_keep_selected_project else None
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
        self.refresh(select_session_id=self._agent.session_id)
        self._update_header()
        self._set_status("会话已删除")
