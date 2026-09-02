"""验证网页正文读取的请求边界、来源一致性和动态确认策略。"""

import json
import unittest
from collections.abc import Mapping
from urllib.error import HTTPError, URLError

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.tools import (
    ReadWebPageTool,
    TavilyExtractClient,
    ToolEffect,
    ToolExecutionContext,
    ToolExecutionError,
    build_default_registry,
)
from deepseek_agent.tools.web_sources import source_id_for_url


class RecordingExtractTransport:
    def __init__(
        self,
        response: object | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response if response is not None else {"results": []}
        self.error = error
        self.calls: list[tuple[dict[str, object], dict[str, str], float]] = []

    def __call__(
        self,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(
            (json.loads(body.decode("utf-8")), dict(headers), timeout_seconds)
        )
        if self.error is not None:
            raise self.error
        if isinstance(self.response, bytes):
            return self.response
        return json.dumps(self.response, ensure_ascii=False).encode("utf-8")


class ReadWebPageToolTests(unittest.TestCase):
    def build_tool(
        self,
        transport: RecordingExtractTransport,
        **kwargs: object,
    ) -> ReadWebPageTool:
        client = TavilyExtractClient(
            "tvly-private-test-key",
            transport=transport,
        )
        return ReadWebPageTool(
            "unused-because-client-is-injected",
            client=client,
            **kwargs,
        )

    def test_extracts_focused_content_with_bounded_protocol(self) -> None:
        transport = RecordingExtractTransport(
            {
                "request_id": "extract-1",
                "response_time": 0.4,
                "results": [
                    {
                        "url": "https://official.example.com/article",
                        "raw_content": "A" * 200,
                    }
                ],
                "failed_results": [
                    {
                        "url": "https://news.example.org/report",
                        "error": "page unavailable",
                    }
                ],
            }
        )
        tool = self.build_tool(
            transport,
            timeout_seconds=18,
            chunks_per_source=4,
            extract_depth="advanced",
            max_content_chars=120,
        )

        result = json.loads(
            tool.execute(
                {
                    "urls": [
                        "https://official.example.com/article#details",
                        "https://news.example.org/report",
                    ],
                    "question": "这两个来源是否支持产品已经发布？",
                }
            )
        )

        self.assertEqual(result["provider"], "tavily")
        self.assertEqual(result["provider_request_count"], 1)
        self.assertEqual(result["requested_page_count"], 2)
        self.assertEqual(result["page_count"], 1)
        self.assertTrue(result["partial_failure"])
        self.assertEqual(len(result["pages"][0]["content"]), 120)
        self.assertTrue(result["pages"][0]["content_truncated"])
        self.assertEqual(
            result["pages"][0]["requested_url"],
            "https://official.example.com/article",
        )
        self.assertNotIn("tvly-private-test-key", json.dumps(result))

        request, headers, timeout = transport.calls[0]
        self.assertEqual(
            request["urls"],
            [
                "https://official.example.com/article",
                "https://news.example.org/report",
            ],
        )
        self.assertEqual(request["query"], "这两个来源是否支持产品已经发布？")
        self.assertEqual(request["chunks_per_source"], 4)
        self.assertEqual(request["extract_depth"], "advanced")
        self.assertEqual(request["format"], "markdown")
        self.assertFalse(request["include_images"])
        self.assertFalse(request["include_favicon"])
        self.assertEqual(request["timeout"], 18)
        self.assertEqual(headers["Authorization"], "Bearer tvly-private-test-key")
        self.assertEqual(timeout, 18)

    def test_rejects_unsafe_or_credential_bearing_urls_before_network(self) -> None:
        transport = RecordingExtractTransport()
        tool = self.build_tool(transport, excluded_domains=("blocked.example",))
        cases = (
            "file:///etc/passwd",
            "http://localhost/admin",
            "http://127.0.0.1/private",
            "http://[::1]/private",
            "http://169.254.169.254/latest/meta-data",
            "https://user:password@example.com/private",
            "https://example.com:8443/private",
            "https://www.google.com/url?q=https://example.com/article",
            "https://blocked.example/article",
            "https://sub.blocked.example/article",
            "https://example.com/private?access_token=very-secret-value",
        )
        for url in cases:
            with self.subTest(url=url):
                with self.assertRaises(ToolExecutionError):
                    tool.execute({"urls": [url], "question": "核对内容"})
        self.assertEqual(transport.calls, [])

    def test_rejects_sensitive_question_before_network(self) -> None:
        transport = RecordingExtractTransport()
        tool = self.build_tool(transport)

        with self.assertRaisesRegex(ToolExecutionError, "疑似包含密钥"):
            tool.execute(
                {
                    "urls": ["https://example.com/article"],
                    "question": "password=an-actual-secret-value 是否有效",
                }
            )

        self.assertEqual(transport.calls, [])

    def test_ignores_cross_domain_results_and_reports_partial_failure(self) -> None:
        transport = RecordingExtractTransport(
            {
                "results": [
                    {
                        "url": "https://example.com/final-article",
                        "raw_content": "supported content",
                    },
                    {
                        "url": "https://unexpected.example.net/injected",
                        "raw_content": "unrequested content",
                    },
                ],
                "failed_results": [],
            }
        )
        tool = self.build_tool(transport)

        result = json.loads(
            tool.execute(
                {
                    "urls": [
                        "http://www.example.com/original",
                        "https://second.example.org/report",
                    ],
                    "question": "核对发布日期",
                }
            )
        )

        self.assertEqual(result["page_count"], 1)
        self.assertTrue(result["partial_failure"])
        self.assertTrue(result["pages"][0]["redirected"])
        self.assertIn("不属于请求域名", result["failures"][0]["error"])
        self.assertNotIn("unrequested content", json.dumps(result))

    def test_source_ids_link_requested_pages_and_sanitize_provider_failures(
        self,
    ) -> None:
        transport = RecordingExtractTransport(
            {
                "request_id": "request-" + "x" * 500,
                "response_time": float("nan"),
                "results": [
                    {
                        "url": "https://official.example.com/article",
                        "raw_content": "verified content",
                    }
                ],
                "failed_results": [
                    {
                        "url": (
                            "https://evil.example.net/report?"
                            "access_token=very-secret-value"
                        ),
                        "error": "unexpected",
                    },
                    {
                        "url": "https://news.example.org/report",
                        "error": "password=provider-returned-secret-value",
                    },
                ],
            }
        )
        tool = self.build_tool(transport)
        official_url = "https://official.example.com/article"
        failed_url = "https://news.example.org/report"

        result = json.loads(
            tool.execute(
                {
                    "urls": [official_url, failed_url],
                    "question": "核对发布状态",
                }
            )
        )

        official_source_id = source_id_for_url(official_url)
        failed_source_id = source_id_for_url(failed_url)
        self.assertEqual(result["pages"][0]["source_id"], official_source_id)
        self.assertEqual(result["verified_source_ids"], [official_source_id])
        self.assertEqual(result["failed_source_ids"], [failed_source_id])
        self.assertEqual(
            result["requested_sources"],
            [
                {"source_id": official_source_id, "url": official_url},
                {"source_id": failed_source_id, "url": failed_url},
            ],
        )
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("very-secret-value", serialized)
        self.assertNotIn("provider-returned-secret-value", serialized)
        self.assertNotIn("evil.example.net", serialized)
        self.assertNotIn("response_time", result)
        self.assertEqual(len(result["request_id"]), 200)

    def test_all_failed_and_malformed_responses_are_visible_errors(self) -> None:
        failed_transport = RecordingExtractTransport(
            {
                "results": [],
                "failed_results": [
                    {"url": "https://example.com/", "error": "blocked by site"}
                ],
            }
        )
        tool = self.build_tool(failed_transport)
        with self.assertRaisesRegex(ToolExecutionError, "blocked by site"):
            tool.execute(
                {"urls": ["https://example.com"], "question": "核对内容"}
            )

        malformed_tool = self.build_tool(RecordingExtractTransport({"results": {}}))
        with self.assertRaisesRegex(ToolExecutionError, "results"):
            malformed_tool.execute(
                {"urls": ["https://example.com"], "question": "核对内容"}
            )

    def test_network_errors_are_mapped_without_leaking_credentials(self) -> None:
        cases = (
            (
                HTTPError("https://api.tavily.com/extract", 401, "", {}, None),
                "认证失败",
            ),
            (
                HTTPError("https://api.tavily.com/extract", 429, "", {}, None),
                "额度不足",
            ),
            (URLError("offline"), "无法连接"),
            (b"not-json", "无法解析"),
        )
        for response_or_error, message in cases:
            with self.subTest(message=message):
                transport = (
                    RecordingExtractTransport(response=response_or_error)
                    if isinstance(response_or_error, bytes)
                    else RecordingExtractTransport(error=response_or_error)
                )
                tool = self.build_tool(transport)
                with self.assertRaisesRegex(ToolExecutionError, message) as raised:
                    tool.execute(
                        {
                            "urls": ["https://example.com/article"],
                            "question": "核对内容",
                        }
                    )
                self.assertNotIn("tvly-private-test-key", str(raised.exception))

    def test_deadline_and_cancellation_are_enforced(self) -> None:
        response = {
            "results": [
                {"url": "https://example.com/", "raw_content": "content"}
            ]
        }
        transport = RecordingExtractTransport(response)
        tool = self.build_tool(transport, timeout_seconds=20)

        tool.execute_with_context(
            {"urls": ["https://example.com"], "question": "核对内容"},
            ToolExecutionContext(deadline=10, clock=lambda: 7),
        )
        self.assertEqual(transport.calls[0][2], 3)

        with self.assertRaisesRegex(ToolExecutionError, "超过执行期限"):
            tool.execute_with_context(
                {"urls": ["https://example.com"], "question": "核对内容"},
                ToolExecutionContext(deadline=7, clock=lambda: 7),
            )
        with self.assertRaisesRegex(ToolExecutionError, "已取消"):
            tool.execute_with_context(
                {"urls": ["https://example.com"], "question": "核对内容"},
                ToolExecutionContext(should_cancel=lambda: True),
            )

    def test_repeat_or_excessive_page_reads_require_confirmation(self) -> None:
        transport = RecordingExtractTransport()
        tool = self.build_tool(transport, auto_pages_per_turn=2)
        first = {
            "urls": [
                "https://one.example.com/article",
                "https://two.example.org/report",
            ],
            "question": "核对结论",
        }
        self.assertFalse(tool.requires_confirmation_for(first))
        self.assertTrue(
            tool.requires_confirmation_for(
                {
                    "urls": ["https://one.example.com/article"],
                    "question": "再次核对",
                }
            )
        )
        preview = json.loads(tool.confirmation_arguments_for(first, "{}"))
        self.assertIn("超过本轮自动读取页面数", preview["confirmation_reason"])
        self.assertIn("已经读取过", preview["confirmation_reason"])

        tool.begin_turn()
        self.assertFalse(tool.requires_confirmation_for(first))

    def test_registry_enables_search_and_page_read_together(self) -> None:
        offline = build_default_registry(web_search_api_key=None)
        online = build_default_registry(
            web_search_api_key="tvly-test",
            web_page_max_pages_per_call=2,
            web_page_auto_pages_per_turn=1,
        )

        self.assertNotIn("read_web_page", offline.names)
        self.assertIn("web_search", online.names)
        self.assertIn("read_web_page", online.names)
        tool = online.get("read_web_page")
        self.assertEqual(tool.effect, ToolEffect.EXTERNAL_SIDE_EFFECT)
        self.assertIn("1 到 2", tool.description)


if __name__ == "__main__":
    unittest.main()
