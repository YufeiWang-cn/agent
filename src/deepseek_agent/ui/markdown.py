import json
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    kind: str
    text: str = ""
    level: int = 0
    language: str = "text"
    marker: str = ""
    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class InlineSpan:
    text: str
    style: str = "plain"


_BLOCK_START = re.compile(
    r"^(?:#{1,6}\s+|\s*[-*+]\s+|\s*\d+[.)]\s+|\s*>|\s*(?:-{3,}|\*{3,}|_{3,})\s*$)"
)
_INLINE_TOKEN = re.compile(
    r"(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|"
    r"(?<!\*)\*[^*\n]+\*(?!\*)|(?<!_)_[^_\n]+_(?!_)|"
    r"\[[^\]\n]+\]\([^)\n]+\))"
)


def parse_markdown(content: str) -> list[MarkdownBlock]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    pure_json = _format_pure_json(normalized)
    if pure_json is not None:
        return [MarkdownBlock("code", pure_json, language="json")]

    lines = normalized.split("\n")
    blocks: list[MarkdownBlock] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        fence = re.match(r"^\s*(```|~~~)\s*([\w.+-]*)\s*$", line)
        if fence:
            delimiter = fence.group(1)
            language = _normalize_language(fence.group(2))
            index += 1
            code_lines: list[str] = []
            while index < len(lines):
                if re.match(rf"^\s*{re.escape(delimiter)}\s*$", lines[index]):
                    index += 1
                    break
                code_lines.append(lines[index])
                index += 1
            blocks.append(
                MarkdownBlock("code", "\n".join(code_lines), language=language)
            )
            continue

        if not stripped:
            if blocks and blocks[-1].kind != "blank":
                blocks.append(MarkdownBlock("blank"))
            index += 1
            continue

        if index + 1 < len(lines):
            headers = _split_table_row(line)
            separator = _split_table_row(lines[index + 1])
            if headers and _is_table_separator(separator, len(headers)):
                index += 2
                rows: list[tuple[str, ...]] = []
                while index < len(lines) and lines[index].strip():
                    row = _split_table_row(lines[index])
                    if not row:
                        break
                    normalized_row = tuple(
                        (row[column] if column < len(row) else "")
                        for column in range(len(headers))
                    )
                    rows.append(normalized_row)
                    index += 1
                blocks.append(
                    MarkdownBlock(
                        "table",
                        headers=tuple(headers),
                        rows=tuple(rows),
                    )
                )
                continue

        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            blocks.append(
                MarkdownBlock(
                    "heading",
                    heading.group(2),
                    level=len(heading.group(1)),
                )
            )
            index += 1
            continue

        if re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", line):
            blocks.append(MarkdownBlock("rule"))
            index += 1
            continue

        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            blocks.append(MarkdownBlock("quote", quote.group(1)))
            index += 1
            continue

        unordered = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if unordered:
            blocks.append(MarkdownBlock("list_item", unordered.group(1), marker="•"))
            index += 1
            continue

        ordered = re.match(r"^\s*(\d+)[.)]\s+(.+)$", line)
        if ordered:
            blocks.append(
                MarkdownBlock(
                    "list_item",
                    ordered.group(2),
                    marker=f"{ordered.group(1)}.",
                )
            )
            index += 1
            continue

        paragraph_lines = [stripped]
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if not candidate.strip():
                break
            if re.match(r"^\s*(```|~~~)", candidate) or _BLOCK_START.match(candidate):
                break
            if index + 1 < len(lines):
                candidate_headers = _split_table_row(candidate)
                candidate_separator = _split_table_row(lines[index + 1])
                if candidate_headers and _is_table_separator(
                    candidate_separator,
                    len(candidate_headers),
                ):
                    break
            paragraph_lines.append(candidate.strip())
            index += 1
        blocks.append(MarkdownBlock("paragraph", " ".join(paragraph_lines)))

    while blocks and blocks[-1].kind == "blank":
        blocks.pop()
    return blocks


def parse_inline(content: str) -> list[InlineSpan]:
    spans: list[InlineSpan] = []
    position = 0
    for match in _INLINE_TOKEN.finditer(content):
        if match.start() > position:
            spans.append(InlineSpan(content[position : match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            spans.append(InlineSpan(token[1:-1], "code"))
        elif token.startswith(("**", "__")):
            spans.append(InlineSpan(token[2:-2], "bold"))
        elif token.startswith(("*", "_")):
            spans.append(InlineSpan(token[1:-1], "italic"))
        elif token.startswith("["):
            label, _separator, _target = token[1:].partition("](")
            spans.append(InlineSpan(label, "link"))
        position = match.end()
    if position < len(content):
        spans.append(InlineSpan(content[position:]))
    return spans or [InlineSpan(content)]


def _format_pure_json(content: str) -> str | None:
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _normalize_language(language: str) -> str:
    normalized = language.strip().lower()
    aliases = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "sh": "bash",
        "shell": "bash",
        "md": "markdown",
    }
    return aliases.get(normalized, normalized or "text")


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if "|" not in stripped:
        return []
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for character in stripped:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "`":
            in_code = not in_code
            current.append(character)
            continue
        if character == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _is_table_separator(cells: list[str], column_count: int) -> bool:
    return len(cells) == column_count and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", ""))
        for cell in cells
    )
