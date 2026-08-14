import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from _path_setup import add_project_root_to_path

add_project_root_to_path()

from src.deepseek_agent.journal import RunJournal
from src.deepseek_agent.tool_execution import (
    ToolExecutionRecord,
    ToolExecutionStart,
    ToolExecutionStatus,
)
from src.deepseek_agent.tools import ToolEffect


class RunJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.journal = RunJournal(
            self.directory,
            run_id="run_test",
            durable=False,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def start() -> ToolExecutionStart:
        return ToolExecutionStart(
            call_id="call_write",
            tool_name="write_text_file",
            raw_arguments=(
                '{"path":"secret.txt","content":"TOP-SECRET-CONTENT"}'
            ),
            arguments={
                "path": "secret.txt",
                "content": "TOP-SECRET-CONTENT",
            },
            effect=ToolEffect.IRREVERSIBLE_WRITE,
            retryable=False,
            idempotent=False,
            supports_rollback=False,
            timeout_seconds=None,
        )

    def test_tool_arguments_are_summarized_without_values(self) -> None:
        self.journal.record_tool_started("turn_test", self.start())

        content = self.journal.path.read_text(encoding="utf-8")
        event = json.loads(content)

        self.assertNotIn("TOP-SECRET-CONTENT", content)
        self.assertNotIn("secret.txt", content)
        self.assertEqual(event["arguments"]["keys"], ["content", "path"])
        self.assertEqual(len(event["arguments"]["sha256"]), 64)

    def test_unfinished_tool_is_reported_as_recovery_issue(self) -> None:
        self.journal.record_tool_started("turn_test", self.start())

        restarted = RunJournal(
            self.directory,
            run_id="run_restarted",
            durable=False,
        )
        issues = restarted.find_recovery_issues()

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].run_id, "run_test")
        self.assertEqual(issues[0].turn_id, "turn_test")
        self.assertEqual(issues[0].call_id, "call_write")
        self.assertIn("不要直接自动重试", issues[0].message)

    def test_finished_tool_stays_pending_until_turn_is_saved(self) -> None:
        start = self.start()
        self.journal.record_tool_started("turn_test", start)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.journal.record_tool_finished(
            "turn_test",
            ToolExecutionRecord(
                call_id=start.call_id,
                tool_name=start.tool_name,
                raw_arguments=start.raw_arguments,
                arguments=start.arguments,
                status=ToolExecutionStatus.SUCCEEDED,
                effect=start.effect,
                model_result="写入成功",
                started_at=now,
                finished_at=now,
                confirmation_requested=True,
                confirmation_granted=True,
                retryable=False,
                idempotent=False,
                supports_rollback=False,
                timeout_seconds=None,
                execution_started=True,
            ),
        )

        issues = self.journal.find_recovery_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].tool_status, "succeeded")

        self.journal.record_turn_finished(
            "turn_test",
            status="completed",
            steps_completed=2,
            tool_calls_completed=1,
            history_preserved=True,
            error_present=False,
        )

        self.assertEqual(self.journal.find_recovery_issues(), ())

    def test_truncated_last_line_does_not_break_recovery_scan(self) -> None:
        self.journal.path.write_text(
            '{"event":"tool_started","run_id":"broken"',
            encoding="utf-8",
        )

        self.assertEqual(self.journal.find_recovery_issues(), ())


if __name__ == "__main__":
    unittest.main()
