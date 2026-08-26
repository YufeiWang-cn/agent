"""验证任务计划的数据约束和工具更新规则。"""

import json
import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.planning import (
    PlanKind,
    TaskPlan,
    validate_plan_transition,
)
from deepseek_agent.tools import UpdatePlanTool


def _plan_items(
    first_status: str = "in_progress",
    second_status: str = "pending",
) -> list[dict[str, str]]:
    return [
        {"step": "读取项目结构", "status": first_status},
        {"step": "运行回归测试", "status": second_status},
    ]


class PlanningModelTests(unittest.TestCase):
    def test_plan_rejects_multiple_active_steps_and_duplicate_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "最多只能有一个"):
            TaskPlan.create(_plan_items("in_progress", "in_progress"))

        with self.assertRaisesRegex(ValueError, "重复步骤"):
            TaskPlan.create(
                [
                    {"step": "同一步骤", "status": "in_progress"},
                    {"step": "同一步骤", "status": "pending"},
                ]
            )

    def test_terminal_step_cannot_regress(self) -> None:
        previous = TaskPlan.create(_plan_items("completed", "in_progress"))
        current = TaskPlan.create(_plan_items("pending", "in_progress"))

        with self.assertRaisesRegex(ValueError, "不能回退"):
            validate_plan_transition(previous, current)

    def test_replanning_requires_explanation(self) -> None:
        previous = TaskPlan.create(_plan_items())
        changed = TaskPlan.create(
            [
                {"step": "读取项目结构", "status": "completed"},
                {"step": "补充测试", "status": "in_progress"},
            ]
        )

        with self.assertRaisesRegex(ValueError, "必须提供 explanation"):
            validate_plan_transition(previous, changed)

    def test_update_plan_tool_returns_committed_revision(self) -> None:
        def commit(plan: TaskPlan, _replace: bool) -> TaskPlan:
            return plan.with_revision(4)

        result = json.loads(
            UpdatePlanTool(commit).execute(
                {
                    "kind": "execution",
                    "scope": "entire_plan",
                    "plan": _plan_items(),
                }
            )
        )

        self.assertEqual(result["revision"], 4)
        self.assertEqual(result["kind"], "execution")
        self.assertEqual(result["scope"], "entire_plan")
        self.assertEqual(result["completed"], 0)
        self.assertFalse(result["terminal"])

    def test_proposal_is_terminal_without_claiming_steps_completed(self) -> None:
        plan = TaskPlan.create(
            _plan_items("pending", "pending"),
            kind=PlanKind.PROPOSAL,
        )

        self.assertTrue(plan.terminal)
        self.assertEqual(plan.completed_count, 0)

        with self.assertRaisesRegex(ValueError, "必须全部为 pending"):
            TaskPlan.create(_plan_items(), kind=PlanKind.PROPOSAL)


if __name__ == "__main__":
    unittest.main()
