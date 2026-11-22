"""求职评测自测：坏结果必须判失败，不能仅凭模型说完成就通过。"""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.config import PROJECT_ROOT
from deepseek_agent.evaluation.career_cases import CareerCase, load_career_cases
from deepseek_agent.evaluation.career_runner import CareerEvalRunner
from deepseek_agent.evaluation.career_cli import run
from deepseek_agent.evaluation.career_graders import grade_turn
from deepseek_agent.evaluation.models import EvalCaseError


class CareerEvaluationTests(unittest.TestCase):
    def cases(self):
        return load_career_cases(PROJECT_ROOT / "evals/career/cases")

    def test_all_synthetic_cases_run_offline_through_real_agent(self):
        with (
            patch(
                "deepseek_agent.bootstrap.DeepSeekModel",
                side_effect=AssertionError("real model forbidden"),
            ),
            patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
        ):
            cases = self.cases()
            self.assertEqual(len(cases), 14)
            for case in cases:
                with self.subTest(case=case.id):
                    result = CareerEvalRunner().run_case(case)
                    self.assertTrue(
                        result.passed, [(c.name, c.details) for c in result.checks if not c.passed]
                    )
                    self.assertEqual(result.metrics["real_api_calls"], 0)
                    self.assertEqual(len(result.metrics["observations"]), len(case.turns))

    def test_negative_control_detects_missing_state_despite_successful_text_and_tools(self):
        result = CareerEvalRunner().run_case(self.cases()[0], omit_state_control=True)
        self.assertFalse(result.passed)
        self.assertTrue(
            any(c.name.endswith("business_state") and not c.passed for c in result.checks)
        )
        self.assertTrue(all(c.passed for c in result.checks if c.name.endswith("tool_trace")))

    def test_attempts_do_not_share_profile_or_bindings(self):
        case = self.cases()[0]
        first = CareerEvalRunner().run_case(case)
        second = CareerEvalRunner().run_case(case, attempt=2)
        self.assertTrue(first.passed and second.passed)
        a = first.metrics["observations"][0]["snapshot"]["facts"][0]["id"]
        b = second.metrics["observations"][0]["snapshot"]["facts"][0]["id"]
        self.assertNotEqual(a, b)
        self.assertEqual(second.attempt, 2)

    def test_parser_rejects_unknown_fields_and_unsafe_artifact_paths(self):
        raw = json.loads(
            (PROJECT_ROOT / "evals/career/cases/01_resume_jd.json").read_text(encoding="utf-8")
        )
        for path in ("../escape.md", "C:/escape.md", "C:escape.md"):
            bad = deepcopy(raw)
            bad["turns"][0]["expected"]["files"] = {path: {"exists": True}}
            with self.assertRaises(EvalCaseError):
                CareerCase.parse(bad)
        bad = deepcopy(raw)
        bad["turns"][0]["expected"]["netwrok_requests"] = 0
        with self.assertRaises(EvalCaseError):
            CareerCase.parse(bad)
        bad = deepcopy(raw)
        bad["turns"][0]["expected"]["status"] = "complete"
        with self.assertRaises(EvalCaseError):
            CareerCase.parse(bad)

    def test_grader_detects_bad_trace_privacy_unapproved_write_and_unexpected_file(self):
        case = self.cases()[2]
        expected = case.turns[0]["expected"]
        observed = dict(
            status="completed",
            answer="完成",
            error=None,
            trace=[["career_memory", "succeeded"]],
            snapshot=dict(facts=[], documents=[], reviews=[]),
            network=[{"query": "13800138000"}],
            unapproved_writes=["call"],
            decisions=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "unexpected.md").write_text("unexpected", encoding="utf-8")
            checks = grade_turn(expected, observed, root, "turn_1")
        failed = {c.name.split(".")[-1] for c in checks if not c.passed}
        self.assertTrue({"tool_trace", "privacy", "confirmation", "artifacts"} <= failed)

    def test_cli_writes_failure_report_and_returns_nonzero(self):
        case = json.loads(
            (PROJECT_ROOT / "evals/career/cases/03_clarification.json").read_text(encoding="utf-8")
        )
        case["turns"][0]["expected"]["state"][0]["count"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "bad.json"
            output = root / "report.json"
            source.write_text(json.dumps(case), encoding="utf-8")
            self.assertEqual(run(["--cases", str(source), "--output", str(output)]), 1)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("business_state", report["failure_categories"])
            self.assertEqual(report["human_review"]["status"], "not_run")
