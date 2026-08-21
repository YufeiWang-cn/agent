"""提供带新旧行号、语义配色和统计信息的统一差异查看器。"""

import re
import tkinter as tk
from dataclasses import dataclass

from .theme import (
    EDITOR_BACKGROUND,
    EDITOR_BORDER,
    EDITOR_MUTED,
    EDITOR_TEXT,
    EDITOR_TOOLBAR,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


HUNK_HEADER_PATTERN = re.compile(
    r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@"
)


@dataclass(frozen=True, slots=True)
class DiffRow:
    """描述统一差异中的一行及其新旧文件行号。"""

    text: str
    kind: str
    old_line: int | None = None
    new_line: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedDiff:
    """保存统一差异的渲染行和增删统计。"""

    rows: tuple[DiffRow, ...]
    files: int
    additions: int
    deletions: int


def parse_unified_diff(value: str) -> ParsedDiff:
    """解析统一差异，并计算每一行对应的新旧文件行号。"""
    rows: list[DiffRow] = []
    old_line: int | None = None
    new_line: int | None = None
    files = 0
    additions = 0
    deletions = 0

    for line in value.splitlines():
        if line.startswith("--- "):
            rows.append(DiffRow(line, "file_header"))
            continue
        if line.startswith("+++ "):
            files += 1
            rows.append(DiffRow(line, "file_header"))
            continue
        if line.startswith("@@"):
            match = HUNK_HEADER_PATTERN.match(line)
            if match is not None:
                old_line = int(match.group(1))
                new_line = int(match.group(2))
            rows.append(DiffRow(line, "hunk"))
            continue
        if line.startswith("+"):
            additions += 1
            rows.append(DiffRow(line, "added", new_line=new_line))
            if new_line is not None:
                new_line += 1
            continue
        if line.startswith("-"):
            deletions += 1
            rows.append(DiffRow(line, "deleted", old_line=old_line))
            if old_line is not None:
                old_line += 1
            continue
        if line.startswith(" "):
            rows.append(
                DiffRow(
                    line,
                    "context",
                    old_line=old_line,
                    new_line=new_line,
                )
            )
            if old_line is not None:
                old_line += 1
            if new_line is not None:
                new_line += 1
            continue
        rows.append(DiffRow(line, "metadata"))

    return ParsedDiff(
        rows=tuple(rows),
        files=files,
        additions=additions,
        deletions=deletions,
    )


class DiffViewer(tk.Frame):
    """使用同步行号栏和代码区域展示只读统一差异。"""

    _COLORS = {
        "file_header": ("#EAF0F6", "#DDE5ED", "#25324A"),
        "hunk": ("#DDF4FF", "#B6E3FF", "#0550AE"),
        "added": ("#E6FFEC", "#CCFFD8", "#116329"),
        "deleted": ("#FFEBE9", "#FFD7D5", "#82071E"),
        "context": (EDITOR_BACKGROUND, "#F3F5F8", EDITOR_TEXT),
        "metadata": ("#F6F8FA", "#EEF1F4", EDITOR_MUTED),
    }

    def __init__(
        self,
        parent: tk.Misc,
        content: str,
        *,
        font_size: int = 11,
    ) -> None:
        super().__init__(parent, background=EDITOR_BACKGROUND, borderwidth=0)
        self._content = content
        self._font_size = font_size
        self._parsed = parse_unified_diff(content)
        self._copy_reset_job: str | None = None

        self._build_summary()
        self._build_body()
        self._configure_tags()
        self._render_rows()

    @property
    def content(self) -> str:
        return self._content

    @property
    def language(self) -> str:
        return "diff"

    @property
    def parsed(self) -> ParsedDiff:
        return self._parsed

    def _build_summary(self) -> None:
        summary = tk.Frame(self, background="#FFFFFF", height=38)
        summary.pack(fill="x")
        summary.pack_propagate(False)

        tk.Label(
            summary,
            text="旧行   新行",
            width=11,
            anchor="e",
            background="#F3F5F8",
            foreground=EDITOR_MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", fill="y", padx=(0, 1), pady=0)
        file_label = f"{self._parsed.files} 个文件"
        tk.Label(
            summary,
            text=file_label,
            background="#FFFFFF",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(12, 10), pady=9)
        tk.Label(
            summary,
            text=f"+{self._parsed.additions}",
            background="#FFFFFF",
            foreground="#116329",
            font=("Cascadia Mono", 10, "bold"),
        ).pack(side="left", padx=(0, 8), pady=8)
        tk.Label(
            summary,
            text=f"-{self._parsed.deletions}",
            background="#FFFFFF",
            foreground="#82071E",
            font=("Cascadia Mono", 10, "bold"),
        ).pack(side="left", pady=8)

        self._copy_button = tk.Button(
            summary,
            text="复制 diff",
            command=self._copy,
            background="#EEF2F6",
            activebackground="#DDE3EB",
            foreground=TEXT_SECONDARY,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=3,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9),
        )
        self._copy_button.pack(side="right", padx=10, pady=6)
        tk.Frame(self, background=EDITOR_BORDER, height=1).pack(fill="x")

    def _build_body(self) -> None:
        body = tk.Frame(self, background=EDITOR_BACKGROUND)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        common_options = {
            "wrap": "none",
            "height": 1,
            "font": ("Cascadia Mono", self._font_size),
            "relief": "flat",
            "borderwidth": 0,
            "pady": 10,
            "state": "disabled",
        }
        self.gutter = tk.Text(
            body,
            width=11,
            background="#F3F5F8",
            foreground=EDITOR_MUTED,
            padx=6,
            takefocus=0,
            cursor="arrow",
            **common_options,
        )
        self.text = tk.Text(
            body,
            width=1,
            background=EDITOR_BACKGROUND,
            foreground=EDITOR_TEXT,
            padx=12,
            selectbackground="#B6D7FF",
            selectforeground=TEXT_PRIMARY,
            **common_options,
        )
        self._scroll_y = tk.Scrollbar(
            body,
            orient="vertical",
            command=self._scroll_vertical,
            width=12,
        )
        self._scroll_x = tk.Scrollbar(
            body,
            orient="horizontal",
            command=self.text.xview,
            width=12,
        )
        self.text.configure(
            yscrollcommand=self._sync_vertical_scroll,
            xscrollcommand=self._scroll_x.set,
        )
        self.gutter.grid(row=0, column=0, sticky="ns")
        self.text.grid(row=0, column=1, sticky="nsew")
        self._scroll_y.grid(row=0, column=2, sticky="ns")
        self._scroll_x.grid(row=1, column=1, sticky="ew")

        for widget in (self.gutter, self.text):
            widget.bind("<MouseWheel>", self._handle_mousewheel)
            widget.bind("<Button-4>", self._handle_mousewheel)
            widget.bind("<Button-5>", self._handle_mousewheel)

    def _configure_tags(self) -> None:
        for kind, (body_color, gutter_color, foreground) in self._COLORS.items():
            font = (
                ("Cascadia Mono", self._font_size, "bold")
                if kind in {"file_header", "hunk"}
                else ("Cascadia Mono", self._font_size)
            )
            self.text.tag_configure(
                kind,
                background=body_color,
                foreground=foreground,
                font=font,
            )
            self.gutter.tag_configure(
                kind,
                background=gutter_color,
                foreground=foreground,
                font=font,
                justify="right",
            )

    def _render_rows(self) -> None:
        self.gutter.configure(state="normal")
        self.text.configure(state="normal")
        self.gutter.delete("1.0", "end")
        self.text.delete("1.0", "end")

        for row in self._parsed.rows:
            old_number = "" if row.old_line is None else str(row.old_line)
            new_number = "" if row.new_line is None else str(row.new_line)
            self.gutter.insert(
                "end",
                f"{old_number:>4} {new_number:>4}\n",
                row.kind,
            )
            self.text.insert("end", f"{row.text}\n", row.kind)

        self.gutter.configure(state="disabled")
        self.text.configure(state="disabled")

    def _scroll_vertical(self, *arguments: str) -> None:
        self.text.yview(*arguments)
        self.gutter.yview(*arguments)

    def _sync_vertical_scroll(self, first: str, last: str) -> None:
        self._scroll_y.set(first, last)
        self.gutter.yview_moveto(first)

    def _handle_mousewheel(self, event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            direction = -1 if delta > 0 else 1
            units = direction * max(1, abs(delta) // 120)
        else:
            units = -1 if getattr(event, "num", 0) == 4 else 1
        self.text.yview_scroll(units, "units")
        self.gutter.yview_scroll(units, "units")
        return "break"

    def _copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._content)
        self._copy_button.configure(text="已复制")
        if self._copy_reset_job is not None:
            self.after_cancel(self._copy_reset_job)
        self._copy_reset_job = self.after(1200, self._reset_copy_button)

    def _reset_copy_button(self) -> None:
        self._copy_reset_job = None
        if self._copy_button.winfo_exists():
            self._copy_button.configure(text="复制 diff")

    def destroy(self) -> None:
        if self._copy_reset_job is not None:
            try:
                self.after_cancel(self._copy_reset_job)
            except tk.TclError:
                pass
            self._copy_reset_job = None
        super().destroy()


__all__ = ["DiffRow", "DiffViewer", "ParsedDiff", "parse_unified_diff"]
