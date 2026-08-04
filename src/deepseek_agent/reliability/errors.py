from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)


class RetryableModelError(RuntimeError):
    """A transient model error used by adapters and tests."""


class ModelCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,  # 星号表示后面的参数必须使用关键字传递
        category: str,
        retryable: bool,
        partial: bool,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.partial = partial


def classify_model_error(error: Exception) -> tuple[str, bool, str]:
    if isinstance(error, RetryableModelError):
        return "temporary", True, "模型服务暂时不可用"
    if isinstance(error, APITimeoutError):
        return "timeout", True, "模型请求超时"
    if isinstance(error, RateLimitError):
        return "rate_limit", True, "模型请求受到限流"
    if isinstance(error, APIConnectionError):
        return "connection", True, "无法连接模型服务"
    if isinstance(error, InternalServerError):
        return "server", True, "模型服务器发生临时错误"
    if isinstance(error, AuthenticationError):
        return "authentication", False, "模型认证失败，请检查 API Key"
    if isinstance(error, BadRequestError):
        return "bad_request", False, "模型请求参数错误"
    if isinstance(error, APIStatusError):
        status_code = error.status_code
        retryable = status_code >= 500
        message = (
            "模型服务器发生临时错误"
            if retryable
            else f"模型请求失败，HTTP 状态码 {status_code}"
        )
        return f"http_{status_code}", retryable, message
    return type(error).__name__, False, "模型调用发生未预期错误"
