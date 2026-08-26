"""验证任务计划在会话数据中的持久化兼容性。"""

import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.memory import Session
from deepseek_agent.planning import (
    PlanExecutionScope,
    PlanKind,
    PlanStepStatus,
    TaskPlan,
)


def _plan_items(
    first_status: str = "in_progress",
    second_status: str = "pending",
) -> list[dict[str, str]]:
    return [
        {"step": "读取项目结构", "status": first_status},
        {"step": "运行回归测试", "status": second_status},
    ]


class PlanningSessionTests(unittest.TestCase):
    def test_session_round_trip_and_old_file_compatibility(self) -> None:
        plan = TaskPlan.create(_plan_items()).with_revision(3)
        session = Session.create([{"role": "system", "content": "system"}])
        session.update_plan(plan)

        restored = Session.from_dict(session.to_dict())
        old_data = session.to_dict()
        old_data.pop("plan")

        legacy_plan = plan.to_dict()
        legacy_plan.pop("id")
        legacy_plan.pop("kind")
        legacy_plan.pop("scope")

        self.assertEqual(restored.plan, plan)
        self.assertIsNone(Session.from_dict(old_data).plan)
        self.assertIs(
            TaskPlan.from_dict(legacy_plan).kind,
            PlanKind.EXECUTION,
        )
        self.assertIs(
            TaskPlan.from_dict(legacy_plan).scope,
            PlanExecutionScope.ENTIRE_PLAN,
        )
        self.assertIs(
            restored.plan.steps[0].status,
            PlanStepStatus.IN_PROGRESS,
        )


if __name__ == "__main__":
    unittest.main()
