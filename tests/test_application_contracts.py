"""入口契约回归：相同业务约束、确认边界和统一事件流。"""

import io
import json
import queue
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent
from deepseek_agent.career.commands import (
    DecideReview,
    ExportReview,
    ForgetFact,
    ImportDocument,
    RecallMemory,
    SaveFact,
)
from deepseek_agent.career.domain import CareerDataError
from deepseek_agent.career.service import CareerService
from deepseek_agent.career.store import CareerStore
from deepseek_agent.career.tools import CareerMemoryTool, CareerReviewTool
from deepseek_agent.cli import CliApplication
from deepseek_agent.config import Settings
from deepseek_agent.memory import JsonProjectStore, JsonSessionStore
from deepseek_agent.models import TextDelta, ToolCallRequest
from deepseek_agent.runtime import AgentEventType
from deepseek_agent.tools import ToolRegistry
from deepseek_agent.tool_execution import ToolExecutor, ToolExecutionStatus
from deepseek_agent.ui.chat_runner import AgentChatRunner


class Allow:
    def confirm(self, tool, arguments):
        return True


class CalculatorModel:
    model_name = "offline-contract"

    def stream(self, messages, tools):
        if messages[-1]["role"] == "user":
            yield ToolCallRequest("calculate", "calculator", '{"expression":"1+1"}')
        else:
            yield TextDelta("结果为二。")


class ApplicationContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CareerStore(self.root / "profile.sqlite3")
        self.service = CareerService(self.store, self.root, 100_000)
        self.app = self.service.application

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self):
        return Agent(
            Settings(
                api_key="offline",
                base_url="https://example.invalid",
                model="offline-contract",
                system_prompt="system",
                workspace_root=self.root,
            ),
            model=CalculatorModel(),
            confirmer=Allow(),
            session_store=JsonSessionStore(self.root / "sessions"),
            project_store=JsonProjectStore(self.root / "projects.json"),
        )

    def test_application_requires_confirmation_before_any_sensitive_write(self):
        commands = [
            SaveFact("project", "project", "API", "current_user", "API"),
            ForgetFact("missing"),
            DecideReview("missing", "accepted"),
            ExportReview("missing", "report.md"),
        ]
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaisesRegex(CareerDataError, "需要用户确认"):
                    self.app.execute(command, user_message="API")
        self.assertEqual(self.store.snapshot()["facts"], [])
        self.assertFalse((self.root / "report.md").exists())

    def test_source_constraints_are_shared_by_app_and_tool(self):
        resume = self.app.execute(
            ImportDocument("resume", "resume", text="API tests"), user_message="API tests"
        )
        with self.assertRaises(CareerDataError):
            self.app.execute(
                ImportDocument("resume", "invented", text="not provided"), user_message="API tests"
            )
        arguments = dict(
            action="save",
            key="project",
            category="project",
            statement="API",
            source_id=resume["id"],
            source_quote="invented quote",
        )
        tool = CareerMemoryTool(self.service)
        record = ToolExecutor(ToolRegistry([tool]), Allow()).execute(
            ToolCallRequest("fact", tool.name, json.dumps(arguments))
        )
        self.assertEqual(record.status, ToolExecutionStatus.FAILED)
        with self.assertRaises(CareerDataError):
            self.app.execute(
                SaveFact("project", "project", "API", resume["id"], "invented quote"),
                confirmed=True,
            )
        self.assertEqual(self.app.execute(RecallMemory())["facts"], [])

    def test_stale_review_rejected_through_both_entry_paths(self):
        resume = self.store.import_document("resume", "resume", "API tests")
        fact = self.app.execute(
            SaveFact("role", "project", "API tests", resume["id"], "API tests"), confirmed=True
        )
        review = self.store.save_review(
            resume_id=resume["id"],
            job_id=None,
            title="review",
            findings=[],
            general_advice=["draft"],
            changes=[
                dict(
                    original="API tests",
                    suggested="Tested API",
                    reason="clear",
                    evidence_fact_ids=[fact["id"]],
                )
            ],
        )
        self.app.execute(
            SaveFact("role", "project", "documentation", "current_user", "documentation"),
            user_message="documentation",
            confirmed=True,
        )
        with self.assertRaises(CareerDataError):
            self.app.execute(DecideReview(review["id"], "accepted"), confirmed=True)
        tool = CareerReviewTool(self.service)
        result = ToolExecutor(ToolRegistry([tool]), Allow()).execute(
            ToolCallRequest(
                "accept",
                tool.name,
                json.dumps(dict(action="decide", review_id=review["id"], decision="accepted")),
            )
        )
        self.assertEqual(result.status, ToolExecutionStatus.FAILED)
        self.assertEqual(self.store.review(review["id"])["status"], "draft")

    def test_events_have_ordered_turn_identity_and_no_duplicate_gui_output(self):
        agent = self.agent()
        first, second = [], []
        agent.chat("first", on_event=first.append)
        agent.chat("second", on_event=second.append)
        for events in (first, second):
            self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
            self.assertEqual(len({event.turn_id for event in events}), 1)
            self.assertTrue(events[0].run_id)
            self.assertEqual(events[0].type, AgentEventType.TURN_STARTED)
        self.assertNotEqual(first[0].turn_id, second[0].turn_id)
        events = queue.Queue()
        AgentChatRunner(agent, events, threading.Event()).run("third")
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        self.assertEqual(
            [name for name, _ in emitted], ["tool_call", "tool_result", "text", "done"]
        )
        self.assertEqual(emitted[2][1], "结果为二。")

    def test_cli_renders_each_event_once(self):
        output = io.StringIO()
        with redirect_stdout(output):
            CliApplication(self.agent())._chat("calculate")
        rendered = output.getvalue()
        self.assertEqual(rendered.count("结果为二。"), 1)
        self.assertEqual(rendered.count("[工具调用]"), 1)
        self.assertEqual(rendered.count("[工具结果]"), 1)
