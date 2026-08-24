"""提供由模型维护公开任务计划的内部状态工具。"""

import json
from collections.abc import Callable

from ..planning import (
    MAX_PLAN_STEPS,
    MIN_PLAN_STEPS,
    PlanExecutionScope,
    PlanKind,
    TaskPlan,
)
from .base import JsonObject, Tool, ToolExecutionError


PlanUpdater = Callable[[TaskPlan, bool], TaskPlan]


class UpdatePlanTool(Tool):
    """校验完整计划快照，并通过 Agent 回调原子提交新状态。"""

    name = "update_plan"
    description = (
        "创建、更新或替换当前任务的结构化计划。实际执行任务使用 execution；"
        "用户只要求一份规划方案时使用 proposal，并把全部步骤设为 pending。"
        "用户只要求执行当前或下一个步骤时使用 single_step；明确要求全部完成时使用 entire_plan。"
        "每次传入完整快照，且最多只能有一个步骤处于 in_progress。"
        "需要用户补充信息时，把当前步骤设为 waiting_user 后再停止。"
    )
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["execution", "proposal"],
                "description": (
                    "execution 用于 Agent 将实际执行的任务；proposal 用于把规划本身作为交付物。"
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["single_step", "entire_plan"],
                "description": (
                    "single_step 在当前目标步骤结束后暂停；entire_plan 持续执行到整个计划结束。"
                ),
            },
            "replace": {
                "type": "boolean",
                "description": (
                    "用户明确切换到无关的新任务时设为 true；替换未结束计划时 explanation 必填。"
                ),
            },
            "explanation": {
                "type": "string",
                "description": (
                    "本次更新的简短原因；修改已有计划的步骤结构时必填。"
                ),
            },
            "plan": {
                "type": "array",
                "description": "包含全部步骤的最新计划快照。",
                "minItems": MIN_PLAN_STEPS,
                "maxItems": MAX_PLAN_STEPS,
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {
                            "type": "string",
                            "description": "一句简洁、可验证的任务步骤。",
                        },
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending",
                                "in_progress",
                                "waiting_user",
                                "completed",
                                "failed",
                                "skipped",
                            ],
                        },
                    },
                    "required": ["step", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["kind", "scope", "plan"],
        "additionalProperties": False,
    }

    def __init__(self, updater: PlanUpdater) -> None:
        self._updater = updater

    def execute(self, arguments: JsonObject) -> str:
        items = arguments.get("plan")
        if not isinstance(items, list):
            raise ToolExecutionError("update_plan 需要数组参数 plan。")
        try:
            plan = TaskPlan.create(
                items,
                kind=arguments.get("kind", PlanKind.EXECUTION.value),
                scope=arguments.get(
                    "scope",
                    PlanExecutionScope.ENTIRE_PLAN.value,
                ),
                explanation=arguments.get("explanation"),
            )
            replace_current = arguments.get("replace", False)
            if not isinstance(replace_current, bool):
                raise ValueError("replace 必须是布尔值。")
            committed = self._updater(plan, replace_current)
        except ValueError as error:
            raise ToolExecutionError(str(error)) from error

        return json.dumps(
            {
                "id": committed.id,
                "kind": committed.kind.value,
                "scope": committed.scope.value,
                "revision": committed.revision,
                "completed": committed.completed_count,
                "total": len(committed.steps),
                "terminal": committed.terminal,
                "plan": [step.to_dict() for step in committed.steps],
            },
            ensure_ascii=False,
        )


__all__ = ["PlanUpdater", "UpdatePlanTool"]
