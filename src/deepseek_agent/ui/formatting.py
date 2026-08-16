"""提供 Tk 界面共用且不依赖控件状态的格式化函数。"""

import json
from pathlib import Path

EXTENSION_LANGUAGES = {
    ".py": "python",
    ".json": "json",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
    ".sql": "sql",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "shell",
    ".ps1": "powershell",
}


def _content_language_hint(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return "text"
    if not isinstance(parsed, dict):
        return "text"
    path = parsed.get("path")
    if not isinstance(path, str):
        return "text"
    return EXTENSION_LANGUAGES.get(Path(path).suffix.lower(), "text")


def _confirmation_content_previews(
    arguments: str,
) -> list[tuple[str, str, str, str]]:
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []

    titles = {
        "content": "内容预览",
        "text": "文本预览",
        "old_text": "原文本",
        "new_text": "新文本",
        "replacement": "替换内容",
    }
    language_hint = _content_language_hint(arguments)
    previews: list[tuple[str, str, str, str]] = []
    for field, title in titles.items():
        value = parsed.get(field)
        if not isinstance(value, str) or not value:
            continue
        previews.append(
            (
                f"preview_{field}",
                title,
                value,
                language_hint if field != "text" else "text",
            )
        )
    return previews[:4]


def _format_editor_content(value: str, language_hint: str = "text") -> tuple[str, str]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value, language_hint
    if isinstance(parsed, (dict, list)):
        return json.dumps(parsed, ensure_ascii=False, indent=2), "json"
    return value, language_hint


def _conversation_preview(value: str, limit: int = 28) -> str:
    compact = " ".join(value.split()) or "（空问题）"
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)] + "…"


def _split_emoji_spans(value: str) -> list[tuple[str, bool]]:
    """拆分普通文本和表情，并保持连接后的表情序列完整。"""

    def is_emoji_base(character: str) -> bool:
        codepoint = ord(character)
        return (
            0x1F000 <= codepoint <= 0x1FAFF
            or 0x1FC00 <= codepoint <= 0x1FFFD
            or 0x2300 <= codepoint <= 0x23FF
            or 0x2600 <= codepoint <= 0x27BF
            or 0x2B00 <= codepoint <= 0x2BFF
            or codepoint
            in {0x00A9, 0x00AE, 0x2122, 0x3030, 0x303D, 0x3297, 0x3299}
        )

    def consume_suffix(position: int) -> int:
        while position < len(value) and (
            ord(value[position]) in {0xFE0E, 0xFE0F, 0x20E3}
            or 0x1F3FB <= ord(value[position]) <= 0x1F3FF
        ):
            position += 1
        return position

    spans: list[tuple[str, bool]] = []
    plain_start = 0
    index = 0
    while index < len(value):
        keycap_end = index + 1
        if value[index] in "#*0123456789":
            if keycap_end < len(value) and ord(value[keycap_end]) == 0xFE0F:
                keycap_end += 1
            is_keycap = (
                keycap_end < len(value) and ord(value[keycap_end]) == 0x20E3
            )
        else:
            is_keycap = False

        if not is_keycap and not is_emoji_base(value[index]):
            index += 1
            continue

        if plain_start < index:
            spans.append((value[plain_start:index], False))

        emoji_start = index
        if is_keycap:
            index = keycap_end + 1
        else:
            first_codepoint = ord(value[index])
            index = consume_suffix(index + 1)
            if (
                0x1F1E6 <= first_codepoint <= 0x1F1FF
                and index < len(value)
                and 0x1F1E6 <= ord(value[index]) <= 0x1F1FF
            ):
                index = consume_suffix(index + 1)
            while index + 1 < len(value) and ord(value[index]) == 0x200D:
                index = consume_suffix(index + 2)

        spans.append((value[emoji_start:index], True))
        plain_start = index

    if plain_start < len(value):
        spans.append((value[plain_start:], False))
    return spans or [(value, False)]


