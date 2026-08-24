"""验证任务计划的数据约束、工具更新和 Agent 集成。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent
from deepseek_agent.cli import CliApplication
from deepseek_agent.config import Settings
from deepseek_agent.memory import JsonSessionStore, Session
from deepseek_agent.models import TextDelta, ToolCallRequest
from deepseek_agent.planning import (
    PlanStepStatus,
    TaskPlan,
    validate_plan_transition,
)
from deepseek_agent.runtime import AgentEventType, RunStatus
from deepseek_agent.tools import UpdatePlanTool


def _plan_items(
    first_status: str = "in_progress",
    second_status: str = "pending",
) -> list[dict[str, str]]:
    return [
        {"step": "读取项目结构", "status": first_status},
        {"step": "运行回归测试", "status": second_status},
    ]


class QueueModel:
    """按测试预设顺序返回模型事件。"""

    model_name = "queue-model"
    available_models = ("queue-model",)

    def __init__(self, batches) -> None:
        self.batches = list(batches)
        self.schemas = []

    def stream(self, _messages, tools):
        self.schemas = tools
        return iter(self.batches.pop(0))

    def select_model(self, _model_name: str) -> None:
        return None


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
        def commit(plan: TaskPlan) -> TaskPlan:
            return plan.with_revision(4)

        result = json.loads(
            UpdatePlanTool(commit).execute({"plan": _plan_items()})
        )

        self.assertEqual(result["revision"], 4)
        self.assertEqual(result["completed"], 0)
        self.assertFalse(result["terminal"])


class PlanningAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = JsonSessionStore(Path(self.temporary_directory.name))
        self.settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="queue-model",
            system_prompt="system",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _update_request(call_id: str, items) -> ToolCallRequest:
        return ToolCallRequest(
            id=call_id,
            name="update_plan",
            arguments=json.dumps({"plan": items}, ensure_ascii=False),
        )

    def test_agent_updates_emits_and_restores_plan(self) -> None:
        model = QueueModel(
            [
                [self._update_request("plan_1", _plan_items())],
                [
                    self._update_request(
                        "plan_2",
                        _plan_items("completed", "in_progress"),
                    )
                ],
                [
                    self._update_request(
                        "plan_3",
                        _plan_items("completed", "completed"),
                    )
                ],
                [TextDelta("任务已经完成。")],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)
        events = []

        outcome = agent.chat("完成复杂任务", on_event=events.append)

        self.assertTrue(outcome.succeeded)
        self.assertIn("update_plan", agent.tool_names)
        self.assertIn(
            "update_plan",
            [schema["function"]["name"] for schema in model.schemas],
        )
        plan_events = [
            event
            for event in events
            if event.type is AgentEventType.PLAN_UPDATED
        ]
        self.assertEqual(
            [event.plan.revision for event in plan_events],
            [1, 2, 3],
        )
        self.assertIsNotNone(agent.current_plan)
        self.assertTrue(agent.current_plan.terminal)
        self.assertEqual(agent.current_plan.completed_count, 2)

        stored = self.store.load(agent.session_id)
        self.assertEqual(stored.plan, agent.current_plan)
        restarted = Agent(
            self.settings,
            model=QueueModel([]),
            session_store=self.store,
        )
        self.assertEqual(restarted.current_plan, agent.current_plan)

    def test_simple_next_turn_clears_previous_plan(self) -> None:
        model = QueueModel(
            [
                [self._update_request("plan_1", _plan_items())],
                [
                    self._update_request(
                        "plan_2",
                        _plan_items("completed", "completed"),
                    )
                ],
                [TextDelta("复杂任务完成。")],
                [TextDelta("简单回答。")],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)
        agent.chat("复杂任务")
        events = []

        agent.chat("简单问题", on_event=events.append)

        self.assertIsNone(agent.current_plan)
        self.assertIsNone(self.store.load(agent.session_id).plan)
        self.assertEqual(
            [event.type for event in events[:2]],
            [AgentEventType.TURN_STARTED, AgentEventType.PLAN_UPDATED],
        )
        self.assertIsNone(events[1].plan)

    def test_nonterminal_plan_is_not_reported_as_success(self) -> None:
        model = QueueModel(
            [
                [self._update_request("plan_1", _plan_items())],
                [TextDelta("先到这里。")],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)

        outcome = agent.chat("执行多步骤任务")

        self.assertEqual(outcome.status, RunStatus.PLAN_INCOMPLETE)
        self.assertFalse(outcome.succeeded)
        self.assertTrue(outcome.status.terminal)
        self.assertEqual(outcome.final_text, "先到这里。")

    def test_cli_plan_command_prints_latest_plan(self) -> None:
        agent = Agent(
            self.settings,
            model=QueueModel([[TextDelta("回答")]]),
            session_store=self.store,
        )
        agent._current_plan = TaskPlan.create(_plan_items()).with_revision(2)
        output = io.StringIO()

        with redirect_stdout(output):
            handled = CliApplication(agent).handle_command("/plan")

        self.assertTrue(handled)
        self.assertIn("任务计划 v2", output.getvalue())
        self.assertIn("[>] 1. 读取项目结构", output.getvalue())


class PlanningSessionTests(unittest.TestCase):
    def test_session_round_trip_and_old_file_compatibility(self) -> None:
        plan = TaskPlan.create(_plan_items()).with_revision(3)
        session = Session.create([{"role": "system", "content": "system"}])
        session.update_plan(plan)

        restored = Session.from_dict(session.to_dict())
        old_data = session.to_dict()
        old_data.pop("plan")

        self.assertEqual(restored.plan, plan)
        self.assertIsNone(Session.from_dict(old_data).plan)
        self.assertIs(
            restored.plan.steps[0].status,
            PlanStepStatus.IN_PROGRESS,
        )


if __name__ == "__main__":
    unittest.main()
