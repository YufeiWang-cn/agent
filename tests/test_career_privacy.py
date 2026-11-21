"""求职联网外发测试：使用真实客户端序列化和假的网络 transport。"""

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.career.service import CareerService
from deepseek_agent.career.store import CareerStore
from deepseek_agent.models import ToolCallRequest
from deepseek_agent.tools import (
    WebSearchTool,
    ReadWebPageTool,
    TavilySearchClient,
    TavilyExtractClient,
    ToolRegistry,
    ToolExecutionError,
)
from deepseek_agent.tool_execution import ToolExecutor, ToolExecutionStatus
from test_web_search import RecordingTransport
from test_web_page import RecordingExtractTransport


class Confirmer:
    def __init__(self, allow=True):
        self.allow = allow
        self.previews = []

    def confirm(self, tool, arguments):
        self.previews.append(json.loads(arguments))
        return self.allow


class CareerPrivacyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.search_transport = RecordingTransport()
        self.extract_transport = RecordingExtractTransport(
            {"results": [{"url": "https://example.com/jobs", "raw_content": "Python 后端岗位"}]}
        )
        self.search = WebSearchTool(
            "test", client=TavilySearchClient("test", transport=self.search_transport)
        )
        self.page = ReadWebPageTool(
            "test", client=TavilyExtractClient("test", transport=self.extract_transport)
        )
        self.original = ToolRegistry([self.search, self.page])
        self.registry = CareerService(
            CareerStore(root / "profile.sqlite3"), root, 100_000
        ).registry(self.original)
        self.confirmer = Confirmer()
        self.executor = ToolExecutor(self.registry, self.confirmer)

    def run_tool(self, name, **arguments):
        return self.executor.execute(ToolCallRequest("test", name, json.dumps(arguments)))

    def test_phone_in_search_never_reaches_provider_even_if_confirmed(self):
        result = self.run_tool(
            "web_search", query="为手机号 13800138000 的学生找工作", scope="domestic"
        )
        self.assertEqual(result.status, ToolExecutionStatus.FAILED)
        self.assertEqual(self.search_transport.calls, [])
        self.assertNotIn("13800138000", result.model_result)

    def test_email_in_web_question_never_reaches_provider(self):
        result = self.run_tool(
            "read_web_page",
            urls=["https://example.com/jobs"],
            question="给 student@example.org 匹配岗位",
        )
        self.assertEqual(result.status, ToolExecutionStatus.FAILED)
        self.assertEqual(self.extract_transport.calls, [])

    def test_rejected_normal_query_makes_no_request(self):
        self.confirmer.allow = False
        result = self.run_tool("web_search", query="北京 Python 后端实习", scope="domestic")
        self.assertEqual(result.status, ToolExecutionStatus.REJECTED)
        self.assertEqual(self.search_transport.calls, [])

    def test_international_query_url_and_encoded_values_are_checked(self):
        for secret in (
            "student@example.org",
            "13800138000",
            "110101199001011234",
            "１３８００１３８０００",
            "138\u200b00138000",
            "api_key=abcdefg123456789",
        ):
            with self.subTest(secret=secret):
                self.run_tool(
                    "web_search", query="北京 后端", international_query=secret, scope="balanced"
                )
                self.run_tool(
                    "read_web_page",
                    urls=["https://example.com/jobs?q=" + quote(quote(secret, safe=""), safe="")],
                    question="岗位要求",
                )
        self.assertEqual(self.search_transport.calls, [])
        self.assertEqual(self.extract_transport.calls, [])
        self.assertEqual(self.confirmer.previews, [])

    def test_approved_normal_queries_show_destinations_and_send_exact_text(self):
        result = self.run_tool("web_search", query="北京 Python 后端实习", scope="domestic")
        self.assertEqual(result.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(self.search_transport.calls[0][0]["query"], "北京 Python 后端实习")
        preview = self.confirmer.previews[0]
        self.assertIn("Tavily", preview["recipient"])
        self.assertEqual(preview["arguments"]["query"], self.search_transport.calls[0][0]["query"])
        result = self.run_tool(
            "read_web_page", urls=["https://example.com/jobs"], question="岗位技能要求"
        )
        self.assertEqual(result.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(self.extract_transport.calls[0][0]["query"], "岗位技能要求")
        self.assertEqual(len(self.confirmer.previews), 2)

    def test_every_call_requires_confirmation_and_general_mode_is_unchanged(self):
        args = dict(query="Python 后端实习", scope="domestic")
        self.assertFalse(self.original.get("web_search").requires_confirmation_for(args))
        self.run_tool("web_search", **args)
        self.registry.begin_turn()
        self.confirmer.allow = False
        self.assertEqual(self.run_tool("web_search", **args).status, ToolExecutionStatus.REJECTED)
        self.assertEqual(len(self.search_transport.calls), 1)

    def test_execution_rechecks_sensitive_data_and_rejects_unknown_fields(self):
        tool = self.registry.get("web_search")
        with self.assertRaises(ToolExecutionError):
            tool.execute(dict(query="13800138000", scope="domestic"))
        result = self.run_tool("web_search", query="Python", scope="domestic", resume="简历内容")
        self.assertEqual(result.status, ToolExecutionStatus.FAILED)
        self.assertEqual(self.search_transport.calls, [])

    def test_cancel_after_confirmation_sends_nothing(self):
        result = self.executor.execute(
            ToolCallRequest(
                "cancel", "web_search", json.dumps(dict(query="Python", scope="domestic"))
            ),
            should_cancel=lambda: bool(self.confirmer.previews),
        )
        self.assertFalse(result.status.succeeded)
        self.assertEqual(self.search_transport.calls, [])

    def test_oversized_confirmation_is_rejected_instead_of_hidden_by_clipping(self):
        result = self.run_tool(
            "read_web_page", urls=["https://example.com/" + ("a" * 1800)], question="岗位要求"
        )
        self.assertEqual(result.status, ToolExecutionStatus.FAILED)
        self.assertEqual(self.extract_transport.calls, [])

    def test_balanced_approval_covers_both_actual_queries(self):
        result = self.run_tool(
            "web_search",
            query="北京 后端实习",
            international_query="Beijing backend internships",
            scope="balanced",
        )
        self.assertEqual(result.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(len(self.confirmer.previews), 1)
        args = self.confirmer.previews[0]["arguments"]
        self.assertEqual(
            {call[0]["query"] for call in self.search_transport.calls},
            {args["query"], args["international_query"]},
        )

    def test_public_technical_terms_and_year_are_not_blocked(self):
        result = self.run_tool(
            "web_search", query="Python 3.12 FastAPI 2026 后端实习", scope="domestic"
        )
        self.assertEqual(result.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(len(self.confirmer.previews), 1)

    def test_adapter_preserves_execution_limits_and_schema_isolation(self):
        wrapped = self.registry.get("read_web_page")
        self.assertEqual(wrapped.timeout_seconds, self.page.timeout_seconds)
        self.assertEqual(wrapped.retryable, self.page.retryable)
        self.assertEqual(wrapped.idempotent, self.page.idempotent)
        wrapped.schema["function"]["parameters"]["properties"].clear()
        self.assertIn("urls", wrapped.schema["function"]["parameters"]["properties"])
        self.assertIn("urls", self.page.parameters["properties"])


if __name__ == "__main__":
    unittest.main()
