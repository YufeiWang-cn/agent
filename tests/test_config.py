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
