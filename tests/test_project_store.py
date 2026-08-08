import json
import tempfile
import unittest
from pathlib import Path

from _path_setup import add_project_root_to_path

add_project_root_to_path()

from src.deepseek_agent.memory import JsonProjectStore, ProjectStoreError


class JsonProjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "projects.json"
        self.store = JsonProjectStore(self.path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_create_and_reload_project(self) -> None:
        project = self.store.create("Agent 学习")
        reloaded_store = JsonProjectStore(self.path)

        self.assertEqual(reloaded_store.get(project.id[:8]), project)
        self.assertEqual(reloaded_store.list_projects(), [project])

    def test_project_name_is_trimmed_and_must_be_unique(self) -> None:
        project = self.store.create("  Python  ")

        self.assertEqual(project.name, "Python")
        with self.assertRaises(ProjectStoreError):
            self.store.create("python")

    def test_invalid_project_names_are_rejected(self) -> None:
        with self.assertRaises(ProjectStoreError):
            self.store.create("   ")
        with self.assertRaises(ProjectStoreError):
            self.store.create("x" * 51)

    def test_corrupt_project_file_is_reported(self) -> None:
        self.path.write_text("[]", encoding="utf-8")

        with self.assertRaises(ProjectStoreError):
            self.store.list_projects()

    def test_saved_file_has_version_and_project_list(self) -> None:
        self.store.create("研究")

        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["projects"][0]["name"], "研究")

    def test_rename_preserves_id_and_rejects_duplicate_name(self) -> None:
        first = self.store.create("第一个项目")
        self.store.create("第二个项目")

        renamed = self.store.rename(first.id, "重命名后的项目")

        self.assertEqual(renamed.id, first.id)
        self.assertEqual(renamed.name, "重命名后的项目")
        with self.assertRaises(ProjectStoreError):
            self.store.rename(first.id, "第二个项目")

    def test_delete_removes_only_selected_project(self) -> None:
        first = self.store.create("第一个项目")
        second = self.store.create("第二个项目")

        deleted = self.store.delete(first.id)

        self.assertEqual(deleted, first)
        self.assertEqual(self.store.list_projects(), [second])
        with self.assertRaises(ProjectStoreError):
            self.store.get(first.id)


if __name__ == "__main__":
    unittest.main()
