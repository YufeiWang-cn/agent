"""报告导出安全重试：临时文件、假模型，不连接真实供应商。"""

import unittest
from unittest.mock import patch
from _path_setup import add_src_to_path

add_src_to_path()
from deepseek_agent.career.commands import ExportReview
from deepseek_agent.career.domain import CareerDataError
from deepseek_agent.workspace import WorkspaceAccessError


class ExportRetryTests(unittest.TestCase):
    def setUp(self):
        from test_career_exports import ExportTrackingTests

        ExportTrackingTests.setUp(self)

    def export(self, path="review.md", *, review_id=None, confirmed=True):
        return self.app.execute(
            ExportReview(review_id or self.review["id"], path), confirmed=confirmed
        )

    def test_completed_export_retry_reuses_receipt_without_file_or_database_write(self):
        first = self.export()
        target = self.root / "review.md"
        before = target.read_bytes(), target.stat().st_mtime_ns
        records = self.store.list_exports()
        generation = self.store.generation
        with patch.object(
            self.app._exporter, "write_new", side_effect=AssertionError("duplicate write")
        ):
            second = self.export()
        self.assertTrue(second["reused"])
        self.assertEqual(second["export_id"], first["export_id"])
        self.assertEqual(second["file_status"], "match")
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
        self.assertEqual(self.store.list_exports(), records)
        self.assertEqual(self.store.generation, generation)

    def test_file_appearing_after_initial_check_is_never_overwritten(self):
        guard = self.service.guard
        original = guard.prepare_text_write
        checks = 0
        target = self.root / "race.md"

        def race(path, content):
            nonlocal checks
            checks += 1
            if checks == 2:
                target.write_bytes(b"created by another writer")
            return original(path, content)

        with patch.object(guard, "prepare_text_write", side_effect=race):
            with self.assertRaises((CareerDataError, WorkspaceAccessError)):
                self.app._exporter.write_new("race.md", "agent output")
        self.assertEqual(target.read_bytes(), b"created by another writer")

    def test_untracked_existing_file_is_conflict_even_with_identical_content(self):
        target = self.root / "review.md"
        target.write_text(self.store.render_review(self.review["id"]), encoding="utf-8")
        before = target.read_bytes()
        with self.assertRaisesRegex(CareerDataError, "没有匹配的导出记录"):
            self.export()
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.store.list_exports(), [])

    def test_modified_file_is_not_reused_even_with_same_byte_count(self):
        self.export()
        target = self.root / "review.md"
        before = target.read_bytes()
        changed = b"!" + before[1:]
        target.write_bytes(changed)
        records = self.store.list_exports()
        with self.assertRaisesRegex(CareerDataError, "已被修改"):
            self.export()
        self.assertEqual(target.read_bytes(), changed)
        self.assertEqual(self.store.list_exports(), records)

    def test_deleted_file_is_not_silently_recreated(self):
        self.export()
        target = self.root / "review.md"
        target.unlink()
        records = self.store.list_exports()
        with patch.object(self.app._exporter, "write_new", side_effect=AssertionError("recreated")):
            with self.assertRaisesRegex(CareerDataError, "已缺失"):
                self.export()
        self.assertFalse(target.exists())
        self.assertEqual(self.store.list_exports(), records)
        fresh = self.export("new.md")
        self.assertFalse(fresh["reused"])
        self.assertEqual(len(self.store.list_exports()), 2)

    def test_same_path_cannot_switch_to_another_report(self):
        self.export()
        other = self.store.save_review(
            resume_id=self.review["resume_id"],
            job_id=None,
            title="另一个报告",
            findings=[],
            general_advice=["补充方法"],
            changes=[],
        )
        before = (self.root / "review.md").read_bytes()
        with self.assertRaisesRegex(CareerDataError, "另一份报告"):
            self.export(review_id=other["id"])
        self.assertEqual((self.root / "review.md").read_bytes(), before)
        self.assertEqual(len(self.store.list_exports()), 1)

    def test_retry_revalidates_review_status_and_current_evidence(self):
        self.export()
        self.store.decide_review(self.review["id"], "accepted")
        with self.assertRaisesRegex(CareerDataError, "审阅状态"):
            self.export()
        accepted = self.export("accepted.md")
        self.assertEqual(accepted["status"], "accepted")
        self.store.forget_fact(self.fact["id"])
        with self.assertRaisesRegex(CareerDataError, "不可继续使用"):
            self.export("accepted.md")
        self.assertEqual(len(self.store.list_exports()), 2)

    def test_reuse_still_requires_current_confirmation(self):
        first = self.export()
        with patch.object(
            self.app._exporter, "inspect", side_effect=AssertionError("before approval")
        ):
            with self.assertRaisesRegex(CareerDataError, "需要用户确认"):
                self.export(confirmed=False)
        self.assertEqual(self.store.list_exports()[0]["id"], first["export_id"])

    def test_pending_and_unconfirmed_results_can_be_verified_without_rewriting_history(self):
        from deepseek_agent.career.store import CareerStore
        from deepseek_agent.career.service import CareerService

        for index, error in enumerate((SystemExit("crash"), OSError("lost return"))):
            path = f"recover-{index}.md"
            original = self.app._exporter.write_new

            def interrupted(name, content):
                original(name, content)
                raise error

            with self.subTest(error=type(error).__name__):
                with patch.object(self.app._exporter, "write_new", side_effect=interrupted):
                    with self.assertRaises((SystemExit, CareerDataError)):
                        self.export(path)
                record = self.store.list_exports()[0]
                expected = "pending" if isinstance(error, SystemExit) else "unconfirmed"
                self.assertEqual(record["result"], expected)
                store = CareerStore(self.path)
                app = CareerService(store, self.root, 100_000).application
                generation = store.generation
                with patch.object(app._exporter, "write_new", side_effect=AssertionError("replay")):
                    result = app.execute(ExportReview(self.review["id"], path), confirmed=True)
                self.assertTrue(result["reused"])
                self.assertEqual(result["export_id"], record["id"])
                self.assertEqual(result["export_result"], expected)
                self.assertEqual(store.export_record(record["id"]), record)
                self.assertEqual(store.generation, generation)

    def test_unreadable_file_cannot_be_reused(self):
        self.export()
        with patch.object(
            self.service.guard, "read_bytes", side_effect=WorkspaceAccessError("no read")
        ):
            with self.assertRaisesRegex(CareerDataError, "无法安全核对"):
                self.export()
        self.assertEqual(len(self.store.list_exports()), 1)

    def test_old_receipt_outside_display_limit_is_found_after_restart(self):
        from deepseek_agent.career.ports import ExportTarget
        from deepseek_agent.career.store import CareerStore
        from deepseek_agent.career.service import CareerService

        first = self.export()
        workspace_id = self.store.export_record(first["export_id"])["workspace_id"]
        for index in range(51):
            self.store.prepare_export(
                self.review["id"], ExportTarget(f"pending-{index}.md", workspace_id)
            )
        self.assertNotIn(first["export_id"], [r["id"] for r in self.store.list_exports()])
        app = CareerService(CareerStore(self.path), self.root, 100_000).application
        repeated = app.execute(ExportReview(self.review["id"], "review.md"), confirmed=True)
        self.assertEqual(repeated["export_id"], first["export_id"])
        self.assertTrue(repeated["reused"])

    def test_path_aliases_do_not_create_duplicate_receipts(self):
        import os

        first = self.export()
        (self.root / "folder").mkdir()
        paths = ["./review.md", "folder/../review.md"]
        if os.name == "nt":
            paths.append("REVIEW.MD")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.export(path)["export_id"], first["export_id"])
        self.assertEqual(len(self.store.list_exports()), 1)

    def test_workspace_and_profile_isolation_do_not_adopt_existing_files(self):
        from deepseek_agent.career.service import CareerService
        from deepseek_agent.career.store import CareerStore

        self.export()
        other_root = self.root / "other"
        other_root.mkdir()
        (other_root / "review.md").write_bytes((self.root / "review.md").read_bytes())
        moved = CareerService(self.store, other_root, 100_000).application
        with self.assertRaisesRegex(CareerDataError, "没有匹配的导出记录"):
            moved.execute(ExportReview(self.review["id"], "review.md"), confirmed=True)
        other_store = CareerStore(other_root / "other.sqlite3")
        other_app = CareerService(other_store, self.root, 100_000).application
        with self.assertRaises(CareerDataError):
            other_app.execute(ExportReview(self.review["id"], "review.md"), confirmed=True)
        self.assertEqual(other_store.list_exports(), [])

    def test_conflict_at_final_publication_never_overwrites_and_cleans_temporary_file(self):
        import os

        original_link = os.link
        target = self.root / "review.md"

        def conflicting_link(source, destination):
            target.write_bytes(b"other application content")
            return original_link(source, destination)

        with patch(
            "deepseek_agent.infrastructure.local_reports.os.link", side_effect=conflicting_link
        ):
            with self.assertRaises(CareerDataError):
                self.export()
        self.assertEqual(target.read_bytes(), b"other application content")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])
        self.assertEqual(self.store.list_exports()[0]["result"], "unconfirmed")
        with self.assertRaisesRegex(CareerDataError, "已被修改"):
            self.export()
        self.assertEqual(target.read_bytes(), b"other application content")

    def test_unsupported_exclusive_publication_does_not_fall_back_to_overwrite(self):
        with (
            patch(
                "deepseek_agent.infrastructure.local_reports.os.link",
                side_effect=OSError("unsupported"),
            ),
            patch.object(
                self.service.guard, "write_text", side_effect=AssertionError("unsafe fallback")
            ),
        ):
            with self.assertRaises(CareerDataError):
                self.export()
        self.assertFalse((self.root / "review.md").exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])
        self.assertEqual(self.store.list_exports()[0]["result"], "unconfirmed")

    def test_fake_model_success_conflict_modified_and_refused_retry(self):
        import json
        from deepseek_agent.agent import Agent
        from deepseek_agent.config import Settings
        from deepseek_agent.memory import JsonSessionStore, JsonProjectStore
        from deepseek_agent.models import TextDelta, ToolCallRequest
        from deepseek_agent.tool_execution import ToolExecutionStatus
        from test_career import Confirmer

        class Fake:
            model_name = "offline-export-retry"

            def __init__(self, call_id):
                self.review_id = None
                self.call_id = call_id
                self.calls = 0

            def stream(self, messages, tools):
                self.calls += 1
                if messages[-1]["role"] == "user":
                    yield ToolCallRequest(
                        self.call_id,
                        "export_career_review",
                        json.dumps(dict(review_id=self.review_id, path="review.md")),
                    )
                else:
                    yield TextDelta("已收到本次导出工具结果。")

        with (
            patch(
                "deepseek_agent.models.deepseek.OpenAI", side_effect=AssertionError("SDK forbidden")
            ),
            patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
        ):
            for scenario in ("completed", "conflict", "modified", "refused"):
                with self.subTest(scenario=scenario):
                    root = self.root / scenario
                    root.mkdir()
                    settings = Settings(
                        "offline",
                        "https://example.invalid",
                        "offline-export-retry",
                        "system",
                        workspace_root=root,
                        agent_mode="career",
                    )

                    def make_agent(model, confirmer):
                        return Agent(
                            settings,
                            model=model,
                            confirmer=confirmer,
                            session_store=JsonSessionStore(root / "sessions"),
                            project_store=JsonProjectStore(root / "projects.json"),
                        )

                    first_model = Fake("first-call")
                    first_agent = make_agent(first_model, Confirmer())
                    store = first_agent._career.store
                    resume = store.import_document("resume", "合成简历", "参与测试。")
                    review = store.save_review(
                        resume_id=resume["id"],
                        job_id=None,
                        title="报告",
                        findings=[],
                        general_advice=["补充验证方式。"],
                        changes=[],
                    )
                    first_model.review_id = review["id"]
                    target = root / "review.md"
                    if scenario != "conflict":
                        first_agent.chat("请导出报告")
                    else:
                        target.write_bytes(b"original user file")
                    if scenario == "modified":
                        target.write_bytes(b"user changes after export")
                    before = target.read_bytes(), target.stat().st_mtime_ns
                    receipts = store.list_exports()
                    model = Fake("different-call-id-after-restart")
                    model.review_id = review["id"]
                    confirmer = Confirmer(scenario != "refused")
                    agent = make_agent(model, confirmer)
                    records = []

                    def collect(event):
                        if event.tool_record is not None:
                            records.append(event.tool_record)

                    with patch.object(
                        agent.career_application._exporter,
                        "write_new",
                        side_effect=AssertionError("retry wrote file"),
                    ):
                        agent.chat("再次导出同一报告", on_event=collect)
                    self.assertEqual(len(records), 1)
                    self.assertEqual(len(confirmer.calls), 1)
                    self.assertEqual(model.calls, 2)
                    if scenario == "completed":
                        self.assertEqual(records[0].status, ToolExecutionStatus.SUCCEEDED)
                        result = json.loads(records[0].model_result)
                        self.assertTrue(result["reused"])
                        self.assertEqual(result["export_id"], receipts[0]["id"])
                    elif scenario == "refused":
                        self.assertEqual(records[0].status, ToolExecutionStatus.REJECTED)
                    else:
                        self.assertEqual(records[0].status, ToolExecutionStatus.FAILED)
                        self.assertIn(
                            "没有匹配" if scenario == "conflict" else "已被修改",
                            records[0].model_result,
                        )
                    self.assertEqual(agent._career.store.list_exports(), receipts)
                    self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
