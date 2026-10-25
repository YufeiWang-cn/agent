"""验证对话消息创建、恢复和截断操作。"""

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

    def test_restore_without_copy_takes_over_exclusive_messages(self) -> None:
        messages = [
            {"role": "system", "content": "旧提示词"},
            {"role": "user", "content": "问题"},
        ]
        conversation = Conversation("新提示词")

        conversation.restore(messages, copy=False)

        # 列表是独立容器：后续追加与截断不影响原列表。
        conversation.add_assistant("回答")
        self.assertEqual(len(messages), 2)
        # 首条按当前提示词重建，其余消息对象直接共享（省一次深拷贝）。
        self.assertEqual(conversation.messages[0]["content"], "新提示词")
        self.assertIs(conversation.messages[1], messages[1])

    def test_restore_with_copy_defends_shared_messages(self) -> None:
        messages = [
            {"role": "system", "content": "旧提示词"},
            {"role": "user", "content": "问题"},
        ]
        conversation = Conversation("新提示词")

        conversation.restore(messages)

        self.assertIsNot(conversation.messages[1], messages[1])
        messages[1]["content"] = "外部修改"
        self.assertEqual(conversation.messages[1]["content"], "问题")


if __name__ == "__main__":
    unittest.main()
