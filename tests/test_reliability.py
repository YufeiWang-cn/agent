import tempfile
import unittest
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent
from deepseek_agent.config import Settings
from deepseek_agent.memory import JsonSessionStore
from deepseek_agent.models import TextDelta
from deepseek_agent.observability import RuntimeMetrics, build_file_logger
from deepseek_agent.reliability import (
    ModelCallError,
    RetryableModelError,
    RetryPolicy,
    RetryingChatModel,
)


class SequenceModel:
    model_name = "sequence-model"

    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def stream(self, messages, tools):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for item in outcome:
            if isinstance(item, Exception):
                raise item
            yield item


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class RetryingChatModelTests(unittest.TestCase):
    def build_model(self, outcomes, max_retries=3):
        base_model = SequenceModel(outcomes)
        metrics = RuntimeMetrics()
        clock = FakeClock()
        model = RetryingChatModel(
            base_model,
            RetryPolicy(
                max_retries=max_retries,
                base_delay_seconds=1.0,
            ),
            metrics,
            sleeper=clock.sleep,
            clock=clock.now,
        )
        return model, base_model, metrics, clock

    def test_transient_failures_retry_with_exponential_backoff(self) -> None:
        model, base_model, metrics, clock = self.build_model(
            [
                RetryableModelError("temporary 1"),
                RetryableModelError("temporary 2"),
                [TextDelta("成功")],
            ]
        )

        events = list(model.stream([], []))

        self.assertEqual(events, [TextDelta("成功")])
        self.assertEqual(base_model.calls, 3)
        self.assertEqual(clock.sleeps, [1.0, 2.0])
        self.assertEqual(metrics.model_requests, 1)
        self.assertEqual(metrics.api_attempts, 3)
        self.assertEqual(metrics.retries, 2)
        self.assertEqual(metrics.successful_requests, 1)
        self.assertEqual(metrics.failed_requests, 0)

    def test_non_retryable_error_fails_immediately(self) -> None:
        model, base_model, metrics, clock = self.build_model(
            [ValueError("bad request")]
        )

        with self.assertRaises(ModelCallError) as caught:
            list(model.stream([], []))

        self.assertFalse(caught.exception.retryable)
        self.assertFalse(caught.exception.partial)
        self.assertEqual(base_model.calls, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(metrics.failed_requests, 1)

    def test_error_after_first_event_is_not_retried(self) -> None:
        model, base_model, metrics, clock = self.build_model(
            [
                [
                    TextDelta("部分内容"),
                    RetryableModelError("stream interrupted"),
                ]
            ]
        )
        events = iter(model.stream([], []))

        self.assertEqual(next(events), TextDelta("部分内容"))
        with self.assertRaises(ModelCallError) as caught:
            next(events)

        self.assertTrue(caught.exception.retryable)
        self.assertTrue(caught.exception.partial)
        self.assertEqual(base_model.calls, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(metrics.retries, 0)
        self.assertEqual(metrics.failed_requests, 1)

    def test_retryable_error_stops_after_retry_limit(self) -> None:
        model, base_model, metrics, clock = self.build_model(
            [
                RetryableModelError("temporary 1"),
                RetryableModelError("temporary 2"),
                RetryableModelError("temporary 3"),
            ],
            max_retries=2,
        )

        with self.assertRaises(ModelCallError) as caught:
            list(model.stream([], []))

        self.assertTrue(caught.exception.retryable)
        self.assertFalse(caught.exception.partial)
        self.assertEqual(base_model.calls, 3)
        self.assertEqual(clock.sleeps, [1.0, 2.0])
        self.assertEqual(metrics.retries, 2)
        self.assertEqual(metrics.failed_requests, 1)

    def test_file_log_does_not_include_message_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "agent.log"
            logger = build_file_logger(log_path)
            try:
                base_model = SequenceModel([[TextDelta("secret-model-answer")]])
                model = RetryingChatModel(
                    base_model,
                    RetryPolicy(max_retries=0),
                    RuntimeMetrics(),
                    logger=logger,
                )

                list(
                    model.stream(
                        [{"role": "user", "content": "secret-user-message"}],
                        [],
                    )
                )
                for handler in logger.handlers:
                    handler.flush()
                content = log_path.read_text(encoding="utf-8")

                self.assertIn("model_request_succeeded", content)
                self.assertNotIn("secret-user-message", content)
                self.assertNotIn("secret-model-answer", content)
            finally:
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

if __name__ == "__main__":
    unittest.main()
