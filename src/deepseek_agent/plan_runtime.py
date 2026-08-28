"""管理计划快照、单步执行边界和模型运行时提示。"""

import json
from dataclasses import dataclass

from .conversation import Message
from .planning import (
    PlanExecutionScope,
    PlanKind,
    PlanStepStatus,
    TaskPlan,
    validate_plan_transition,
)


@dataclass(slots=True)
class PlanTurnState:
    """保存仅在当前对话轮次内有效的计划执行边界。"""

    target: str | None = None
    boundary_reached: bool = False
    scope: PlanExecutionScope | None = None

    def reset(self) -> None:
        """清空上一轮留下的目标步骤和执行范围。"""
        self.target = None
        self.boundary_reached = False
        self.scope = None


class PlanRuntime:
    """集中维护计划状态，避免 Agent 同时承担计划状态机职责。"""

    def __init__(self) -> None:
        self._current: TaskPlan | None = None
        self._turn = PlanTurnState()

    @property
    def current(self) -> TaskPlan | None:
        """返回当前会话最近一次公开计划快照。"""
        return self._current

    def restore(self, plan: TaskPlan | None) -> None:
        """恢复会话计划，并清除不应跨轮次保留的执行边界。"""
        self._current = plan
        self._turn.reset()

    def begin_turn(self) -> None:
        """在新轮次开始前重置单步执行状态。"""
        self._turn.reset()

    @property
    def target(self) -> str | None:
        """返回当前轮次锁定的单步目标。"""
        return self._turn.target

    @target.setter
    def target(self, value: str | None) -> None:
        self._turn.target = value

    @property
    def boundary_reached(self) -> bool:
        """返回当前单步目标是否已经结束或暂停。"""
        return self._turn.boundary_reached

    @boundary_reached.setter
    def boundary_reached(self, value: bool) -> None:
        self._turn.boundary_reached = value

    @property
    def scope(self) -> PlanExecutionScope | None:
        """返回本轮首次计划更新确定的执行范围。"""
        return self._turn.scope

    @scope.setter
    def scope(self, value: PlanExecutionScope | None) -> None:
        self._turn.scope = value

    def commit(self, plan: TaskPlan, replace_current: bool = False) -> TaskPlan:
        """校验并提交完整计划快照。"""
        previous = self._current
        starts_new_plan = previous is None or previous.terminal or replace_current
        if starts_new_plan:
            replacing_active_plan = (
                previous is not None and not previous.terminal and replace_current
            )
            if replacing_active_plan and not plan.explanation:
                raise ValueError("替换未结束的计划时必须提供 explanation。")
            committed = plan.with_identity(plan.id, 1)
        else:
            assert previous is not None
            validate_plan_transition(previous, plan)
            committed = plan.with_identity(previous.id, previous.revision + 1)

        # 先验证单步边界，再公开新快照，避免非法更新污染当前状态。
        self._track_single_step_progress(previous, committed)
        self._current = committed
        return committed

    def attach_to_messages(
        self,
        messages: list[Message],
        *,
        force_continuation: bool,
        finalization: bool = False,
    ) -> list[Message]:
        """把计划快照作为临时约束注入模型消息，不修改会话历史。"""
        prepared = [dict(message) for message in messages]
        if not prepared:
            return prepared

        finalization_message = (
            "[运行时控制] 常规执行步数已经用尽，现在只能收尾。"
            "如现有计划状态不准确，只能调用 update_plan 闭合计划；"
            "否则不得调用工具，必须根据已有结果给出最终回答。"
            "不得请求其他工具或开展新工作。"
        )
        if self._current is None:
            if finalization:
                prepared.append({"role": "user", "content": finalization_message})
            return prepared

        plan = self._current
        instruction = self._runtime_instruction(plan)
        if force_continuation and not finalization:
            instruction += self._continuation_instruction(plan)

        snapshot = json.dumps(plan.to_dict(), ensure_ascii=False)
        runtime_context = f"\n\n[运行时计划状态]\n{instruction}\n{snapshot}"
        first = prepared[0]
        prepared[0] = {
            **first,
            "content": str(first.get("content", "")) + runtime_context,
        }
        if finalization:
            prepared.append({"role": "user", "content": finalization_message})
        elif force_continuation:
            # DeepSeek 不接受以普通 assistant 消息结尾的续执行请求。
            prepared.append(
                {
                    "role": "user",
                    "content": (
                        "[运行时控制] 当前目标尚未结束。请遵守计划的 scope，"
                        "继续允许范围内的工作，或调用 update_plan 准确更新状态。"
                    ),
                }
            )
        return prepared

    def _track_single_step_progress(
        self,
        previous: TaskPlan | None,
        current: TaskPlan,
    ) -> None:
        """锁定本轮单步目标，并拒绝同时推进其他步骤。"""
        if previous is not None and previous.id != current.id:
            self._turn.reset()

        if self._turn.scope is None:
            self._turn.scope = current.scope
        else:
            same_plan = previous is not None and previous.id == current.id
            if same_plan and current.scope is not self._turn.scope:
                raise ValueError("同一轮内不能改变计划执行范围。")

        is_execution_plan = current.kind is PlanKind.EXECUTION
        uses_single_step = (
            is_execution_plan and current.scope is PlanExecutionScope.SINGLE_STEP
        )
        if not uses_single_step:
            self._turn.target = None
            self._turn.boundary_reached = False
            return

        previous_statuses = (
            {step.step: step.status for step in previous.steps}
            if previous is not None and previous.id == current.id
            else {}
        )
        if self._turn.target is None:
            self._turn.target = self._select_single_step_target(
                previous,
                current,
                previous_statuses,
            )

        if previous is not None and previous.id == current.id:
            self._validate_single_step_changes(previous, current, previous_statuses)

        if self._turn.target is None:
            self._turn.boundary_reached = current.terminal
            return

        target_status = next(
            (
                step.status
                for step in current.steps
                if step.step == self._turn.target
            ),
            None,
        )
        if target_status is None:
            raise ValueError("single_step 模式不能在本轮删除目标步骤。")
        self._turn.boundary_reached = (
            target_status.terminal or target_status is PlanStepStatus.WAITING_USER
        )

    def _select_single_step_target(
        self,
        previous: TaskPlan | None,
        current: TaskPlan,
        previous_statuses: dict[str, PlanStepStatus],
    ) -> str | None:
        """按照活动步骤、新结束步骤和待处理步骤的顺序选择本轮目标。"""
        same_plan = previous is not None and previous.id == current.id
        target_steps = (
            previous.steps
            if same_plan and previous is not None
            else current.steps
        )
        active_step = next(
            (
                step
                for step in target_steps
                if step.status in {
                    PlanStepStatus.IN_PROGRESS,
                    PlanStepStatus.WAITING_USER,
                }
            ),
            None,
        )
        newly_finished = next(
            (
                step
                for step in current.steps
                if self._became_terminal(step.step, step.status, previous_statuses)
            ),
            None,
        )
        pending_step = next(
            (
                step
                for step in target_steps
                if step.status is PlanStepStatus.PENDING
            ),
            None,
        )
        target = active_step or newly_finished or pending_step
        return target.step if target is not None else None

    @staticmethod
    def _became_terminal(
        step_text: str,
        current_status: PlanStepStatus,
        previous_statuses: dict[str, PlanStepStatus],
    ) -> bool:
        """返回步骤是否在当前快照中首次进入终态。"""
        previous_status = previous_statuses.get(
            step_text,
            PlanStepStatus.PENDING,
        )
        return current_status.terminal and not previous_status.terminal

    def _validate_single_step_changes(
        self,
        previous: TaskPlan,
        current: TaskPlan,
        previous_statuses: dict[str, PlanStepStatus],
    ) -> None:
        """确认本轮只有锁定目标发生了状态变化。"""
        for step in current.steps:
            if step.step == self._turn.target:
                continue
            old_status = previous_statuses.get(step.step)
            if old_status is not None and step.status is not old_status:
                raise ValueError(
                    "single_step 模式本轮只能推进目标步骤："
                    f"{self._turn.target}"
                )

    def _runtime_instruction(self, plan: TaskPlan) -> str:
        """根据计划用途和当前边界生成模型约束。"""
        if plan.kind is PlanKind.PROPOSAL:
            return "当前会话最近保存的是已经交付的规划方案，不代表这些步骤已经执行。"
        if plan.terminal:
            return "当前执行计划已经结束，可以处理用户的新请求。"
        if plan.scope is PlanExecutionScope.SINGLE_STEP:
            return self._single_step_instruction(plan)
        return (
            "当前执行计划仍然有效。继续相关任务时必须基于该快照推进并调用 "
            "update_plan 更新状态；用户切换到无关的新复杂任务时，应使用 "
            "replace=true 替换计划并说明原因；切换到简单请求时，应先把旧计划"
            "未结束步骤标为 skipped。需要用户补充信息时，先把当前步骤设为 "
            "waiting_user，再向用户提问。"
        )

    def _single_step_instruction(self, plan: TaskPlan) -> str:
        """生成只允许推进一个目标步骤的模型约束。"""
        target = self._turn.target or next(
            (
                step.step
                for step in plan.steps
                if step.status in {
                    PlanStepStatus.IN_PROGRESS,
                    PlanStepStatus.WAITING_USER,
                    PlanStepStatus.PENDING,
                }
            ),
            "当前步骤",
        )
        if self._turn.boundary_reached:
            return (
                f"本轮 single_step 目标“{target}”已经结束。"
                "不得调用工具或开始后续步骤，只需简洁总结本步结果并结束本轮。"
            )
        return (
            f"当前计划采用 single_step，本轮只允许执行目标“{target}”。"
            "完成、失败、跳过或等待用户后应调用 update_plan 更新该步骤，"
            "然后结束本轮；不得推进后续步骤。"
        )

    @staticmethod
    def _continuation_instruction(plan: TaskPlan) -> str:
        """生成模型过早停止工具调用时使用的续执行提示。"""
        if plan.scope is PlanExecutionScope.SINGLE_STEP:
            return (
                " 你刚才在单步目标尚未结束时停止了工具调用。"
                "请只继续当前目标，不得开始后续步骤。"
            )
        return (
            " 你刚才在执行计划尚未结束时停止了工具调用。不要重复总结；"
            "请继续执行当前步骤，或者用 update_plan 准确更新为完成、失败、"
            "跳过或等待用户。"
        )


__all__ = ["PlanRuntime", "PlanTurnState"]
