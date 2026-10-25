"""验证求职来源、事实确认、纠正、版本审阅和现有入口的完整闭环。"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent import cli, gui_app
from deepseek_agent.agent import Agent
from deepseek_agent.career.documents import DocumentReader
from deepseek_agent.career.service import CareerService
from deepseek_agent.career.store import CareerDataError, CareerStore
from deepseek_agent.career.tools import (
    CareerMemoryTool,
    CareerReviewTool,
    ExportCareerReviewTool,
    ImportCareerDocumentTool,
)
from deepseek_agent.config import Settings
from deepseek_agent.memory import JsonProjectStore, JsonSessionStore
from deepseek_agent.models import TextDelta, ToolCallRequest
from deepseek_agent.tool_execution import ToolExecutor, ToolExecutionStatus
from deepseek_agent.tools import ToolRegistry, ToolExecutionError
from deepseek_agent.ui.tk_app import AgentApp
from deepseek_agent.workspace import WorkspaceAccessError, WorkspaceGuard


class Confirmer:
    def __init__(self, allow=True):
        self.allow = allow
        self.calls = []

    def confirm(self, tool, arguments):
        self.calls.append((tool.name, json.loads(arguments)))
        return self.allow


class Model:
    model_name = "offline-career"

    def __init__(self, respond=None):
        self.requests = []
        self.respond = respond or (lambda messages: [TextDelta("请介绍项目经历。")])

    def stream(self, messages, tools):
        self.requests.append((deepcopy(messages), deepcopy(tools)))
        yield from self.respond(messages)


def call(name, **arguments):
    return ToolCallRequest(
        name + "-" + str(len(arguments)), name, json.dumps(arguments, ensure_ascii=False)
    )


class CareerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CareerStore(self.root / "profile.sqlite3")
        self.service = CareerService(self.store, self.root, 100_000)
        self.resume = self.store.import_document(
            "resume", "学生简历", "参与 Agent 项目，负责工具调用和后端接口。"
        )
        self.job = self.store.import_document(
            "job", "开发实习", "要求 Python、工具调用、后端接口；每周四天。"
        )
        self.fact = self.store.save_fact(
            key="agent项目/职责",
            category="project",
            statement="负责工具调用和后端接口。",
            source_id=self.resume["id"],
            source_quote="负责工具调用和后端接口。",
        )
        self.settings = Settings(
            api_key="offline",
            base_url="https://example.invalid",
            model="offline-career",
            system_prompt="system",
            workspace_root=self.root,
            agent_mode="career",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def review(self, **overrides):
        arguments = dict(
            resume_id=self.resume["id"],
            job_id=self.job["id"],
            title="针对开发实习的修改",
            markdown="待补充：测试方法。",
            changes=[
                dict(
                    original="负责工具调用和后端接口。",
                    suggested="实现 Agent 工具调用及后端接口。",
                    reason="明确本人职责。",
                    evidence_fact_ids=[self.fact["id"]],
                )
            ],
        )
        arguments.update(overrides)
        return self.store.save_review(**arguments)

    def agent(self, model=None, confirmer=None, settings=None):
        return Agent(
            settings or self.settings,
            model=model or Model(),
            confirmer=confirmer or Confirmer(),
            session_store=JsonSessionStore(self.root / "sessions"),
            project_store=JsonProjectStore(self.root / "projects.json"),
        )

    def executor(self, tool, allow=True):
        confirmer = Confirmer(allow)
        return ToolExecutor(ToolRegistry([tool]), confirmer), confirmer

    def test_documents_are_deduplicated_without_confirming_facts(self):
        before = self.store.snapshot()
        duplicate = self.store.import_document("resume", "另一名称", self.resume["content"])
        self.assertEqual(duplicate["id"], self.resume["id"])
        self.assertEqual(self.store.snapshot(), before)

    def test_fact_correction_survives_restart_and_replaces_active_version(self):
        correction = "我只负责接口，没有负责工具调用。"
        updated = self.store.save_fact(
            key="agent项目/职责",
            category="project",
            statement=correction,
            source_id="current_user",
            source_quote=correction,
            current_user=correction,
        )
        restarted = CareerStore(self.store.path)
        self.assertEqual(updated["id"], self.fact["id"])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(restarted.snapshot()["facts"], [updated])
        with restarted.connection() as db:
            history = json.loads(db.execute("SELECT data FROM fact_history").fetchone()[0])
        self.assertEqual(history["statement"], self.fact["statement"])

    def test_job_and_invented_quote_cannot_be_personal_fact_sources(self):
        for source, quote in [
            (self.job["id"], "要求 Python"),
            (self.resume["id"], "主导系统并提升性能 80%"),
            ("current_user", "不存在的原话"),
        ]:
            with self.subTest(source=source), self.assertRaises(CareerDataError):
                self.store.save_fact(
                    key="虚构经历",
                    category="project",
                    statement="虚构",
                    source_id=source,
                    source_quote=quote,
                    current_user="你好",
                )
        self.assertEqual(len(self.store.snapshot()["facts"]), 1)

    def test_rejected_confirmation_never_saves_or_corrects_fact(self):
        executor, confirmer = self.executor(CareerMemoryTool(self.service), allow=False)
        record = executor.execute(
            call(
                "career_memory",
                action="save",
                key="agent项目/职责",
                category="project",
                statement="修改职责",
                source_id=self.resume["id"],
                source_quote="负责工具调用和后端接口。",
            )
        )
        self.assertEqual(record.status, ToolExecutionStatus.REJECTED)
        self.assertEqual(self.store.snapshot()["facts"], [self.fact])
        self.assertEqual(len(confirmer.calls), 1)

    def test_approved_fact_requires_real_source_and_recall_needs_no_confirmation(self):
        self.service.current_user = "我每周可以实习四天。"
        executor, confirmer = self.executor(CareerMemoryTool(self.service))
        saved = executor.execute(
            call(
                "career_memory",
                action="save",
                key="实习/天数",
                category="preference",
                statement="每周实习四天",
                source_id="current_user",
                source_quote=self.service.current_user,
            )
        )
        recalled = executor.execute(call("career_memory", action="recall", query="实习"))
        self.assertEqual(saved.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(recalled.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(len(confirmer.calls), 1)
        self.assertEqual(json.loads(recalled.model_result)["facts"][0]["fact_key"], "实习/天数")

    def test_review_remains_draft_and_does_not_change_personal_facts(self):
        before = self.store.snapshot()["facts"]
        review = self.review()
        self.assertEqual(review["status"], "draft")
        self.assertFalse(review["stale"])
        self.assertEqual(self.store.snapshot()["facts"], before)
        self.assertIn("待审阅草稿", self.store.render_review(review["id"]))

    def test_review_rejects_missing_evidence_and_wrong_original_or_document_types(self):
        bad_changes = [[], ["missing"], [self.fact["id"]]]
        for evidence in bad_changes:
            with self.subTest(evidence=evidence), self.assertRaises(CareerDataError):
                self.review(
                    changes=[
                        dict(
                            original="不存在的原文"
                            if evidence == [self.fact["id"]]
                            else "负责工具调用和后端接口。",
                            suggested="建议",
                            reason="理由",
                            evidence_fact_ids=evidence,
                        )
                    ]
                )
        with self.assertRaises(CareerDataError):
            self.review(resume_id=self.job["id"])
        with self.assertRaises(CareerDataError):
            self.review(job_id=self.resume["id"])
        self.assertEqual(self.store.snapshot()["reviews"], [])

    def test_review_decisions_are_confirmed_and_do_not_accept_after_rejection(self):
        review = self.review()
        executor, confirmer = self.executor(CareerReviewTool(self.service), allow=False)
        rejected = executor.execute(
            call("career_review", action="decide", review_id=review["id"], decision="accepted")
        )
        self.assertEqual(rejected.status, ToolExecutionStatus.REJECTED)
        self.assertEqual(self.store.review(review["id"])["status"], "draft")
        confirmer.allow = True
        executor.execute(
            call("career_review", action="decide", review_id=review["id"], decision="accepted")
        )
        self.assertEqual(self.store.review(review["id"])["status"], "accepted")
        executor.execute(
            call("career_review", action="decide", review_id=review["id"], decision="rejected")
        )
        self.assertEqual(self.store.review(review["id"])["status"], "rejected")

    def test_corrected_or_forgotten_facts_invalidate_dependent_reviews(self):
        review = self.review()
        self.store.decide_review(review["id"], "accepted")
        self.store.save_fact(
            key="agent项目/职责",
            category="project",
            statement="只负责接口",
            source_id="current_user",
            source_quote="只负责接口",
            current_user="只负责接口",
        )
        self.assertTrue(self.store.review(review["id"])["stale"])
        with self.assertRaises(CareerDataError):
            self.store.decide_review(review["id"], "accepted")
        with self.assertRaises(CareerDataError):
            self.store.render_review(review["id"])
        self.store.forget_fact(self.fact["id"])
        self.assertEqual(self.store.snapshot()["facts"], [])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fact_history").fetchone()[0], 0)

    def test_export_keeps_original_files_and_contains_review_status_and_sources(self):
        review = self.review()
        tool = ExportCareerReviewTool(self.service)
        original = self.root / "resume.md"
        original.write_text(self.resume["content"], encoding="utf-8")
        with self.assertRaises(ToolExecutionError):
            tool.execute(dict(review_id=review["id"], path="resume.md"))
        self.assertEqual(original.read_text(encoding="utf-8"), self.resume["content"])
        result = json.loads(tool.execute(dict(review_id=review["id"], path="review.md")))
        content = (self.root / result["path"]).read_text(encoding="utf-8")
        self.assertIn("待审阅草稿", content)
        self.assertIn(self.fact["id"], content)
        self.assertIn(self.fact["source_quote"], content)
        with self.assertRaises(ToolExecutionError):
            tool.execute(dict(review_id=review["id"], path="../outside.md"))
        with self.assertRaises(ToolExecutionError):
            tool.execute(dict(review_id=review["id"], path="review.txt"))

    def test_import_requires_user_provided_text_or_path(self):
        tool = ImportCareerDocumentTool(self.service)
        self.service.current_user = "我的简历：参与课程设计，负责接口测试。"
        source = json.loads(
            tool.execute(dict(kind="resume", title="新简历", text="参与课程设计，负责接口测试。"))
        )
        self.assertEqual(source["kind"], "resume")
        for arguments in [
            dict(kind="resume", title="虚构", text="主导团队"),
            dict(kind="resume", title="虚构", path="unknown.txt"),
            dict(kind="resume", title="虚构"),
            dict(kind="resume", title="虚构", path="x.txt", text="参与"),
        ]:
            with self.subTest(arguments=arguments), self.assertRaises(ToolExecutionError):
                tool.execute(arguments)

    def test_text_and_docx_import_preserve_paragraph_and_table_order(self):
        reader = DocumentReader(self.root)
        (self.root / "resume.txt").write_bytes("\ufeff学生简历\n项目经历".encode("utf-8"))
        self.assertEqual(reader.read("resume.txt"), "学生简历\n项目经历")
        with ZipFile(self.root / "resume.docx", "w") as archive:
            archive.writestr(
                "word/document.xml",
                """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>项目</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>接口测试</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>""",
            )
        self.assertEqual(reader.read("resume.docx"), "项目\n接口测试")

    def test_pdf_text_is_read_and_encrypted_or_scanned_pdf_is_reported(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(600, 800)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 700 Td (Backend student resume) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.write(self.root / "resume.pdf")
        self.assertIn("Backend student resume", DocumentReader(self.root).read("resume.pdf"))
        blank = PdfWriter()
        blank.add_blank_page(600, 800)
        blank.write(self.root / "scan.pdf")
        with self.assertRaisesRegex(CareerDataError, "OCR"):
            DocumentReader(self.root).read("scan.pdf")
        blank.encrypt("password")
        blank.write(self.root / "encrypted.pdf")
        with self.assertRaisesRegex(CareerDataError, "密码"):
            DocumentReader(self.root).read("encrypted.pdf")

    def test_invalid_oversized_and_sensitive_documents_fail_without_partial_import(self):
        reader = DocumentReader(self.root)
        for filename, data in [
            ("broken.docx", b"broken"),
            ("legacy.doc", b"old"),
            ("long.txt", b"x" * 50_001),
            (".env", b"secret"),
        ]:
            (self.root / filename).write_bytes(data)
            with (
                self.subTest(filename=filename),
                self.assertRaises((CareerDataError, WorkspaceAccessError)),
            ):
                reader.read(filename)
        with self.assertRaises(WorkspaceAccessError):
            reader.read("../escape.txt")
        (self.root / "huge.pdf").write_bytes(b"x" * 5_000_001)
        with self.assertRaises(WorkspaceAccessError):
            reader.read("huge.pdf")

    def test_docx_entity_definitions_are_not_parsed(self):
        with ZipFile(self.root / "entity.docx", "w") as archive:
            archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "text">]><x>&a;</x>')
        with self.assertRaisesRegex(CareerDataError, "实体"):
            DocumentReader(self.root).read("entity.docx")

    def test_workspace_tools_cannot_edit_internal_career_memory(self):
        guard = WorkspaceGuard(self.root, 100_000)
        with self.assertRaises(WorkspaceAccessError):
            guard.prepare_text_write("data/career/default/profile.sqlite3", "replace")
        career = self.agent()
        self.assertIn("career_memory", career.tool_names)
        self.assertIn("get_current_time", career.tool_names)
        self.assertNotIn("run_command", career.tool_names)
        self.assertNotIn("write_text_file", career.tool_names)
        general = self.agent(settings=replace(self.settings, agent_mode="general"))
        self.assertNotIn("career_memory", general.tool_names)
        self.assertIn("run_command", general.tool_names)

    def test_new_session_and_restart_keep_memory_while_profiles_are_isolated(self):
        first = self.agent()
        first._career.store.save_fact(
            key="目标/方向",
            category="preference",
            statement="Agent 与后端",
            source_id="current_user",
            source_quote="Agent 与后端",
            current_user="Agent 与后端",
        )
        first.start_new_session()
        first.clear_conversation()
        restarted = self.agent()
        self.assertEqual(restarted.career_snapshot()["facts"][0]["statement"], "Agent 与后端")
        other = self.agent(settings=replace(self.settings, career_profile_id="other"))
        self.assertEqual(other.career_snapshot()["facts"], [])

    def test_default_career_sessions_and_projects_are_isolated_by_profile(self):
        with patch("deepseek_agent.agent.PROJECT_ROOT", self.root):
            first = Agent(self.settings, model=Model(), confirmer=Confirmer())
            second = Agent(
                replace(self.settings, career_profile_id="other"),
                model=Model(),
                confirmer=Confirmer(),
            )
        self.assertNotEqual(first.session_id, second.session_id)
        first.create_project("学生 A")
        self.assertEqual(second.list_projects(), [])

    def test_runtime_context_cache_invalidates_on_data_or_user_change(self):
        service = self.agent()._career
        first = service.runtime_context()

        # 数据与输入都没变时复用同一份缓存结果。
        self.assertIs(service.runtime_context(), first)

        # 事实变化（版本号自增）后必须重建。
        service.current_user = "用户原话：缓存必须随事实更新失效。"
        service.store.save_fact(
            key="缓存/失效",
            category="skill",
            statement="缓存必须随事实更新失效",
            source_id="current_user",
            source_quote="缓存必须随事实更新失效",
            current_user=service.current_user,
        )
        refreshed = service.runtime_context()
        self.assertIsNot(refreshed, first)
        self.assertIn("缓存必须随事实更新失效", refreshed)

        # 本轮用户输入变化后同样重建。
        service.current_user = "我补充了新的原话。"
        self.assertIsNot(service.runtime_context(), refreshed)

    def test_runtime_reinjects_current_facts_with_budget_and_older_fact_retrieval(self):
        agent = self.agent()
        for index in range(35):
            statement = "稀有 Zeta 内容" if index == 0 else f"项目 {index} " + "证据" * 200
            agent._career.store.save_fact(
                key=f"项目/{index}",
                category="project",
                statement=statement,
                source_id="current_user",
                source_quote=statement,
                current_user=statement,
            )
        model = Model()
        agent._model._model = model
        agent.chat("请使用 Zeta 项目分析我的简历。")
        system = model.requests[0][0][0]["content"]
        self.assertIn("稀有 Zeta 内容", system)
        self.assertIn('"total_facts": 35', system)
        self.assertLess(len(agent._career.runtime_context()), 15_000)
        recalled = json.loads(
            CareerMemoryTool(agent._career).execute(dict(action="recall", query="Zeta"))
        )
        self.assertEqual(recalled["facts"][0]["fact_key"], "项目/0")

    def test_end_to_end_import_fact_review_accept_export_and_correct_after_restart(self):
        (self.root / "resume.txt").write_text(self.resume["content"], encoding="utf-8")
        states = {}
        steps = iter(["import", "fact", "review", "accept", "export", "answer"])

        def respond(messages):
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            step = next(steps)
            if step == "import":
                return [
                    call("import_career_document", kind="resume", title="简历", path="resume.txt")
                ]
            if step == "fact":
                states["document"] = results[-1]["id"]
                return [
                    call(
                        "career_memory",
                        action="save",
                        key="agent项目/职责",
                        category="project",
                        statement="负责工具调用和后端接口。",
                        source_id=states["document"],
                        source_quote="负责工具调用和后端接口。",
                    )
                ]
            if step == "review":
                states["fact"] = results[-1]["id"]
                return [
                    call(
                        "career_review",
                        action="save",
                        resume_id=states["document"],
                        title="简历优化",
                        markdown="需要补充验证方法。",
                        changes=[
                            dict(
                                original="负责工具调用和后端接口。",
                                suggested="实现工具调用与后端接口。",
                                reason="明确职责",
                                evidence_fact_ids=[states["fact"]],
                            )
                        ],
                    )
                ]
            if step == "accept":
                states["review"] = results[-1]["id"]
                return [
                    call(
                        "career_review",
                        action="decide",
                        review_id=states["review"],
                        decision="accepted",
                    )
                ]
            if step == "export":
                return [
                    call("export_career_review", review_id=states["review"], path="approved.md")
                ]
            return [TextDelta("报告已保存，请准备项目面试。")]

        confirmer = Confirmer()
        model = Model(respond)
        agent = self.agent(model, confirmer)
        agent.chat("请根据 resume.txt 优化简历，确认后导出 approved.md。")
        self.assertTrue((self.root / "approved.md").exists())
        self.assertEqual(
            [name for name, args in confirmer.calls],
            ["career_memory", "career_review", "export_career_review"],
        )
        self.assertIn("已接受", (self.root / "approved.md").read_text(encoding="utf-8"))
        history = agent.history()
        requested = [c["id"] for m in history for c in m.get("tool_calls", [])]
        results = [m["tool_call_id"] for m in history if m["role"] == "tool"]
        self.assertCountEqual(requested, results)
        correction = "我只负责接口测试。"
        corrected_model = Model(
            lambda m: (
                [
                    call(
                        "career_memory",
                        action="save",
                        key="agent项目/职责",
                        category="project",
                        statement=correction,
                        source_id="current_user",
                        source_quote=correction,
                    )
                ]
                if m[-1]["role"] == "user"
                else [TextDelta("已更新职责。")]
            )
        )
        restarted = self.agent(corrected_model)
        restarted.start_new_session()
        restarted.chat(correction)
        self.assertEqual(restarted.career_snapshot()["facts"][0]["revision"], 2)
        self.assertTrue(restarted._career.store.review(states["review"])["stale"])
        fresh = self.agent()
        messages = fresh._prepare_model_messages(force_plan_continuation=True)
        self.assertIn(correction, messages[0]["content"])
        self.assertNotIn("负责工具调用和后端接口。", messages[0]["content"])

    def test_cli_paste_import_and_cancellation(self):
        application = cli.CliApplication(self.agent())
        with (
            patch.object(application, "_chat") as chat,
            patch("builtins.input", side_effect=["项目经历", "/end"]),
        ):
            with redirect_stdout(io.StringIO()):
                application.handle_command("/resume")
        self.assertIn("项目经历", chat.call_args[0][0])
        with (
            patch.object(application, "_chat") as chat,
            patch("builtins.input", side_effect=["/cancel"]),
        ):
            with redirect_stdout(io.StringIO()):
                application.handle_command("/jd")
        chat.assert_not_called()
        with patch.object(application, "_chat") as chat:
            application.handle_command('/resume "我的 简历.pdf"')
        self.assertIn("我的 简历.pdf", chat.call_args[0][0])

    def test_cli_memory_and_explicit_forgetting(self):
        agent = self.agent()
        fact = agent._career.store.save_fact(
            key="方向",
            category="preference",
            statement="后端",
            source_id="current_user",
            source_quote="后端",
            current_user="后端",
        )
        application = cli.CliApplication(agent)
        with patch("builtins.input", return_value="n"), redirect_stdout(io.StringIO()):
            application.handle_command("/forget " + fact["id"])
        self.assertEqual(len(agent.career_snapshot()["facts"]), 1)
        with patch("builtins.input", return_value="y"), redirect_stdout(io.StringIO()):
            application.handle_command("/forget " + fact["id"])
            application.handle_command("/memory")
        self.assertEqual(agent.career_snapshot()["facts"], [])

    def test_gui_import_only_prepares_prompt_and_rejects_outside_workspace(self):
        app = AgentApp.__new__(AgentApp)
        app._root = Mock()
        app._workspace_root = self.root
        app._is_busy = Mock(return_value=False)
        app._use_prompt_suggestion = Mock()
        with patch(
            "deepseek_agent.ui.tk_app.filedialog.askopenfilename",
            return_value=str(self.root / "resume.pdf"),
        ):
            app._import_career_resume()
        self.assertIn("resume.pdf", app._use_prompt_suggestion.call_args[0][0])
        app._use_prompt_suggestion.reset_mock()
        with (
            patch(
                "deepseek_agent.ui.tk_app.filedialog.askopenfilename",
                return_value=str(self.root.parent / "outside.pdf"),
            ),
            patch("deepseek_agent.ui.tk_app.messagebox.showerror") as error,
        ):
            app._import_career_resume()
        error.assert_called_once()
        app._use_prompt_suggestion.assert_not_called()

    def test_career_entry_points_keep_existing_interfaces(self):
        with (
            patch.object(cli.Settings, "from_env", return_value=self.settings),
            patch.object(cli, "build_file_logger"),
            patch.object(cli, "Agent") as agent,
            patch.object(cli, "CliApplication"),
        ):
            cli.career_main()
        self.assertEqual(agent.call_args[0][0].agent_mode, "career")
        with (
            patch.object(gui_app.Settings, "from_env", return_value=self.settings),
            patch.object(gui_app, "build_file_logger"),
            patch.object(gui_app, "run_gui") as gui,
        ):
            gui_app.career_main()
        self.assertEqual(gui.call_args[0][0].agent_mode, "career")

    def test_mode_configuration_and_profile_path_validation(self):
        with (
            patch.dict(
                os.environ,
                {
                    "DEEPSEEK_API_KEY": "test",
                    "AGENT_MODE": "career",
                    "AGENT_CAREER_PROFILE_ID": "student_01",
                    "AGENT_WORKSPACE": str(self.root),
                },
                clear=True,
            ),
            patch("deepseek_agent.config.load_dotenv"),
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.agent_mode, "career")
        self.assertEqual(settings.career_profile_id, "student_01")
        for profile in ["../escape", "a/b", "", "x" * 65]:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                replace(self.settings, career_profile_id=profile)
        with self.assertRaises(ValueError):
            replace(self.settings, agent_mode="unknown")

    def test_invalid_database_is_not_silently_replaced(self):
        broken = self.root / "broken.sqlite3"
        broken.write_bytes(b"invalid database")
        with self.assertRaises(CareerDataError):
            CareerStore(broken)
        self.assertEqual(broken.read_bytes(), b"invalid database")

    def test_zero_resume_draft_uses_confirmed_facts_and_cannot_create_fact_cycles(self):
        empty = CareerStore(self.root / "new_student.sqlite3")
        fact = empty.save_fact(
            key="课程项目/职责",
            category="project",
            statement="负责接口测试",
            source_id="current_user",
            source_quote="负责接口测试",
            current_user="负责接口测试",
        )
        draft = empty.build_resume_draft([fact["id"]])
        self.assertEqual(draft["kind"], "resume_draft")
        self.assertIn("负责接口测试", draft["content"])
        self.assertNotIn("主导", draft["content"])
        with self.assertRaises(CareerDataError):
            empty.save_fact(
                key="循环事实",
                category="project",
                statement="负责接口测试",
                source_id=draft["id"],
                source_quote="负责接口测试",
            )
        report = empty.save_review(
            resume_id=draft["id"],
            job_id=None,
            title="初稿审阅",
            markdown="待补充方法",
            changes=[
                dict(
                    original="负责接口测试",
                    suggested="承担课程项目的接口测试",
                    reason="明确上下文",
                    evidence_fact_ids=[fact["id"]],
                )
            ],
        )
        self.assertEqual(report["status"], "draft")

    def test_initial_draft_rejects_unknown_or_feedback_facts(self):
        with self.assertRaises(CareerDataError):
            self.store.build_resume_draft([])
        with self.assertRaises(CareerDataError):
            self.store.build_resume_draft(["missing"])
        feedback = self.store.save_fact(
            key="面试反馈",
            category="feedback",
            statement="需要补充测试思路",
            source_id="current_user",
            source_quote="需要补充测试思路",
            current_user="需要补充测试思路",
        )
        with self.assertRaises(CareerDataError):
            self.store.build_resume_draft([feedback["id"]])

    def test_snapshot_exposes_stale_review_after_fact_deletion(self):
        report = self.review()
        self.store.forget_fact(self.fact["id"])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["reviews"][0]["id"], report["id"])
        self.assertTrue(snapshot["reviews"][0]["stale"])

    def test_real_tk_career_controls_and_zero_resume_entry(self):
        import tkinter as tk
        from tkinter import ttk

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk 不可用：{error}")
        root.withdraw()
        app = None
        try:
            with patch("deepseek_agent.ui.tk_app.Agent", return_value=self.agent()):
                app = AgentApp(root, self.settings)
            root.update_idletasks()

            def widgets(parent):
                for name in root.tk.splitlist(root.tk.call("winfo", "children", parent._w)):
                    widget = root.nametowidget(name)
                    yield widget
                    yield from widgets(widget)

            buttons = {
                widget.cget("text"): widget
                for widget in widgets(root)
                if isinstance(widget, (tk.Button, ttk.Button))
            }
            self.assertIn("导入简历", buttons)
            self.assertIn("求职记忆", buttons)
            buttons["从零整理"].invoke()
            self.assertIn("我还没有简历", app._input.get("1.0", "end"))
            buttons["简历诊断"].invoke()
            self.assertIn("诊断我的简历", app._input.get("1.0", "end"))
        finally:
            if app is not None:
                app._on_close()
            elif root.winfo_exists():
                root.destroy()


if __name__ == "__main__":
    unittest.main()
