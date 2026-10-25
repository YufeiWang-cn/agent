"""验证轻量级 Token 估算对中英文的校准行为。"""

import unittest

from _path_setup import add_src_to_path

add_src_to_path()

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


if __name__ == "__main__":
    unittest.main()
