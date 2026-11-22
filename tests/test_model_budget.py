"""模型调用预算：用假供应商复现重试及跨步骤调用超过单轮额度。"""

import json
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent
from deepseek_agent.config import Settings
from deepseek_agent.memory import JsonProjectStore, JsonSessionStore
from deepseek_agent.models import TextDelta, ToolCallRequest, UsageUpdate
from deepseek_agent.observability import RuntimeMetrics
from deepseek_agent.reliability import (
    ModelBudget,
    ModelBudgetExceeded,
    ModelCancelledError,
    ModelCallError,
    RetryableModelError,
    RetryPolicy,
    RetryingChatModel,
)
from deepseek_agent.tools import ToolRegistry
from deepseek_agent.tools.calculator import CalculatorTool
from deepseek_agent.evaluation.career_runner import CareerEvalRunner
from deepseek_agent.evaluation.career_cases import load_career_cases
from deepseek_agent.evaluation.career_cli import run as run_eval
from deepseek_agent.config import PROJECT_ROOT
from deepseek_agent.ui.chat_runner import AgentChatRunner


class FakeModel:
    model_name = "budget-probe"

    def __init__(self, mode="text"):
        self.calls = 0
        self.mode = mode

    def stream(self, messages, tools):
        self.calls += 1
        if self.mode == "fail":
            raise RetryableModelError("offline temporary failure")
        if self.mode == "partial":
            yield TextDelta("部分输出")
            raise RetryableModelError("interrupted")
        if self.mode == "usage_failure":
            yield UsageUpdate(12, 3, 15)
            raise ValueError("failed after usage")
        if self.mode == "fact":
            yield ToolCallRequest(
                "save",
                "career_memory",
                json.dumps(
                    dict(
                        action="save",
                        key="职责",
                        category="project",
                        statement="参与接口测试",
                        source_id="current_user",
                        source_quote="参与接口测试",
                    )
                ),
            )
            return
        if self.mode == "tool":
            yield ToolCallRequest(str(self.calls), "calculator", json.dumps(dict(expression="1+1")))
        else:
            yield TextDelta("完成")


class ModelBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def agent(self, model, *, budget=None, confirmer=None, **options):
        options = dict(dict(max_agent_steps=30), **options)
        return Agent(
            Settings(
                api_key="offline",
                base_url="https://example.invalid",
                model=model.model_name,
                system_prompt="system",
                workspace_root=self.root,
                **options,
            ),
            model=model,
            model_budget=budget,
            confirmer=confirmer,
            tools=ToolRegistry([CalculatorTool()]),
            session_store=JsonSessionStore(self.root / "sessions"),
            project_store=JsonProjectStore(self.root / "projects.json"),
        )

    def test_retry_attempts_stop_at_default_turn_budget(self):
        base = FakeModel("fail")
        metrics = RuntimeMetrics()
        model = RetryingChatModel(base, RetryPolicy(30, 0), metrics)
        with self.assertRaises(ModelCallError) as caught:
            list(model.stream([], []))
        self.assertEqual(base.calls, 20)
        self.assertEqual(metrics.api_attempts, 20)
        self.assertEqual(caught.exception.category, "budget_exhausted")
        self.assertFalse(caught.exception.retryable)

    def test_agent_steps_share_default_turn_budget(self):
        base = FakeModel("tool")
        agent = self.agent(base)
        outcome = agent.chat("计算")
        self.assertEqual(base.calls, 20)
        self.assertEqual(outcome.status.value, "budget_exhausted")
        self.assertFalse(outcome.succeeded)
        self.assertIn("额度", outcome.final_text)
        self.assertTrue(outcome.history_preserved)

    def test_small_shared_budget_counts_failed_attempts_and_denials(self):
        budget = ModelBudget(2)
        base = FakeModel("fail")
        metrics = RuntimeMetrics()
        model = RetryingChatModel(base, RetryPolicy(10, 0), metrics, budget=budget)
        with self.assertRaises(ModelBudgetExceeded) as caught:
            list(model.stream([{"role": "user", "content": "test input"}], []))
        self.assertEqual(caught.exception.scope, "batch")
        self.assertEqual(base.calls, 2)
        self.assertEqual(budget.snapshot()["used"], 2)
        self.assertEqual(metrics.api_attempts, 2)
        self.assertEqual(metrics.budget_rejections, 1)
        self.assertEqual(metrics.usage_unknown_attempts, 2)
        self.assertEqual(metrics.token_source, "unknown")
        self.assertGreater(metrics.input_tokens, 0)

    def test_finalization_uses_same_allowance(self):
        base = FakeModel("tool")
        agent = self.agent(base, max_agent_steps=1, max_model_calls_per_turn=1)
        outcome = agent.chat("计算")
        self.assertEqual(base.calls, 1)
        self.assertEqual(outcome.status.value, "budget_exhausted")
        self.assertEqual(outcome.steps_completed, 1)
        self.assertEqual(len(outcome.tool_records), 1)
        self.assertTrue(outcome.tool_records[0].status.succeeded)

    def test_finalization_can_use_remaining_allowance(self):
        base = FakeModel("tool")
        agent = self.agent(base, max_agent_steps=1, max_model_calls_per_turn=2)
        outcome = agent.chat("计算")
        self.assertEqual(base.calls, 2)
        self.assertEqual(outcome.status.value, "step_limit_reached")
        self.assertEqual(agent.model_budget_snapshot["turn"]["used"], 2)

    def test_new_turn_resets_only_turn_allowance_and_new_agent_shares_batch(self):
        budget = ModelBudget(3)
        base = FakeModel()
        first = self.agent(base, budget=budget, max_model_calls_per_turn=1)
        self.assertTrue(first.chat("一").succeeded)
        self.assertTrue(first.chat("二").succeeded)
        second = self.agent(base, budget=budget, max_model_calls_per_turn=1)
        self.assertTrue(second.chat("三").succeeded)
        outcome = second.chat("四")
        self.assertEqual(outcome.status.value, "budget_exhausted")
        self.assertEqual(outcome.error_message, "batch")
        self.assertEqual(base.calls, 3)
        self.assertEqual(second.model_budget_snapshot["turn"]["used"], 0)

    def test_cancel_before_attempt_does_not_consume_budget(self):
        base = FakeModel()
        budget = ModelBudget(1)
        metrics = RuntimeMetrics()
        model = RetryingChatModel(base, RetryPolicy(), metrics, budget=budget)
        with self.assertRaises(ModelCancelledError):
            list(model.stream([], [], should_cancel=lambda: True))
        self.assertEqual(base.calls, 0)
        self.assertEqual(metrics.api_attempts, 0)
        self.assertEqual(budget.snapshot()["used"], 0)

    def test_partial_failure_is_not_retried_or_refunded(self):
        base = FakeModel("partial")
        budget = ModelBudget(1)
        metrics = RuntimeMetrics()
        model = RetryingChatModel(base, RetryPolicy(3, 0), metrics, budget=budget)
        with self.assertRaises(ModelCallError) as caught:
            list(model.stream([], []))
        self.assertTrue(caught.exception.partial)
        with self.assertRaises(ModelBudgetExceeded):
            list(model.stream([], []))
        self.assertEqual(base.calls, 1)
        self.assertEqual(metrics.usage_unknown_attempts, 1)
        self.assertGreater(metrics.output_tokens, 0)

    def test_failure_with_usage_preserves_reported_tokens(self):
        metrics = RuntimeMetrics()
        model = RetryingChatModel(FakeModel("usage_failure"), RetryPolicy(0), metrics)
        with self.assertRaises(ModelCallError):
            list(model.stream([], []))
        self.assertEqual((metrics.input_tokens, metrics.output_tokens), (12, 3))
        self.assertEqual(metrics.usage_unknown_attempts, 0)
        self.assertEqual(metrics.token_source, "api")

    def test_all_levels_reserved_atomically_and_concurrent_batch_is_bounded(self):
        batch = ModelBudget(3)

        def attempt(_):
            local = ModelBudget(1, scope="turn")
            try:
                ModelBudget.consume(local, batch)
                return True
            except ModelBudgetExceeded:
                self.assertEqual(local.snapshot()["used"], 0)
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(attempt, range(30))), 3)
        self.assertEqual(batch.snapshot()["remaining"], 0)

    def test_invalid_limits_rejected_and_env_loads_limits(self):
        for invalid in (0, -1, True, 1.5, "2"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ModelBudget(invalid)
            for field in ("max_model_calls_per_turn", "max_output_tokens"):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    Settings(
                        "offline", "https://example.invalid", "test", "system", **{field: invalid}
                    )
        with (
            patch.dict(
                os.environ,
                dict(
                    DEEPSEEK_API_KEY="offline",
                    AGENT_WORKSPACE=str(self.root),
                    AGENT_MAX_MODEL_CALLS_PER_TURN="6",
                    DEEPSEEK_MAX_OUTPUT_TOKENS="2048",
                ),
                clear=True,
            ),
            patch("deepseek_agent.config.load_dotenv"),
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.max_model_calls_per_turn, 6)
        self.assertEqual(settings.max_output_tokens, 2048)

    def test_budget_stop_preserves_confirmed_fact_and_session(self):
        class Allow:
            def confirm(self, tool, arguments):
                return True

        base = FakeModel("fact")
        agent = self.agent(base, agent_mode="career", max_model_calls_per_turn=1, confirmer=Allow())
        outcome = agent.chat("参与接口测试")
        self.assertEqual(outcome.status.value, "budget_exhausted")
        self.assertEqual(base.calls, 1)
        self.assertTrue(outcome.history_preserved)
        self.assertEqual(len(agent.career_snapshot()["facts"]), 1)
        restarted = self.agent(FakeModel(), agent_mode="career")
        self.assertEqual(len(restarted.career_snapshot()["facts"]), 1)
        self.assertTrue(any(m.get("role") == "tool" for m in restarted.history()))
        self.assertTrue(any("额度" in str(m.get("content", "")) for m in restarted.history()))

    def test_tk_runner_reports_budget_stop_instead_of_done(self):
        events = queue.Queue()
        agent = self.agent(FakeModel("tool"), max_model_calls_per_turn=1)
        AgentChatRunner(agent, events, threading.Event()).run("计算")
        names = []
        while not events.empty():
            names.append(events.get_nowait()[0])
        self.assertIn("budget_exhausted", names)
        self.assertNotIn("done", names)

    def test_eval_repeats_share_batch_without_real_models(self):
        budget = ModelBudget(1)
        runner = CareerEvalRunner(model_budget=budget)
        case = load_career_cases(PROJECT_ROOT / "evals/career/cases")[0]
        with patch(
            "deepseek_agent.bootstrap.DeepSeekModel", side_effect=AssertionError("no real model")
        ):
            first = runner.run_case(case)
            second = runner.run_case(case, attempt=2)
        self.assertFalse(first.passed)
        self.assertFalse(second.passed)
        self.assertEqual(first.metrics["api_attempts"], 1)
        self.assertEqual(second.metrics["api_attempts"], 0)
        self.assertEqual(second.metrics["model_budget"]["batch"]["used"], 1)
        self.assertEqual(second.metrics["real_api_calls"], 0)

    def test_eval_cli_limit_includes_repeats_and_negative_controls(self):
        output = self.root / "report.json"
        with patch(
            "deepseek_agent.bootstrap.DeepSeekModel", side_effect=AssertionError("no real model")
        ):
            status = run_eval(
                [
                    "--max-model-calls",
                    "1",
                    "--repeat",
                    "2",
                    "--compare-controls",
                    "--output",
                    str(output),
                ]
            )
        data = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(status, 1)
        self.assertEqual(data["model_budget"]["used"], 1)
        results = data["results"] + data["negative_controls"]
        self.assertEqual(sum(r["metrics"]["api_attempts"] for r in results), 1)
        self.assertEqual(len(data["results"]), 28)
        self.assertEqual(data["controls_detected"], 0)
        self.assertEqual(data["real_api_calls"], 0)

    def test_configured_output_limit_reaches_provider_without_network(self):
        from deepseek_agent.models.deepseek import DeepSeekModel

        settings = Settings(
            "offline", "https://example.invalid", "test", "system", max_output_tokens=123
        )
        with patch("deepseek_agent.models.deepseek.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = iter([])
            model = DeepSeekModel(settings)
            list(model.stream([], []))
            self.assertEqual(client.call_args.kwargs["max_retries"], 0)
            self.assertEqual(
                client.return_value.chat.completions.create.call_args.kwargs["max_tokens"], 123
            )

    def test_unknown_usage_does_not_become_zero_cost_in_aggregate(self):
        from deepseek_agent.evaluation.cli import _case_aggregates, _report_payload
        from deepseek_agent.evaluation.models import EvalResult

        result = EvalResult(
            "unknown",
            False,
            (),
            dict(input_tokens=0, output_tokens=0, usage_unknown_attempts=1, token_source="unknown"),
        )
        aggregate = _case_aggregates((result,), 1, 2)[0]
        self.assertIsNone(aggregate["estimated_cost"])
        settings = Settings("offline", "https://example.invalid", "test", "system")
        report = _report_payload(
            settings, (result,), input_price_per_million=1, output_price_per_million=2
        )
        self.assertIsNone(report["summary"]["estimated_cost"])
