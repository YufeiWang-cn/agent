"""验证命令行命令分发和失败状态处理。"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent
from deepseek_agent.cli import CliApplication
from deepseek_agent.config import Settings
from deepseek_agent.memory import JsonSessionStore
from deepseek_agent.models import TextDelta
from deepseek_agent.observability import RuntimeMetrics


class AnswerModel:
    model_name = "answer-model"

    def stream(self, _messages, _tools):
        return [TextDelta("回答")]


class CliApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = JsonSessionStore(Path(self.temporary_directory.name))
        self.settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="answer-model",
            system_prompt="system",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def build_application(
        self,
        *,
        metrics: RuntimeMetrics | None = None,
    ) -> tuple[Agent, CliApplication]:
        agent = Agent(
            self.settings,
            model=AnswerModel(),
            session_store=self.store,
            metrics=metrics,
        )
        return agent, CliApplication(agent)

    def test_clear_command_uses_transactional_public_method(self) -> None:
        agent, application = self.build_application()
        agent.chat("应保留的问题")
        history_before = agent.history()

        with patch.object(
            self.store,
            "save",
            side_effect=OSError("disk full"),
        ):
            with redirect_stdout(io.StringIO()):
                handled = application.handle_command("/clear")

        self.assertTrue(handled)
        self.assertEqual(agent.history(), history_before)

    def test_new_command_keeps_current_session_when_save_fails(self) -> None:
        agent, application = self.build_application()
        agent.chat("原会话")
        session_id = agent.session_id
        history_before = agent.history()

        with patch.object(
            self.store,
            "save",
            side_effect=OSError("disk full"),
        ):
            with redirect_stdout(io.StringIO()):
                handled = application.handle_command("/new")

        self.assertTrue(handled)
        self.assertEqual(agent.session_id, session_id)
        self.assertEqual(agent.history(), history_before)

    def test_stats_command_prints_runtime_metrics(self) -> None:
        metrics = RuntimeMetrics(
            model_requests=2,
            api_attempts=3,
            successful_requests=1,
            failed_requests=1,
            retries=1,
            total_duration_seconds=4.0,
            input_tokens=120,
            output_tokens=30,
            usage_api_reports=1,
        )
        _agent, application = self.build_application(metrics=metrics)
        output = io.StringIO()

        with redirect_stdout(output):
            handled = application.handle_command("/stats")

        self.assertTrue(handled)
        self.assertIn("模型请求数：2", output.getvalue())
        self.assertIn("API 尝试次数：3", output.getvalue())
        self.assertIn("自动重试次数：1", output.getvalue())
        self.assertIn("输入 Token：120", output.getvalue())
        self.assertIn("输出 Token：30", output.getvalue())
        self.assertIn("Token 来源：api", output.getvalue())
        self.assertIn("平均请求耗时：2.000 秒", output.getvalue())

    def test_regular_text_is_not_treated_as_command(self) -> None:
        _agent, application = self.build_application()

        self.assertFalse(application.handle_command("普通问题"))


if __name__ == "__main__":
    unittest.main()
