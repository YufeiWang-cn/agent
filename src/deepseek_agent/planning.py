"""定义结构化任务计划、步骤状态和更新约束。"""

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable
from uuid import uuid4


MIN_PLAN_STEPS = 2
MAX_PLAN_STEPS = 7
MAX_PLAN_STEP_LENGTH = 160
MAX_PLAN_EXPLANATION_LENGTH = 500


class PlanKind(str, Enum):
    """区分用于实际执行的计划和作为回答交付的规划方案。"""

    EXECUTION = "execution"
    PROPOSAL = "proposal"


class PlanExecutionScope(str, Enum):
    """表示执行计划在一轮中推进一个步骤还是持续推进全部步骤。"""

    SINGLE_STEP = "single_step"
    ENTIRE_PLAN = "entire_plan"


class PlanStepStatus(str, Enum):
    """表示一个计划步骤当前所处的公开执行状态。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    WAITING_USER = "waiting_user"
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
    """保存具有稳定身份、明确用途和递增版本号的计划快照。"""

    steps: tuple[PlanStep, ...]
    kind: PlanKind = PlanKind.EXECUTION
    scope: PlanExecutionScope = PlanExecutionScope.ENTIRE_PLAN
    explanation: str | None = None
    revision: int = 1
    id: str = ""

    @classmethod
    def create(
        cls,
        items: Iterable[dict[str, Any]],
        *,
        kind: PlanKind | str = PlanKind.EXECUTION,
        scope: PlanExecutionScope | str = PlanExecutionScope.ENTIRE_PLAN,
        explanation: str | None = None,
        revision: int = 1,
        plan_id: str | None = None,
    ) -> "TaskPlan":
        """从工具参数创建经过完整结构校验的计划。"""
        raw_items = list(items)
        if not MIN_PLAN_STEPS <= len(raw_items) <= MAX_PLAN_STEPS:
            raise ValueError(
                f"计划必须包含 {MIN_PLAN_STEPS} 到 {MAX_PLAN_STEPS} 个步骤。"
            )
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("计划版本号必须是正整数。")
        try:
            normalized_kind = PlanKind(kind)
        except (TypeError, ValueError) as error:
            raise ValueError(f"无效的计划用途：{kind}") from error
        try:
            normalized_scope = PlanExecutionScope(scope)
        except (TypeError, ValueError) as error:
            raise ValueError(f"无效的执行范围：{scope}") from error
        normalized_id = _normalize_plan_id(plan_id)

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
            step.status
            in {PlanStepStatus.IN_PROGRESS, PlanStepStatus.WAITING_USER}
            for step in steps
        )
        if active_count > 1:
            raise ValueError("同一时间最多只能有一个进行中或等待用户的步骤。")
        if normalized_kind is PlanKind.PROPOSAL and any(
            step.status is not PlanStepStatus.PENDING for step in steps
        ):
            raise ValueError("规划方案中的步骤必须全部为 pending。")
        return cls(
            tuple(steps),
            normalized_kind,
            normalized_scope,
            normalized_explanation,
            revision,
            normalized_id,
        )

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
            kind=data.get("kind", PlanKind.EXECUTION.value),
            scope=data.get("scope", PlanExecutionScope.ENTIRE_PLAN.value),
            explanation=data.get("explanation"),
            revision=data.get("revision", 1),
            plan_id=data.get("id"),
        )

    def to_dict(self) -> dict[str, Any]:
        """返回可直接写入会话 JSON 的计划快照。"""
        return {
            "id": self.id,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "revision": self.revision,
            "explanation": self.explanation,
            "steps": [step.to_dict() for step in self.steps],
        }

    def with_revision(self, revision: int) -> "TaskPlan":
        """返回内容不变但版本号已更新的新计划。"""
        if revision < 1:
            raise ValueError("计划版本号必须是正整数。")
        return replace(self, revision=revision)

    def with_identity(self, plan_id: str, revision: int) -> "TaskPlan":
        """返回使用指定稳定身份和版本号的新计划快照。"""
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValueError("计划标识必须是非空字符串。")
        if revision < 1:
            raise ValueError("计划版本号必须是正整数。")
        return replace(self, id=plan_id.strip(), revision=revision)

    @property
    def completed_count(self) -> int:
        """返回已经成功完成的步骤数量。"""
        return sum(
            step.status is PlanStepStatus.COMPLETED for step in self.steps
        )

    @property
    def terminal(self) -> bool:
        """返回规划是否已交付，或执行计划中的步骤是否都已结束。"""
        return self.kind is PlanKind.PROPOSAL or all(
            step.status.terminal for step in self.steps
        )

    @property
    def waiting_for_user(self) -> bool:
        """返回执行计划是否已经明确暂停并等待用户输入。"""
        return self.kind is PlanKind.EXECUTION and any(
            step.status is PlanStepStatus.WAITING_USER for step in self.steps
        )


def validate_plan_transition(
    previous: TaskPlan | None,
    current: TaskPlan,
) -> None:
    """校验同一计划的终态不可回退，并约束重新规划必须说明原因。"""
    if previous is None:
        return
    if previous.kind is not current.kind:
        raise ValueError("更新同一计划时不能改变计划用途。")

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


def _normalize_plan_id(plan_id: str | None) -> str:
    """校验持久化标识，并为新计划生成随机标识。"""
    if plan_id is None:
        return uuid4().hex
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise ValueError("计划标识必须是非空字符串。")
    return plan_id.strip()


__all__ = [
    "MAX_PLAN_STEPS",
    "MIN_PLAN_STEPS",
    "PlanExecutionScope",
    "PlanKind",
    "PlanStep",
    "PlanStepStatus",
    "TaskPlan",
    "validate_plan_transition",
]
