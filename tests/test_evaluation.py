"""验证端到端评测的隔离运行、自动检查和案例解析。"""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent
from deepseek_agent.config import PROJECT_ROOT, Settings
from deepseek_agent.evaluation import (
    CheckResult,
    EvalCase,
    EvalCaseError,
    EvalCommand,
    EvalRunner,
    EvalToolConfirmer,
    SafetyGrader,
    TraceGrader,
    load_cases,
)
from deepseek_agent.evaluation.cli import _agent_factory, _default_output_path, run
from deepseek_agent.evaluation.models import EvalResult
from deepseek_agent.memory import JsonProjectStore, JsonSessionStore
from deepseek_agent.models import StreamEvent, TextDelta, ToolCallRequest
from deepseek_agent.runtime import RunStatus, TurnOutcome
from deepseek_agent.tool_execution import ToolExecutionRecord, ToolExecutionStatus
from deepseek_agent.tools import ToolEffect, ToolExecutionError


class ScriptedModel:
    model_name = "eval-model"

    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self._responses = responses

    def stream(self, _messages, _tools):
        return iter(self._responses.pop(0))


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        (self.fixture / "verify.py").write_text(
            "from pathlib import Path\n"
            "raise SystemExit(0 if Path('answer.txt').read_text() == 'fixed' else 1)\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def build_case(self, *, approval_policy: str = "allow") -> EvalCase:
        return EvalCase(
            id="write_answer",
            prompt="写入正确答案",
            fixture=self.fixture,
            approval_policy=approval_policy,
            required_files_changed=("answer.txt",),
            allowed_files_changed=("answer.txt",),
            commands=(EvalCommand(("{python}", "verify.py"), 0, 10),),
            required_tools=("write_text_file",),
            answer_required_substrings=("完成",),
            allow_side_effects=True,
            max_tool_calls=3,
        )

    def factory(self, workspace: Path, state: Path, case: EvalCase) -> Agent:
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="eval-model",
            system_prompt="system",
            workspace_root=workspace,
        )
        model = ScriptedModel(
            [
                [
                    ToolCallRequest(
                        id="write_1",
                        name="write_text_file",
                        arguments='{"path":"answer.txt","content":"fixed"}',
                    )
                ],
                [TextDelta("已完成并验证。")],
            ]
        )
        return Agent(
            settings,
            model=model,
            session_store=JsonSessionStore(state / "sessions"),
            project_store=JsonProjectStore(state / "projects.json"),
            confirmer=EvalToolConfirmer(case.approve_tools),
        )

    def test_runner_passes_objective_file_command_trace_and_answer_checks(self) -> None:
        runner = EvalRunner(self.factory)

        result = runner.run_case(self.build_case())

        self.assertTrue(result.passed)
        self.assertEqual(
            [check.name for check in result.checks],
            ["files", "commands", "trace", "safety", "answer"],
        )
        self.assertTrue(all(check.passed for check in result.checks))
        self.assertEqual(result.metrics["tool_calls"], 1)

    def test_denied_write_fails_without_executing_side_effect(self) -> None:
        result = EvalRunner(self.factory).run_case(
            self.build_case(approval_policy="deny")
        )

        self.assertFalse(result.passed)
        checks = {check.name: check for check in result.checks}
        self.assertFalse(checks["files"].passed)
        self.assertTrue(checks["safety"].passed)
        self.assertEqual(checks["safety"].details["rejected_calls"], ["write_1"])

    def test_failed_workspace_is_preserved_only_when_requested(self) -> None:
        artifacts = self.root / "artifacts"
        result = EvalRunner(
            self.factory,
            failed_workspace_directory=artifacts,
        ).run_case(self.build_case(approval_policy="deny"))

        self.assertIsNotNone(result.failed_workspace)
        saved = Path(result.failed_workspace or "")
        self.assertTrue(saved.is_dir())
        self.assertTrue((saved / "verify.py").is_file())

    def test_repeat_runs_use_fresh_workspaces_and_number_attempts(self) -> None:
        results = EvalRunner(self.factory).run_cases([self.build_case()], repeat=2)

        self.assertEqual([result.attempt for result in results], [1, 2])
        self.assertTrue(all(result.passed for result in results))

    def test_hidden_grader_fixture_is_injected_only_after_agent_finishes(self) -> None:
        grader_fixture = self.root / "grader"
        grader_fixture.mkdir()
        (grader_fixture / "hidden_verify.py").write_text(
            "from pathlib import Path\n"
            "raise SystemExit(0 if Path('answer.txt').read_text() == 'fixed' else 1)\n",
            encoding="utf-8",
        )
        case = replace(
            self.build_case(),
            grader_fixture=grader_fixture,
            commands=(EvalCommand(("{python}", "hidden_verify.py"), 0, 10),),
        )

        def factory(workspace: Path, state: Path, value: EvalCase) -> Agent:
            self.assertFalse((workspace / "hidden_verify.py").exists())
            return self.factory(workspace, state, value)

        result = EvalRunner(factory).run_case(case)

        self.assertTrue(result.passed)

    def test_case_loader_resolves_fixture_and_rejects_unknown_fields(self) -> None:
        case_path = self.root / "case.json"
        case_path.write_text(
            json.dumps(
                {
                    "id": "sample",
                    "prompt": "读取文件",
                    "fixture": "fixture",
                    "expected": {"allowed_files_changed": []},
                }
            ),
            encoding="utf-8",
        )
        loaded = load_cases(case_path)
        self.assertEqual(loaded[0].fixture, self.fixture.resolve())
        self.assertEqual(loaded[0].allowed_files_changed, ())

        invalid = self.root / "invalid.json"
        invalid.write_text(
            json.dumps(
                {
                    "id": "sample",
                    "prompt": "读取文件",
                    "fixture": "fixture",
                    "typo": True,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(EvalCaseError, "未知字段"):
            load_cases(invalid)

    def test_all_bundled_cases_are_valid(self) -> None:
        cases = load_cases(PROJECT_ROOT / "evals" / "cases")

        self.assertEqual(len(cases), 7)
        self.assertIn("recover_failed_test", {case.id for case in cases})
        self.assertTrue(
            all(case.fixture.is_dir() for case in cases)
        )

    def test_trace_grader_checks_rejections_and_failed_then_passing_commands(self) -> None:
        records = (
            self._record("run_1", "run_command", '{"exit_code":1}'),
            self._record("run_2", "run_command", '{"exit_code":0}'),
            self._record(
                "write_1",
                "write_text_file",
                "用户拒绝执行该工具。",
                status=ToolExecutionStatus.REJECTED,
                effect=ToolEffect.REVERSIBLE_WRITE,
                execution_started=False,
            ),
        )
        case = EvalCase(
            id="trace_case",
            prompt="test",
            fixture=self.fixture,
            required_tools=("run_command",),
            min_rejected_calls=1,
            required_command_exit_sequence=(1, 0),
            max_tool_calls=5,
        )
        outcome = TurnOutcome(RunStatus.COMPLETED, "done", 3, 3, True, tool_records=records)

        check = TraceGrader().grade(outcome, records, case, None)

        self.assertTrue(check.passed)
        self.assertEqual(check.details["command_exit_codes"], [1, 0])

    def test_safety_grader_detects_changes_outside_workspace(self) -> None:
        case = EvalCase(id="outside", prompt="test", fixture=self.fixture)

        check = SafetyGrader().grade((), case, {"secret.txt": "old"}, {"secret.txt": "new"})

        self.assertFalse(check.passed)
        self.assertEqual(check.details["outside_files_changed"], ["secret.txt"])

    def test_eval_cli_writes_report_and_returns_pass_or_fail_status(self) -> None:
        case = self.build_case()
        case_path = self.root / "case.json"
        case_path.write_text(
            json.dumps(
                {
                    "id": case.id,
                    "prompt": case.prompt,
                    "fixture": "fixture",
                }
            ),
            encoding="utf-8",
        )
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="eval-model",
            system_prompt="system",
        )
        report_path = self.root / "reports" / "result.json"
        check = CheckResult("files", True, True)
        passed_result = EvalResult("write_answer", True, (check,), {})

        with (
            patch(
                "deepseek_agent.evaluation.cli.Settings.from_env",
                return_value=settings,
            ),
            patch.object(EvalRunner, "run_cases", return_value=(passed_result,)),
            redirect_stdout(StringIO()),
        ):
            exit_code = run(
                [
                    str(case_path),
                    "--output",
                    str(report_path),
                    "--allow-unsafe-local-commands",
                ]
            )

        self.assertEqual(exit_code, 0)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertEqual(report["model"], "eval-model")
        self.assertEqual(report["command_execution_mode"], "local")
        self.assertTrue(report["generated_at"].endswith("+08:00"))

        failed_result = EvalResult(
            "write_answer",
            False,
            (CheckResult("files", False, True, {"missing": ["answer.txt"]}),),
            {},
        )
        with (
            patch(
                "deepseek_agent.evaluation.cli.Settings.from_env",
                return_value=settings,
            ),
            patch.object(EvalRunner, "run_cases", return_value=(failed_result,)),
            redirect_stdout(StringIO()),
        ):
            exit_code = run(
                [
                    str(case_path),
                    "--output",
                    str(report_path),
                    "--keep-failed-workspaces",
                    "--allow-unsafe-local-commands",
                ]
            )
        self.assertEqual(exit_code, 1)

    def test_eval_cli_aggregates_repeated_attempts_and_cost(self) -> None:
        case_path = self.root / "case.json"
        case_path.write_text(
            json.dumps({"id": "sample", "prompt": "test", "fixture": "fixture"}),
            encoding="utf-8",
        )
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="eval-model",
            system_prompt="system",
        )
        check = CheckResult("safety", True, True)
        results = (
            EvalResult(
                "sample",
                True,
                (check,),
                {"duration_seconds": 1, "input_tokens": 100, "output_tokens": 20},
                attempt=1,
            ),
            EvalResult(
                "sample",
                False,
                (check,),
                {"duration_seconds": 3, "input_tokens": 200, "output_tokens": 40},
                attempt=2,
            ),
        )
        report_path = self.root / "repeat.json"
        with (
            patch("deepseek_agent.evaluation.cli.Settings.from_env", return_value=settings),
            patch.object(EvalRunner, "run_cases", return_value=results) as run_cases,
            redirect_stdout(StringIO()),
        ):
            exit_code = run(
                [
                    str(case_path), "--output", str(report_path), "--repeat", "2",
                    "--input-price-per-million", "1", "--output-price-per-million", "2",
                    "--allow-unsafe-local-commands",
                ]
            )

        self.assertEqual(exit_code, 1)
        run_cases.assert_called_once_with(unittest.mock.ANY, repeat=2)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        aggregate = report["case_aggregates"][0]
        self.assertEqual(aggregate["success_rate"], 0.5)
        self.assertEqual(aggregate["median_duration_seconds"], 2.0)
        self.assertEqual(aggregate["estimated_cost"], 0.00042)
        self.assertEqual(report["summary"]["safety_rate"], 1.0)
        self.assertEqual(report["summary"]["total_tokens"], 360)
        self.assertEqual(report["summary"]["estimated_cost"], 0.00042)

    def test_eval_cli_fails_closed_when_default_isolation_is_unavailable(self) -> None:
        case_path = self.root / "case.json"
        case_path.write_text(
            json.dumps({"id": "sample", "prompt": "test", "fixture": "fixture"}),
            encoding="utf-8",
        )
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="eval-model",
            system_prompt="system",
        )
        executor = Mock()
        executor.preflight.side_effect = ToolExecutionError("Docker unavailable")
        error_output = StringIO()
        with (
            patch("deepseek_agent.evaluation.cli.Settings.from_env", return_value=settings),
            patch(
                "deepseek_agent.evaluation.cli.build_command_executor",
                return_value=executor,
            ) as build_executor,
            redirect_stderr(error_output),
        ):
            exit_code = run([str(case_path)])

        self.assertEqual(exit_code, 2)
        self.assertIn("评测隔离不可用", error_output.getvalue())
        build_executor.assert_called_once_with("docker", "python:3.10-slim")

    def test_eval_cli_reports_invalid_case_path(self) -> None:
        error_output = StringIO()
        with redirect_stderr(error_output):
            exit_code = run([str(self.root / "missing")])

        self.assertEqual(exit_code, 2)
        self.assertIn("评测配置错误", error_output.getvalue())

    def test_default_report_filename_uses_china_timezone_offset(self) -> None:
        self.assertRegex(
            _default_output_path().name,
            r"^eval-\d{8}T\d{6}\+0800\.json$",
        )

    def test_cli_agent_factory_uses_isolated_workspace_and_state(self) -> None:
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="eval-model",
            system_prompt="system",
        )
        state = self.root / "state"
        state.mkdir()

        agent = _agent_factory(settings)(self.fixture, state, self.build_case())

        self.assertIn("write_text_file", agent.tool_names)
        self.assertTrue((state / "sessions").is_dir())

    @staticmethod
    def _record(
        call_id: str,
        tool_name: str,
        model_result: str,
        *,
        status: ToolExecutionStatus = ToolExecutionStatus.SUCCEEDED,
        effect: ToolEffect = ToolEffect.READ_ONLY,
        execution_started: bool = True,
    ) -> ToolExecutionRecord:
        now = datetime.now(timezone.utc)
        return ToolExecutionRecord(
            call_id=call_id,
            tool_name=tool_name,
            raw_arguments="{}",
            arguments={},
            status=status,
            effect=effect,
            model_result=model_result,
            started_at=now,
            finished_at=now,
            confirmation_requested=effect is not ToolEffect.READ_ONLY,
            confirmation_granted=(status is not ToolExecutionStatus.REJECTED),
            retryable=False,
            idempotent=False,
            supports_rollback=False,
            timeout_seconds=None,
            execution_started=execution_started,
        )


if __name__ == "__main__":
    unittest.main()
