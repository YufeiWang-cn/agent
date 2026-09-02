"""验证环境变量配置的统一类型转换和范围检查。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.config import Settings


class SettingsTests(unittest.TestCase):
    """验证配置帮助方法不会改变 Settings 的公开加载行为。"""

    def test_from_env_loads_valid_numeric_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = {
                "DEEPSEEK_API_KEY": "test-key",
                "AGENT_WORKSPACE": temporary_directory,
                "AGENT_MAX_STEPS": "7",
                "AGENT_MAX_FINALIZATION_STEPS": "3",
                "AGENT_COMMAND_TIMEOUT": "2.5",
                "AGENT_COMMAND_EXECUTION_MODE": "docker",
                "AGENT_COMMAND_CONTAINER_IMAGE": "example/python:test",
            }
            with patch.dict(os.environ, environment, clear=True):
                with patch("deepseek_agent.config.load_dotenv"):
                    settings = Settings.from_env()

        self.assertEqual(settings.max_agent_steps, 7)
        self.assertEqual(settings.max_finalization_steps, 3)
        self.assertEqual(settings.command_timeout, 2.5)
        self.assertEqual(settings.command_execution_mode, "docker")
        self.assertEqual(settings.command_container_image, "example/python:test")
        self.assertEqual(settings.workspace_root, Path(temporary_directory).resolve())
        self.assertEqual(settings.web_search_default_scope, "balanced")
        self.assertEqual(settings.web_search_min_score, 0.25)
        self.assertEqual(settings.web_search_domestic_results, 3)
        self.assertEqual(settings.web_search_international_results, 3)
        self.assertEqual(settings.web_search_domestic_domains, ())
        self.assertEqual(settings.web_search_international_domains, ())
        self.assertEqual(settings.web_search_excluded_domains, ())
        self.assertEqual(settings.web_page_timeout, 20)
        self.assertEqual(settings.web_page_max_pages_per_call, 4)
        self.assertEqual(settings.web_page_auto_pages_per_turn, 4)
        self.assertEqual(settings.web_page_chunks_per_source, 3)
        self.assertEqual(settings.web_page_extract_depth, "basic")
        self.assertEqual(settings.web_page_max_content_chars, 6_000)

    def test_from_env_rejects_invalid_integer(self) -> None:
        environment = {
            "DEEPSEEK_API_KEY": "test-key",
            "AGENT_MAX_STEPS": "invalid",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch("deepseek_agent.config.load_dotenv"):
                with self.assertRaisesRegex(RuntimeError, "AGENT_MAX_STEPS"):
                    Settings.from_env()

    def test_from_env_rejects_example_api_key_placeholder(self) -> None:
        environment = {"DEEPSEEK_API_KEY": "replace_with_your_api_key"}
        with patch.dict(os.environ, environment, clear=True):
            with patch("deepseek_agent.config.load_dotenv"):
                with self.assertRaisesRegex(RuntimeError, "占位值"):
                    Settings.from_env()

    def test_from_env_loads_optional_web_search_configuration(self) -> None:
        environment = {
            "DEEPSEEK_API_KEY": "test-key",
            "TAVILY_API_KEY": "tvly-real-key",
            "AGENT_WEB_SEARCH_TIMEOUT": "12.5",
            "AGENT_WEB_SEARCH_MAX_RESULTS": "7",
            "AGENT_WEB_SEARCH_MIN_SCORE": "0.4",
            "AGENT_WEB_SEARCH_AUTO_CALLS_PER_TURN": "3",
            "AGENT_WEB_SEARCH_DEFAULT_SCOPE": "international",
            "AGENT_WEB_SEARCH_DOMESTIC_RESULTS": "2",
            "AGENT_WEB_SEARCH_INTERNATIONAL_RESULTS": "6",
            "AGENT_WEB_SEARCH_DOMESTIC_DOMAINS": "Gov.cn, xinhuanet.com,gov.cn",
            "AGENT_WEB_SEARCH_INTERNATIONAL_DOMAINS": "Reuters.com,apnews.com",
            "AGENT_WEB_SEARCH_EXCLUDED_DOMAINS": "spam.example",
            "AGENT_WEB_PAGE_TIMEOUT": "9.5",
            "AGENT_WEB_PAGE_MAX_PAGES_PER_CALL": "3",
            "AGENT_WEB_PAGE_AUTO_PAGES_PER_TURN": "7",
            "AGENT_WEB_PAGE_CHUNKS_PER_SOURCE": "5",
            "AGENT_WEB_PAGE_EXTRACT_DEPTH": "advanced",
            "AGENT_WEB_PAGE_MAX_CONTENT_CHARS": "9000",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch("deepseek_agent.config.load_dotenv"):
                settings = Settings.from_env()

        self.assertEqual(settings.tavily_api_key, "tvly-real-key")
        self.assertEqual(settings.web_search_timeout, 12.5)
        self.assertEqual(settings.web_search_max_results, 7)
        self.assertEqual(settings.web_search_min_score, 0.4)
        self.assertEqual(settings.web_search_auto_calls_per_turn, 3)
        self.assertEqual(settings.web_search_default_scope, "international")
        self.assertEqual(settings.web_search_domestic_results, 2)
        self.assertEqual(settings.web_search_international_results, 6)
        self.assertEqual(
            settings.web_search_domestic_domains,
            ("gov.cn", "xinhuanet.com"),
        )
        self.assertEqual(
            settings.web_search_international_domains,
            ("reuters.com", "apnews.com"),
        )
        self.assertEqual(settings.web_search_excluded_domains, ("spam.example",))
        self.assertEqual(settings.web_page_timeout, 9.5)
        self.assertEqual(settings.web_page_max_pages_per_call, 3)
        self.assertEqual(settings.web_page_auto_pages_per_turn, 7)
        self.assertEqual(settings.web_page_chunks_per_source, 5)
        self.assertEqual(settings.web_page_extract_depth, "advanced")
        self.assertEqual(settings.web_page_max_content_chars, 9_000)

    def test_from_env_rejects_tavily_placeholder_and_excessive_results(self) -> None:
        cases = (
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "TAVILY_API_KEY": "replace_with_your_tavily_api_key",
                },
                "TAVILY_API_KEY",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_SEARCH_MAX_RESULTS": "11",
                },
                "不能大于 10",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_SEARCH_AUTO_CALLS_PER_TURN": "6",
                },
                "不能大于 5",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_SEARCH_MIN_SCORE": "1.1",
                },
                "AGENT_WEB_SEARCH_MIN_SCORE",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_SEARCH_MIN_SCORE": "nan",
                },
                "AGENT_WEB_SEARCH_MIN_SCORE",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_SEARCH_DEFAULT_SCOPE": "worldwide",
                },
                "AGENT_WEB_SEARCH_DEFAULT_SCOPE",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_SEARCH_DOMESTIC_RESULTS": "11",
                },
                "不能大于 10",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_SEARCH_INTERNATIONAL_RESULTS": "0",
                },
                "必须是正整数",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_SEARCH_DOMESTIC_DOMAINS": "https://gov.cn/news",
                },
                "只填写域名",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_SEARCH_DOMESTIC_DOMAINS": "example.com",
                    "AGENT_WEB_SEARCH_EXCLUDED_DOMAINS": "example.com",
                },
                "同时出现在",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_PAGE_MAX_PAGES_PER_CALL": "5",
                },
                "AGENT_WEB_PAGE_MAX_PAGES_PER_CALL 不能大于 4",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_PAGE_AUTO_PAGES_PER_TURN": "13",
                },
                "AGENT_WEB_PAGE_AUTO_PAGES_PER_TURN 不能大于 12",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_PAGE_CHUNKS_PER_SOURCE": "6",
                },
                "AGENT_WEB_PAGE_CHUNKS_PER_SOURCE 不能大于 5",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_PAGE_EXTRACT_DEPTH": "deep",
                },
                "AGENT_WEB_PAGE_EXTRACT_DEPTH 只能是",
            ),
            (
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "AGENT_WEB_PAGE_MAX_CONTENT_CHARS": "20001",
                },
                "AGENT_WEB_PAGE_MAX_CONTENT_CHARS 不能大于 20000",
            ),
        )
        for environment, message in cases:
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    with patch("deepseek_agent.config.load_dotenv"):
                        with self.assertRaisesRegex(RuntimeError, message):
                            Settings.from_env()

    def test_from_env_rejects_negative_finalization_steps(self) -> None:
        environment = {
            "DEEPSEEK_API_KEY": "test-key",
            "AGENT_MAX_FINALIZATION_STEPS": "-1",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch("deepseek_agent.config.load_dotenv"):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "AGENT_MAX_FINALIZATION_STEPS",
                ):
                    Settings.from_env()

    def test_from_env_rejects_unknown_command_execution_mode(self) -> None:
        environment = {
            "DEEPSEEK_API_KEY": "test-key",
            "AGENT_COMMAND_EXECUTION_MODE": "automatic",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch("deepseek_agent.config.load_dotenv"):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "AGENT_COMMAND_EXECUTION_MODE",
                ):
                    Settings.from_env()


if __name__ == "__main__":
    unittest.main()
