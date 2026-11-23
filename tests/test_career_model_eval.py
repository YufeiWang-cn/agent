"""真实模型入口只以注入的假模型验收，禁止触达真实客户端。"""

import contextlib
import io
import json
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()


class CareerModelEntryTests(unittest.TestCase):
    def test_default_preview_does_not_load_keys_or_construct_model(self):
        from deepseek_agent.evaluation.career_model_cli import run

        output = io.StringIO()
        with (
            patch("deepseek_agent.config.Settings.from_env", side_effect=AssertionError("no keys")),
            patch("deepseek_agent.models.deepseek.OpenAI", side_effect=AssertionError("no SDK")),
            patch("socket.create_connection", side_effect=AssertionError("no network")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(run([]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["mode"], "preview")
        self.assertEqual(plan["cases"], 4)
        self.assertEqual(plan["user_turns"], 10)
        self.assertEqual(plan["effective_call_ceiling"], 60)
        self.assertEqual(plan["real_api_calls"], 0)

    def test_non_execute_command_does_not_run_injected_factory(self):
        from deepseek_agent.evaluation.career_model_cli import run

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run(["--output", str(output)], model_factory=lambda _: self.fail("executed")), 0
                )
            self.assertFalse(output.exists())


class PilotFake:
    """测试端预设动作；入口只能通过普通 ChatModel 接口调用它。"""

    model_name = "fake-career-model"

    def __init__(self, source, *, fail_at=None, reverse_imports=False):
        from deepseek_agent.evaluation.career_script import CareerScriptModel

        self.script = CareerScriptModel()
        self.source = source
        self.current_turn = 0
        self.calls = 0
        self.fail_at = fail_at
        self.reverse_imports = reverse_imports
        self.seen = []

    def stream(self, messages, tools):
        from deepseek_agent.models import UsageUpdate

        self.calls += 1
        self.seen.append(deepcopy(list(messages)))
        if self.calls == self.fail_at:
            raise RuntimeError("synthetic provider failure")
        turn = sum(m["role"] == "user" for m in messages)
        if turn != self.current_turn:
            self.current_turn = turn
            actions = deepcopy(self.source.turns[turn - 1]["script"])
            if self.reverse_imports and turn == 1 and len(actions) >= 3:
                actions[0], actions[1] = actions[1], actions[0]
            for action in actions:
                if (
                    action["tool"] == "career_memory"
                    and action["arguments"].get("action") == "save"
                ):
                    action["arguments"]["key"] = "模型选择的职责名称"
                if (
                    action["tool"] == "career_review"
                    and action["arguments"].get("action") == "save"
                ):
                    action["arguments"]["title"] = "模型选择的报告标题"
            self.script.begin(actions, self.source.turns[turn - 1]["answer"])
        yield from self.script.stream(messages, tools)
        yield UsageUpdate(100, 20, 120)


class CareerModelFlowTests(unittest.TestCase):
    def setUp(self):
        from deepseek_agent.config import PROJECT_ROOT
        from deepseek_agent.evaluation.career_cases import load_career_cases
        from deepseek_agent.evaluation.career_model_cases import DEFAULT_MANIFEST

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sources = {c.id: c for c in load_career_cases(PROJECT_ROOT / "evals/career/cases")}
        self.manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        self.output = self.root / "report.json"

    def subset(self, name):
        path = self.root / "pilot.json"
        selected = [c for c in self.manifest["cases"] if c["source_case"] == name]
        path.write_text(
            json.dumps(dict(version=1, cases=selected), ensure_ascii=False), encoding="utf-8"
        )
        return path

    def execute(self, factory, extra=()):
        from deepseek_agent.evaluation.career_model_cli import run

        with (
            patch("deepseek_agent.config.Settings.from_env", side_effect=AssertionError("no keys")),
            patch("deepseek_agent.models.deepseek.OpenAI", side_effect=AssertionError("no SDK")),
            patch("socket.create_connection", side_effect=AssertionError("no network")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = run(["--execute", "--output", str(self.output), *extra], model_factory=factory)
        return code, json.loads(self.output.read_text(encoding="utf-8"))

    def test_complete_multiturn_flow_reuses_grader_confirmer_and_isolates_repeats(self):
        models = []
        settings_seen = []
        ids = [c["source_case"] for c in self.manifest["cases"]]

        def factory(settings):
            self.assertTrue(self.output.exists())
            settings_seen.append(settings)
            model = PilotFake(self.sources[ids[len(models) // 2]], reverse_imports=True)
            models.append(model)
            return model

        code, report = self.execute(factory, ["--repeat", "2"])
        self.assertEqual(code, 0, report["failure_categories"])
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["passed"], 8)
        self.assertEqual(report["evidence_mode"], "injected-model")
        self.assertIsNone(report["real_api_calls"])
        self.assertEqual(report["provider_attempts"], sum(m.calls for m in models))
        self.assertEqual(report["human_review"]["status"], "not_run")
        self.assertEqual(report["baseline"]["status"], "not_run")
        self.assertEqual(report["not_run"], [])
        self.assertTrue(
            all(
                s.max_retries == 0
                and s.max_model_calls_per_turn == 6
                and s.max_output_tokens == 2048
                for s in settings_seen
            )
        )
        a, b = report["results"][:2]
        fact_a = a["metrics"]["observations"][0]["snapshot"]["facts"][0]["id"]
        fact_b = b["metrics"]["observations"][0]["snapshot"]["facts"][0]["id"]
        self.assertNotEqual(fact_a, fact_b)
        self.assertIn("review.md", a["metrics"]["observations"][-1]["artifacts"])
        self.assertEqual(
            a["metrics"]["observations"][-1]["snapshot"]["reviews"][0]["status"], "accepted"
        )
        self.assertTrue(all(not c["name"].endswith("tool_trace") for c in a["checks"]))
        for model in models:
            text = json.dumps(model.seen, ensure_ascii=False)
            self.assertNotIn('"$ref"', text)
            self.assertNotIn("answer_contains", text)
            self.assertNotIn('"expected"', text)

    def test_batch_budget_stops_unstarted_cases_and_preserves_progress(self):
        models = []

        def factory(settings):
            model = PilotFake(self.sources["01_resume_jd"])
            models.append(model)
            return model

        code, report = self.execute(factory, ["--max-model-calls", "1"])
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "budget_exhausted")
        self.assertEqual(len(models), 1)
        self.assertEqual(report["provider_attempts"], 1)
        self.assertEqual(report["model_budget"]["used"], 1)
        self.assertEqual(len(report["not_run"]), 3)
        result = report["results"][0]
        self.assertFalse(result["passed"])
        self.assertEqual(result["metrics"]["observations"][0]["status"], "budget_exhausted")
        self.assertEqual(len(result["metrics"]["observations"][0]["snapshot"]["documents"]), 1)

    def test_input_precheck_includes_tool_schemas_and_never_reaches_provider(self):
        model = PilotFake(self.sources["03_clarification"])
        code, report = self.execute(
            lambda _: model,
            ["--manifest", str(self.subset("03_clarification")), "--max-input-tokens", "10"],
        )
        self.assertEqual(code, 1)
        self.assertEqual(model.calls, 0)
        self.assertEqual(report["provider_attempts"], 0)
        self.assertGreater(report["results"][0]["metrics"]["input_rejections"], 0)

    def test_provider_failure_is_recorded_without_retries(self):
        model = PilotFake(self.sources["01_resume_jd"], fail_at=1)
        code, report = self.execute(
            lambda _: model, ["--manifest", str(self.subset("01_resume_jd"))]
        )
        self.assertEqual(code, 1)
        first = report["results"][0]["metrics"]["observations"][0]
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["snapshot"]["facts"], [])
        self.assertEqual(report["results"][0]["metrics"]["retries"], 0)
        self.assertGreater(report["model_budget"]["used"], 0)

    def test_checkpoint_survives_keyboard_interrupt_after_completed_turn(self):
        source = self.sources["03_clarification"]
        model = PilotFake(source)
        original_stream = model.stream

        def stream(messages, tools):
            if sum(m["role"] == "user" for m in messages) == 2:
                saved = json.loads(self.output.read_text(encoding="utf-8"))
                self.assertEqual(len(saved["in_progress"]["observations"]), 1)
                raise KeyboardInterrupt()
            yield from original_stream(messages, tools)

        model.stream = stream
        code, report = self.execute(
            lambda _: model, ["--manifest", str(self.subset("03_clarification"))]
        )
        self.assertEqual(code, 130)
        self.assertEqual(report["status"], "interrupted")
        observations = report["results"][0]["metrics"]["observations"]
        self.assertEqual(observations[0]["status"], "completed")
        self.assertEqual(observations[1]["status"], "cancelled")

    def test_success_claim_without_business_artifacts_fails(self):
        from deepseek_agent.models import TextDelta

        class EmptyModel:
            model_name = "empty"

            def stream(self, messages, tools):
                yield TextDelta("报告已接受并导出。")

        code, report = self.execute(
            lambda _: EmptyModel(), ["--manifest", str(self.subset("01_resume_jd"))]
        )
        self.assertEqual(code, 1)
        failed = report["failure_categories"]
        self.assertIn("business_state", failed)
        self.assertIn("artifacts", failed)

    def test_existing_report_is_never_overwritten(self):
        from deepseek_agent.evaluation.career_model_cli import run

        self.output.write_text("keep", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run(
                ["--execute", "--output", str(self.output)],
                model_factory=lambda _: self.fail("created"),
            )
        self.assertEqual(self.output.read_text(encoding="utf-8"), "keep")

    def test_manifest_rejects_unknown_fields_duplicates_and_script_injection(self):
        from deepseek_agent.evaluation.career_model_cases import load_model_cases
        from deepseek_agent.evaluation.models import EvalCaseError

        path = self.root / "bad.json"
        for mutation in ("unknown", "duplicate", "script", "missing_instruction", "source"):
            data = deepcopy(self.manifest)
            if mutation == "unknown":
                data["typo"] = True
            if mutation == "duplicate":
                data["cases"].append(data["cases"][0])
            if mutation == "script":
                data["cases"][0]["script"] = []
            if mutation == "missing_instruction":
                data["cases"][0]["instructions"] = []
            if mutation == "source":
                data["cases"][0]["source_case"] = "../outside"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.subTest(mutation=mutation), self.assertRaises(EvalCaseError):
                load_model_cases(path)

    def test_model_case_contains_only_materials_policy_and_private_grading(self):
        from deepseek_agent.evaluation.career_model_cases import load_model_cases

        cases = load_model_cases()
        self.assertEqual(sum(len(c.turns) for c in cases), 10)
        for case in cases:
            for turn in case.turns:
                self.assertEqual(set(turn), {"prompt", "approvals", "expected"})
                self.assertNotIn("trace", turn["expected"])
                self.assertNotIn("answer_contains", turn["expected"])

    def test_model_initialization_failure_is_saved_without_secret_message(self):
        def factory(settings):
            raise ValueError("sk-secret-do-not-log")

        code, report = self.execute(factory, ["--manifest", str(self.subset("03_clarification"))])
        self.assertEqual(code, 1)
        self.assertEqual(report["provider_attempts"], 0)
        self.assertEqual(report["results"][0]["checks"][0]["details"]["error_type"], "ValueError")
        self.assertNotIn("sk-secret-do-not-log", self.output.read_text(encoding="utf-8"))

    def test_real_execution_branch_uses_mocked_provider_and_forces_pilot_limits(self):
        from deepseek_agent.config import Settings
        from deepseek_agent.evaluation.career_model_cli import run

        model = PilotFake(self.sources["03_clarification"])
        settings = Settings(
            "fake-key", "https://example.invalid", "original", "system", max_retries=9
        )
        with (
            patch("deepseek_agent.config.Settings.from_env", return_value=settings),
            patch(
                "deepseek_agent.evaluation.career_model_cli.DeepSeekModel", return_value=model
            ) as factory,
            patch("deepseek_agent.models.deepseek.OpenAI", side_effect=AssertionError("no SDK")),
            patch("socket.create_connection", side_effect=AssertionError("no network")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = run(
                [
                    "--execute",
                    "--manifest",
                    str(self.subset("03_clarification")),
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 0)
        actual_settings = factory.call_args.args[0]
        self.assertEqual(actual_settings.max_retries, 0)
        self.assertEqual(actual_settings.max_model_calls_per_turn, 6)
        self.assertEqual(actual_settings.max_output_tokens, 2048)
        self.assertNotIn("fake-key", self.output.read_text(encoding="utf-8"))

    def test_turn_budget_stops_loop_even_with_larger_batch_budget(self):
        from deepseek_agent.models import ToolCallRequest

        class LoopModel:
            model_name = "loop"
            calls = 0

            def stream(self, messages, tools):
                self.calls += 1
                yield ToolCallRequest(str(self.calls), "career_memory", '{"action":"recall"}')

        model = LoopModel()
        code, report = self.execute(
            lambda _: model,
            ["--manifest", str(self.subset("03_clarification")), "--max-calls-per-turn", "2"],
        )
        self.assertEqual(code, 1)
        self.assertEqual(model.calls, 2)
        self.assertEqual(report["model_budget"]["used"], 2)
        self.assertEqual(report["results"][0]["metrics"]["turns"], 1)
        self.assertEqual(report["results"][0]["metrics"]["planned_turns"], 2)

    def test_final_case_budget_exhaustion_has_explicit_batch_status(self):
        code, report = self.execute(
            lambda _: PilotFake(self.sources["01_resume_jd"]),
            ["--manifest", str(self.subset("01_resume_jd")), "--max-model-calls", "1"],
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "budget_exhausted")
        self.assertEqual(report["not_run"], [])

    def test_report_write_failure_stops_before_provider_calls(self):
        from deepseek_agent.evaluation.career_model_cli import run

        models = []

        def factory(settings):
            models.append(PilotFake(self.sources["01_resume_jd"]))
            return models[-1]

        with patch(
            "deepseek_agent.evaluation.career_model_cli._checkpoint",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                run(["--execute", "--output", str(self.output)], model_factory=factory)
        self.assertEqual(models, [])
