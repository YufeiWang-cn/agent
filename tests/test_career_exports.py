"""导出记录和结果核对；仅使用临时档案、合成材料及假模型。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.career.commands import ExportReview
from deepseek_agent.career.store import CareerStore
from deepseek_agent.career.service import CareerService


class ExportTrackingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "profile.sqlite3"
        self.store = CareerStore(self.path)
        self.service = CareerService(self.store, self.root, 100_000)
        self.app = self.service.application
        resume = self.store.import_document("resume", "合成简历", "负责接口测试。")
        self.fact = self.store.save_fact(
            key="项目/职责",
            category="project",
            statement="负责接口测试。",
            source_id=resume["id"],
            source_quote="负责接口测试。",
        )
        self.review = self.store.save_review(
            resume_id=resume["id"],
            job_id=None,
            title="测试报告",
            findings=[dict(text="负责接口测试。", evidence_fact_ids=[self.fact["id"]])],
            general_advice=[],
            changes=[],
        )

    def export(self):
        return self.app.execute(ExportReview(self.review["id"], "review.md"), confirmed=True)

    def test_success_returns_durable_receipt_after_restart(self):
        result = self.export()
        self.assertIn("export_id", result)
        restarted = CareerStore(self.path)
        records = restarted.list_exports()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], result["export_id"])
        self.assertEqual(records[0]["result"], "succeeded")
        self.assertEqual(records[0]["review_status"], "draft")
        self.assertEqual(records[0]["fact_revisions"], {self.fact["id"]: 1})

    def test_file_written_before_interruption_keeps_pending_receipt(self):
        exporter = self.app._exporter
        original = exporter.write_new

        def interrupted(path, content):
            original(path, content)
            raise SystemExit("模拟进程在文件写出后退出")

        with patch.object(exporter, "write_new", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.export()
        self.assertTrue((self.root / "review.md").is_file())
        restarted = CareerStore(self.path)
        records = restarted.list_exports()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"], "pending")
        from deepseek_agent.career.commands import CheckExport

        app = CareerService(restarted, self.root, 100_000).application
        checked = app.execute(CheckExport(records[0]["id"]))
        self.assertEqual(checked["file_status"], "match")
        self.assertEqual(restarted.export_record(records[0]["id"])["result"], "pending")

    def check(self, export_id):
        from deepseek_agent.career.commands import CheckExport

        return self.app.execute(CheckExport(export_id))

    def test_check_is_read_only_and_distinguishes_changed_missing_and_unreadable(self):
        from deepseek_agent.workspace import WorkspaceAccessError

        receipt = self.export()["export_id"]
        before = self.store.export_record(receipt)
        generation = self.store.generation
        self.assertEqual(self.check(receipt)["file_status"], "match")
        (self.root / "review.md").write_bytes(b"changed")
        self.assertEqual(self.check(receipt)["file_status"], "modified")
        with patch.object(
            self.service.guard, "read_bytes", side_effect=WorkspaceAccessError("blocked")
        ):
            self.assertEqual(self.check(receipt)["file_status"], "unreadable")
        (self.root / "review.md").unlink()
        self.assertEqual(self.check(receipt)["file_status"], "missing")
        self.assertFalse((self.root / "review.md").exists())
        self.assertEqual(self.store.export_record(receipt), before)
        self.assertEqual(self.store.generation, generation)

    def test_check_matches_exact_bytes_not_bom_or_newline_normalization(self):
        receipt = self.export()["export_id"]
        path = self.root / "review.md"
        original = path.read_bytes()
        for content in (b"\xef\xbb\xbf" + original, original.replace(b"\n", b"\r\n")):
            path.write_bytes(content)
            self.assertEqual(self.check(receipt)["file_status"], "modified")

    def test_workspace_change_and_different_profile_cannot_reuse_receipt(self):
        from deepseek_agent.career.commands import CheckExport
        from deepseek_agent.career.domain import CareerDataError

        receipt = self.export()["export_id"]
        other = self.root / "other"
        other.mkdir()
        (other / "review.md").write_bytes((self.root / "review.md").read_bytes())
        service = CareerService(self.store, other, 100_000)
        with patch.object(
            service.guard, "read_bytes", side_effect=AssertionError("wrong workspace read")
        ):
            self.assertEqual(
                service.application.execute(CheckExport(receipt))["file_status"],
                "workspace_mismatch",
            )
        other_store = CareerStore(other / "profile.sqlite3")
        self.assertEqual(other_store.list_exports(), [])
        with self.assertRaises(CareerDataError):
            CareerService(other_store, self.root, 100_000).application.execute(CheckExport(receipt))

    def test_fact_revision_and_review_decision_do_not_rewrite_export_snapshot(self):
        receipt = self.export()["export_id"]
        before = self.store.export_record(receipt)
        self.store.decide_review(self.review["id"], "accepted")
        checked = self.check(receipt)
        self.assertEqual(checked["record"]["review_status"], "draft")
        self.assertEqual(checked["current_review_status"], "accepted")
        self.assertEqual(checked["file_status"], "match")
        self.store.save_fact(
            key="项目/职责",
            category="project",
            statement="只参与测试。",
            source_id="current_user",
            source_quote="只参与测试。",
            current_user="只参与测试。",
        )
        checked = self.check(receipt)
        self.assertTrue(checked["review_stale"])
        self.assertEqual(checked["file_status"], "match")
        self.assertEqual(checked["stale_reasons"][0]["reason"], "fact_updated")
        self.assertEqual(self.store.export_record(receipt), before)

    def test_rejection_existing_file_bad_path_and_stale_review_do_not_create_intent(self):
        from deepseek_agent.career.domain import CareerDataError
        from deepseek_agent.workspace import WorkspaceAccessError

        with self.assertRaises(CareerDataError):
            self.app.execute(ExportReview(self.review["id"], "review.md"))
        original = self.root / "existing.md"
        original.write_text("owned", encoding="utf-8")
        for path in ("existing.md", "../outside.md", "data/career/hidden.md", "report.txt"):
            with (
                self.subTest(path=path),
                self.assertRaises((CareerDataError, WorkspaceAccessError)),
            ):
                self.app.execute(ExportReview(self.review["id"], path), confirmed=True)
        self.store.forget_fact(self.fact["id"])
        with self.assertRaises(CareerDataError):
            self.export()
        self.assertEqual(self.store.list_exports(), [])
        self.assertEqual(original.read_text(encoding="utf-8"), "owned")
        self.assertFalse((self.root / "review.md").exists())

    def test_intent_commit_failure_prevents_file_write(self):
        from deepseek_agent.career.domain import CareerDataError

        with self.store.connection() as db:
            db.execute(
                "CREATE TRIGGER reject_export BEFORE INSERT ON exports BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        with patch.object(
            self.app._exporter, "write_new", side_effect=AssertionError("unexpected write")
        ):
            with self.assertRaises(CareerDataError):
                self.export()
        self.assertEqual(self.store.list_exports(), [])
        self.assertFalse((self.root / "review.md").exists())

    def test_interrupt_before_file_write_leaves_missing_pending_intent(self):
        with patch.object(self.app._exporter, "write_new", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.export()
        record = self.store.list_exports()[0]
        self.assertEqual(record["result"], "pending")
        self.assertEqual(self.check(record["id"])["file_status"], "missing")

    def test_completion_commit_failure_does_not_report_success_or_lose_file(self):
        from deepseek_agent.career.domain import ExportUnconfirmedError

        with self.store.connection() as db:
            db.execute(
                "CREATE TRIGGER reject_finish BEFORE UPDATE ON exports BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        with self.assertRaises(ExportUnconfirmedError):
            self.export()
        record = CareerStore(self.path).list_exports()[0]
        self.assertEqual(record["result"], "pending")
        self.assertEqual(self.check(record["id"])["file_status"], "match")

    def test_export_exception_keeps_unknown_side_effect_in_tool_record(self):
        import json
        from deepseek_agent.career.tools import ExportCareerReviewTool
        from deepseek_agent.models import ToolCallRequest
        from deepseek_agent.tool_execution import ToolExecutor, ToolExecutionStatus
        from deepseek_agent.tools import ToolRegistry
        from test_career import Confirmer

        original = self.app._exporter.write_new

        def fail_after_write(path, content):
            original(path, content)
            raise OSError("PRIVATE-ERROR-DETAIL")

        executor = ToolExecutor(ToolRegistry([ExportCareerReviewTool(self.service)]), Confirmer())
        with patch.object(self.app._exporter, "write_new", side_effect=fail_after_write):
            outcome = executor.execute(
                ToolCallRequest(
                    "export",
                    "export_career_review",
                    json.dumps(dict(review_id=self.review["id"], path="review.md")),
                )
            )
        self.assertEqual(outcome.status, ToolExecutionStatus.RESULT_UNKNOWN)
        record = self.store.list_exports()[0]
        self.assertEqual(record["result"], "unconfirmed")
        self.assertEqual(record["error_type"], "OSError")
        self.assertNotIn("PRIVATE-ERROR-DETAIL", json.dumps(record) + outcome.model_result)
        self.assertEqual(self.check(record["id"])["file_status"], "match")

    def test_cli_lists_and_checks_without_model_or_writes(self):
        import io
        import json
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from deepseek_agent.cli import CliApplication

        receipt = self.export()["export_id"]
        application = CliApplication(
            SimpleNamespace(career_enabled=True, career_application=self.app)
        )
        generation = self.store.generation
        with patch.object(application, "_chat", side_effect=AssertionError("model called")):
            for command in ("/exports", "/exports " + receipt, "/exports missing"):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertTrue(application.handle_command(command))
                self.assertTrue(output.getvalue())
                if command == "/exports":
                    self.assertEqual(json.loads(output.getvalue())["exports"][0]["id"], receipt)
                if command.endswith(receipt):
                    self.assertEqual(json.loads(output.getvalue())["file_status"], "match")
        self.assertEqual(self.store.generation, generation)

    def test_hard_process_exit_after_write_can_be_checked_after_restart(self):
        import os
        import subprocess
        import sys

        script = """
import os,sys
from pathlib import Path
from deepseek_agent.career.store import CareerStore
from deepseek_agent.career.service import CareerService
from deepseek_agent.career.commands import ExportReview
root=Path(sys.argv[1])
app=CareerService(CareerStore(root/'profile.sqlite3'), root, 100_000).application
original=app._exporter.write_new
def crash(path, content):
    original(path, content)
    os._exit(23)
app._exporter.write_new=crash
app.execute(ExportReview(sys.argv[2], 'review.md'), confirmed=True)
"""
        env = dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.root), self.review["id"]],
            env=env,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 23, completed.stderr)
        record = CareerStore(self.path).list_exports()[0]
        self.assertEqual(record["result"], "pending")
        self.assertEqual(self.check(record["id"])["file_status"], "match")

    def test_fake_agent_interrupt_and_restart_check_do_not_repeat_export(self):
        import json
        from deepseek_agent.agent import Agent, AgentCancelledError
        from deepseek_agent.config import Settings
        from deepseek_agent.memory import JsonSessionStore, JsonProjectStore
        from deepseek_agent.models import TextDelta, ToolCallRequest
        from test_career import Confirmer

        class Fake:
            model_name = "offline-export"

            def __init__(self):
                self.request = None
                self.calls = 0

            def stream(self, messages, tools):
                self.calls += 1
                if messages[-1]["role"] == "user":
                    yield self.request
                else:
                    yield TextDelta("已核对导出记录。")

        settings = Settings(
            "offline",
            "https://example.invalid",
            "offline-export",
            "system",
            workspace_root=self.root,
            agent_mode="career",
        )

        def make_agent(model, confirmer):
            return Agent(
                settings,
                model=model,
                confirmer=confirmer,
                session_store=JsonSessionStore(self.root / "sessions"),
                project_store=JsonProjectStore(self.root / "projects.json"),
            )

        model = Fake()
        with (
            patch(
                "deepseek_agent.models.deepseek.OpenAI", side_effect=AssertionError("SDK forbidden")
            ),
            patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
        ):
            confirmer = Confirmer()
            agent = make_agent(model, confirmer)
            store = agent._career.store
            resume = store.import_document("resume", "合成简历", "参与接口测试。")
            review = store.save_review(
                resume_id=resume["id"],
                job_id=None,
                title="报告",
                findings=[],
                general_advice=["请补充测试方法。"],
                changes=[],
            )
            model.request = ToolCallRequest(
                "export",
                "export_career_review",
                json.dumps(dict(review_id=review["id"], path="agent.md")),
            )
            exporter = agent.career_application._exporter
            original = exporter.write_new

            def interrupted(path, content):
                original(path, content)
                raise KeyboardInterrupt()

            with patch.object(exporter, "write_new", side_effect=interrupted):
                with self.assertRaises(AgentCancelledError):
                    agent.chat("导出报告到 agent.md")
            self.assertEqual(model.calls, 1)
            self.assertEqual(len(confirmer.calls), 1)
            receipt = store.list_exports()[0]
            self.assertEqual(receipt["result"], "pending")
            file_before = (self.root / "agent.md").read_bytes()
            checker = Fake()
            checker.request = ToolCallRequest(
                "check",
                "career_review",
                json.dumps(dict(action="check_export", export_id=receipt["id"])),
            )
            deny = Confirmer(False)
            restarted = make_agent(checker, deny)
            with patch.object(
                restarted.career_application._exporter,
                "write_new",
                side_effect=AssertionError("replay"),
            ):
                restarted.chat("核对上次导出结果")
            tool_results = [
                json.loads(m["content"])
                for m in restarted.history()
                if m.get("role") == "tool" and m.get("tool_call_id") == "check"
            ]
            self.assertEqual(tool_results[-1]["file_status"], "match")
            self.assertEqual(deny.calls, [])
            self.assertEqual(restarted._career.store.list_exports()[0]["result"], "pending")
            self.assertEqual((self.root / "agent.md").read_bytes(), file_before)


class ExportMigrationTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        from test_career_evidence import create_v1
        from deepseek_agent.infrastructure.sqlite_career import SqliteCareerRepository

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "v2.sqlite3"
        create_v1(self.path)
        with sqlite3.connect(self.path) as db:
            SqliteCareerRepository._migrate_v1(db)

    def test_v2_upgrade_preserves_rows_and_backs_up_original_once(self):
        import sqlite3

        with sqlite3.connect(self.path) as db:
            before = {
                name: db.execute("SELECT * FROM " + name).fetchall()
                for name in ("documents", "facts", "fact_history", "reviews")
            }
        store = CareerStore(self.path)
        self.assertEqual(store.list_exports(), [])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            for name, rows in before.items():
                self.assertEqual(db.execute("SELECT * FROM " + name).fetchall(), rows)
        backup = next(self.root.glob("*.v2-*.bak"))
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(
                db.execute("SELECT name FROM sqlite_master WHERE name='exports'").fetchone()
            )
        CareerStore(self.path)
        self.assertEqual(len(list(self.root.glob("*.bak"))), 1)

    def test_failed_v2_migration_rolls_back_every_change(self):
        import sqlite3
        from deepseek_agent.infrastructure.sqlite_career import SqliteCareerRepository
        from deepseek_agent.career.domain import CareerDataError

        def fail(db):
            db.execute("CREATE TABLE exports(partial TEXT)")
            db.execute("PRAGMA user_version=3")
            raise sqlite3.OperationalError("injected")

        with patch.object(SqliteCareerRepository, "_migrate_v2", side_effect=fail):
            with self.assertRaises(CareerDataError):
                CareerStore(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(
                db.execute("SELECT name FROM sqlite_master WHERE name='exports'").fetchone()
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)

    def test_v2_backup_failure_prevents_migration(self):
        import sqlite3
        from deepseek_agent.infrastructure.sqlite_career import SqliteCareerRepository
        from deepseek_agent.career.domain import CareerDataError

        before = self.path.read_bytes()
        with patch.object(
            SqliteCareerRepository, "_backup", side_effect=CareerDataError("unavailable")
        ):
            with self.assertRaises(CareerDataError):
                CareerStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_v2_backup_includes_committed_wal_rows(self):
        import sqlite3

        with sqlite3.connect(self.path) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE documents SET content='WAL 最新来源' WHERE id='resume'")
            writer.commit()
            CareerStore(self.path)
            backup = next(self.root.glob("*.v2-*.bak"))
            with sqlite3.connect(backup) as db:
                self.assertEqual(
                    db.execute("SELECT content FROM documents WHERE id='resume'").fetchone()[0],
                    "WAL 最新来源",
                )
