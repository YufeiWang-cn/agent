import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..conversation import Message
from ..models.base import ChatModel, StreamEvent
from ..observability import RuntimeMetrics
from .errors import ModelCallError, classify_model_error


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries 不能小于 0")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds 不能小于 0")

    def delay_for_retry(self, retry_number: int) -> float:
        return self.base_delay_seconds * (2 ** (retry_number - 1))


class RetryingChatModel:
    def __init__(
        self,
        model: ChatModel,
        policy: RetryPolicy,
        metrics: RuntimeMetrics,
        logger: logging.Logger | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._model = model
        self._policy = policy
        self._metrics = metrics
        if logger is None:
            logger = logging.Logger("deepseek_agent.silent")
            logger.addHandler(logging.NullHandler())
        self._logger = logger
        self._sleeper = sleeper
        self._clock = clock

    @property
    def model_name(self) -> str:
        return self._model.model_name

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> Iterable[StreamEvent]:
        self._metrics.start_request()
        started_at = self._clock()

        for attempt_index in range(self._policy.max_retries + 1):
            self._metrics.record_attempt()
            emitted_event = False  # 表示本次尝试是否已经产生任何流式事件
            try:
                for event in self._model.stream(messages, tools):
                    emitted_event = True
                    yield event
            except Exception as error:
                category, retryable, message = classify_model_error(error)
                retries_exhausted = attempt_index >= self._policy.max_retries

                if emitted_event:
                    duration = self._clock() - started_at
                    self._metrics.record_failure(duration)
                    self._logger.error(
                        "model_request_failed model=%s category=%s "
                        "partial=true attempts=%d duration=%.3f",
                        self.model_name,
                        category,
                        attempt_index + 1,
                        duration,
                    )
                    raise ModelCallError(
                        f"{message}；流式输出已经开始，为避免重复内容未自动重试。",
                        category=category,
                        retryable=retryable,
                        partial=True,
                    ) from error

                if retryable and not retries_exhausted:
                    retry_number = attempt_index + 1
                    delay = self._policy.delay_for_retry(retry_number)
                    self._metrics.record_retry()
                    self._logger.warning(
                        "model_request_retry model=%s category=%s "
                        "retry=%d delay=%.3f",
                        self.model_name,
                        category,
                        retry_number,
                        delay,
                    )
                    self._sleeper(delay)
                    continue

                duration = self._clock() - started_at
                self._metrics.record_failure(duration)
                self._logger.error(
                    "model_request_failed model=%s category=%s "
                    "partial=false attempts=%d duration=%.3f",
                    self.model_name,
                    category,
                    attempt_index + 1,
                    duration,
                )
                suffix = "，已达到最大重试次数" if retryable else ""
                raise ModelCallError(
                    f"{message}{suffix}。",
                    category=category,
                    retryable=retryable,
                    partial=False,
                ) from error

            duration = self._clock() - started_at
            self._metrics.record_success(duration)
            self._logger.info(
                "model_request_succeeded model=%s attempts=%d duration=%.3f",
                self.model_name,
                attempt_index + 1,
                duration,
            )
            return
