"""验证 Markdown 解析和界面文本格式化。"""

import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.ui.formatting import (
    _content_language_hint,
    _conversation_preview,
    _format_editor_content,
    _split_emoji_spans,
)
from deepseek_agent.ui.markdown import normalize_web_url, parse_inline, parse_markdown


class UiMarkdownTests(unittest.TestCase):
    def test_json_is_pretty_printed_for_editor(self) -> None:
        content, language = _format_editor_content(
            '{"path":"src/app.py","enabled":true}'
        )

        self.assertEqual(language, "json")
        self.assertIn('\n  "path": "src/app.py"', content)
        self.assertIn('\n  "enabled": true', content)

    def test_result_language_comes_from_file_extension(self) -> None:
        self.assertEqual(
            _content_language_hint('{"path":"src/app.py"}'),
            "python",
        )
        self.assertEqual(
            _content_language_hint('{"path":"README.md"}'),
            "markdown",
        )

    def test_invalid_arguments_fall_back_to_plain_text(self) -> None:
        self.assertEqual(_content_language_hint("not json"), "text")
        content, language = _format_editor_content("plain text", "text")
        self.assertEqual((content, language), ("plain text", "text"))

    def test_conversation_preview_is_compact_and_descriptive(self) -> None:
        self.assertEqual(
            _conversation_preview("  如何\n切换模型？  "),
            "如何 切换模型？",
        )
        self.assertEqual(_conversation_preview("123456", limit=5), "1234…")

    def test_emoji_sequences_remain_complete_spans(self) -> None:
        spans = _split_emoji_spans("开始 👍🏽 🇨🇳 👨‍👩‍👧‍👦 1️⃣ 结束")

        self.assertEqual(
            [text for text, is_emoji in spans if is_emoji],
            ["👍🏽", "🇨🇳", "👨‍👩‍👧‍👦", "1️⃣"],
        )
        self.assertEqual(
            "".join(text for text, _is_emoji in spans),
            "开始 👍🏽 🇨🇳 👨‍👩‍👧‍👦 1️⃣ 结束",
        )

    def test_markdown_blocks_include_headings_lists_and_code(self) -> None:
        blocks = parse_markdown(
            "# 标题\n\n- 项目\n\n```python\nprint('hello')\n```"
        )

        self.assertEqual(
            [block.kind for block in blocks],
            ["heading", "blank", "list_item", "blank", "code"],
        )
        self.assertEqual(blocks[0].level, 1)
        self.assertEqual(blocks[-1].language, "python")

    def test_markdown_table_keeps_rows_and_inline_pipes(self) -> None:
        blocks = parse_markdown(
            "| 文件 | 行号 | 内容 |\n"
            "| --- | ---: | --- |\n"
            "| src/app.py | 13 | `value = left | right` |\n"
            "| tests/test_app.py | 7 | `assert value` |"
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "table")
        self.assertEqual(blocks[0].headers, ("文件", "行号", "内容"))
        self.assertEqual(
            blocks[0].rows[0],
            ("src/app.py", "13", "`value = left | right`"),
        )

    def test_pure_json_becomes_pretty_code_block(self) -> None:
        blocks = parse_markdown('{"enabled":true,"count":2}')

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "code")
        self.assertEqual(blocks[0].language, "json")
        self.assertIn('\n  "enabled": true', blocks[0].text)

    def test_inline_markdown_removes_delimiters_and_keeps_styles(self) -> None:
        spans = parse_inline("普通 **加粗** `code` *斜体*")

        self.assertEqual(
            [(span.text, span.style) for span in spans],
            [
                ("普通 ", "plain"),
                ("加粗", "bold"),
                (" ", "plain"),
                ("code", "code"),
                (" ", "plain"),
                ("斜体", "italic"),
            ],
        )

    def test_inline_links_keep_only_safe_http_targets(self) -> None:
        spans = parse_inline(
            "访问 [官网](https://example.com/path?q=1) 或 "
            "[危险链接](javascript:void)。"
        )

        self.assertEqual(
            [(span.text, span.style, span.target) for span in spans],
            [
                ("访问 ", "plain", None),
                ("官网", "link", "https://example.com/path?q=1"),
                (" 或 ", "plain", None),
                ("危险链接", "plain", None),
                ("。", "plain", None),
            ],
        )
        self.assertEqual(
            normalize_web_url("<https://example.com/docs>"),
            "https://example.com/docs",
        )
        for target in (
            "file:///C:/secret.txt",
            "javascript:alert(1)",
            "https://user:password@example.com",
            "../relative/path",
            "https://example.com/contains space",
        ):
            with self.subTest(target=target):
                self.assertIsNone(normalize_web_url(target))


if __name__ == "__main__":
    unittest.main()
