"""通过 Tavily Extract 按需读取公开网页的相关正文。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .base import (
    JsonObject,
    Tool,
    ToolEffect,
    ToolExecutionContext,
    ToolExecutionError,
)
from .web_sources import (
    PublicWebUrlError,
    normalize_public_web_url,
    source_id_for_url,
)


TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
DEFAULT_WEB_PAGE_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_PAGES_PER_CALL = 4
DEFAULT_AUTO_PAGES_PER_TURN = 4
DEFAULT_CHUNKS_PER_SOURCE = 3
DEFAULT_MAX_CONTENT_CHARS = 6_000
MAX_PAGES_PER_CALL = 4
MAX_AUTO_PAGES_PER_TURN = 12
MAX_CHUNKS_PER_SOURCE = 5
MAX_CONTENT_CHARS = 20_000
MAX_QUESTION_LENGTH = 400
MAX_ERROR_LENGTH = 500
MAX_PROVIDER_ID_LENGTH = 200
MAX_RESPONSE_BYTES = 2_000_000
SUPPORTED_EXTRACT_DEPTHS = frozenset({"basic", "advanced"})
SECRET_VALUE_PATTERN = re.compile(
    r"(?:\b(?:sk|tvly)-[A-Za-z0-9_-]{12,}\b|"
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----)",
    re.IGNORECASE,
)
CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|password|passwd|secret)"
    r"\s*[:=]\s*([^\s]+)",
    re.IGNORECASE,
)
PLACEHOLDER_MARKERS = (
    "<",
    "example",
    "placeholder",
    "replace",
    "sample",
    "your_",
    "your-",
)
ExtractTransport = Callable[[bytes, Mapping[str, str], float], bytes]


@dataclass(frozen=True, slots=True)
class WebPageArguments:
    """保存完成校验的一次网页读取请求。"""

    urls: tuple[str, ...]
    question: str


def _default_transport(
    body: bytes,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> bytes:
    """只向固定 Tavily Extract 端点发送受大小限制的 HTTPS 请求。"""
    request = Request(
        TAVILY_EXTRACT_URL,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ToolExecutionError("网页读取响应过大，已拒绝处理。")
    return payload


class TavilyExtractClient:
    """封装 Tavily Extract 鉴权、请求协议和稳定错误映射。"""

    def __init__(
        self,
        api_key: str,
        *,
        transport: ExtractTransport | None = None,
    ) -> None:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("Tavily API Key 不能为空。")
        self._api_key = normalized_key
        self._transport = transport or _default_transport

    def extract(
        self,
        urls: Sequence[str],
        *,
        question: str,
        chunks_per_source: int,
        extract_depth: str,
        timeout_seconds: float,
    ) -> JsonObject:
        request_payload: JsonObject = {
            "urls": list(urls),
            "query": question,
            "chunks_per_source": chunks_per_source,
            "extract_depth": extract_depth,
            "include_images": False,
            "include_favicon": False,
            "format": "markdown",
            "timeout": max(1.0, min(60.0, timeout_seconds)),
        }
        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "deepseek-agent/0.1",
        }
        try:
            raw_response = self._transport(body, headers, timeout_seconds)
        except HTTPError as error:
            if error.code in {401, 403}:
                message = "Tavily 认证失败，请检查 TAVILY_API_KEY。"
            elif error.code == 429:
                message = "Tavily 请求过于频繁或额度不足，请稍后重试。"
            else:
                message = f"Tavily 网页读取服务返回 HTTP {error.code}。"
            raise ToolExecutionError(message) from error
        except (TimeoutError, URLError, OSError) as error:
            raise ToolExecutionError("无法连接 Tavily 网页读取服务或请求超时。") from error

        try:
            decoded = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ToolExecutionError("Tavily 返回了无法解析的网页读取响应。") from error
        if not isinstance(decoded, dict):
            raise ToolExecutionError("Tavily 返回的网页读取响应格式无效。")
        return decoded


class ReadWebPageTool(Tool):
    """按具体问题读取少量公开网页，用于核对搜索摘要和重要结论。"""

    name = "read_web_page"
    requires_confirmation = True
    effect = ToolEffect.EXTERNAL_SIDE_EFFECT
    retryable = True
    idempotent = False
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PAGES_PER_CALL,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2_048,
                },
                "description": (
                    "需要读取的 1 到 4 个直接来源 URL。优先选择官方一手来源和"
                    "相互独立的高质量来源，不要传搜索引擎跳转链接。"
                ),
            },
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUESTION_LENGTH,
                "description": (
                    "希望从这些页面核对的具体问题或结论；工具只提取与该问题最"
                    "相关的正文片段。不得包含密钥、密码、Token 或隐私数据。"
                ),
            },
        },
        "required": ["urls", "question"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_WEB_PAGE_TIMEOUT_SECONDS,
        max_pages_per_call: int = DEFAULT_MAX_PAGES_PER_CALL,
        auto_pages_per_turn: int = DEFAULT_AUTO_PAGES_PER_TURN,
        chunks_per_source: int = DEFAULT_CHUNKS_PER_SOURCE,
        extract_depth: str = "basic",
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        excluded_domains: Sequence[str] = (),
        client: TavilyExtractClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0。")
        if (
            not isinstance(max_pages_per_call, int)
            or isinstance(max_pages_per_call, bool)
            or not 1 <= max_pages_per_call <= MAX_PAGES_PER_CALL
        ):
            raise ValueError(
                f"max_pages_per_call 必须在 1 到 {MAX_PAGES_PER_CALL} 之间。"
            )
        if (
            not isinstance(auto_pages_per_turn, int)
            or isinstance(auto_pages_per_turn, bool)
            or not 0 <= auto_pages_per_turn <= MAX_AUTO_PAGES_PER_TURN
        ):
            raise ValueError(
                "auto_pages_per_turn 必须在 0 到 "
                f"{MAX_AUTO_PAGES_PER_TURN} 之间。"
            )
        if (
            not isinstance(chunks_per_source, int)
            or isinstance(chunks_per_source, bool)
            or not 1 <= chunks_per_source <= MAX_CHUNKS_PER_SOURCE
        ):
            raise ValueError(
                f"chunks_per_source 必须在 1 到 {MAX_CHUNKS_PER_SOURCE} 之间。"
            )
        if extract_depth not in SUPPORTED_EXTRACT_DEPTHS:
            raise ValueError("extract_depth 只能是 basic 或 advanced。")
        if (
            not isinstance(max_content_chars, int)
            or isinstance(max_content_chars, bool)
            or not 1 <= max_content_chars <= MAX_CONTENT_CHARS
        ):
            raise ValueError(
                f"max_content_chars 必须在 1 到 {MAX_CONTENT_CHARS} 之间。"
            )
        self.timeout_seconds = timeout_seconds
        self._max_pages_per_call = max_pages_per_call
        self._auto_pages_per_turn = auto_pages_per_turn
        self._chunks_per_source = chunks_per_source
        self._extract_depth = extract_depth
        self._max_content_chars = max_content_chars
        self._excluded_domains = tuple(
            domain.casefold().removeprefix("www.") for domain in excluded_domains
        )
        self._client = client or TavilyExtractClient(api_key)
        self.parameters = deepcopy(type(self).parameters)
        url_schema = self.parameters["properties"]["urls"]
        url_schema["maxItems"] = self._max_pages_per_call
        url_schema["description"] = (
            "需要读取的 1 到 "
            f"{self._max_pages_per_call} 个直接来源 URL。优先选择官方一手来源"
            "和相互独立的高质量来源，不要传搜索引擎跳转链接。"
        )
        self.description = (
            "读取并核对 1 到 "
            f"{self._max_pages_per_call} 个公开网页中与具体问题相关的正文片段。"
            "搜索摘要只能用于发现来源；回答重要事实前，应优先用本工具读取官方"
            "一手来源和至少两个相互独立的高质量来源。读取成功只表示已取得页面"
            "内容，不自动证明内容真实。返回的 source_id 可与搜索结果对应，"
            "verified_source_ids 只包含成功取得正文的来源。"
        )
        self.confirmation_description = (
            "将把下方 URL 和核对问题发送给第三方 Tavily 读取网页正文；"
            "请确认其中不含带访问令牌的私有链接、隐私或敏感信息。"
        )
        self._turn_page_count = 0
        self._turn_urls: set[str] = set()
        self._confirmation_reason: str | None = None

    def begin_turn(self) -> None:
        self._turn_page_count = 0
        self._turn_urls.clear()
        self._confirmation_reason = None

    def requires_confirmation_for(self, arguments: JsonObject) -> bool:
        """自动读取有限页面；重复 URL 或超过页面额度时请求确认。"""
        validated = self._validate_arguments(arguments)
        duplicate_urls = [url for url in validated.urls if url in self._turn_urls]
        self._turn_page_count += len(validated.urls)
        self._turn_urls.update(validated.urls)
        reasons: list[str] = []
        if duplicate_urls:
            reasons.append("包含本轮已经读取过的 URL")
        if self._turn_page_count > self._auto_pages_per_turn:
            reasons.append(
                "超过本轮自动读取页面数"
                f"（{self._auto_pages_per_turn} 页）"
            )
        self._confirmation_reason = "；".join(reasons) or None
        return bool(reasons)

    def confirmation_arguments_for(
        self,
        arguments: JsonObject,
        raw_arguments: str,
    ) -> str:
        del raw_arguments
        payload = dict(arguments)
        if self._confirmation_reason is not None:
            payload["confirmation_reason"] = self._confirmation_reason
        return json.dumps(payload, ensure_ascii=False)

    def execute(self, arguments: JsonObject) -> str:
        return self.execute_with_context(arguments, ToolExecutionContext())

    def execute_with_context(
        self,
        arguments: JsonObject,
        context: ToolExecutionContext,
    ) -> str:
        validated = self._validate_arguments(arguments)
        self._raise_if_cancelled(context)
        timeout_seconds = self._effective_timeout(context)
        response = self._client.extract(
            validated.urls,
            question=validated.question,
            chunks_per_source=self._chunks_per_source,
            extract_depth=self._extract_depth,
            timeout_seconds=timeout_seconds,
        )
        self._raise_if_cancelled(context)
        payload = self._normalize_response(
            response,
            urls=validated.urls,
            question=validated.question,
        )
        return json.dumps(payload, ensure_ascii=False)

    def _validate_arguments(self, arguments: JsonObject) -> WebPageArguments:
        raw_urls = arguments.get("urls")
        if not isinstance(raw_urls, list):
            raise ToolExecutionError("read_web_page 的 urls 必须是 URL 数组。")
        if not 1 <= len(raw_urls) <= self._max_pages_per_call:
            raise ToolExecutionError(
                "read_web_page 每次必须读取 1 到 "
                f"{self._max_pages_per_call} 个 URL。"
            )
        urls: list[str] = []
        seen: set[str] = set()
        for raw_url in raw_urls:
            url = self._validate_url(raw_url)
            if url in seen:
                raise ToolExecutionError("read_web_page 的 urls 不能包含重复 URL。")
            urls.append(url)
            seen.add(url)

        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ToolExecutionError("read_web_page 的 question 必须是非空字符串。")
        normalized_question = question.strip()
        if len(normalized_question) > MAX_QUESTION_LENGTH:
            raise ToolExecutionError(
                "read_web_page 的 question 不能超过 "
                f"{MAX_QUESTION_LENGTH} 个字符。"
            )
        if self._contains_sensitive_text(normalized_question):
            raise ToolExecutionError(
                "read_web_page 的 question 疑似包含密钥或私钥，已拒绝联网发送。"
            )
        return WebPageArguments(tuple(urls), normalized_question)

    def _validate_url(self, value: object) -> str:
        try:
            return normalize_public_web_url(
                value,
                excluded_domains=self._excluded_domains,
            )
        except PublicWebUrlError as error:
            messages = {
                "empty_url": "read_web_page 的每个 URL 都必须是非空字符串。",
                "url_too_long": "read_web_page 的 URL 不能超过 2048 个字符。",
                "credential_url": "URL 疑似包含访问凭据，已拒绝发送。",
                "nonstandard_port": "read_web_page 不接受非标准网络端口。",
                "non_public_host": "read_web_page 不接受本机、内网或保留地址。",
                "excluded_domain": "read_web_page 拒绝读取用户配置中排除的域名。",
                "search_redirect": (
                    "read_web_page 不接受搜索引擎跳转链接，请使用最终来源 URL。"
                ),
            }
            raise ToolExecutionError(
                messages.get(
                    error.reason,
                    "read_web_page 只接受有效、完整的 HTTP 或 HTTPS URL。",
                )
            ) from error

    @staticmethod
    def _contains_sensitive_text(value: str) -> bool:
        if SECRET_VALUE_PATTERN.search(value):
            return True
        assignment = CREDENTIAL_ASSIGNMENT_PATTERN.search(value)
        if assignment is None:
            return False
        secret = assignment.group(1).strip("\"'`.,;()[]{}")
        normalized = secret.casefold()
        return len(secret) >= 12 and not any(
            marker in normalized for marker in PLACEHOLDER_MARKERS
        )

    def _normalize_response(
        self,
        response: JsonObject,
        *,
        urls: tuple[str, ...],
        question: str,
    ) -> JsonObject:
        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise ToolExecutionError("Tavily 网页读取响应缺少有效的 results。")
        pages: list[JsonObject] = []
        used_requested_urls: set[str] = set()
        integrity_failures: list[JsonObject] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            raw_url = item.get("url")
            raw_content = item.get("raw_content")
            if not isinstance(raw_url, str) or not isinstance(raw_content, str):
                continue
            try:
                resolved_url = self._validate_url(raw_url)
            except ToolExecutionError:
                continue
            requested_url = self._match_requested_url(
                resolved_url,
                urls,
                used_requested_urls,
            )
            if requested_url is None:
                integrity_failures.append(
                    {
                        "url": "",
                        "error": "网页读取服务返回了不属于请求域名的结果，已忽略。",
                    }
                )
                continue
            content = raw_content.strip()
            if not content:
                continue
            truncated = len(content) > self._max_content_chars
            pages.append(
                {
                    "source_id": source_id_for_url(requested_url),
                    "requested_url": requested_url,
                    "url": resolved_url,
                    "redirected": requested_url != resolved_url,
                    "content": content[: self._max_content_chars],
                    "content_chars": min(len(content), self._max_content_chars),
                    "content_truncated": truncated,
                    "content_status": "extracted",
                }
            )
            used_requested_urls.add(requested_url)
            if len(pages) >= len(urls):
                break

        failures: list[JsonObject] = integrity_failures
        raw_failures = response.get("failed_results", [])
        if isinstance(raw_failures, list):
            for item in raw_failures:
                if not isinstance(item, dict):
                    continue
                failed_url = item.get("url")
                error = item.get("error")
                try:
                    normalized_failed_url = self._validate_url(failed_url)
                except ToolExecutionError:
                    failures.append(
                        {
                            "url": "",
                            "error": (
                                "网页读取服务返回了无效或未请求的失败 URL，已忽略。"
                            ),
                        }
                    )
                    continue
                requested_url = self._match_requested_url(
                    normalized_failed_url,
                    urls,
                    used_requested_urls,
                )
                if requested_url is None:
                    failures.append(
                        {
                            "url": "",
                            "error": (
                                "网页读取服务返回了不属于请求来源的失败结果，已忽略。"
                            ),
                        }
                    )
                    continue
                failures.append(
                    {
                        "source_id": source_id_for_url(requested_url),
                        "url": requested_url,
                        "error": self._safe_provider_error(error),
                    }
                )
        reported_failure_urls = {
            item["url"] for item in failures if isinstance(item.get("url"), str)
        }
        for requested_url in urls:
            if (
                requested_url not in used_requested_urls
                and requested_url not in reported_failure_urls
            ):
                failures.append(
                    {
                        "url": requested_url,
                        "error": "网页读取服务未返回该 URL 的可读正文。",
                    }
                )
        if not pages:
            details = failures[0]["error"] if failures else "未返回可读正文。"
            raise ToolExecutionError(f"所有网页均读取失败：{details}")

        payload: JsonObject = {
            "provider": "tavily",
            "question": question,
            "requested_urls": list(urls),
            "requested_sources": [
                {"source_id": source_id_for_url(url), "url": url}
                for url in urls
            ],
            "provider_request_count": 1,
            "requested_page_count": len(urls),
            "page_count": len(pages),
            "pages": pages,
            "failures": failures,
            "partial_failure": bool(failures) or len(pages) < len(urls),
            "verified_source_ids": [page["source_id"] for page in pages],
            "failed_source_ids": [
                failure["source_id"]
                for failure in failures
                if isinstance(failure.get("source_id"), str)
            ],
            "verification_notice": (
                "已提取相关页面正文；这不自动证明内容真实，重要结论仍需比较"
                "至少两个相互独立的高质量来源。"
            ),
        }
        response_time = response.get("response_time")
        if isinstance(response_time, (int, float)) and not isinstance(
            response_time, bool
        ) and math.isfinite(float(response_time)):
            payload["response_time"] = float(response_time)
        request_id = response.get("request_id")
        if isinstance(request_id, str) and request_id.strip():
            payload["request_id"] = request_id.strip()[:MAX_PROVIDER_ID_LENGTH]
        return payload

    @classmethod
    def _safe_provider_error(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            return "网页内容提取失败。"
        normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).strip()
        if cls._contains_sensitive_text(normalized):
            return "网页内容提取失败（服务返回的错误信息已隐藏）。"
        return normalized[:MAX_ERROR_LENGTH]

    @staticmethod
    def _match_requested_url(
        resolved_url: str,
        requested_urls: tuple[str, ...],
        used_requested_urls: set[str],
    ) -> str | None:
        if resolved_url in requested_urls and resolved_url not in used_requested_urls:
            return resolved_url
        resolved_host = (
            urlsplit(resolved_url).hostname or ""
        ).casefold().removeprefix("www.")
        candidates = [
            requested_url
            for requested_url in requested_urls
            if requested_url not in used_requested_urls
            and (urlsplit(requested_url).hostname or "")
            .casefold()
            .removeprefix("www.")
            == resolved_host
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _effective_timeout(self, context: ToolExecutionContext) -> float:
        timeout_seconds = self.timeout_seconds or DEFAULT_WEB_PAGE_TIMEOUT_SECONDS
        if context.deadline is None:
            return timeout_seconds
        remaining = context.deadline - context.clock()
        if remaining <= 0:
            raise ToolExecutionError("网页读取超过执行期限。")
        return min(timeout_seconds, remaining)

    @staticmethod
    def _raise_if_cancelled(context: ToolExecutionContext) -> None:
        if context.timed_out:
            raise ToolExecutionError("网页读取超过执行期限。")
        if context.cancellation_requested:
            raise ToolExecutionError("网页读取已取消。")


__all__ = [
    "ReadWebPageTool",
    "TavilyExtractClient",
]
