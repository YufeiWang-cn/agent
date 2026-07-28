from dataclasses import dataclass


@dataclass(slots=True)
class RuntimeMetrics:
    model_requests: int = 0
    api_attempts: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    retries: int = 0
    total_duration_seconds: float = 0.0

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

    @property
    def average_duration_seconds(self) -> float:
        completed = self.successful_requests + self.failed_requests
        if completed == 0:
            return 0.0
        return self.total_duration_seconds / completed
