"""重复保存确认事实不能制造修订或使依赖产物失效。"""

import json
import tempfile
import unittest
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.career.commands import SaveFact
from deepseek_agent.career.domain import CareerDataError
from deepseek_agent.career.service import CareerService
from deepseek_agent.career.store import CareerStore
from deepseek_agent.career.tools import CareerMemoryTool
from deepseek_agent.models import ToolCallRequest
from deepseek_agent.tool_execution import ToolExecutor, ToolExecutionStatus
from deepseek_agent.tools import ToolRegistry


class CountingConfirmer:
    def __init__(self):
        self.calls = 0

    def confirm(self, tool, arguments):
        self.calls += 1
        return True


class CareerIdempotencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CareerStore(self.root / "profile.sqlite3")
        self.args = dict(
            key="职责",
            category="project",
            statement="参与接口测试",
            source_id="current_user",
            source_quote="参与接口测试",
        )
        self.user = "参与接口测试；负责测试文档"
        self.fact = self.store.save_fact(**self.args, current_user=self.user)

    def history(self):
        with self.store.connection() as db:
            return [
                tuple(row)
                for row in db.execute(
                    "SELECT fact_id,revision,data FROM fact_history ORDER BY fact_id,revision"
                )
            ]

    def artifacts(self):
        draft = self.store.build_resume_draft([self.fact["id"]])
        report = self.store.save_review(
            resume_id=draft["id"],
            job_id=None,
            title="报告",
            changes=[],
            findings=[dict(text="参与接口测试", evidence_fact_ids=[self.fact["id"]])],
        )
        self.store.decide_review(report["id"], "accepted")
        return draft, report

    def test_repeat_preserves_fact_history_and_generation_across_restart(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                if restart:
                    self.store = CareerStore(self.store.path)
                generation = self.store.generation
                actual = self.store.save_fact(**self.args, current_user=self.user)
                self.assertEqual(actual, self.fact)
                self.assertEqual(self.history(), [])
                self.assertEqual(self.store.generation, generation)

    def test_repeat_keeps_draft_and_accepted_report_current(self):
        draft, report = self.artifacts()
        rendered = self.store.render_review(report["id"])
        self.store.save_fact(**self.args, current_user=self.user)
        self.assertFalse(self.store.document(draft["id"])["stale"])
        self.assertFalse(self.store.review(report["id"])["stale"])
        self.assertEqual(self.store.review(report["id"])["status"], "accepted")
        self.assertEqual(self.store.render_review(report["id"]), rendered)
        self.assertEqual(self.store.build_resume_draft([self.fact["id"]])["id"], draft["id"])

    def test_actual_content_or_evidence_changes_still_create_revisions(self):
        source = self.store.import_document("resume", "简历", self.user)
        for change in (
            dict(category="skill"),
            dict(statement="负责测试文档"),
            dict(source_quote="负责测试文档"),
            dict(source_id=source["id"]),
        ):
            with self.subTest(change=change):
                # 每种修改独立从原始事实开始，覆盖四个字段的变化边界。
                original = self.store.save_fact(**self.args, current_user=self.user)
                draft, report = self.artifacts()
                generation = self.store.generation
                updated = self.store.save_fact(**dict(self.args, **change), current_user=self.user)
                self.assertEqual(updated["id"], original["id"])
                self.assertEqual(updated["revision"], original["revision"] + 1)
                self.assertEqual(self.store.generation, generation + 1)
                self.assertTrue(self.store.document(draft["id"])["stale"])
                self.assertTrue(self.store.review(report["id"])["stale"])
                with self.assertRaises(CareerDataError):
                    self.store.render_review(report["id"])

    def test_retries_do_not_fill_or_evict_real_revision_history(self):
        self.args["statement"] = "协助接口测试"
        changed = self.store.save_fact(**self.args, current_user=self.user)
        history = self.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0][2]), self.fact)
        for _ in range(12):
            self.store.save_fact(**self.args, current_user=self.user)
        self.assertEqual(self.history(), history)
        self.assertEqual(self.store.snapshot()["facts"][0], changed)

    def test_repeat_still_requires_current_source_validation(self):
        with self.assertRaises(CareerDataError):
            self.store.save_fact(**self.args, current_user="请继续")
        self.assertEqual(self.store.snapshot()["facts"][0], self.fact)
        self.assertEqual(self.history(), [])

    def test_repeat_still_requires_application_confirmation(self):
        service = CareerService(self.store, self.root, 100_000)
        with self.assertRaisesRegex(CareerDataError, "需要用户确认"):
            service.application.execute(SaveFact(**self.args), user_message=self.user)
        actual = service.application.execute(
            SaveFact(**self.args), user_message=self.user, confirmed=True
        )
        self.assertEqual(actual, self.fact)

    def test_model_retries_with_new_call_ids_keep_fact_and_request_confirmation(self):
        service = CareerService(self.store, self.root, 100_000)
        service.current_user = self.user
        tool = CareerMemoryTool(service)
        confirmer = CountingConfirmer()
        executor = ToolExecutor(ToolRegistry([tool]), confirmer)
        for call_id in ("first", "retry"):
            record = executor.execute(
                ToolCallRequest(call_id, tool.name, json.dumps(dict(action="save", **self.args)))
            )
            self.assertEqual(record.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(confirmer.calls, 2)
        self.assertEqual(self.store.snapshot()["facts"], [self.fact])
        self.assertEqual(self.history(), [])

    def test_distinct_keys_and_profiles_are_not_merged(self):
        second = self.store.save_fact(**dict(self.args, key="另一事项"), current_user=self.user)
        other = CareerStore(self.root / "other.sqlite3")
        separate = other.save_fact(**self.args, current_user=self.user)
        self.assertNotEqual(second["id"], self.fact["id"])
        self.assertNotEqual(separate["id"], self.fact["id"])
        self.assertEqual(len(self.store.snapshot()["facts"]), 2)
        self.assertEqual(len(other.snapshot()["facts"]), 1)
