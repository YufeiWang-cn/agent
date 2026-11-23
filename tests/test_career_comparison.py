"""两组对照与人工评分流程；所有供应商均由假模型替代。"""

import contextlib
import io
import json
import tempfile
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()


class ComparisonEntryTests(unittest.TestCase):
    def test_compare_preview_is_zero_call_and_has_both_arms(self):
        from deepseek_agent.evaluation.career_model_cli import run

        output = io.StringIO()
        with (
            patch("deepseek_agent.config.Settings.from_env", side_effect=AssertionError("no key")),
            patch("deepseek_agent.models.deepseek.OpenAI", side_effect=AssertionError("no SDK")),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(run(["--compare"]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["arms"], ["agent", "baseline"])
        self.assertEqual(plan["attempts"], 8)
        self.assertEqual(plan["user_turns"], 20)
        self.assertEqual(plan["effective_call_ceiling"], 70)
        self.assertEqual(plan["real_api_calls"], 0)

    def test_semantic_scores_are_not_inferred_from_successful_answer(self):
        from deepseek_agent.evaluation.career_comparison import comparison_summary

        report = dict(plan=dict(tasks=[], repeat=1), results=[], not_run=[])
        summary = comparison_summary(report)
        self.assertEqual(summary["semantic_review"], "not_run")
        self.assertIsNone(summary["quality_delta"])


class DualFake:
    model_name = "fake-comparison"

    def __init__(self, source):
        from test_career_model_eval import PilotFake

        self.agent = PilotFake(source)
        self.calls = 0
        self.user_inputs = []
        self.tools_seen = []

    def stream(self, messages, tools):
        from deepseek_agent.models import TextDelta, UsageUpdate

        self.calls += 1
        self.user_inputs = [m["content"] for m in messages if m["role"] == "user"]
        self.tools_seen.append(bool(tools))
        if tools:
            yield from self.agent.stream(messages, tools)
        else:
            yield TextDelta(
                "以下仅供手工整理。请补充具体职责和验证方式；我未操作数据库或导出文件。"
            )
            yield UsageUpdate(80, 15, 95)


class ComparisonFlowTests(unittest.TestCase):
    def setUp(self):
        from deepseek_agent.config import PROJECT_ROOT
        from deepseek_agent.evaluation.career_cases import load_career_cases
        from deepseek_agent.evaluation.career_model_cases import DEFAULT_MANIFEST

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / "report.json"
        self.sources = {c.id: c for c in load_career_cases(PROJECT_ROOT / "evals/career/cases")}
        self.manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))

    def execute(self, *, single=True, extra=()):
        from deepseek_agent.evaluation.career_model_cli import run

        manifest = deepcopy(self.manifest)
        if single:
            manifest["cases"] = manifest["cases"][:1]
        path = self.root / "pilot.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        models = []

        def factory(settings):
            repeat = int(extra[extra.index("--repeat") + 1]) if "--repeat" in extra else 1
            source_id = manifest["cases"][len(models) // (2 * repeat)]["source_case"]
            model = DualFake(self.sources[source_id])
            models.append(model)
            return model

        with (
            patch("deepseek_agent.config.Settings.from_env", side_effect=AssertionError("no key")),
            patch("deepseek_agent.models.deepseek.OpenAI", side_effect=AssertionError("no SDK")),
            patch("socket.create_connection", side_effect=AssertionError("no network")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = run(
                [
                    "--execute",
                    "--compare",
                    "--manifest",
                    str(path),
                    "--output",
                    str(self.output),
                    *extra,
                ],
                model_factory=factory,
            )
        return code, json.loads(self.output.read_text(encoding="utf-8")), models

    def test_two_groups_share_inputs_limits_and_isolate_business_checks(self):
        code, report, models = self.execute(single=False)
        self.assertEqual(code, 0, report["failure_categories"])
        self.assertEqual(len(report["results"]), 8)
        self.assertEqual(report["comparison"]["eligible_pairs"], 4)
        self.assertEqual(report["human_review"]["status"], "not_run")
        self.assertIsNone(report["comparison"]["quality_delta"])
        self.assertEqual(report["provider_attempts"], sum(m.calls for m in models))
        self.assertEqual(report["model_budget"]["used"], report["provider_attempts"])
        self.assertEqual(
            [r["metrics"]["arm"] for r in report["results"]],
            ["agent", "baseline", "baseline", "agent", "agent", "baseline", "baseline", "agent"],
        )
        for index in range(0, 8, 2):
            self.assertEqual(models[index].user_inputs, models[index + 1].user_inputs)
        for row in report["results"]:
            if row["metrics"]["arm"] == "baseline":
                self.assertTrue(row["passed"])
                self.assertFalse(row["metrics"]["business_checks_applicable"])
                self.assertEqual(row["metrics"]["tool_calls"], 0)
                self.assertFalse(
                    any(
                        c["name"].endswith(("business_state", "artifacts", "confirmation"))
                        for c in row["checks"]
                    )
                )
            else:
                self.assertIn("tool_behavior", row["metrics"])
                self.assertEqual(row["metrics"]["tool_behavior"]["unapproved_writes"], 0)
        metrics = report["comparison"]["arm_metrics"]
        self.assertEqual(metrics["agent"]["pass_meaning"], "business_workflow")
        self.assertEqual(metrics["baseline"]["pass_meaning"], "response_protocol_only")

    def test_shared_budget_leaves_pair_incomplete_instead_of_zero_scoring(self):
        code, report, models = self.execute(extra=["--max-model-calls", "1"])
        self.assertEqual(code, 1)
        self.assertEqual(len(models), 1)
        self.assertEqual(report["provider_attempts"], 1)
        self.assertEqual(report["not_run"][0]["arm"], "baseline")
        self.assertEqual(report["comparison"]["eligible_pairs"], 0)
        self.assertIsNone(report["comparison"]["quality_delta"])
        from deepseek_agent.evaluation.career_comparison import make_review_sheet

        with self.assertRaises(ValueError):
            make_review_sheet(report)

    def test_batch_budget_is_not_reset_between_agent_and_baseline(self):
        code, report, models = self.execute(extra=["--max-model-calls", "10"])
        self.assertEqual(code, 1)
        self.assertEqual(len(models), 2)
        self.assertTrue(all(model.calls > 0 for model in models))
        self.assertEqual(report["provider_attempts"], 10)
        self.assertEqual(sum(model.calls for model in models), 10)
        self.assertEqual(report["model_budget"]["used"], 10)
        self.assertEqual(report["status"], "budget_exhausted")
        baseline = report["results"][1]
        self.assertEqual(baseline["metrics"]["arm"], "baseline")
        self.assertEqual(baseline["metrics"]["observations"][-1]["status"], "budget_exhausted")
        self.assertEqual(report["comparison"]["eligible_pairs"], 0)

    def test_repeat_keeps_pairs_distinct_and_excludes_interrupted_pair(self):
        from deepseek_agent.evaluation.career_comparison import comparison_summary, score_reviews

        code, report, models = self.execute(extra=["--repeat", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(len(models), 4)
        self.assertEqual(report["comparison"]["eligible_pairs"], 2)
        scored = score_reviews(report, self.filled_sheet(report))
        self.assertEqual([p["attempt"] for p in scored["pairs"]], [1, 2])
        report["results"][-1]["metrics"]["observations"][-1]["status"] = "cancelled"
        summary = comparison_summary(report)
        self.assertEqual(summary["eligible_pairs"], 1)
        self.assertEqual(summary["pairs"][1]["reasons"], ["agent:incomplete"])
        self.assertEqual(len(score_reviews(report, self.filled_sheet(report))["pairs"]), 1)

    def test_baseline_rejects_tool_calls_without_creating_agent_or_store(self):
        from deepseek_agent.evaluation.career_baseline import CareerBaselineRunner
        from deepseek_agent.evaluation.career_model_cases import load_model_cases
        from deepseek_agent.config import Settings
        from deepseek_agent.models import ToolCallRequest
        from deepseek_agent.reliability import ModelBudget

        class ToolModel:
            model_name = "bad-baseline"
            calls = 0

            def stream(self, messages, tools):
                self.calls += 1
                self.assertions = tools
                yield ToolCallRequest("x", "career_memory", '{"action":"save"}')

        model = ToolModel()
        with patch(
            "deepseek_agent.infrastructure.sqlite_career.SqliteCareerRepository",
            side_effect=AssertionError("baseline created DB"),
        ):
            result = CareerBaselineRunner(ModelBudget(5)).run_case(
                load_model_cases()[2],
                model,
                Settings("offline", "https://example.invalid", "test", "system"),
            )
        self.assertFalse(result.passed)
        self.assertEqual(model.assertions, [])
        self.assertEqual(model.calls, 2)
        self.assertTrue(
            any(c.name.endswith("no_tool_execution") and not c.passed for c in result.checks)
        )

    def filled_sheet(self, report):
        from deepseek_agent.evaluation.career_comparison import make_review_sheet, _samples

        sheet = make_review_sheet(report)
        private = _samples(report)
        for item in sheet["items"]:
            # 这是测试夹具，不是对真实模型的人工评分。
            score = 2 if private[item["sample_id"]]["arm"] == "agent" else 1
            input_quote = item["prompts"][0].split("。")[0]
            output_quote = item["outputs"][0]["answer"][:6]
            for rating in item["ratings"].values():
                rating.update(
                    score=score,
                    input_quote=input_quote,
                    output_quote=output_quote,
                    reason="合成评分夹具，用于验证映射和差值。",
                )
            item["fabricated_claim"].update(
                present=False,
                input_quote=input_quote,
                output_quote=output_quote,
                reason="合成标记。",
            )
        return sheet

    def test_blank_blind_sheet_and_paired_score_round_trip(self):
        from deepseek_agent.evaluation.career_comparison import make_review_sheet, score_reviews

        _, report, _ = self.execute()
        sheet = make_review_sheet(report)
        self.assertEqual(len(sheet["items"]), 2)
        self.assertNotIn("arm", sheet["items"][0])
        self.assertNotIn("blinding_seed", sheet)
        self.assertNotIn("model", sheet["items"][0])
        self.assertTrue(
            all(r["score"] is None for i in sheet["items"] for r in i["ratings"].values())
        )
        with self.assertRaises(ValueError):
            score_reviews(report, sheet)
        scored = score_reviews(report, self.filled_sheet(report))
        self.assertEqual(scored["status"], "complete")
        self.assertEqual(len(scored["pairs"]), 1)
        self.assertEqual(set(scored["mean_paired_delta"].values()), {1.0})
        self.assertEqual(scored["evidence_mode"], "injected-model")
        self.assertEqual(report["human_review"]["status"], "not_run")

    def test_annotations_reject_bad_scores_evidence_tampering_and_omissions(self):
        from deepseek_agent.evaluation.career_comparison import score_reviews

        _, report, _ = self.execute()
        valid = self.filled_sheet(report)
        for mutation in (
            "empty",
            "boolean",
            "range",
            "evidence",
            "reason",
            "output",
            "missing",
            "duplicate",
            "digest",
            "unknown",
        ):
            bad = deepcopy(valid)
            item = bad["items"][0]
            rating = item["ratings"]["faithfulness"]
            if mutation == "empty":
                rating["score"] = None
            if mutation == "boolean":
                rating["score"] = True
            if mutation == "range":
                rating["score"] = 3
            if mutation == "evidence":
                rating["output_quote"] = "不存在的输出证据"
            if mutation == "reason":
                rating["reason"] = ""
            if mutation == "output":
                item["outputs"][0]["answer"] = "改写结果"
            if mutation == "missing":
                bad["items"].pop()
            if mutation == "duplicate":
                bad["items"][1] = deepcopy(item)
            if mutation == "digest":
                bad["report_sha256"] = "other-report"
            if mutation == "unknown":
                item["unexpected"] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                score_reviews(report, bad)

    def test_fabricated_claim_flag_is_retained_even_when_dimension_scores_high(self):
        from deepseek_agent.evaluation.career_comparison import score_reviews

        _, report, _ = self.execute()
        sheet = self.filled_sheet(report)
        sheet["items"][0]["fabricated_claim"]["present"] = True
        scored = score_reviews(report, sheet)
        self.assertEqual(len(scored["fabricated_claims"]), 1)
        self.assertEqual(
            scored["fabricated_claims"][0]["sample_id"], sheet["items"][0]["sample_id"]
        )

    def test_empty_responses_do_not_block_review_of_complete_pairs(self):
        from deepseek_agent.evaluation.career_comparison import (
            comparison_summary,
            make_review_sheet,
        )

        _, report, _ = self.execute(extra=["--repeat", "2"])
        baseline = report["results"][1]
        baseline["passed"] = False
        baseline["metrics"]["observations"][0]["answer"] = "   "
        summary = comparison_summary(report)
        self.assertEqual(summary["eligible_pairs"], 1)
        self.assertEqual(summary["pairs"][0]["reasons"], ["baseline:incomplete"])
        self.assertEqual(len(make_review_sheet(report)["items"]), 2)

    def test_business_failure_with_complete_responses_is_not_cherry_picked_away(self):
        from deepseek_agent.evaluation.career_comparison import (
            comparison_summary,
            make_review_sheet,
        )

        _, report, _ = self.execute()
        agent = next(r for r in report["results"] if r["metrics"]["arm"] == "agent")
        agent["passed"] = False
        agent["checks"][0]["passed"] = False
        summary = comparison_summary(report)
        self.assertEqual(summary["eligible_pairs"], 1)
        self.assertEqual(len(make_review_sheet(report)["items"]), 2)
        self.assertEqual(summary["arm_metrics"]["agent"]["checks_passed"], 0)

    def test_review_cli_writes_separate_files_without_models_or_overwrite(self):
        from deepseek_agent.evaluation.career_comparison import run

        _, report, _ = self.execute()
        sheet_path = self.root / "sheet.json"
        score_path = self.root / "scores.json"
        with (
            patch("deepseek_agent.models.deepseek.OpenAI", side_effect=AssertionError("no SDK")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(run(["--report", str(self.output), "--output", str(sheet_path)]), 0)
            sheet_path.write_text(
                json.dumps(self.filled_sheet(report), ensure_ascii=False), encoding="utf-8"
            )
            self.assertEqual(
                run(
                    [
                        "--report",
                        str(self.output),
                        "--annotations",
                        str(sheet_path),
                        "--output",
                        str(score_path),
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(score_path.read_text(encoding="utf-8"))["status"], "complete")
        original = score_path.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run(["--report", str(self.output), "--output", str(score_path)])
        self.assertEqual(score_path.read_bytes(), original)
