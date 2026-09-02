"""验证联网搜索的请求边界、响应过滤和可选注册行为。"""

import json
import tempfile
import threading
import unittest
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.models import ToolCallRequest
from deepseek_agent.tool_execution import ToolExecutionStatus, ToolExecutor
from deepseek_agent.tools import (
    TavilySearchClient,
    ToolEffect,
    ToolExecutionContext,
    ToolExecutionError,
    ToolRegistry,
    WebSearchTool,
    build_default_registry,
)


class RecordingTransport:
    def __init__(self, response: object | None = None, error: Exception | None = None):
        self.response = response if response is not None else {"results": []}
        self.error = error
        self.calls: list[tuple[dict[str, object], dict[str, str], float]] = []

    def __call__(
        self,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append((json.loads(body.decode("utf-8")), dict(headers), timeout_seconds))
        if self.error is not None:
            raise self.error
        if isinstance(self.response, bytes):
            return self.response
        return json.dumps(self.response, ensure_ascii=False).encode("utf-8")


class ParallelTransport:
    """只有两次请求同时到达时才返回，用于证明 balanced 不是串行执行。"""

    def __init__(self, failing_queries: set[str] | None = None) -> None:
        self._barrier = threading.Barrier(2, timeout=2)
        self._lock = threading.Lock()
        self._failing_queries = failing_queries or set()
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> bytes:
        del headers, timeout_seconds
        request = json.loads(body.decode("utf-8"))
        with self._lock:
            self.calls.append(request)
        self._barrier.wait()
        query = str(request["query"])
        if query in self._failing_queries:
            raise URLError("simulated failure")
        slug = "domestic" if query == "人工智能进展" else "international"
        hostname = "gov.cn" if slug == "domestic" else "reuters.com"
        return json.dumps(
            {
                "results": [
                    {
                        "title": f"{slug} source",
                        "url": f"https://{hostname}/article",
                        "content": "summary",
                    }
                ]
            }
        ).encode("utf-8")


class QueryResponseTransport:
    """按查询返回不同响应，用于验证 balanced 汇总边界。"""

    def __init__(self, responses: Mapping[str, object]) -> None:
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> bytes:
        del headers, timeout_seconds
        request = json.loads(body.decode("utf-8"))
        self.calls.append(request)
        response = self._responses[str(request["query"])]
        return json.dumps(response, ensure_ascii=False).encode("utf-8")


class RejectingConfirmer:
    def __init__(self) -> None:
        self.arguments: list[str] = []

    def confirm(self, tool: object, arguments: str) -> bool:
        del tool
        self.arguments.append(arguments)
        return False


class WebSearchToolTests(unittest.TestCase):
    def build_tool(
        self,
        transport: RecordingTransport,
        *,
        timeout_seconds: float = 20,
        default_max_results: int = 5,
        minimum_score: float = 0.25,
        auto_calls_per_turn: int = 2,
        default_scope: str = "balanced",
        domestic_results: int = 3,
        international_results: int = 3,
        domestic_domains: tuple[str, ...] = (),
        international_domains: tuple[str, ...] = (),
        excluded_domains: tuple[str, ...] = (),
        now_provider: Callable[[], datetime] = lambda: datetime(2026, 8, 29),
    ) -> WebSearchTool:
        client = TavilySearchClient("tvly-private-test-key", transport=transport)
        return WebSearchTool(
            "unused-because-client-is-injected",
            timeout_seconds=timeout_seconds,
            default_max_results=default_max_results,
            minimum_score=minimum_score,
            auto_calls_per_turn=auto_calls_per_turn,
            default_scope=default_scope,
            domestic_results=domestic_results,
            international_results=international_results,
            domestic_domains=domestic_domains,
            international_domains=international_domains,
            excluded_domains=excluded_domains,
            now_provider=now_provider,
            client=client,
        )

    def test_search_uses_bounded_request_and_returns_filtered_sources(self) -> None:
        transport = RecordingTransport(
            {
                "request_id": "request-1",
                "response_time": 0.25,
                "results": [
                    {
                        "title": "Official result",
                        "url": "https://example.com/source",
                        "content": "A" * 2_000,
                        "score": 0.987654321,
                        "published_date": "2026-08-28",
                    },
                    {
                        "title": "Unsafe URL",
                        "url": "javascript:alert(1)",
                        "content": "ignored",
                    },
                    {
                        "title": "Duplicate domain",
                        "url": "https://www.example.com/duplicate",
                        "content": "ignored duplicate",
                    },
                    {
                        "title": "Search redirect",
                        "url": "https://www.google.com.hk/url?q=https://example.net/news",
                        "content": "not a direct source",
                    },
                    {
                        "title": "Second result",
                        "url": "http://example.org/second",
                        "content": "summary",
                    },
                ],
            }
        )
        tool = self.build_tool(transport)

        result = json.loads(
            tool.execute(
                {
                    "query": " latest agent news ",
                    "scope": "unrestricted",
                    "max_results": 2,
                    "topic": "news",
                    "time_range": "week",
                }
            )
        )

        self.assertEqual(result["provider"], "tavily")
        self.assertEqual(result["result_content_type"], "search_snippet")
        self.assertFalse(result["page_content_read"])
        self.assertEqual(result["query"], "latest agent news")
        self.assertEqual(result["requested_scope"], "unrestricted")
        self.assertEqual(result["provider_request_count"], 1)
        self.assertEqual(result["result_limit"], 2)
        self.assertEqual(result["candidate_count"], 5)
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["minimum_score"], 0.25)
        self.assertEqual(result["quality_filtered_count"], 3)
        self.assertEqual(result["quality_filter_reasons"]["invalid_url"], 1)
        self.assertEqual(result["quality_filter_reasons"]["duplicate_domain"], 1)
        self.assertEqual(result["quality_filter_reasons"]["search_redirect"], 1)
        self.assertEqual(len(result["results"][0]["content"]), 1_500)
        self.assertEqual(result["results"][0]["score"], 0.987654)
        self.assertEqual(result["results"][1]["url"], "http://example.org/second")
        self.assertNotIn("google.com", json.dumps(result))
        self.assertNotIn("tvly-private-test-key", json.dumps(result))

        request, headers, timeout = transport.calls[0]
        self.assertEqual(request["query"], "latest agent news")
        self.assertEqual(request["max_results"], 4)
        self.assertEqual(request["topic"], "news")
        self.assertEqual(request["time_range"], "week")
        self.assertNotIn("scope", request)
        self.assertEqual(request["search_depth"], "basic")
        self.assertFalse(request["include_answer"])
        self.assertFalse(request["include_raw_content"])
        self.assertFalse(request["include_images"])
        self.assertEqual(headers["Authorization"], "Bearer tvly-private-test-key")
        self.assertEqual(timeout, 20)

    def test_quality_gate_filters_low_relevance_and_cross_domain_duplicates(
        self,
    ) -> None:
        repeated_content = (
            "DeepSeek released the same model update with verified details. " * 3
        )
        transport = RecordingTransport(
            {
                "results": [
                    {
                        "title": "DeepSeek model release",
                        "url": "https://official.example.com/release",
                        "content": repeated_content,
                        "score": 0.92,
                    },
                    {
                        "title": "Unrelated low score",
                        "url": "https://low.example.net/story",
                        "content": "unrelated",
                        "score": 0.1,
                    },
                    {
                        "title": "Football results",
                        "url": "https://sports.example.org/table",
                        "content": "Scores and league standings from this weekend.",
                        "score": 0.4,
                    },
                    {
                        "title": "DeepSeek model release",
                        "url": "https://mirror.example.net/copy",
                        "content": "A mirrored report.",
                        "score": 0.8,
                    },
                    {
                        "title": "Syndicated model story",
                        "url": "https://syndicated.example.org/copy",
                        "content": repeated_content,
                        "score": 0.8,
                    },
                    {
                        "title": "DeepSeek developer analysis",
                        "url": "https://analysis.example.edu/article",
                        "content": "Independent DeepSeek release analysis.",
                        "score": 0.75,
                    },
                ]
            }
        )

        result = json.loads(
            self.build_tool(transport).execute(
                {
                    "query": "DeepSeek latest model release",
                    "scope": "international",
                }
            )
        )

        self.assertEqual(result["candidate_count"], 6)
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["quality_filtered_count"], 4)
        self.assertTrue(result["quality_limited"])
        self.assertEqual(
            result["quality_filter_reasons"],
            {
                "low_score": 1,
                "weak_query_match": 1,
                "duplicate_title": 1,
                "duplicate_content": 1,
            },
        )
        self.assertEqual(
            [item["url"] for item in result["results"]],
            [
                "https://official.example.com/release",
                "https://analysis.example.edu/article",
            ],
        )

    def test_quality_gate_can_be_relaxed_and_rejects_invalid_scores(self) -> None:
        transport = RecordingTransport(
            {
                "results": [
                    {
                        "title": "Low scored but retained",
                        "url": "https://example.com/low",
                        "content": "DeepSeek release",
                        "score": 0.05,
                    },
                    {
                        "title": "Invalid score",
                        "url": "https://example.org/invalid",
                        "content": "DeepSeek release",
                        "score": 1.5,
                    },
                ]
            }
        )

        result = json.loads(
            self.build_tool(transport, minimum_score=0).execute(
                {
                    "query": "DeepSeek release",
                    "scope": "unrestricted",
                    "max_results": 2,
                }
            )
        )

        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["score"], 0.05)
        self.assertEqual(result["quality_filter_reasons"], {"invalid_score": 1})

    def test_provider_urls_are_normalized_and_unsafe_candidates_are_filtered(
        self,
    ) -> None:
        transport = RecordingTransport(
            {
                "results": [
                    {
                        "title": "Canonical source",
                        "url": "HTTPS://Example.COM:443/article#section",
                        "content": "DeepSeek release",
                    },
                    {
                        "title": "Local service",
                        "url": "http://127.0.0.1/private",
                    },
                    {
                        "title": "Credential authority",
                        "url": "https://user:password@example.org/private",
                    },
                    {
                        "title": "Credential query",
                        "url": (
                            "https://news.example.net/report?"
                            "access_token=very-secret-value"
                        ),
                    },
                    {
                        "title": "Nonstandard port",
                        "url": "https://service.example.edu:8443/report",
                    },
                    {
                        "title": "Bing redirect",
                        "url": "https://www.bing.com/ck/a?target=example",
                    },
                ]
            }
        )

        result = json.loads(
            self.build_tool(transport).execute(
                {
                    "query": "DeepSeek release",
                    "scope": "unrestricted",
                    "max_results": 5,
                }
            )
        )

        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["url"], "https://example.com/article")
        self.assertRegex(result["results"][0]["source_id"], r"^src_[0-9a-f]{12}$")
        self.assertEqual(
            result["quality_filter_reasons"],
            {"unsafe_url": 4, "search_redirect": 1},
        )
        self.assertNotIn("very-secret-value", json.dumps(result))

    def test_balanced_search_deduplicates_the_same_source_across_scopes(
        self,
    ) -> None:
        shared = {
            "title": "Shared source",
            "url": "https://shared.example.com/report#top",
            "content": "shared",
        }
        transport = QueryResponseTransport(
            {
                "人工智能进展": {
                    "results": [
                        shared,
                        {
                            "title": "Domestic source",
                            "url": "https://domestic.example.cn/report",
                            "content": "domestic",
                        },
                    ]
                },
                "artificial intelligence progress": {
                    "results": [
                        {**shared, "url": "https://shared.example.com/report"},
                        {
                            "title": "International source",
                            "url": "https://international.example.org/report",
                            "content": "international",
                        },
                    ]
                },
            }
        )
        client = TavilySearchClient("tvly-private-test-key", transport=transport)
        tool = WebSearchTool(
            "unused-because-client-is-injected",
            client=client,
        )

        result = json.loads(
            tool.execute(
                {
                    "query": "人工智能进展",
                    "international_query": "artificial intelligence progress",
                    "scope": "balanced",
                }
            )
        )

        self.assertEqual(list(result["groups"]), ["domestic", "international"])
        self.assertEqual(result["result_count"], 3)
        self.assertEqual(result["cross_scope_duplicate_count"], 1)
        self.assertEqual(result["quality_filtered_count"], 1)
        self.assertEqual(result["groups"]["international"]["result_count"], 1)
        self.assertEqual(
            result["groups"]["international"]["quality_filter_reasons"],
            {"cross_scope_duplicate": 1},
        )

    def test_domestic_quality_gate_removes_observed_off_topic_results(self) -> None:
        transport = RecordingTransport(
            {
                "results": [
                    {
                        "title": "K-Dramas To Watch This Month",
                        "url": "https://entertainment.example.com/kdramas",
                        "content": "A packed slate of Korean television dramas.",
                        "score": 0.36,
                    },
                    {
                        "title": "2026 夏季恋爱动画一览",
                        "url": "https://anime.example.org/summer",
                        "content": "整理本季新番中的恋爱动画和恋爱喜剧作品。",
                        "score": 0.34,
                    },
                    {
                        "title": "Fantasy Football Rankings",
                        "url": "https://sports.example.net/rankings",
                        "content": "Rookie player rankings.",
                        "score": 0.1,
                    },
                ]
            }
        )

        result = json.loads(
            self.build_tool(transport).execute(
                {
                    "query": "2026年夏季新番 恋爱动画推荐",
                    "scope": "domestic",
                }
            )
        )

        self.assertEqual(result["result_count"], 1)
        self.assertEqual(
            result["results"][0]["url"],
            "https://anime.example.org/summer",
        )
        self.assertEqual(
            result["quality_filter_reasons"],
            {"weak_query_match": 1, "low_score": 1},
        )

    def test_configured_domain_filters_are_enforced_on_provider_results(self) -> None:
        transport = RecordingTransport(
            {
                "results": [
                    {
                        "title": "Trusted source",
                        "url": "https://news.trusted.example/article",
                        "content": "人工智能进展",
                        "score": 0.9,
                    },
                    {
                        "title": "Provider ignored allowlist",
                        "url": "https://other.example/article",
                        "content": "人工智能进展",
                        "score": 0.9,
                    },
                    {
                        "title": "Explicitly blocked",
                        "url": "https://blocked.example/article",
                        "content": "人工智能进展",
                        "score": 0.9,
                    },
                ]
            }
        )
        tool = self.build_tool(
            transport,
            domestic_domains=("trusted.example",),
            excluded_domains=("blocked.example",),
        )

        result = json.loads(
            tool.execute(
                {
                    "query": "人工智能进展",
                    "scope": "domestic",
                }
            )
        )

        self.assertEqual(result["result_count"], 1)
        self.assertEqual(
            result["results"][0]["url"],
            "https://news.trusted.example/article",
        )
        self.assertEqual(
            result["quality_filter_reasons"],
            {"outside_allowed_domains": 1, "excluded_domain": 1},
        )

    def test_default_result_limit_and_deadline_are_propagated(self) -> None:
        transport = RecordingTransport()
        tool = self.build_tool(
            transport,
            timeout_seconds=20,
            default_max_results=3,
        )

        tool.execute_with_context(
            {"query": "Python release", "scope": "unrestricted"},
            ToolExecutionContext(deadline=10, clock=lambda: 7),
        )

        request, _headers, timeout = transport.calls[0]
        self.assertEqual(request["max_results"], 6)
        self.assertEqual(request["topic"], "general")
        self.assertNotIn("time_range", request)
        self.assertEqual(timeout, 3)

    def test_invalid_arguments_are_rejected_before_network_access(self) -> None:
        invalid_arguments = (
            {},
            {"query": "   ", "scope": "unrestricted"},
            {"query": "x"},
            {"query": "x", "scope": "unrestricted", "max_results": True},
            {"query": "x", "scope": "unrestricted", "max_results": 0},
            {"query": "x", "scope": "unrestricted", "max_results": 11},
            {"query": "x", "scope": "unrestricted", "topic": "images"},
            {"query": "x", "scope": "unrestricted", "time_range": "hour"},
            {"query": "x", "scope": "global"},
            {"query": "x", "scope": "domestic", "max_results": 2},
            {"query": "x", "scope": "balanced"},
            {
                "query": "x",
                "international_query": "x",
                "scope": "domestic",
            },
            {"query": "x" * 401, "scope": "unrestricted"},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                transport = RecordingTransport()
                with self.assertRaises(ToolExecutionError):
                    self.build_tool(transport).execute(arguments)
                self.assertEqual(transport.calls, [])

    def test_stale_year_in_latest_search_is_rejected_before_network(self) -> None:
        cases = (
            {
                "query": "DeepSeek 最新新闻 2025",
                "scope": "domestic",
                "topic": "news",
                "time_range": "month",
            },
            {
                "query": "DeepSeek 最新新闻",
                "international_query": "DeepSeek latest news 2025",
                "scope": "balanced",
                "topic": "news",
                "time_range": "month",
            },
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                transport = RecordingTransport()
                with self.assertRaisesRegex(
                    ToolExecutionError,
                    "当前北京时间日期为 2026-08-29",
                ):
                    self.build_tool(transport).execute(arguments)
                self.assertEqual(transport.calls, [])

    def test_current_year_and_explicit_historical_topics_are_allowed(self) -> None:
        transport = RecordingTransport()
        tool = self.build_tool(transport)

        tool.execute(
            {
                "query": "DeepSeek 最新新闻 2026",
                "scope": "domestic",
                "topic": "news",
                "time_range": "month",
            }
        )
        tool.execute(
            {
                "query": "2025 好看的恋爱番推荐",
                "scope": "unrestricted",
                "time_range": "year",
            }
        )

        self.assertEqual(len(transport.calls), 2)

    def test_normal_google_content_page_is_not_treated_as_redirect(self) -> None:
        transport = RecordingTransport(
            {
                "results": [
                    {
                        "title": "Google AI",
                        "url": "https://www.google.com/about/ai/",
                        "content": "official content",
                    }
                ]
            }
        )

        result = json.loads(
            self.build_tool(transport).execute(
                {
                    "query": "Google AI",
                    "scope": "unrestricted",
                    "max_results": 1,
                }
            )
        )

        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["url"], "https://www.google.com/about/ai/")

    def test_stale_year_failure_record_never_starts_network_execution(self) -> None:
        transport = RecordingTransport()
        tool = self.build_tool(transport)
        executor = ToolExecutor(ToolRegistry([tool]), RejectingConfirmer())

        record = executor.execute(
            ToolCallRequest(
                id="call_stale_year",
                name="web_search",
                arguments=json.dumps(
                    {
                        "query": "DeepSeek 最新新闻 2025",
                        "scope": "domestic",
                        "topic": "news",
                        "time_range": "month",
                    }
                ),
            )
        )

        self.assertEqual(record.status, ToolExecutionStatus.FAILED)
        self.assertFalse(record.execution_started)
        self.assertFalse(record.confirmation_requested)
        self.assertEqual(transport.calls, [])

    def test_cancellation_and_expired_deadline_do_not_start_request(self) -> None:
        for context in (
            ToolExecutionContext(should_cancel=lambda: True),
            ToolExecutionContext(deadline=1, clock=lambda: 1),
        ):
            with self.subTest(context=context):
                transport = RecordingTransport()
                with self.assertRaises(ToolExecutionError):
                    self.build_tool(transport).execute_with_context(
                        {"query": "latest news", "scope": "unrestricted"}, context
                    )
                self.assertEqual(transport.calls, [])

    def test_http_and_protocol_errors_are_mapped_without_leaking_key(self) -> None:
        cases = (
            (
                HTTPError(
                    "https://api.tavily.com/search",
                    401,
                    "unauthorized",
                    None,
                    None,
                ),
                "TAVILY_API_KEY",
            ),
            (
                HTTPError(
                    "https://api.tavily.com/search",
                    429,
                    "rate limited",
                    None,
                    None,
                ),
                "额度",
            ),
            (URLError("offline"), "无法连接"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                tool = self.build_tool(RecordingTransport(error=error))
                with self.assertRaisesRegex(ToolExecutionError, expected) as raised:
                    tool.execute({"query": "latest news", "scope": "unrestricted"})
                self.assertNotIn("tvly-private-test-key", str(raised.exception))

        with self.assertRaisesRegex(ToolExecutionError, "无法解析"):
            self.build_tool(RecordingTransport(response=b"not json")).execute(
                {"query": "latest news", "scope": "unrestricted"}
            )
        with self.assertRaisesRegex(ToolExecutionError, "results"):
            self.build_tool(RecordingTransport(response={})).execute(
                {"query": "latest news", "scope": "unrestricted"}
            )

    def test_registry_only_enables_web_search_when_key_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offline = build_default_registry(root)
            online = build_default_registry(
                root,
                web_search_api_key="tvly-test",
                web_search_auto_calls_per_turn=0,
                web_search_default_scope="balanced",
                web_search_domestic_results=2,
                web_search_international_results=4,
            )

        self.assertNotIn("web_search", offline.names)
        self.assertIn("web_search", online.names)
        tool = online.get("web_search")
        self.assertTrue(tool.retryable)
        self.assertFalse(tool.idempotent)
        self.assertTrue(tool.requires_confirmation)
        self.assertEqual(tool.effect, ToolEffect.EXTERNAL_SIDE_EFFECT)
        self.assertIn("domestic 默认 2 条", tool.description)
        self.assertIn("international 默认 4 条", tool.description)
        self.assertIn("第三方 Tavily", tool.confirmation_description or "")
        self.assertIn("scope", tool.parameters["required"])
        self.assertTrue(
            tool.requires_confirmation_for(
                {
                    "query": "第一个问题",
                    "international_query": "first query",
                    "scope": "balanced",
                }
            )
        )

    def test_scope_uses_independent_configured_result_counts(self) -> None:
        transport = RecordingTransport()
        tool = self.build_tool(
            transport,
            default_max_results=5,
            domestic_results=2,
            international_results=4,
        )

        domestic = json.loads(
            tool.execute({"query": "人工智能", "scope": "domestic"})
        )
        international = json.loads(
            tool.execute({"query": "artificial intelligence", "scope": "international"})
        )
        unrestricted = json.loads(
            tool.execute({"query": "AI", "scope": "unrestricted"})
        )

        self.assertEqual(
            [call[0]["max_results"] for call in transport.calls],
            [4, 8, 10],
        )
        self.assertEqual(domestic["requested_scope"], "domestic")
        self.assertEqual(international["requested_scope"], "international")
        self.assertEqual(unrestricted["requested_scope"], "unrestricted")
        self.assertEqual(transport.calls[0][0]["country"], "china")
        self.assertNotIn("country", transport.calls[1][0])

    def test_domestic_country_hint_is_only_sent_for_general_search(self) -> None:
        transport = RecordingTransport()
        tool = self.build_tool(transport)

        tool.execute(
            {"query": "人工智能新闻", "scope": "domestic", "topic": "news"}
        )

        self.assertNotIn("country", transport.calls[0][0])

    def test_balanced_search_runs_two_scopes_in_parallel(self) -> None:
        transport = ParallelTransport()
        tool = self.build_tool(
            transport,  # type: ignore[arg-type]
            domestic_results=2,
            international_results=4,
            domestic_domains=("Gov.cn",),
            international_domains=("Reuters.com",),
            excluded_domains=("spam.example",),
        )

        result = json.loads(
            tool.execute(
                {
                    "query": "人工智能进展",
                    "international_query": "artificial intelligence progress",
                    "scope": "balanced",
                    "topic": "general",
                }
            )
        )

        self.assertEqual(result["requested_scope"], "balanced")
        self.assertEqual(result["provider_request_count"], 2)
        self.assertEqual(result["result_count"], 2)
        self.assertFalse(result["partial_failure"])
        self.assertEqual(set(result["groups"]), {"domestic", "international"})
        requests = {str(request["query"]): request for request in transport.calls}
        domestic = requests["人工智能进展"]
        international = requests["artificial intelligence progress"]
        self.assertEqual(domestic["max_results"], 4)
        self.assertEqual(domestic["include_domains"], ["gov.cn"])
        self.assertNotIn("country", domestic)
        self.assertEqual(international["max_results"], 8)
        self.assertEqual(international["include_domains"], ["reuters.com"])
        self.assertEqual(domestic["exclude_domains"], ["spam.example"])
        self.assertEqual(international["exclude_domains"], ["spam.example"])

    def test_balanced_search_keeps_successful_group_on_partial_failure(self) -> None:
        transport = ParallelTransport(
            failing_queries={"artificial intelligence progress"}
        )
        tool = self.build_tool(transport)  # type: ignore[arg-type]

        result = json.loads(
            tool.execute(
                {
                    "query": "人工智能进展",
                    "international_query": "artificial intelligence progress",
                    "scope": "balanced",
                }
            )
        )

        self.assertTrue(result["partial_failure"])
        self.assertEqual(set(result["groups"]), {"domestic"})
        self.assertEqual(set(result["errors"]), {"international"})
        self.assertEqual(result["result_count"], 1)

    def test_balanced_search_fails_only_when_both_requests_fail(self) -> None:
        transport = ParallelTransport(
            failing_queries={
                "人工智能进展",
                "artificial intelligence progress",
            }
        )
        tool = self.build_tool(transport)  # type: ignore[arg-type]

        with self.assertRaisesRegex(ToolExecutionError, "均失败"):
            tool.execute(
                {
                    "query": "人工智能进展",
                    "international_query": "artificial intelligence progress",
                    "scope": "balanced",
                }
            )

    def test_balanced_search_uses_two_automatic_request_credits(self) -> None:
        tool = self.build_tool(RecordingTransport(), auto_calls_per_turn=2)

        self.assertFalse(
            tool.requires_confirmation_for(
                {
                    "query": "人工智能进展",
                    "international_query": "artificial intelligence progress",
                    "scope": "balanced",
                }
            )
        )
        self.assertTrue(
            tool.requires_confirmation_for(
                {"query": "another target", "scope": "unrestricted"}
            )
        )

    def test_same_query_in_different_scopes_is_not_duplicate(self) -> None:
        tool = self.build_tool(
            RecordingTransport(),
            auto_calls_per_turn=3,
        )

        self.assertFalse(
            tool.requires_confirmation_for(
                {"query": "DeepSeek", "scope": "domestic"}
            )
        )
        self.assertFalse(
            tool.requires_confirmation_for(
                {"query": "DeepSeek", "scope": "international"}
            )
        )
        self.assertTrue(
            tool.requires_confirmation_for(
                {"query": " deepseek ", "scope": "domestic"}
            )
        )

    def test_confirmation_is_dynamic_and_begin_turn_resets_usage(self) -> None:
        tool = self.build_tool(RecordingTransport(), auto_calls_per_turn=2)

        self.assertFalse(
            tool.requires_confirmation_for(
                {"query": "first query", "scope": "unrestricted"}
            )
        )
        self.assertFalse(
            tool.requires_confirmation_for(
                {"query": "second query", "scope": "unrestricted"}
            )
        )
        self.assertTrue(
            tool.requires_confirmation_for(
                {"query": "third query", "scope": "unrestricted"}
            )
        )

        tool.begin_turn()

        self.assertFalse(
            tool.requires_confirmation_for(
                {"query": "First   Query", "scope": "unrestricted", "topic": "news"}
            )
        )
        self.assertFalse(
            tool.requires_confirmation_for(
                {"query": "first query", "scope": "unrestricted", "topic": "general"}
            )
        )

        tool.begin_turn()

        self.assertFalse(
            tool.requires_confirmation_for(
                {"query": "First   Query", "scope": "unrestricted", "topic": "news"}
            )
        )
        self.assertTrue(
            tool.requires_confirmation_for(
                {"query": " first query ", "scope": "unrestricted", "topic": "news"}
            )
        )

    def test_sensitive_search_query_is_rejected_before_network_access(self) -> None:
        sensitive_queries = (
            "find sk-abcdefghijklmnop",
            "use Bearer abcdefghijklmnop",
            "password=correct-horse-battery-staple",
            "-----BEGIN PRIVATE KEY-----",
        )
        for query in sensitive_queries:
            with self.subTest(query=query):
                transport = RecordingTransport()
                tool = self.build_tool(transport)
                with self.assertRaisesRegex(ToolExecutionError, "疑似包含"):
                    tool.requires_confirmation_for(
                        {"query": query, "scope": "unrestricted"}
                    )
                self.assertEqual(transport.calls, [])

        placeholder_tool = self.build_tool(RecordingTransport())
        self.assertFalse(
            placeholder_tool.requires_confirmation_for(
                {
                    "query": "how to set api_key=replace_with_your_api_key",
                    "scope": "unrestricted",
                }
            )
        )
        international_transport = RecordingTransport()
        with self.assertRaisesRegex(ToolExecutionError, "international_query"):
            self.build_tool(international_transport).requires_confirmation_for(
                {
                    "query": "普通查询",
                    "international_query": "use sk-abcdefghijklmnop",
                    "scope": "balanced",
                }
            )
        self.assertEqual(international_transport.calls, [])

    def test_auto_call_limit_must_be_bounded_integer(self) -> None:
        for value in (-1, 6, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "auto_calls_per_turn"):
                    self.build_tool(
                        RecordingTransport(),
                        auto_calls_per_turn=value,
                    )

    def test_scope_configuration_must_be_valid(self) -> None:
        cases = (
            ({"default_scope": "worldwide"}, "default_scope"),
            ({"minimum_score": -0.1}, "minimum_score"),
            ({"minimum_score": 1.1}, "minimum_score"),
            ({"minimum_score": float("nan")}, "minimum_score"),
            ({"minimum_score": True}, "minimum_score"),
            ({"domestic_results": 0}, "domestic_results"),
            ({"international_results": 11}, "international_results"),
            ({"domestic_domains": ("https://example.com",)}, "domestic_domains"),
            ({"international_domains": "example.com"}, "international_domains"),
            (
                {
                    "domestic_domains": ("example.com",),
                    "excluded_domains": ("example.com",),
                },
                "同时出现在",
            ),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, message):
                    self.build_tool(RecordingTransport(), **overrides)

    def test_rejected_confirmation_never_sends_search_request(self) -> None:
        transport = RecordingTransport()
        tool = self.build_tool(transport)
        confirmer = RejectingConfirmer()
        executor = ToolExecutor(ToolRegistry([tool]), confirmer)

        for index in range(2):
            automatic = executor.execute(
                ToolCallRequest(
                    id=f"call_auto_{index}",
                    name="web_search",
                    arguments=json.dumps(
                        {
                            "query": f"automatic query {index}",
                            "scope": "unrestricted",
                        }
                    ),
                )
            )
            self.assertEqual(automatic.status, ToolExecutionStatus.SUCCEEDED)
            self.assertFalse(automatic.confirmation_requested)

        self.assertEqual(len(transport.calls), 2)
        record = executor.execute(
            ToolCallRequest(
                id="call_search",
                name="web_search",
                arguments=json.dumps(
                    {
                        "query": "latest agent news",
                        "scope": "unrestricted",
                        "max_results": 3,
                        "topic": "news",
                    }
                ),
            )
        )

        self.assertEqual(record.status, ToolExecutionStatus.REJECTED)
        self.assertEqual(record.effect, ToolEffect.EXTERNAL_SIDE_EFFECT)
        self.assertTrue(record.confirmation_requested)
        self.assertFalse(record.confirmation_granted)
        self.assertFalse(record.execution_started)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(confirmer.arguments), 1)
        self.assertIn("latest agent news", confirmer.arguments[0])
        self.assertIn("超过本轮自动网络请求次数", confirmer.arguments[0])
        self.assertNotIn("tvly-private-test-key", confirmer.arguments[0])

    def test_duplicate_confirmation_explains_its_reason(self) -> None:
        transport = RecordingTransport()
        tool = self.build_tool(transport)
        confirmer = RejectingConfirmer()
        executor = ToolExecutor(ToolRegistry([tool]), confirmer)
        request = ToolCallRequest(
            id="call_duplicate",
            name="web_search",
            arguments=(
                '{"query":"same query","scope":"unrestricted",'
                '"topic":"general"}'
            ),
        )

        first = executor.execute(request)
        duplicate = executor.execute(request)

        self.assertEqual(first.status, ToolExecutionStatus.SUCCEEDED)
        self.assertFalse(first.confirmation_requested)
        self.assertEqual(duplicate.status, ToolExecutionStatus.REJECTED)
        self.assertTrue(duplicate.confirmation_requested)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("与本轮之前的搜索重复", confirmer.arguments[0])


if __name__ == "__main__":
    unittest.main()
