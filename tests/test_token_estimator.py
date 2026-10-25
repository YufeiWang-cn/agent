"""验证轻量级 Token 估算对中英文的校准行为。"""

import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.context import (
    clear_estimation_cache,
    estimate_message_tokens,
    estimate_messages_tokens,
)
from deepseek_agent.context.estimator import estimate_text_tokens


class EstimateTextTokensTests(unittest.TestCase):
    def test_ascii_text_counts_one_token_per_four_characters(self) -> None:
        self.assertEqual(estimate_text_tokens("abcdefgh"), 2)
        self.assertEqual(estimate_text_tokens("abc"), 1)

    def test_cjk_text_counts_three_tokens_per_five_characters(self) -> None:
        """DeepSeek 分词器对中文约 0.6 token/字，不应按 1 token/字高估。"""
        text = "汉字汉字汉"  # 5 个 CJK 字符
        self.assertEqual(estimate_text_tokens(text), 3)
        long_text = "证据" * 5000  # 10000 个 CJK 字符
        self.assertEqual(estimate_text_tokens(long_text), 6000)

    def test_mixed_text_sums_per_script_allowances(self) -> None:
        text = "abc你好"  # 3 ASCII + 2 CJK
        self.assertEqual(estimate_text_tokens(text), 1 + 2)

    def test_emoji_and_other_non_ascii_stay_at_one_token_each(self) -> None:
        self.assertEqual(estimate_text_tokens("🎉🎉"), 2)


class EstimateMessageCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_estimation_cache()

    def tearDown(self) -> None:
        clear_estimation_cache()

    def test_same_message_object_returns_cached_estimate(self) -> None:
        message = {"role": "user", "content": "你好，请帮我分析这段需求。" * 50}

        first = estimate_message_tokens(message)
        second = estimate_message_tokens(message)

        self.assertEqual(first, second)

    def test_equal_content_in_distinct_objects_estimates_independently(self) -> None:
        first = {"role": "user", "content": "相同内容"}
        second = {"role": "user", "content": "相同内容"}

        first_tokens = estimate_message_tokens(first)
        self.assertEqual(first_tokens, estimate_message_tokens(second))
        # 各对象各自命中缓存，重复估算保持稳定。
        # 契约：消息进入会话后不得原地修改；缓存按对象身份生效，
        # 原地改动不会刷新估算值。
        self.assertEqual(estimate_message_tokens(first), first_tokens)
        self.assertEqual(estimate_message_tokens(second), first_tokens)

    def test_cache_is_scoped_by_object_identity(self) -> None:
        first = {"role": "user", "content": "第一条消息"}
        second = {"role": "user", "content": "第二条完全不同的消息"}
        first_tokens = estimate_message_tokens(first)
        second_tokens = estimate_message_tokens(second)

        self.assertNotEqual(first_tokens, second_tokens)

    def test_batch_estimation_is_stable_across_repeated_calls(self) -> None:
        messages = [{"role": "user", "content": f"消息 {index}"} for index in range(10)]

        first_total = estimate_messages_tokens(messages)
        second_total = estimate_messages_tokens(messages)

        self.assertEqual(first_total, second_total)
        self.assertGreater(first_total, 0)


if __name__ == "__main__":
    unittest.main()
