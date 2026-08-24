"""提供由模型维护公开任务计划的内部状态工具。"""

import json
from collections.abc import Callable

from ..planning import MAX_PLAN_STEPS, MIN_PLAN_STEPS, TaskPlan
from .base import JsonObject, Tool, ToolExecutionError


PlanUpdater = Callable[[TaskPlan], TaskPlan]


class UpdatePlanTool(Tool):
    """校验完整计划快照，并通过 Agent 回调原子提交新状态。"""

    name = "update_plan"
    description = (
        "创建或更新当前任务的简洁执行计划。仅在任务确实需要多个步骤时调用；"
        "简单问答或单一步骤操作不要调用。每次传入完整计划，且最多只能有一个步骤处于 in_progress。"
        "不要在步骤文本中写分析过程或内部推理。"
    )
    parameters: JsonObject = {
        "type": "object",
        "properties": {
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
        "required": ["plan"],
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
                explanation=arguments.get("explanation"),
            )
            committed = self._updater(plan)
        except ValueError as error:
            raise ToolExecutionError(str(error)) from error

        return json.dumps(
            {
                "revision": committed.revision,
                "completed": committed.completed_count,
                "total": len(committed.steps),
                "terminal": committed.terminal,
                "plan": [step.to_dict() for step in committed.steps],
            },
            ensure_ascii=False,
        )


__all__ = ["PlanUpdater", "UpdatePlanTool"]
