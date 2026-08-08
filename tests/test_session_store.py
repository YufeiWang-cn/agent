import json
import tempfile
import unittest
from pathlib import Path

from _path_setup import add_project_root_to_path

add_project_root_to_path()

from src.deepseek_agent.memory import JsonSessionStore, Session, SessionStoreError


class JsonSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.store = JsonSessionStore(self.directory)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_save_and_load_preserve_tool_messages(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "1+1"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression":"1+1"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "calculator",
                "content": "2",
            },
        ]
        session = Session.create(messages, title="计算")
        session.move_to_project("project-1")

        self.store.save(session)
        loaded = self.store.load(session.id[:8])

        self.assertEqual(loaded.id, session.id)
        self.assertEqual(loaded.messages, messages)
        self.assertEqual(loaded.project_id, "project-1")

    def test_old_session_without_project_id_remains_compatible(self) -> None:
        session = Session.create([{"role": "system", "content": "system"}])
        data = session.to_dict()
        data.pop("project_id")
        (self.directory / f"{session.id}.json").write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )

        loaded = self.store.load(session.id)

        self.assertIsNone(loaded.project_id)

    def test_rename_and_move_session(self) -> None:
        session = Session.create([{"role": "system", "content": "system"}])
        self.store.save(session)

        renamed = self.store.rename(session.id, "新的名称")
        moved = self.store.move_to_project(session.id, "project-1")

        self.assertEqual(renamed.title, "新的名称")
        self.assertEqual(moved.project_id, "project-1")
        self.assertEqual(self.store.load(session.id).title, "新的名称")

    def test_sessions_are_sorted_by_updated_time(self) -> None:
        older = Session.create([{"role": "system", "content": "system"}])
        newer = Session.create([{"role": "system", "content": "system"}])
        older.updated_at = "2026-01-01T00:00:00+00:00"
        newer.updated_at = "2026-02-01T00:00:00+00:00"
        self.store.save(older)
        self.store.save(newer)

        sessions = self.store.list_sessions()

        self.assertEqual([session.id for session in sessions], [newer.id, older.id])

    def test_delete_accepts_short_id(self) -> None:
        session = Session.create([{"role": "system", "content": "system"}])
        self.store.save(session)

        deleted_id = self.store.delete(session.id[:8])

        self.assertEqual(deleted_id, session.id)
        with self.assertRaises(SessionStoreError):
            self.store.load(session.id)

    def test_corrupt_file_does_not_break_session_listing(self) -> None:
        session = Session.create([{"role": "system", "content": "system"}])
        self.store.save(session)
        (self.directory / "bad.json").write_text("{not json", encoding="utf-8")

        sessions = self.store.list_sessions()

        self.assertEqual([item.id for item in sessions], [session.id])

    def test_saved_json_does_not_contain_api_key(self) -> None:
        session = Session.create([{"role": "system", "content": "system"}])
        self.store.save(session)

        data = json.loads((self.directory / f"{session.id}.json").read_text("utf-8"))

        self.assertNotIn("api_key", data)


if __name__ == "__main__":
    unittest.main()
