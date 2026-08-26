"""验证会话协调器的持久化、切换和补偿事务。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.conversation import Conversation
from deepseek_agent.memory import (
    JsonProjectStore,
    JsonSessionStore,
    SessionCoordinator,
)
from deepseek_agent.planning import TaskPlan


class SessionCoordinatorTests(unittest.TestCase):
    """验证会话协调职责可以脱离 Agent 独立工作。"""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.session_store = JsonSessionStore(root / "sessions")
        self.project_store = JsonProjectStore(root / "projects.json")
        self.conversation = Conversation("system")
        self.coordinator = SessionCoordinator(
            self.conversation,
            self.session_store,
            self.project_store,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_save_current_derives_title_and_persists_plan(self) -> None:
        self.conversation.add_user("  分析   Agent 架构  ")
        plan = TaskPlan.create(
            [
                {"step": "读取代码", "status": "in_progress"},
                {"step": "整理结论", "status": "pending"},
            ]
        )

        self.coordinator.save_current(plan)

        saved = self.session_store.load(self.coordinator.current.id)
        self.assertEqual(saved.title, "分析 Agent 架构")
        self.assertEqual(saved.plan, plan)
        self.assertEqual(saved.messages, self.conversation.messages)

    def test_start_load_and_clear_commit_matching_memory_state(self) -> None:
        first_id = self.coordinator.current.id
        self.conversation.add_user("第一条消息")

        second = self.coordinator.start_new_session(None)
        self.assertNotEqual(second.id, first_id)
        self.assertEqual(self.conversation.visible_history(), [])

        loaded = self.coordinator.load_session(first_id, None)
        self.assertEqual(loaded.id, first_id)
        self.assertEqual(self.conversation.visible_history()[0]["content"], "第一条消息")

        cleared = self.coordinator.clear_conversation()
        self.assertIsNone(cleared.plan)
        self.assertEqual(self.conversation.visible_history(), [])
        self.assertEqual(self.session_store.load(first_id).messages[1:], [])

    def test_failed_project_delete_restores_session_membership(self) -> None:
        project = self.coordinator.create_project("需要回滚的项目")
        current_id = self.coordinator.current.id
        self.coordinator.move_session(current_id, project.id, None)

        with patch.object(
            self.project_store,
            "delete",
            side_effect=OSError("project store unavailable"),
        ):
            with self.assertRaises(OSError):
                self.coordinator.delete_project(project.id, None)

        restored = self.session_store.load(current_id)
        self.assertEqual(restored.project_id, project.id)
        self.assertEqual(self.coordinator.current.project_id, project.id)

    def test_failed_current_delete_removes_unused_replacement(self) -> None:
        current_id = self.coordinator.current.id
        original_delete = self.session_store.delete

        def fail_for_current(session_id: str) -> str:
            if session_id == current_id:
                raise OSError("session delete unavailable")
            return original_delete(session_id)

        with patch.object(self.session_store, "delete", side_effect=fail_for_current):
            with self.assertRaises(OSError):
                self.coordinator.delete_session(current_id)

        self.assertEqual(self.coordinator.current.id, current_id)
        self.assertEqual(
            [session.id for session in self.session_store.list_sessions()],
            [current_id],
        )


if __name__ == "__main__":
    unittest.main()
