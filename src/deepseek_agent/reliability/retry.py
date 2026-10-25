"""为聊天模型增加指数退避重试、指标统计和不记录敏感正文的日志。"""

import json
import logging
import random
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..conversation import Message
from ..context.estimator import estimate_messages_tokens, estimate_text_tokens
from ..models.base import (
    ChatModel,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallRequest,
    UsageUpdate,
)
from ..observability import RuntimeMetrics
from .errors import (
    ModelCallError,
    ModelCancelledError,
    classify_model_error,
    retry_after_seconds,
)


# 取消信号的轮询间隔；退避等待期间按此粒度检查停止请求。
CANCEL_POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """定义最大重试次数和指数退避的基础等待时间。"""

    max_retries: int = 3
    base_delay_seconds: float = 1.0
    # 指数退避的延迟上限，防止最大重试次数较大时总等待时间无界增长。
    max_delay_seconds: float = 30.0
    # 0 到 0.5 之间的抖动比例；0 表示完全确定性的退避（测试依赖）。
    jitter_fraction: float = 0.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries 不能小于 0")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds 不能小于 0")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds 不能小于 0")
        if not 0 <= self.jitter_fraction <= 0.5:
            raise ValueError("jitter_fraction 必须在 0 到 0.5 之间")

    def delay_for_retry(self, retry_number: int) -> float:
        return min(
            self.base_delay_seconds * (2 ** (retry_number - 1)),
            self.max_delay_seconds,
        )


class RetryingChatModel:
    """包装模型调用，并只在流式输出开始前执行安全重试。"""

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

    @property
    def available_models(self) -> tuple[str, ...]:
        model_names = getattr(self._model, "available_models", None)
        if model_names is None:
            return (self.model_name,)
        return tuple(model_names)

    def select_model(self, model_name: str) -> None:
        selector = getattr(self._model, "select_model", None)
        if selector is None:
            if model_name == self.model_name:
                return
            raise RuntimeError("当前模型实现不支持运行时切换。")
        selector(model_name)

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterable[StreamEvent]:
        self._metrics.start_request()
        started_at = self._clock()
        estimated_input_tokens = estimate_messages_tokens(list(messages))
        if tools:
            serialized_tools = json.dumps(
                list(tools),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            estimated_input_tokens += estimate_text_tokens(serialized_tools)

        for attempt_index in range(self._policy.max_retries + 1):
            self._metrics.record_attempt()
            # 流式输出开始后不再重试，避免向用户重复发送部分内容。
            emitted_event = False
            exact_usage: UsageUpdate | None = None
            estimated_output_parts: list[str] = []
            try:
                for event in self._model.stream(messages, tools):
                    if isinstance(event, UsageUpdate):
                        exact_usage = event
                        continue
                    emitted_event = True
                    estimated_output_parts.append(self._event_token_text(event))
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
                    retry_after = retry_after_seconds(error)
                    if retry_after is not None:
                        # 限流响应的服务器指示优先，但仍受单次延迟上限约束。
                        delay = min(max(delay, retry_after), self._policy.max_delay_seconds)
                    if self._policy.jitter_fraction > 0:
                        spread = delay * self._policy.jitter_fraction
                        delay += random.uniform(-spread, spread)
                    self._metrics.record_retry()
                    self._logger.warning(
                        "model_request_retry model=%s category=%s "
                        "retry=%d delay=%.3f",
                        self.model_name,
                        category,
                        retry_number,
                        delay,
                    )
                    self._wait_before_retry(delay, should_cancel)
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
            if exact_usage is not None:
                self._metrics.record_token_usage(
                    exact_usage.input_tokens,
                    exact_usage.output_tokens,
                    exact=True,
                )
            else:
                self._metrics.record_token_usage(
                    estimated_input_tokens,
                    estimate_text_tokens("\n".join(estimated_output_parts)),
                    exact=False,
                )
            self._metrics.record_success(duration)
            self._logger.info(
                "model_request_succeeded model=%s attempts=%d duration=%.3f",
                self.model_name,
                attempt_index + 1,
                duration,
            )
            return

    def _wait_before_retry(
        self,
        delay_seconds: float,
        should_cancel: Callable[[], bool] | None,
    ) -> None:
        """等待退避时间，期间轮询取消信号以便界面停止按钮立即生效。"""
        if should_cancel is None:
            self._sleeper(delay_seconds)
            return
        remaining = delay_seconds
        while remaining > 0:
            if should_cancel():
                raise ModelCancelledError("等待重试时收到停止请求。")
            slice_seconds = min(CANCEL_POLL_INTERVAL_SECONDS, remaining)
            self._sleeper(slice_seconds)
            remaining -= slice_seconds
        if should_cancel():
            raise ModelCancelledError("等待重试时收到停止请求。")

    @staticmethod
    def _event_token_text(event: StreamEvent) -> str:
        if isinstance(event, (TextDelta, ReasoningDelta)):
            return event.content
        if isinstance(event, ToolCallRequest):
            return json.dumps(
                event.as_message_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return ""
