import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.conversation import Conversation


class ConversationTests(unittest.TestCase):
    def test_tool_messages_are_recorded_and_clear_preserves_system_prompt(self) -> None:
        conversation = Conversation("system")
        conversation.add_user("计算")
        conversation.add_assistant_tool_calls(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "arguments": '{"expression":"1+1"}',
                    },
                }
            ]
        )
        conversation.add_tool_result("call_1", "calculator", "2")

        self.assertEqual([message["role"] for message in conversation.messages], [
            "system",
            "user",
            "assistant",
            "tool",
        ])

        conversation.clear()
        self.assertEqual(
            conversation.messages,
            [{"role": "system", "content": "system"}],
        )

    def test_truncate_rolls_back_failed_turn(self) -> None:
        conversation = Conversation("system")
        checkpoint = len(conversation)
        conversation.add_user("失败的问题")
        conversation.truncate(checkpoint)
        self.assertEqual(len(conversation), 1)


if __name__ == "__main__":
    unittest.main()
