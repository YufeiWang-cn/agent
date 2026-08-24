"""定义结构化任务计划、步骤状态和更新约束。"""

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable


MIN_PLAN_STEPS = 2
MAX_PLAN_STEPS = 7
MAX_PLAN_STEP_LENGTH = 160
MAX_PLAN_EXPLANATION_LENGTH = 500


class PlanStepStatus(str, Enum):
    """表示一个计划步骤当前所处的公开执行状态。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        """返回该状态是否已经结束且不可回退。"""
        return self in {
            PlanStepStatus.COMPLETED,
            PlanStepStatus.FAILED,
            PlanStepStatus.SKIPPED,
        }


@dataclass(frozen=True, slots=True)
class PlanStep:
    """保存一条简洁步骤及其公开状态。"""

    step: str
    status: PlanStepStatus

    def to_dict(self) -> dict[str, str]:
        """返回可直接写入会话 JSON 的步骤数据。"""
        return {"step": self.step, "status": self.status.value}


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """保存当前任务的完整计划快照和递增版本号。"""

    steps: tuple[PlanStep, ...]
    explanation: str | None = None
    revision: int = 1

    @classmethod
    def create(
        cls,
        items: Iterable[dict[str, Any]],
        *,
        explanation: str | None = None,
        revision: int = 1,
    ) -> "TaskPlan":
        """从工具参数创建经过完整结构校验的计划。"""
        raw_items = list(items)
        if not MIN_PLAN_STEPS <= len(raw_items) <= MAX_PLAN_STEPS:
            raise ValueError(
                f"计划必须包含 {MIN_PLAN_STEPS} 到 {MAX_PLAN_STEPS} 个步骤。"
            )
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("计划版本号必须是正整数。")

        normalized_explanation = _normalize_explanation(explanation)
        steps: list[PlanStep] = []
        seen_steps: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                raise ValueError("每个计划步骤都必须是对象。")
            text = item.get("step")
            status = item.get("status")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("计划步骤必须是非空字符串。")
            normalized_text = " ".join(text.split())
            if len(normalized_text) > MAX_PLAN_STEP_LENGTH:
                raise ValueError(
                    f"单个计划步骤不能超过 {MAX_PLAN_STEP_LENGTH} 个字符。"
                )
            if normalized_text in seen_steps:
                raise ValueError("计划中不能包含重复步骤。")
            try:
                normalized_status = PlanStepStatus(status)
            except (TypeError, ValueError) as error:
                raise ValueError(f"无效的计划步骤状态：{status}") from error
            seen_steps.add(normalized_text)
            steps.append(PlanStep(normalized_text, normalized_status))

        active_count = sum(
            step.status is PlanStepStatus.IN_PROGRESS for step in steps
        )
        if active_count > 1:
            raise ValueError("同一时间最多只能有一个进行中的计划步骤。")
        return cls(tuple(steps), normalized_explanation, revision)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskPlan":
        """从会话 JSON 恢复计划，并拒绝损坏的数据。"""
        if not isinstance(data, dict):
            raise ValueError("会话计划格式错误。")
        steps = data.get("steps")
        if not isinstance(steps, list):
            raise ValueError("会话计划缺少步骤列表。")
        return cls.create(
            steps,
            explanation=data.get("explanation"),
            revision=data.get("revision", 1),
        )

    def to_dict(self) -> dict[str, Any]:
        """返回可直接写入会话 JSON 的计划快照。"""
        return {
            "revision": self.revision,
            "explanation": self.explanation,
            "steps": [step.to_dict() for step in self.steps],
        }

    def with_revision(self, revision: int) -> "TaskPlan":
        """返回内容不变但版本号已更新的新计划。"""
        if revision < 1:
            raise ValueError("计划版本号必须是正整数。")
        return replace(self, revision=revision)

    @property
    def completed_count(self) -> int:
        """返回已经成功完成的步骤数量。"""
        return sum(
            step.status is PlanStepStatus.COMPLETED for step in self.steps
        )

    @property
    def terminal(self) -> bool:
        """返回计划中的所有步骤是否都已经结束。"""
        return all(step.status.terminal for step in self.steps)


def validate_plan_transition(
    previous: TaskPlan | None,
    current: TaskPlan,
) -> None:
    """校验同一计划的终态不可回退，并约束重新规划必须说明原因。"""
    if previous is None:
        return

    previous_shape = tuple(step.step for step in previous.steps)
    current_shape = tuple(step.step for step in current.steps)
    if previous_shape != current_shape and not current.explanation:
        raise ValueError("调整已有计划的步骤时必须提供 explanation。")

    previous_statuses = {step.step: step.status for step in previous.steps}
    for step in current.steps:
        old_status = previous_statuses.get(step.step)
        if old_status is None:
            continue
        if old_status.terminal and step.status is not old_status:
            raise ValueError(f"已结束的步骤不能回退状态：{step.step}")
        if (
            old_status is PlanStepStatus.IN_PROGRESS
            and step.status is PlanStepStatus.PENDING
        ):
            raise ValueError(f"进行中的步骤不能退回待处理：{step.step}")


def _normalize_explanation(explanation: str | None) -> str | None:
    """清理可选说明，并限制其长度。"""
    if explanation is None:
        return None
    if not isinstance(explanation, str):
        raise ValueError("explanation 必须是字符串。")
    normalized = " ".join(explanation.split())
    if not normalized:
        return None
    if len(normalized) > MAX_PLAN_EXPLANATION_LENGTH:
        raise ValueError(
            f"explanation 不能超过 {MAX_PLAN_EXPLANATION_LENGTH} 个字符。"
        )
    return normalized


__all__ = [
    "MAX_PLAN_STEPS",
    "MIN_PLAN_STEPS",
    "PlanStep",
    "PlanStepStatus",
    "TaskPlan",
    "validate_plan_transition",
]
