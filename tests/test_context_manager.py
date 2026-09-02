"""验证上下文预算裁剪不会破坏完整对话轮次。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent
from deepseek_agent.config import Settings
from deepseek_agent.context import (
    ContextManager,
    estimate_messages_tokens,
)
from deepseek_agent.conversation import Message
from deepseek_agent.memory import JsonSessionStore
from deepseek_agent.models import StreamEvent, TextDelta


class RecordingModel:
    model_name = "recording-model"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def stream(self, messages, tools) -> list[StreamEvent]:
        self.calls.append(list(messages))
        return [TextDelta("新回答")]


class ContextManagerTests(unittest.TestCase):
    def test_all_messages_are_kept_when_they_fit(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好"},
        ]
        manager = ContextManager(max_tokens=1_000)

        window = manager.prepare(messages)

        self.assertEqual(window.messages, messages)
        self.assertEqual(window.omitted_messages, 0)
        self.assertFalse(window.was_truncated)

    def test_old_turns_are_removed_but_latest_tool_turn_stays_complete(self) -> None:
        system_message = {"role": "system", "content": "system"}
        old_turn = [
            {"role": "user", "content": "旧问题" * 500},
            {"role": "assistant", "content": "旧回答" * 500},
        ]
        latest_turn = [
            {"role": "user", "content": "现在几点？"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_current_time",
                            "arguments": '{"timezone":"Asia/Shanghai"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "get_current_time",
                "content": "2026-07-20 12:00:00",
            },
        ]
        required_messages = [system_message, *latest_turn]
        manager = ContextManager(
            max_tokens=estimate_messages_tokens(required_messages)
        )

        window = manager.prepare([system_message, *old_turn, *latest_turn])

        self.assertEqual(window.messages, required_messages)
        self.assertEqual(
            [message["role"] for message in window.messages],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(window.omitted_messages, 2)
        self.assertTrue(window.was_truncated)

    def test_latest_turn_is_kept_even_when_it_exceeds_the_budget(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "很长的问题" * 500},
        ]
        manager = ContextManager(max_tokens=10)

        window = manager.prepare(messages)

        self.assertEqual(window.messages, messages)
        self.assertGreater(window.estimated_tokens, manager.max_tokens)

    def test_agent_sends_trimmed_context_but_saves_full_history(self) -> None:
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="recording-model",
            system_prompt="system",
            max_context_tokens=100,
        )
        model = RecordingModel()
        with tempfile.TemporaryDirectory() as directory:
            store = JsonSessionStore(Path(directory))
            agent = Agent(settings, model=model, session_store=store)
            agent._conversation.add_user("旧问题" * 500)
            agent._conversation.add_assistant("旧回答" * 500)

            agent.chat("新问题")

            self.assertEqual(
                [message["role"] for message in model.calls[0]],
                ["system", "user"],
            )
            self.assertEqual(model.calls[0][-1]["content"], "新问题")
            saved_session = store.latest()
            self.assertIsNotNone(saved_session)
            self.assertEqual(len(saved_session.messages), 5)
            self.assertEqual(saved_session.messages[1]["content"], "旧问题" * 500)

    def test_agent_injects_fresh_china_date_without_persisting_it(self) -> None:
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="recording-model",
            system_prompt="system",
        )
        model = RecordingModel()
        with tempfile.TemporaryDirectory() as directory:
            store = JsonSessionStore(Path(directory))
            agent = Agent(settings, model=model, session_store=store)
            with patch(
                "deepseek_agent.agent.now_china",
                side_effect=(datetime(2026, 8, 29), datetime(2027, 1, 2)),
            ):
                first = agent._prepare_model_messages(
                    force_plan_continuation=False
                )
                second = agent._prepare_model_messages(
                    force_plan_continuation=False
                )

        self.assertIn("当前北京时间日期为 2026-08-29", first[0]["content"])
        self.assertIn("年份 2027", second[0]["content"])
        self.assertEqual(agent._conversation.messages[0]["content"], "system")


if __name__ == "__main__":
    unittest.main()
