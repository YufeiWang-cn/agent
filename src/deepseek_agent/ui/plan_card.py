"""实现结构化任务计划的可折叠展示卡片。"""

import tkinter as tk

from ..planning import (
    PlanExecutionScope,
    PlanKind,
    PlanStepStatus,
    TaskPlan,
)
from .theme import (
    ACCENT,
    ASSISTANT_COLOR,
    CARD_BACKGROUND,
    DANGER,
    EDITOR_BORDER,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


class PlanCard(tk.Frame):
    """以紧凑可折叠卡片展示当前任务计划和完成进度。"""

    _STATUS_STYLES = {
        PlanStepStatus.PENDING: ("○", "待处理", TEXT_SECONDARY, "#FFFFFF"),
        PlanStepStatus.IN_PROGRESS: ("●", "进行中", ACCENT, "#EEF3FF"),
        PlanStepStatus.WAITING_USER: ("?", "等待输入", "#B45309", "#FFFBEB"),
        PlanStepStatus.COMPLETED: ("✓", "已完成", ASSISTANT_COLOR, "#F0FDF4"),
        PlanStepStatus.FAILED: ("!", "失败", DANGER, "#FEF2F2"),
        PlanStepStatus.SKIPPED: ("–", "已跳过", TEXT_SECONDARY, "#F8FAFC"),
    }

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(
            parent,
            background=CARD_BACKGROUND,
            highlightbackground=EDITOR_BORDER,
            highlightthickness=1,
            borderwidth=0,
        )
        self._expanded = True
        self._plan: TaskPlan | None = None
        self._step_labels: list[tk.Label] = []

        header = tk.Frame(self, background="#F8FAFC")
        header.pack(fill="x")
        self._toggle_button = tk.Button(
            header,
            text="▼  任务计划",
            command=self.toggle,
            anchor="w",
            background="#F8FAFC",
            activebackground="#F1F5F9",
            foreground=TEXT_PRIMARY,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=12,
            pady=7,
        )
        self._toggle_button.pack(side="left", fill="x", expand=True)
        self._progress = tk.Label(
            header,
            text="",
            background="#E9EEF5",
            foreground=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=8,
            pady=3,
        )
        self._progress.pack(side="right", padx=9, pady=7)

        self._body = tk.Frame(self, background=CARD_BACKGROUND)
        self._body.pack(fill="x", padx=10, pady=(7, 9))
        self.bind("<Configure>", self._resize_text)

    def update_plan(self, plan: TaskPlan) -> None:
        """使用最新不可变快照重新绘制步骤列表。"""
        self._plan = plan
        title = self._plan_title(plan)
        self._toggle_button.configure(
            text=("▼  " if self._expanded else "▶  ") + title
        )
        progress = (
            f"共 {len(plan.steps)} 步"
            if plan.kind is PlanKind.PROPOSAL
            else f"{plan.completed_count}/{len(plan.steps)} 已完成"
        )
        self._progress.configure(text=progress)
        for child in self._body.winfo_children():
            child.destroy()
        self._step_labels.clear()

        if plan.explanation:
            explanation = tk.Label(
                self._body,
                text=plan.explanation,
                anchor="w",
                justify="left",
                background="#F8FAFC",
                foreground=TEXT_SECONDARY,
                font=("Microsoft YaHei UI", 9),
                padx=9,
                pady=5,
            )
            explanation.pack(fill="x", pady=(0, 5))
            self._step_labels.append(explanation)

        for index, step in enumerate(plan.steps, start=1):
            symbol, status_text, color, background = self._STATUS_STYLES[
                step.status
            ]
            row = tk.Frame(self._body, background=background)
            row.pack(fill="x", pady=1)
            tk.Label(
                row,
                text=symbol,
                background=background,
                foreground=color,
                font=("Microsoft YaHei UI", 11, "bold"),
                width=2,
            ).pack(side="left", padx=(7, 2), pady=5)
            label = tk.Label(
                row,
                text=f"{index}. {step.step}",
                anchor="w",
                justify="left",
                background=background,
                foreground=TEXT_PRIMARY,
                font=("Microsoft YaHei UI", 9),
            )
            label.pack(side="left", fill="x", expand=True, pady=5)
            self._step_labels.append(label)
            tk.Label(
                row,
                text=status_text,
                background=background,
                foreground=color,
                font=("Microsoft YaHei UI", 9),
            ).pack(side="right", padx=(8, 9), pady=5)
        self._resize_text()

    def toggle(self) -> None:
        """切换步骤详情的展开状态。"""
        self._expanded = not self._expanded
        title = (
            self._plan_title(self._plan)
            if self._plan is not None
            else "执行计划"
        )
        self._toggle_button.configure(
            text=("▼  " if self._expanded else "▶  ") + title
        )
        if self._expanded:
            self._body.pack(fill="x", padx=10, pady=(7, 9))
        else:
            self._body.pack_forget()

    @staticmethod
    def _plan_title(plan: TaskPlan) -> str:
        """返回包含用途和执行范围的计划卡片标题。"""
        if plan.kind is PlanKind.PROPOSAL:
            return "规划方案"
        if plan.scope is PlanExecutionScope.SINGLE_STEP:
            return "执行计划 · 单步"
        return "执行计划 · 连续"

    def _resize_text(self, _event: tk.Event | None = None) -> None:
        """根据卡片宽度调整步骤文本的换行范围。"""
        wraplength = max(220, self.winfo_width() - 180)
        for label in self._step_labels:
            label.configure(wraplength=wraplength)
