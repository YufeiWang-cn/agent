"""报告正文、派生初稿和 v1 数据迁移的业务回归。"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.career.store import CareerStore, CareerDataError
from deepseek_agent.infrastructure.sqlite_career import SqliteCareerRepository
from deepseek_agent.career.commands import DecideReview, ExportReview
from deepseek_agent.career.service import CareerService
from deepseek_agent.career.tools import CareerReviewTool
from deepseek_agent.models import ToolCallRequest
from deepseek_agent.tool_execution import ToolExecutor, ToolExecutionStatus
from deepseek_agent.tools import ToolRegistry


class CareerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CareerStore(self.root / "profile.sqlite3")
        self.resume = self.store.import_document("resume", "简历", "参与接口测试")
        self.fact = self.save_fact("参与接口测试")

    def save_fact(self, statement):
        return self.store.save_fact(
            key="职责",
            category="project",
            statement=statement,
            source_id="current_user",
            source_quote=statement,
            current_user=statement,
        )

    def review(self, **extra):
        args = dict(
            resume_id=self.resume["id"],
            job_id=None,
            title="报告",
            changes=[],
            findings=[dict(text="参与接口测试", evidence_fact_ids=[self.fact["id"]])],
            general_advice=["建议补充测试方法"],
        )
        args.update(extra)
        return self.store.save_review(**args)

    def assert_blocked(self, review_id):
        with self.assertRaises(CareerDataError):
            self.store.decide_review(review_id, "accepted")
        with self.assertRaises(CareerDataError):
            self.store.render_review(review_id)

    def test_free_markdown_with_empty_changes_cannot_be_accepted_or_exported(self):
        old = self.store.save_review(
            resume_id=self.resume["id"],
            job_id=None,
            title="旧接口",
            markdown="主导接口测试",
            changes=[],
        )
        self.assert_blocked(old["id"])

    def test_correction_invalidates_draft_even_without_report_changes(self):
        draft = self.store.build_resume_draft([self.fact["id"]])
        self.save_fact("仅协助测试")
        with self.assertRaises(CareerDataError):
            self.store.save_review(
                resume_id=draft["id"], job_id=None, title="旧初稿", markdown="建议", changes=[]
            )

    def test_body_only_evidence_is_versioned_and_survives_restart(self):
        report = self.review()
        self.assertEqual(report["fact_revisions"], {self.fact["id"]: 1})
        self.assertIn("参与接口测试", self.store.render_review(report["id"]))
        self.save_fact("协助测试")
        self.store = CareerStore(self.store.path)
        report = self.store.review(report["id"])
        self.assertTrue(report["stale"])
        self.assertEqual(report["stale_reasons"][0]["fact_id"], self.fact["id"])
        self.assertEqual(report["stale_reasons"][0]["reason"], "fact_updated")
        self.assert_blocked(report["id"])

    def test_draft_dependencies_are_inherited_even_with_advice_only(self):
        draft = self.store.build_resume_draft([self.fact["id"]])
        self.assertEqual(draft["fact_revisions"], {self.fact["id"]: 1})
        report = self.review(resume_id=draft["id"], findings=[])
        self.store.forget_fact(self.fact["id"])
        self.assert_blocked(report["id"])
        snapshot = self.store.snapshot()
        indexed = next(d for d in snapshot["documents"] if d["id"] == draft["id"])
        self.assertTrue(indexed["stale"])
        self.assertEqual(indexed["stale_reasons"][0]["reason"], "fact_deleted")
        self.assertTrue(self.store.document(draft["id"])["stale"])

    def test_body_evidence_validation_and_no_markdown_bypass(self):
        for findings in (
            [dict(text="经历", evidence_fact_ids=[])],
            [dict(text="经历", evidence_fact_ids=["missing"])],
            [dict(text="经历", evidence_fact_ids="bad")],
            "bad",
        ):
            with self.subTest(findings=findings), self.assertRaises(CareerDataError):
                self.review(findings=findings)
        with self.assertRaises(CareerDataError):
            self.review(markdown="绕过结构化正文")
        with self.assertRaises(CareerDataError):
            self.store.import_document("resume_draft", "伪造初稿", "虚构经历")
        self.assertEqual(self.store.snapshot()["reviews"], [])

    def test_union_of_body_changes_and_draft_and_unrelated_fact_stays_valid(self):
        second = self.store.save_fact(
            key="技能",
            category="skill",
            statement="Python",
            source_id="current_user",
            source_quote="Python",
            current_user="Python",
        )
        draft = self.store.build_resume_draft([second["id"]])
        report = self.review(
            resume_id=draft["id"],
            changes=[
                dict(
                    original="Python",
                    suggested="使用 Python",
                    reason="表达",
                    evidence_fact_ids=[second["id"]],
                )
            ],
        )
        self.assertEqual(report["fact_revisions"], {self.fact["id"]: 1, second["id"]: 1})
        self.store.save_fact(
            key="城市",
            category="preference",
            statement="北京",
            source_id="current_user",
            source_quote="北京",
            current_user="北京",
        )
        self.assertFalse(self.store.review(report["id"])["stale"])
        self.store.decide_review(report["id"], "accepted")
        self.assertIn("已接受", self.store.render_review(report["id"]))

    def test_tool_save_and_application_export_share_body_dependency_checks(self):
        service = CareerService(self.store, self.root, 100_000)

        class Allow:
            def confirm(self, tool, arguments):
                return True

        tool = CareerReviewTool(service)
        executor = ToolExecutor(ToolRegistry([tool]), Allow())
        payload = dict(
            action="save",
            resume_id=self.resume["id"],
            title="正文报告",
            findings=[dict(text="参与接口测试", evidence_fact_ids=[self.fact["id"]])],
        )
        result = executor.execute(ToolCallRequest("save", tool.name, json.dumps(payload)))
        self.assertEqual(result.status, ToolExecutionStatus.SUCCEEDED)
        report = json.loads(result.model_result)
        service.application.execute(DecideReview(report["id"], "accepted"), confirmed=True)
        service.application.execute(ExportReview(report["id"], "before.md"), confirmed=True)
        self.assertIn("参与接口测试", (self.root / "before.md").read_text(encoding="utf-8"))
        self.store.forget_fact(self.fact["id"])
        for command in (
            DecideReview(report["id"], "accepted"),
            ExportReview(report["id"], "after.md"),
        ):
            with self.assertRaises(CareerDataError):
                service.application.execute(command, confirmed=True)
        self.assertFalse((self.root / "after.md").exists())
        self.assertIn("fact_deleted", json.dumps(self.store.review(report["id"])))

    def test_draft_deduplication_and_regeneration_do_not_revalidate_old_reports(self):
        draft = self.store.build_resume_draft([self.fact["id"]])
        self.assertEqual(self.store.build_resume_draft([self.fact["id"]])["id"], draft["id"])
        old_report = self.review(resume_id=draft["id"])
        self.save_fact("协助测试")
        new_draft = self.store.build_resume_draft([self.fact["id"]])
        self.assertNotEqual(new_draft["id"], draft["id"])
        self.assertEqual(new_draft["fact_revisions"][self.fact["id"]], 2)
        self.assertFalse(self.review(resume_id=new_draft["id"], findings=[])["stale"])
        self.assert_blocked(old_report["id"])

    def test_general_advice_is_separate_and_has_no_false_evidence_claim(self):
        report = self.review(findings=[])
        self.assertEqual(report["fact_revisions"], {})
        content = self.store.render_review(report["id"])
        self.assertIn("非已确认的个人经历", content)
        self.assertNotIn("确认事实与来源", content)
        for extra in (
            dict(findings=[], general_advice=[]),
            dict(general_advice="not a list"),
            dict(findings=[dict(text="", evidence_fact_ids=[self.fact["id"]])]),
        ):
            with self.subTest(extra=extra), self.assertRaises(CareerDataError):
                self.review(**extra)


def create_v1(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE documents(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,
                content TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE facts(id TEXT PRIMARY KEY,fact_key TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,statement TEXT NOT NULL,source_id TEXT NOT NULL,
                source_quote TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE fact_history(fact_id TEXT NOT NULL,revision INTEGER NOT NULL,
                data TEXT NOT NULL,PRIMARY KEY(fact_id,revision));
            CREATE TABLE reviews(id TEXT PRIMARY KEY,resume_id TEXT NOT NULL,job_id TEXT,
                title TEXT NOT NULL,markdown TEXT NOT NULL,changes TEXT NOT NULL,
                fact_revisions TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
            INSERT INTO documents VALUES('resume','resume','简历','参与测试','date');
            INSERT INTO documents VALUES('draft','resume_draft','初稿','参与测试','date');
            INSERT INTO facts VALUES('fact','职责','project','参与测试','resume','参与测试',2,'date');
            INSERT INTO fact_history VALUES('fact',1,'{"statement":"旧事实"}');
            INSERT INTO reviews VALUES('review','resume',NULL,'旧报告','旧正文','[]','{}','accepted','date');
            PRAGMA user_version=1;
        """)


class EvidenceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "old.sqlite3"
        create_v1(self.path)

    def test_upgrade_preserves_data_but_does_not_certify_legacy_evidence(self):
        store = CareerStore(self.path)
        with store.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(
                db.execute("SELECT data FROM fact_history").fetchone()[0], '{"statement":"旧事实"}'
            )
        review = store.review("review")
        self.assertEqual(review["markdown"], "旧正文")
        self.assertEqual(review["status"], "accepted")
        self.assertEqual(review["dependency_status"], "needs_review")
        self.assertTrue(review["stale"])
        self.assertFalse(store.document("resume")["stale"])
        self.assertTrue(store.document("draft")["stale"])
        with self.assertRaises(CareerDataError):
            store.render_review("review")
        with self.assertRaises(CareerDataError):
            store.decide_review("review", "accepted")
        with self.assertRaises(CareerDataError):
            store.save_review(
                resume_id="draft",
                job_id=None,
                title="新报告",
                findings=[],
                general_advice=["补充方法"],
                changes=[],
            )
        backups = list(self.path.parent.glob("*.v1-*.bak"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT markdown FROM reviews").fetchone()[0], "旧正文")
        CareerStore(self.path)
        self.assertEqual(len(list(self.path.parent.glob("*.v1-*.bak"))), 1)

    def test_failed_migration_rolls_back_schema_and_version(self):
        def fail(db):
            db.execute("ALTER TABLE documents ADD COLUMN partial TEXT")
            raise sqlite3.OperationalError("injected migration failure")

        with patch.object(SqliteCareerRepository, "_migrate_v1", side_effect=fail):
            with self.assertRaises(CareerDataError):
                CareerStore(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("partial", [r[1] for r in db.execute("PRAGMA table_info(documents)")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 1)
        self.assertEqual(len(list(self.path.parent.glob("*.v1-*.bak"))), 1)

    def test_backup_failure_prevents_any_migration(self):
        with patch.object(
            SqliteCareerRepository, "_backup_v1", side_effect=CareerDataError("backup unavailable")
        ):
            with self.assertRaises(CareerDataError):
                CareerStore(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn(
                "dependency_status", [r[1] for r in db.execute("PRAGMA table_info(reviews)")]
            )
            self.assertEqual(db.execute("SELECT status FROM reviews").fetchone()[0], "accepted")

    def test_backup_contains_committed_wal_data(self):
        with sqlite3.connect(self.path) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE documents SET content='WAL 已提交内容' WHERE id='resume'")
            writer.commit()
            store = CareerStore(self.path)
            self.assertEqual(store.document("resume")["content"], "WAL 已提交内容")
            backup = next(self.path.parent.glob("*.v1-*.bak"))
            with sqlite3.connect(backup) as db:
                self.assertEqual(
                    db.execute("SELECT content FROM documents WHERE id='resume'").fetchone()[0],
                    "WAL 已提交内容",
                )

    def test_future_version_is_rejected_without_mutating_data(self):
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=99")
        before = self.path.read_bytes()
        with self.assertRaises(CareerDataError):
            CareerStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
