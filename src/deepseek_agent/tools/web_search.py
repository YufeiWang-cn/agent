"""通过 Tavily Search API 提供受限、结构化的互联网搜索。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..timekeeping import now_china
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


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
DEFAULT_SEARCH_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RESULTS = 5
DEFAULT_MINIMUM_SCORE = 0.25
MAX_RESULTS = 10
MAX_AUTO_CALLS_PER_TURN = 5
MAX_QUERY_LENGTH = 400
MAX_CONFIGURED_DOMAINS = 50
MAX_RESULT_TITLE_LENGTH = 300
MAX_RESULT_CONTENT_LENGTH = 1_500
MAX_PUBLISHED_DATE_LENGTH = 100
MAX_PROVIDER_ID_LENGTH = 200
MAX_RESPONSE_BYTES = 1_000_000
WEAK_MATCH_SCORE_THRESHOLD = 0.5
SUPPORTED_TOPICS = frozenset({"general", "news", "finance"})
SUPPORTED_TIME_RANGES = frozenset({"day", "week", "month", "year"})
SUPPORTED_SEARCH_SCOPES = frozenset(
    {"balanced", "domestic", "international", "unrestricted"}
)
YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?:年)?(?!\d)")
FRESHNESS_PATTERN = re.compile(
    r"最新(?:消息|新闻|资讯|进展|动态|发布)?|本月|本周|今天|今日|近期|当前|刚刚|"
    r"\b(?:latest|recent|current|today|this\s+(?:month|week))\b",
    re.IGNORECASE,
)
SECRET_PREFIX_PATTERN = re.compile(
    r"\b(?:sk|tvly)-[A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)
BEARER_TOKEN_PATTERN = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}",
    re.IGNORECASE,
)
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    re.IGNORECASE,
)
CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|password|passwd|secret)"
    r"\s*[:=]\s*([^\s]+)",
    re.IGNORECASE,
)
DOMAIN_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
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
LATIN_QUERY_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9.+#-]{2,}", re.IGNORECASE)
CJK_QUERY_RUN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")
ENGLISH_QUERY_STOP_WORDS = frozenset(
    {
        "about",
        "best",
        "current",
        "find",
        "latest",
        "month",
        "news",
        "recent",
        "recommend",
        "recommendation",
        "search",
        "this",
        "today",
        "update",
        "what",
        "when",
        "where",
        "which",
    }
)
CJK_QUERY_STOP_BIGRAMS = frozenset(
    {
        "今天",
        "今日",
        "什么",
        "介绍",
        "值得",
        "信息",
        "关于",
        "动态",
        "如何",
        "推荐",
        "搜索",
        "新闻",
        "最新",
        "本周",
        "本月",
        "消息",
        "现在",
        "目前",
        "近期",
    }
)

SearchTransport = Callable[[bytes, Mapping[str, str], float], bytes]


@dataclass(frozen=True, slots=True)
class SearchArguments:
    """保存完成校验的一次工具调用参数。"""

    query: str
    international_query: str | None
    max_results: int
    topic: str
    time_range: str | None
    scope: str

    @property
    def provider_request_count(self) -> int:
        return 2 if self.scope == "balanced" else 1


def _default_transport(
    body: bytes,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> bytes:
    """向固定 Tavily 端点发送一次受大小限制的 HTTPS 请求。"""
    request = Request(
        TAVILY_SEARCH_URL,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ToolExecutionError("联网搜索响应过大，已拒绝处理。")
    return payload


class TavilySearchClient:
    """封装 Tavily 鉴权、请求协议和稳定的错误映射。"""

    def __init__(
        self,
        api_key: str,
        *,
        transport: SearchTransport | None = None,
    ) -> None:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("Tavily API Key 不能为空。")
        self._api_key = normalized_key
        self._transport = transport or _default_transport

    def search(
        self,
        query: str,
        *,
        max_results: int,
        topic: str,
        time_range: str | None,
        timeout_seconds: float,
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        country: str | None = None,
    ) -> JsonObject:
        request_payload: JsonObject = {
            "query": query,
            "topic": topic,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if time_range is not None:
            request_payload["time_range"] = time_range
        if include_domains:
            request_payload["include_domains"] = list(include_domains)
        if exclude_domains:
            request_payload["exclude_domains"] = list(exclude_domains)
        if country is not None:
            request_payload["country"] = country
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
                message = f"Tavily 搜索服务返回 HTTP {error.code}。"
            raise ToolExecutionError(message) from error
        except (TimeoutError, URLError, OSError) as error:
            raise ToolExecutionError("无法连接 Tavily 搜索服务或请求超时。") from error

        try:
            decoded = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ToolExecutionError("Tavily 返回了无法解析的响应。") from error
        if not isinstance(decoded, dict):
            raise ToolExecutionError("Tavily 返回的响应格式无效。")
        return decoded


class WebSearchTool(Tool):
    """搜索公开互联网信息，并返回带来源链接的有限摘要。"""

    name = "web_search"
    requires_confirmation = True
    effect = ToolEffect.EXTERNAL_SIDE_EFFECT
    description = "搜索公开互联网中的当前信息。"
    retryable = True
    # 重试会再次消耗第三方 API 请求额度，因此不能标记为可安全自动重试。
    idempotent = False
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_LENGTH,
                "description": (
                    "搜索词。balanced 时填写面向国内来源的中文关键词；"
                    "其他范围填写当前范围使用的关键词。不得包含密钥或隐私数据。"
                ),
            },
            "international_query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_LENGTH,
                "description": (
                    "仅 balanced 必填：面向国际来源、表达同一信息目标的英文关键词。"
                ),
            },
            "scope": {
                "type": "string",
                "enum": [
                    "balanced",
                    "domestic",
                    "international",
                    "unrestricted",
                ],
                "description": (
                    "检索来源范围。balanced 会并行检索国内和国际来源；"
                    "domestic 使用中文关键词检索国内来源；international 使用英文"
                    "关键词检索国际来源；unrestricted 不限定来源范围。"
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULTS,
                "description": (
                    "仅 unrestricted 可选，最多 10；其他范围必须省略并使用用户配置。"
                ),
            },
            "topic": {
                "type": "string",
                "enum": ["general", "news", "finance"],
                "description": "搜索类别，默认为 general。",
            },
            "time_range": {
                "type": "string",
                "enum": ["day", "week", "month", "year"],
                "description": "可选的发布时间范围。",
            },
        },
        "required": ["query", "scope"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
        default_max_results: int = DEFAULT_MAX_RESULTS,
        minimum_score: float = DEFAULT_MINIMUM_SCORE,
        auto_calls_per_turn: int = 2,
        default_scope: str = "balanced",
        domestic_results: int = 3,
        international_results: int = 3,
        domestic_domains: Sequence[str] = (),
        international_domains: Sequence[str] = (),
        excluded_domains: Sequence[str] = (),
        now_provider: Callable[[], datetime] = now_china,
        client: TavilySearchClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0。")
        if not 1 <= default_max_results <= MAX_RESULTS:
            raise ValueError(f"default_max_results 必须在 1 到 {MAX_RESULTS} 之间。")
        if (
            not isinstance(minimum_score, (int, float))
            or isinstance(minimum_score, bool)
            or not math.isfinite(float(minimum_score))
            or not 0 <= float(minimum_score) <= 1
        ):
            raise ValueError("minimum_score 必须是 0 到 1 之间的有限数字。")
        if default_scope not in SUPPORTED_SEARCH_SCOPES:
            choices = "、".join(sorted(SUPPORTED_SEARCH_SCOPES))
            raise ValueError(f"default_scope 只能是：{choices}。")
        for name, value in (
            ("domestic_results", domestic_results),
            ("international_results", international_results),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= MAX_RESULTS
            ):
                raise ValueError(f"{name} 必须是 1 到 {MAX_RESULTS} 的整数。")
        if (
            not isinstance(auto_calls_per_turn, int)
            or isinstance(auto_calls_per_turn, bool)
            or not 0 <= auto_calls_per_turn <= MAX_AUTO_CALLS_PER_TURN
        ):
            raise ValueError(
                "auto_calls_per_turn 必须是 0 到 "
                f"{MAX_AUTO_CALLS_PER_TURN} 的整数。"
            )
        self.timeout_seconds = timeout_seconds
        self._default_max_results = default_max_results
        self._minimum_score = float(minimum_score)
        self._auto_calls_per_turn = auto_calls_per_turn
        self._default_scope = default_scope
        self._now_provider = now_provider
        self._domestic_results = domestic_results
        self._international_results = international_results
        self._domestic_domains = self._validate_domains(
            domestic_domains, "domestic_domains"
        )
        self._international_domains = self._validate_domains(
            international_domains, "international_domains"
        )
        self._excluded_domains = self._validate_domains(
            excluded_domains, "excluded_domains"
        )
        conflicting_domains = (
            set(self._domestic_domains) | set(self._international_domains)
        ) & set(self._excluded_domains)
        if conflicting_domains:
            conflicts = "、".join(sorted(conflicting_domains))
            raise ValueError(
                "搜索域名不能同时出现在允许列表和排除列表："
                f"{conflicts}。"
            )
        self.description = self._build_description()
        self.confirmation_description = (
            "将把下方搜索词发送给第三方 Tavily。balanced 会并行发起国内和"
            "国际两次网络请求；请确认搜索词中不含密钥、隐私或敏感信息。"
        )
        self._client = client or TavilySearchClient(api_key)
        self._turn_request_count = 0
        self._turn_signatures: set[tuple[str, str, str, str | None, str]] = set()
        self._confirmation_reason: str | None = None

    def begin_turn(self) -> None:
        """重置当前用户消息对应的搜索次数和去重状态。"""
        self._turn_request_count = 0
        self._turn_signatures.clear()
        self._confirmation_reason = None

    def requires_confirmation_for(self, arguments: JsonObject) -> bool:
        """前若干次网络请求自动执行，重复或超额搜索需要确认。"""
        validated = self._validate_arguments(arguments)
        signature = (
            self._normalize_query(validated.query),
            self._normalize_query(validated.international_query or ""),
            validated.topic,
            validated.time_range,
            validated.scope,
        )
        duplicate = signature in self._turn_signatures
        self._turn_request_count += validated.provider_request_count
        self._turn_signatures.add(signature)
        over_limit = self._turn_request_count > self._auto_calls_per_turn
        reasons: list[str] = []
        if duplicate:
            reasons.append("与本轮之前的搜索重复")
        if over_limit:
            reasons.append(
                "超过本轮自动网络请求次数"
                f"（{self._auto_calls_per_turn} 次）"
            )
        self._confirmation_reason = "；".join(reasons) or None
        return bool(reasons)

    def confirmation_arguments_for(
        self,
        arguments: JsonObject,
        raw_arguments: str,
    ) -> str:
        """在确认参数中解释触发动态确认的原因。"""
        del raw_arguments
        confirmation_payload = dict(arguments)
        if self._confirmation_reason is not None:
            confirmation_payload["confirmation_reason"] = self._confirmation_reason
        return json.dumps(confirmation_payload, ensure_ascii=False)

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
        if validated.scope == "balanced":
            payload = self._execute_balanced(
                validated,
                timeout_seconds=timeout_seconds,
                context=context,
            )
        else:
            response = self._search_scope(
                query=validated.query,
                scope=validated.scope,
                max_results=validated.max_results,
                topic=validated.topic,
                time_range=validated.time_range,
                timeout_seconds=timeout_seconds,
            )
            self._raise_if_cancelled(context)
            payload = self._normalize_response(
                response,
                query=validated.query,
                topic=validated.topic,
                time_range=validated.time_range,
                max_results=validated.max_results,
                requested_scope=validated.scope,
            )
            payload["provider_request_count"] = 1
        return json.dumps(payload, ensure_ascii=False)

    def _execute_balanced(
        self,
        arguments: SearchArguments,
        *,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> JsonObject:
        international_query = arguments.international_query
        if international_query is None:
            raise ToolExecutionError("balanced 搜索缺少 international_query。")
        requests = {
            "domestic": (arguments.query, self._domestic_results),
            "international": (international_query, self._international_results),
        }
        groups: JsonObject = {}
        errors: JsonObject = {}
        futures: dict[Future[JsonObject], tuple[str, str, int]] = {}
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="web-search",
        ) as executor:
            for scope, (query, max_results) in requests.items():
                future = executor.submit(
                    self._search_scope,
                    query=query,
                    scope=scope,
                    max_results=max_results,
                    topic=arguments.topic,
                    time_range=arguments.time_range,
                    timeout_seconds=timeout_seconds,
                )
                futures[future] = (scope, query, max_results)
            for future in as_completed(futures):
                scope, query, max_results = futures[future]
                try:
                    response = future.result()
                    groups[scope] = self._normalize_response(
                        response,
                        query=query,
                        topic=arguments.topic,
                        time_range=arguments.time_range,
                        max_results=max_results,
                        requested_scope=scope,
                    )
                except ToolExecutionError as error:
                    errors[scope] = str(error)

        self._raise_if_cancelled(context)
        if not groups:
            details = "；".join(
                f"{scope}: {message}" for scope, message in sorted(errors.items())
            )
            raise ToolExecutionError(f"国内和国际联网搜索均失败。{details}")
        groups = {
            scope: groups[scope]
            for scope in ("domestic", "international")
            if scope in groups
        }
        cross_scope_duplicate_count = self._deduplicate_balanced_groups(groups)
        result_count = sum(
            int(group.get("result_count", 0))
            for group in groups.values()
            if isinstance(group, dict)
        )
        quality_filtered_count = sum(
            int(group.get("quality_filtered_count", 0))
            for group in groups.values()
            if isinstance(group, dict)
        )
        limit_discarded_count = sum(
            int(group.get("limit_discarded_count", 0))
            for group in groups.values()
            if isinstance(group, dict)
        )
        payload: JsonObject = {
            "provider": "tavily",
            "result_content_type": "search_snippet",
            "page_content_read": False,
            "requested_scope": "balanced",
            "queries": {
                "domestic": arguments.query,
                "international": international_query,
            },
            "topic": arguments.topic,
            "time_range": arguments.time_range,
            "provider_request_count": 2,
            "result_count": result_count,
            "quality_filtered_count": quality_filtered_count,
            "cross_scope_duplicate_count": cross_scope_duplicate_count,
            "limit_discarded_count": limit_discarded_count,
            "groups": groups,
            "partial_failure": bool(errors),
        }
        if errors:
            payload["errors"] = errors
        return payload

    def _search_scope(
        self,
        *,
        query: str,
        scope: str,
        max_results: int,
        topic: str,
        time_range: str | None,
        timeout_seconds: float,
    ) -> JsonObject:
        include_domains: Sequence[str] = ()
        country: str | None = None
        if scope == "domestic":
            include_domains = self._domestic_domains
            if topic == "general" and not include_domains:
                country = "china"
        elif scope == "international":
            include_domains = self._international_domains
        return self._client.search(
            query,
            max_results=self._provider_result_limit(max_results),
            topic=topic,
            time_range=time_range,
            timeout_seconds=timeout_seconds,
            include_domains=include_domains,
            exclude_domains=self._excluded_domains,
            country=country,
        )

    @staticmethod
    def _provider_result_limit(result_limit: int) -> int:
        """多取有限候选，避免过滤无效链接后无法达到期望结果数。"""
        return min(MAX_RESULTS, result_limit * 2)

    def _validate_arguments(self, arguments: JsonObject) -> SearchArguments:
        query = arguments.get("query")
        international_query = arguments.get("international_query")
        max_results = arguments.get("max_results")
        topic = arguments.get("topic", "general")
        time_range = arguments.get("time_range")
        scope = arguments.get("scope")
        query = self._validate_query(query, "query")
        if not isinstance(scope, str) or scope not in SUPPORTED_SEARCH_SCOPES:
            raise ToolExecutionError(
                "web_search 的 scope 只能是 balanced、domestic、international "
                "或 unrestricted。"
            )
        if scope == "balanced":
            international_query = self._validate_query(
                international_query,
                "international_query",
            )
        elif international_query is not None:
            raise ToolExecutionError(
                "international_query 仅可用于 balanced 搜索。"
            )
        if max_results is not None and (
            not isinstance(max_results, int)
            or isinstance(max_results, bool)
            or not 1 <= max_results <= MAX_RESULTS
        ):
            raise ToolExecutionError(
                f"web_search 的 max_results 必须是 1 到 {MAX_RESULTS} 的整数。"
            )
        if scope != "unrestricted" and max_results is not None:
            raise ToolExecutionError(
                "balanced、domestic 和 international 搜索必须省略 max_results，"
                "结果数量由用户配置决定。"
            )
        if max_results is None:
            max_results = self._default_results_for(scope)
        if not isinstance(topic, str) or topic not in SUPPORTED_TOPICS:
            raise ToolExecutionError(
                "web_search 的 topic 只能是 general、news 或 finance。"
            )
        if time_range is not None and (
            not isinstance(time_range, str)
            or time_range not in SUPPORTED_TIME_RANGES
        ):
            raise ToolExecutionError(
                "web_search 的 time_range 只能是 day、week、month 或 year。"
            )
        self._reject_stale_freshness_year(
            query,
            name="query",
            time_range=time_range,
        )
        if international_query is not None:
            self._reject_stale_freshness_year(
                international_query,
                name="international_query",
                time_range=time_range,
            )
        return SearchArguments(
            query=query,
            international_query=international_query,
            max_results=max_results,
            topic=topic,
            time_range=time_range,
            scope=scope,
        )

    def _reject_stale_freshness_year(
        self,
        query: str,
        *,
        name: str,
        time_range: str | None,
    ) -> None:
        """拒绝相对新鲜度、近期范围和过期年份互相冲突的搜索词。"""
        if time_range is None or FRESHNESS_PATTERN.search(query) is None:
            return
        current = self._now_provider()
        stale_years = sorted(
            {
                int(match.group(1))
                for match in YEAR_PATTERN.finditer(query)
                if int(match.group(1)) < current.year
            }
        )
        if not stale_years:
            return
        years = "、".join(str(year) for year in stale_years)
        raise ToolExecutionError(
            f"web_search 的 {name} 同时包含最新类表达、"
            f"time_range={time_range} 和过期年份 {years}；"
            f"当前北京时间日期为 {current:%Y-%m-%d}。"
            "若用户未明确指定历史年份，请删除年份后重试；"
            "若用户确实要求历史主题，请移除相对时间范围并明确历史语境。"
        )

    def _validate_query(self, value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ToolExecutionError(f"web_search 的 {name} 必须是非空字符串。")
        query = value.strip()
        if len(query) > MAX_QUERY_LENGTH:
            raise ToolExecutionError(
                f"web_search 的 {name} 不能超过 {MAX_QUERY_LENGTH} 个字符。"
            )
        if self._contains_sensitive_data(query):
            raise ToolExecutionError(
                f"{name} 疑似包含 API Key、Token、密码或私钥，已拒绝联网发送。"
            )
        return query

    def _default_results_for(self, scope: str) -> int:
        if scope == "domestic":
            return self._domestic_results
        if scope == "international":
            return self._international_results
        return self._default_max_results

    def _build_description(self) -> str:
        if self._default_scope == "balanced":
            scope_instruction = (
                "用户未限定来源时，默认只调用一次 scope=balanced：query 使用中文"
                "关键词，international_query 使用表达同一目标的英文关键词；工具会"
                "并行执行国内和国际两路请求。"
            )
        else:
            scope_instruction = (
                "用户未限定来源时，默认使用 "
                f"scope={self._default_scope}。"
            )
        return (
            "搜索公开互联网中的当前信息，返回标题、来源 URL 和相关摘要。"
            "每条结果带有稳定 source_id，后续正文读取会沿用该标识。"
            "这些摘要不是网页正文；除非用户只需要候选链接，否则重要事实应继续"
            "使用 read_web_page 读取并比较具体来源。"
            f"{scope_instruction}"
            f"domestic 默认 {self._domestic_results} 条，"
            f"international 默认 {self._international_results} 条，"
            f"unrestricted 默认 {self._default_max_results} 条。"
            "以来源质量为先，结果不足时不要用低质量来源凑数。"
            "scope 表示请求意图，不代表搜索服务已证明每个来源的地域归属。"
            "查询会发送给第三方搜索服务，不得包含 API Key、密码或隐私数据。"
            f"相关度低于 {self._minimum_score:g} 的结果会被过滤；"
            "低置信度且与查询缺少关键词关联的结果也不会返回。"
        )

    @staticmethod
    def _validate_domains(domains: Sequence[str], name: str) -> tuple[str, ...]:
        if isinstance(domains, (str, bytes)):
            raise ValueError(f"{name} 必须是域名序列，不能是字符串。")
        if len(domains) > MAX_CONFIGURED_DOMAINS:
            raise ValueError(
                f"{name} 最多配置 {MAX_CONFIGURED_DOMAINS} 个域名。"
            )
        normalized: list[str] = []
        seen: set[str] = set()
        for domain in domains:
            if not isinstance(domain, str):
                raise ValueError(f"{name} 中只能包含字符串域名。")
            value = domain.strip().casefold().removeprefix("www.")
            if not DOMAIN_PATTERN.fullmatch(value):
                raise ValueError(
                    f"{name} 包含无效域名：{domain!r}；请勿填写协议、路径或通配符。"
                )
            if value not in seen:
                normalized.append(value)
                seen.add(value)
        return tuple(normalized)

    @staticmethod
    def _normalize_query(query: str) -> str:
        return " ".join(query.casefold().split())

    @staticmethod
    def _contains_sensitive_data(query: str) -> bool:
        if (
            SECRET_PREFIX_PATTERN.search(query)
            or BEARER_TOKEN_PATTERN.search(query)
            or PRIVATE_KEY_PATTERN.search(query)
        ):
            return True
        assignment = CREDENTIAL_ASSIGNMENT_PATTERN.search(query)
        if assignment is None:
            return False
        value = assignment.group(1).strip("\"'`.,;()[]{}")
        normalized = value.casefold()
        return len(value) >= 12 and not any(
            marker in normalized for marker in PLACEHOLDER_MARKERS
        )

    def _effective_timeout(self, context: ToolExecutionContext) -> float:
        timeout_seconds = self.timeout_seconds or DEFAULT_SEARCH_TIMEOUT_SECONDS
        if context.deadline is None:
            return timeout_seconds
        remaining = context.deadline - context.clock()
        if remaining <= 0:
            raise ToolExecutionError("联网搜索超过执行期限。")
        return min(timeout_seconds, remaining)

    @staticmethod
    def _raise_if_cancelled(context: ToolExecutionContext) -> None:
        if context.timed_out:
            raise ToolExecutionError("联网搜索超过执行期限。")
        if context.cancellation_requested:
            raise ToolExecutionError("联网搜索已取消。")

    def _normalize_response(
        self,
        response: JsonObject,
        *,
        query: str,
        topic: str,
        time_range: str | None,
        max_results: int,
        requested_scope: str,
    ) -> JsonObject:
        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise ToolExecutionError("Tavily 响应缺少有效的 results。")

        results: list[JsonObject] = []
        seen_domains: set[str] = set()
        seen_titles: set[str] = set()
        seen_contents: set[str] = set()
        filter_reasons: dict[str, int] = {}
        limit_discarded_count = 0
        query_anchors = self._query_anchors(query)

        def reject(reason: str) -> None:
            filter_reasons[reason] = filter_reasons.get(reason, 0) + 1

        for item in raw_results:
            if not isinstance(item, dict):
                reject("invalid_item")
                continue
            title = item.get("title")
            url = item.get("url")
            content = item.get("content", "")
            if not isinstance(title, str) or not isinstance(url, str):
                reject("invalid_title_or_url")
                continue
            try:
                normalized_url = normalize_public_web_url(
                    url,
                    excluded_domains=self._excluded_domains,
                )
            except PublicWebUrlError as error:
                reject(self._url_filter_reason(error.reason))
                continue
            normalized_hostname = (
                urlsplit(normalized_url).hostname or ""
            ).casefold().removeprefix("www.")
            allowed_domains = self._allowed_domains_for_scope(requested_scope)
            if allowed_domains and not self._hostname_matches_any(
                normalized_hostname,
                allowed_domains,
            ):
                reject("outside_allowed_domains")
                continue
            if normalized_hostname in seen_domains:
                reject("duplicate_domain")
                continue
            normalized_title = title.strip()
            if not normalized_title:
                reject("empty_title")
                continue
            normalized_content = content.strip() if isinstance(content, str) else ""
            score_value = self._valid_score(item.get("score"))
            if score_value is None and item.get("score") is not None:
                reject("invalid_score")
                continue
            if score_value is not None and score_value < self._minimum_score:
                reject("low_score")
                continue
            if (
                score_value is not None
                and score_value < WEAK_MATCH_SCORE_THRESHOLD
                and requested_scope != "unrestricted"
                and query_anchors
                and not self._has_query_anchor(
                    query_anchors,
                    f"{normalized_title}\n{normalized_content}",
                )
            ):
                reject("weak_query_match")
                continue
            title_signature = self._text_signature(normalized_title)
            if title_signature and title_signature in seen_titles:
                reject("duplicate_title")
                continue
            content_signature = self._text_signature(normalized_content)
            if len(content_signature) >= 80 and content_signature in seen_contents:
                reject("duplicate_content")
                continue
            if len(results) >= max_results:
                limit_discarded_count += 1
                continue

            normalized: JsonObject = {
                "source_id": source_id_for_url(normalized_url),
                "title": normalized_title[:MAX_RESULT_TITLE_LENGTH],
                "url": normalized_url,
                "content": normalized_content[:MAX_RESULT_CONTENT_LENGTH],
            }
            if score_value is not None:
                normalized["score"] = round(score_value, 6)
            published_date = item.get("published_date")
            if isinstance(published_date, str) and published_date.strip():
                normalized["published_date"] = published_date.strip()[
                    :MAX_PUBLISHED_DATE_LENGTH
                ]
            results.append(normalized)
            seen_domains.add(normalized_hostname)
            if title_signature:
                seen_titles.add(title_signature)
            if len(content_signature) >= 80:
                seen_contents.add(content_signature)

        quality_filtered_count = sum(filter_reasons.values())
        payload: JsonObject = {
            "provider": "tavily",
            "query": query,
            "result_content_type": "search_snippet",
            "page_content_read": False,
            "requested_scope": requested_scope,
            "topic": topic,
            "time_range": time_range,
            "result_limit": max_results,
            "candidate_count": len(raw_results),
            "result_count": len(results),
            "minimum_score": self._minimum_score,
            "quality_filtered_count": quality_filtered_count,
            "quality_filter_reasons": filter_reasons,
            "limit_discarded_count": limit_discarded_count,
            "quality_limited": (
                len(results) < max_results and quality_filtered_count > 0
            ),
            "results": results,
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

    @staticmethod
    def _valid_score(value: object) -> float | None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        score = float(value)
        if not math.isfinite(score) or not 0 <= score <= 1:
            return None
        return score

    @staticmethod
    def _text_signature(value: str) -> str:
        return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)

    @staticmethod
    def _query_anchors(query: str) -> tuple[str, ...]:
        anchors: list[str] = []
        seen: set[str] = set()
        for match in LATIN_QUERY_TOKEN_PATTERN.finditer(query.casefold()):
            token = match.group(0)
            if token not in ENGLISH_QUERY_STOP_WORDS and token not in seen:
                anchors.append(token)
                seen.add(token)
        for match in CJK_QUERY_RUN_PATTERN.finditer(query):
            run = match.group(0)
            for index in range(len(run) - 1):
                token = run[index : index + 2]
                if token not in CJK_QUERY_STOP_BIGRAMS and token not in seen:
                    anchors.append(token)
                    seen.add(token)
        return tuple(anchors)

    @staticmethod
    def _has_query_anchor(anchors: Sequence[str], content: str) -> bool:
        normalized_content = content.casefold()
        return any(anchor in normalized_content for anchor in anchors)

    def _allowed_domains_for_scope(self, scope: str) -> tuple[str, ...]:
        if scope == "domestic":
            return self._domestic_domains
        if scope == "international":
            return self._international_domains
        return ()

    @staticmethod
    def _hostname_matches_any(hostname: str, domains: Sequence[str]) -> bool:
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in domains
        )

    @staticmethod
    def _url_filter_reason(reason: str) -> str:
        if reason in {"search_redirect", "excluded_domain"}:
            return reason
        if reason in {"credential_url", "non_public_host", "nonstandard_port"}:
            return "unsafe_url"
        return "invalid_url"

    @staticmethod
    def _deduplicate_balanced_groups(groups: JsonObject) -> int:
        """按固定范围顺序移除两路搜索共同返回的同一规范 URL。"""
        seen_source_ids: set[str] = set()
        duplicate_count = 0
        for scope in ("domestic", "international"):
            group = groups.get(scope)
            if not isinstance(group, dict):
                continue
            raw_results = group.get("results")
            if not isinstance(raw_results, list):
                continue
            retained: list[JsonObject] = []
            removed = 0
            for result in raw_results:
                if not isinstance(result, dict):
                    continue
                source_id = result.get("source_id")
                if isinstance(source_id, str) and source_id in seen_source_ids:
                    removed += 1
                    continue
                retained.append(result)
                if isinstance(source_id, str):
                    seen_source_ids.add(source_id)
            if not removed:
                continue
            duplicate_count += removed
            group["results"] = retained
            group["result_count"] = len(retained)
            reasons = group.get("quality_filter_reasons")
            if not isinstance(reasons, dict):
                reasons = {}
                group["quality_filter_reasons"] = reasons
            reasons["cross_scope_duplicate"] = removed
            previous_filtered = group.get("quality_filtered_count", 0)
            group["quality_filtered_count"] = (
                previous_filtered + removed
                if isinstance(previous_filtered, int)
                and not isinstance(previous_filtered, bool)
                else removed
            )
            group["quality_limited"] = True
        return duplicate_count


__all__ = [
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_MINIMUM_SCORE",
    "DEFAULT_SEARCH_TIMEOUT_SECONDS",
    "MAX_RESULTS",
    "TavilySearchClient",
    "WebSearchTool",
]
