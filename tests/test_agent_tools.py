import unittest

from _path_setup import add_project_root_to_path

add_project_root_to_path()

from src.deepseek_agent.agent import Agent
from src.deepseek_agent.config import Settings
from src.deepseek_agent.conversation import Message
from src.deepseek_agent.models import StreamEvent, TextDelta, ToolCallRequest


class FakeToolModel:
    model_name = "fake-model"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def stream(self, messages, tools) -> list[StreamEvent]:
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return [
                ToolCallRequest(
                    id="call_1",
                    name="calculator",
                    arguments='{"expression":"128 * 37"}',
                )
            ]
        return [TextDelta("计算结果是 4736。")]


class AgentToolLoopTests(unittest.TestCase):
    def test_agent_executes_tool_and_returns_to_model(self) -> None:
        settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="fake-model",
            system_prompt="system",
        )
        model = FakeToolModel()
        agent = Agent(settings, model=model)

        agent._chat("128乘以37是多少？")

        self.assertEqual(len(model.calls), 2)
        second_call_roles = [message["role"] for message in model.calls[1]]
        self.assertEqual(second_call_roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(model.calls[1][-1]["content"], "4736")


if __name__ == "__main__":
    unittest.main()
