"""Verify runtime independence and per-run business context isolation."""

import ast
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.career.service import CareerService
from deepseek_agent.career.store import CareerStore
from deepseek_agent.runtime.contracts import RunContext
from deepseek_agent.runtime import engine
from deepseek_agent.career import use_cases
from deepseek_agent.career.domain import CareerDataError
from deepseek_agent.career.use_cases import CareerUseCases


class ArchitectureTests(unittest.TestCase):
    def test_runtime_has_no_business_or_ui_imports(self):
        tree = ast.parse(Path(engine.__file__).read_text(encoding="utf-8"))
        modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(
            any(name.startswith(("career", "ui", "bootstrap", "config")) for name in modules)
        )

    def test_run_context_is_immutable_and_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = CareerService(CareerStore(root / "first.sqlite3"), root, 100_000)
            second = CareerService(CareerStore(root / "second.sqlite3"), root, 100_000)
            context = RunContext("first", "session", "run", "turn", "user evidence")
            first.bind_turn(context)
            self.assertEqual(first.run_context, context)
            self.assertEqual(first.current_user, "user evidence")
            self.assertEqual(second.current_user, "")
            with self.assertRaises(FrozenInstanceError):
                context.user_message = "changed"
            first.current_user = "next evidence"
            self.assertEqual(context.user_message, "user evidence")

    def test_use_cases_do_not_import_sqlite_or_ui(self):
        tree = ast.parse(Path(use_cases.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
            elif isinstance(node, ast.Import):
                modules.extend(item.name for item in node.names)
        self.assertFalse(
            any(name.startswith(("sqlite3", "infrastructure", "ui", "tools")) for name in modules)
        )

    def test_import_use_case_can_run_without_sqlite(self):
        class MemoryRepository:
            generation = 0

            def __init__(self):
                self.documents = []

            @contextmanager
            def transaction(self):
                yield self

            def find_document(self, kind, content):
                return next(
                    (
                        doc
                        for doc in self.documents
                        if doc["kind"] == kind and doc["content"] == content
                    ),
                    None,
                )

            def count(self, collection):
                self.assert_collection = collection
                return len(self.documents)

            def insert_document(self, document):
                self.documents.append(document)
                self.generation += 1

        repository = MemoryRepository()
        business = CareerUseCases(repository)
        first = business.import_document("resume", "resume", "Synthetic evidence")
        duplicate = business.import_document("resume", "renamed", "Synthetic evidence")
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(len(repository.documents), 1)

    def test_failed_fact_write_rolls_back_history_and_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CareerStore(Path(directory) / "profile.sqlite3")
            arguments = dict(
                key="project",
                category="project",
                statement="original",
                source_id="current_user",
                source_quote="original",
                current_user="original",
            )
            fact = store.save_fact(**arguments)
            generation = store.generation
            with store.connection() as db:
                db.execute(
                    "CREATE TRIGGER reject_fact BEFORE INSERT ON facts "
                    "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
                )
            with self.assertRaises(CareerDataError):
                store.save_fact(**dict(arguments, statement="corrected"))
            self.assertEqual(store.generation, generation)
            self.assertEqual(store.snapshot()["facts"][0]["revision"], 1)
            with store.connection() as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM fact_history WHERE fact_id=?", (fact["id"],)
                ).fetchone()[0]
            self.assertEqual(count, 0)
