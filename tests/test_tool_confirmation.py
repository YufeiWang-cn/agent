"""验证工具确认策略和命令行交互行为。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent
from deepseek_agent.config import Settings
from deepseek_agent.memory import JsonSessionStore
from deepseek_agent.models import ToolCallRequest
from deepseek_agent.permissions import ConsoleToolConfirmer
from deepseek_agent.tools import Tool, ToolRegistry
from deepseek_agent.tools.base import JsonObject


class NoCallModel:
    model_name = "no-call-model"

    def stream(self, messages, tools):
        raise AssertionError("本测试不应调用模型")


class RecordingTool(Tool):
    name = "recording_tool"
    description = "记录是否真正执行。"
    parameters: JsonObject = {"type": "object", "properties": {}}

    def __init__(self, requires_confirmation: bool) -> None:
        self.requires_confirmation = requires_confirmation
        self.executions = 0

    def execute(self, arguments: JsonObject) -> str:
        self.executions += 1
        return "执行成功"


class FakeConfirmer:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str]] = []

    def confirm(self, tool: Tool, arguments: str) -> bool:
        self.calls.append((tool.name, arguments))
        return self.allowed


class ToolConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="no-call-model",
            system_prompt="system",
        )
        self.request = ToolCallRequest(
            id="call_1",
            name="recording_tool",
            arguments="{}",
        )

    def test_confirmed_tool_is_executed(self) -> None:
        tool = RecordingTool(requires_confirmation=True)
        confirmer = FakeConfirmer(allowed=True)
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(
                self.settings,
                model=NoCallModel(),
                tools=ToolRegistry([tool]),
                session_store=JsonSessionStore(Path(directory)),
                confirmer=confirmer,
            )

            agent._execute_tools([self.request])

            self.assertEqual(tool.executions, 1)
            self.assertEqual(confirmer.calls, [("recording_tool", "{}")])
            self.assertEqual(agent._conversation.messages[-1]["content"], "执行成功")

    def test_rejected_tool_is_not_executed_and_result_is_recorded(self) -> None:
        tool = RecordingTool(requires_confirmation=True)
        confirmer = FakeConfirmer(allowed=False)
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(
                self.settings,
                model=NoCallModel(),
                tools=ToolRegistry([tool]),
                session_store=JsonSessionStore(Path(directory)),
                confirmer=confirmer,
            )

            agent._execute_tools([self.request])

            self.assertEqual(tool.executions, 0)
            self.assertEqual(
                agent._conversation.messages[-1]["content"],
                "用户拒绝执行该工具。",
            )

    def test_safe_tool_does_not_request_confirmation(self) -> None:
        tool = RecordingTool(requires_confirmation=False)
        confirmer = FakeConfirmer(allowed=False)
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(
                self.settings,
                model=NoCallModel(),
                tools=ToolRegistry([tool]),
                session_store=JsonSessionStore(Path(directory)),
                confirmer=confirmer,
            )

            agent._execute_tools([self.request])

            self.assertEqual(tool.executions, 1)
            self.assertEqual(confirmer.calls, [])

    def test_console_confirmer_accepts_yes_after_invalid_input(self) -> None:
        tool = RecordingTool(requires_confirmation=True)
        confirmer = ConsoleToolConfirmer()
        with patch("builtins.input", side_effect=["maybe", "yes"]):
            self.assertTrue(confirmer.confirm(tool, "{}"))

    def test_console_confirmer_rejects_empty_input(self) -> None:
        tool = RecordingTool(requires_confirmation=True)
        confirmer = ConsoleToolConfirmer()
        with patch("builtins.input", return_value=""):
            self.assertFalse(confirmer.confirm(tool, "{}"))

    def test_console_confirmer_rejects_interruption(self) -> None:
        tool = RecordingTool(requires_confirmation=True)
        confirmer = ConsoleToolConfirmer()
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertFalse(confirmer.confirm(tool, "{}"))

    def test_console_confirmer_displays_patch_diff_as_plain_text(self) -> None:
        tool = RecordingTool(requires_confirmation=True)
        confirmer = ConsoleToolConfirmer()
        output = io.StringIO()
        arguments = (
            '{"changes": [], "diff": '
            '"--- a/app.py\\n+++ b/app.py\\n-old\\n+new\\n"}'
        )

        with contextlib.redirect_stdout(output):
            with patch("builtins.input", return_value=""):
                self.assertFalse(confirmer.confirm(tool, arguments))

        rendered = output.getvalue()
        self.assertIn("修改差异：\n--- a/app.py\n+++ b/app.py", rendered)
        self.assertNotIn('"diff"', rendered)


if __name__ == "__main__":
    unittest.main()
