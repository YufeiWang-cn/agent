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
                "AGENT_COMMAND_TIMEOUT": "2.5",
            }
            with patch.dict(os.environ, environment, clear=True):
                with patch("deepseek_agent.config.load_dotenv"):
                    settings = Settings.from_env()

        self.assertEqual(settings.max_agent_steps, 7)
        self.assertEqual(settings.command_timeout, 2.5)
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


if __name__ == "__main__":
    unittest.main()
