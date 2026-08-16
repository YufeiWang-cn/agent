"""验证项目与会话侧边栏的交互状态。"""

import tkinter as tk
import unittest
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.memory import Project, Session
from deepseek_agent.ui.sidebar import ProjectSessionSidebar


class FakeNavigationAgent:
    def __init__(self) -> None:
        self.projects = [Project.create("演示项目")]
        self.sessions = [
            Session.create([], title="项目会话", project_id=self.projects[0].id),
            Session.create([], title="未分类会话"),
        ]
        self.current_session_id = self.sessions[0].id
        self.started_in_project: str | None = None
        self.start_error: Exception | None = None

    @property
    def session_id(self) -> str:
        return self.current_session_id

    def list_projects(self) -> list[Project]:
        return list(self.projects)

    def list_sessions(self) -> list[Session]:
        return list(self.sessions)

    def start_new_session(self, project_id: str | None = None) -> Session:
        if self.start_error is not None:
            raise self.start_error
        self.started_in_project = project_id
        session = Session.create([], title="新会话", project_id=project_id)
        self.sessions.append(session)
        self.current_session_id = session.id
        return session


class ProjectSessionSidebarTests(unittest.TestCase):
    def _create_sidebar(
        self,
        root: tk.Tk,
        agent: FakeNavigationAgent,
    ) -> tuple[ProjectSessionSidebar, list[str], list[str]]:
        rendered: list[str] = []
        statuses: list[str] = []
        sidebar = ProjectSessionSidebar(
            root,
            agent,
            is_busy=lambda: False,
            render_history=lambda: rendered.append("rendered"),
            update_header=lambda: rendered.append("header"),
            set_status=statuses.append,
        )
        sidebar.pack(fill="both", expand=True)
        return sidebar, rendered, statuses

    def test_refresh_groups_sessions_and_applies_title_filter(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            agent = FakeNavigationAgent()
            sidebar, _rendered, _statuses = self._create_sidebar(root, agent)

            sidebar.refresh(select_project_id=agent.projects[0].id)

            self.assertEqual(
                sidebar._project_tree.item(agent.projects[0].id, "text"),
                "演示项目  (1)",
            )
            self.assertEqual(
                sidebar._session_tree.get_children(),
                (agent.sessions[0].id,),
            )

            sidebar._filter_var.set(" 不存在 ")
            sidebar._refresh_sessions(agent.list_sessions())

            self.assertEqual(sidebar._session_tree.get_children(), ())
        finally:
            root.destroy()

    def test_new_session_uses_selected_project_and_notifies_main_view(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            agent = FakeNavigationAgent()
            sidebar, rendered, statuses = self._create_sidebar(root, agent)
            sidebar.refresh(select_project_id=agent.projects[0].id)

            sidebar.new_session()

            self.assertEqual(agent.started_in_project, agent.projects[0].id)
            self.assertEqual(rendered, ["rendered", "header"])
            self.assertEqual(statuses, ["已创建新会话"])
            self.assertEqual(
                sidebar._session_tree.selection(),
                (agent.session_id,),
            )
        finally:
            root.destroy()

    def test_failed_new_session_does_not_refresh_or_report_success(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        try:
            agent = FakeNavigationAgent()
            agent.start_error = RuntimeError("保存失败")
            sidebar, rendered, statuses = self._create_sidebar(root, agent)
            sidebar.refresh(select_project_id=agent.projects[0].id)
            original_session_id = agent.session_id

            with patch("deepseek_agent.ui.sidebar.messagebox.showerror") as error:
                sidebar.new_session()

            self.assertEqual(agent.session_id, original_session_id)
            self.assertEqual(rendered, [])
            self.assertEqual(statuses, [])
            error.assert_called_once()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
