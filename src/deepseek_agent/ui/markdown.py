"""把受支持的 Markdown 子集解析为便于 Tk 渲染的结构。"""

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """表示段落、代码、表格等块级 Markdown 节点。"""

    kind: str
    text: str = ""
    level: int = 0
    language: str = "text"
    marker: str = ""
    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class InlineSpan:
    """表示带有粗体、斜体或代码样式的行内文本片段。"""

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
    """将 Markdown 文本解析为保持原有顺序的块级节点列表。"""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    pure_json = _format_pure_json(normalized)
    if pure_json is not None:
        return [MarkdownBlock("code", pure_json, language="json")]

    lines = normalized.split("\n")
    blocks: list[MarkdownBlock] = []
    index = 0
    while index < len(lines):
        fenced = _parse_fenced_code(lines, index)
        if fenced is not None:
            block, index = fenced
            blocks.append(block)
            continue

        if not lines[index].strip():
            if blocks and blocks[-1].kind != "blank":
                blocks.append(MarkdownBlock("blank"))
            index += 1
            continue

        table = _parse_table(lines, index)
        if table is not None:
            block, index = table
            blocks.append(block)
            continue

        simple = _parse_simple_block(lines[index])
        if simple is not None:
            blocks.append(simple)
            index += 1
            continue

        block, index = _parse_paragraph(lines, index)
        blocks.append(block)

    while blocks and blocks[-1].kind == "blank":
        blocks.pop()
    return blocks


def _parse_fenced_code(
    lines: list[str],
    index: int,
) -> tuple[MarkdownBlock, int] | None:
    """读取围栏代码块，并返回解析后的下一个行号。"""
    fence = re.match(r"^\s*(```|~~~)\s*([\w.+-]*)\s*$", lines[index])
    if fence is None:
        return None
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
    return MarkdownBlock("code", "\n".join(code_lines), language=language), index


def _parse_table(
    lines: list[str],
    index: int,
) -> tuple[MarkdownBlock, int] | None:
    """识别表头和分隔行，并规范化后续表格单元格。"""
    if index + 1 >= len(lines):
        return None
    headers = _split_table_row(lines[index])
    separator = _split_table_row(lines[index + 1])
    if not headers or not _is_table_separator(separator, len(headers)):
        return None

    index += 2
    rows: list[tuple[str, ...]] = []
    while index < len(lines) and lines[index].strip():
        row = _split_table_row(lines[index])
        if not row:
            break
        normalized_row = tuple(
            row[column] if column < len(row) else ""
            for column in range(len(headers))
        )
        rows.append(normalized_row)
        index += 1
    return MarkdownBlock("table", headers=tuple(headers), rows=tuple(rows)), index


def _parse_simple_block(line: str) -> MarkdownBlock | None:
    """解析只占一行的标题、分隔线、引用和列表节点。"""
    heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
    if heading:
        return MarkdownBlock(
            "heading",
            heading.group(2),
            level=len(heading.group(1)),
        )
    if re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", line):
        return MarkdownBlock("rule")
    quote = re.match(r"^\s*>\s?(.*)$", line)
    if quote:
        return MarkdownBlock("quote", quote.group(1))
    unordered = re.match(r"^\s*[-*+]\s+(.+)$", line)
    if unordered:
        return MarkdownBlock("list_item", unordered.group(1), marker="•")
    ordered = re.match(r"^\s*(\d+)[.)]\s+(.+)$", line)
    if ordered:
        return MarkdownBlock(
            "list_item",
            ordered.group(2),
            marker=f"{ordered.group(1)}.",
        )
    return None


def _parse_paragraph(lines: list[str], index: int) -> tuple[MarkdownBlock, int]:
    """合并连续普通文本，同时在下一个块级节点前停止。"""
    paragraph_lines = [lines[index].strip()]
    index += 1
    while index < len(lines):
        candidate = lines[index]
        if not candidate.strip():
            break
        starts_block = re.match(r"^\s*(```|~~~)", candidate)
        if starts_block or _BLOCK_START.match(candidate):
            break
        if _parse_table(lines, index) is not None:
            break
        paragraph_lines.append(candidate.strip())
        index += 1
    return MarkdownBlock("paragraph", " ".join(paragraph_lines)), index


def parse_inline(content: str) -> list[InlineSpan]:
    """解析受支持的行内样式，并保留普通文本顺序。"""
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
