"""记录模型请求、重试和耗时等进程内运行指标。"""

from dataclasses import dataclass


@dataclass(slots=True)
class RuntimeMetrics:
    """累计 Agent 当前进程内的模型请求指标。"""

    model_requests: int = 0
    api_attempts: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    retries: int = 0
    total_duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_api_reports: int = 0
    usage_estimated_reports: int = 0

    def start_request(self) -> None:
        self.model_requests += 1

    def record_attempt(self) -> None:
        self.api_attempts += 1

    def record_retry(self) -> None:
        self.retries += 1

    def record_success(self, duration_seconds: float) -> None:
        self.successful_requests += 1
        self.total_duration_seconds += duration_seconds

    def record_failure(self, duration_seconds: float) -> None:
        self.failed_requests += 1
        self.total_duration_seconds += duration_seconds

    def record_token_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        exact: bool,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if exact:
            self.usage_api_reports += 1
        else:
            self.usage_estimated_reports += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def token_source(self) -> str:
        if self.usage_api_reports and self.usage_estimated_reports:
            return "mixed"
        if self.usage_api_reports:
            return "api"
        if self.usage_estimated_reports:
            return "estimated"
        return "unavailable"

    @property
    def average_duration_seconds(self) -> float:
        completed = self.successful_requests + self.failed_requests
        if completed == 0:
            return 0.0
        return self.total_duration_seconds / completed
