"""验证任务计划的数据约束、工具更新和 Agent 集成。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent
from deepseek_agent.cli import CliApplication
from deepseek_agent.config import Settings
from deepseek_agent.memory import JsonSessionStore
from deepseek_agent.models import TextDelta, ToolCallRequest
from deepseek_agent.planning import (
    PlanExecutionScope,
    PlanKind,
    PlanStepStatus,
    TaskPlan,
)
from deepseek_agent.runtime import AgentEventType, RunStatus
from deepseek_agent.reliability import ModelCallError


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
        self.message_batches = []

    def stream(self, messages, tools):
        self.schemas = tools
        self.message_batches.append(list(messages))
        return iter(self.batches.pop(0))

    def select_model(self, _model_name: str) -> None:
        return None

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
    def _update_request(
        call_id: str,
        items,
        *,
        kind: str = "execution",
        scope: str = "entire_plan",
        replace: bool = False,
        explanation: str | None = None,
    ) -> ToolCallRequest:
        arguments = {"kind": kind, "scope": scope, "plan": items}
        if replace:
            arguments["replace"] = True
        if explanation is not None:
            arguments["explanation"] = explanation
        return ToolCallRequest(
            id=call_id,
            name="update_plan",
            arguments=json.dumps(arguments, ensure_ascii=False),
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
        journal_path = next((self.store.directory / ".runs").glob("*.jsonl"))
        journal_events = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        plan_log_events = [
            event
            for event in journal_events
            if event["event"] == "plan_updated"
        ]
        self.assertEqual(
            [event["revision"] for event in plan_log_events],
            [1, 2, 3],
        )
        self.assertNotIn("读取项目结构", journal_path.read_text(encoding="utf-8"))

        stored = self.store.load(agent.session_id)
        self.assertEqual(stored.plan, agent.current_plan)
        restarted = Agent(
            self.settings,
            model=QueueModel([]),
            session_store=self.store,
        )
        self.assertEqual(restarted.current_plan, agent.current_plan)

    def test_simple_next_turn_preserves_latest_plan(self) -> None:
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

        self.assertIsNotNone(agent.current_plan)
        self.assertTrue(agent.current_plan.terminal)
        self.assertEqual(self.store.load(agent.session_id).plan, agent.current_plan)
        self.assertEqual(events[0].type, AgentEventType.TURN_STARTED)
        self.assertNotIn(
            AgentEventType.PLAN_UPDATED,
            [event.type for event in events],
        )

    def test_nonterminal_execution_plan_forces_model_to_continue(self) -> None:
        model = QueueModel(
            [
                [self._update_request("plan_1", _plan_items())],
                [TextDelta("先到这里。")],
                [
                    self._update_request(
                        "plan_2",
                        _plan_items("completed", "completed"),
                    )
                ],
                [TextDelta("任务完成。")],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)

        outcome = agent.chat("执行多步骤任务")

        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.final_text, "任务完成。")
        self.assertIn(
            "你刚才在执行计划尚未结束时停止了工具调用",
            model.message_batches[2][0]["content"],
        )
        self.assertEqual(model.message_batches[2][-1]["role"], "user")
        self.assertIn(
            "[运行时控制]",
            model.message_batches[2][-1]["content"],
        )
        self.assertNotIn(
            "[运行时控制]",
            "\n".join(
                str(message.get("content", ""))
                for message in agent.history()
            ),
        )

    def test_failed_continuation_records_turn_finished_only_once(self) -> None:
        model = QueueModel(
            [
                [self._update_request("plan_1", _plan_items())],
                [TextDelta("提前停止。")],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)

        with self.assertRaises(ModelCallError):
            agent.chat("执行多步骤任务")

        self.assertEqual(model.message_batches[-1][-1]["role"], "user")
        journal_path = next((self.store.directory / ".runs").glob("*.jsonl"))
        events = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            sum(event["event"] == "turn_finished" for event in events),
            1,
        )

    def test_proposal_can_finish_with_pending_steps(self) -> None:
        proposal_items = _plan_items("pending", "pending")
        model = QueueModel(
            [
                [
                    self._update_request(
                        "plan_1",
                        proposal_items,
                        kind="proposal",
                    )
                ],
                [TextDelta("规划已经整理完成。")],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)

        outcome = agent.chat("帮我制定一个规划")

        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertIs(agent.current_plan.kind, PlanKind.PROPOSAL)
        self.assertEqual(agent.current_plan.completed_count, 0)

    def test_single_step_scope_stops_with_remaining_steps_pending(self) -> None:
        model = QueueModel(
            [
                [
                    self._update_request(
                        "plan_1",
                        _plan_items(),
                        scope="single_step",
                    )
                ],
                [
                    self._update_request(
                        "plan_2",
                        _plan_items("completed", "pending"),
                        scope="single_step",
                    )
                ],
                [TextDelta("第一步已完成，剩余步骤留待下一轮。")],
                [
                    self._update_request(
                        "plan_3",
                        _plan_items("completed", "in_progress"),
                        scope="single_step",
                    )
                ],
                [
                    self._update_request(
                        "plan_4",
                        _plan_items("completed", "completed"),
                        scope="single_step",
                    )
                ],
                [TextDelta("第二步已完成。")],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)

        first = agent.chat("只执行第一步")

        self.assertEqual(first.status, RunStatus.COMPLETED)
        self.assertFalse(agent.current_plan.terminal)
        self.assertIs(
            agent.current_plan.scope,
            PlanExecutionScope.SINGLE_STEP,
        )
        self.assertIs(
            agent.current_plan.steps[1].status,
            PlanStepStatus.PENDING,
        )

        second = agent.chat("进入下一步")

        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertTrue(agent.current_plan.terminal)
        self.assertEqual(len(model.message_batches), 6)

    def test_single_step_scope_rejects_advancing_other_steps(self) -> None:
        agent = Agent(
            self.settings,
            model=QueueModel([]),
            session_store=self.store,
        )
        agent._commit_plan(
            TaskPlan.create(
                _plan_items(),
                scope=PlanExecutionScope.SINGLE_STEP,
            )
        )

        with self.assertRaisesRegex(ValueError, "只能推进目标步骤"):
            agent._commit_plan(
                TaskPlan.create(
                    _plan_items("completed", "completed"),
                    scope=PlanExecutionScope.SINGLE_STEP,
                )
            )

        with self.assertRaisesRegex(ValueError, "不能改变计划执行范围"):
            agent._commit_plan(
                TaskPlan.create(
                    _plan_items("completed", "pending"),
                    scope=PlanExecutionScope.ENTIRE_PLAN,
                )
            )

    def test_single_step_uses_previous_snapshot_as_next_turn_target(self) -> None:
        first_finished = [
            {"step": "第一步", "status": "completed"},
            {"step": "第二步", "status": "pending"},
            {"step": "第三步", "status": "pending"},
        ]
        agent = Agent(
            self.settings,
            model=QueueModel([]),
            session_store=self.store,
        )
        agent._commit_plan(
            TaskPlan.create(
                first_finished,
                scope=PlanExecutionScope.SINGLE_STEP,
            )
        )
        # 模拟新一轮开始时清空临时目标，但保留已持久化的计划。
        agent._current_turn_step_target = None
        agent._current_turn_step_boundary_reached = False
        agent._current_turn_plan_scope = None

        with self.assertRaisesRegex(ValueError, "只能推进目标步骤：第二步"):
            agent._commit_plan(
                TaskPlan.create(
                    [
                        {"step": "第一步", "status": "completed"},
                        {"step": "第二步", "status": "completed"},
                        {"step": "第三步", "status": "completed"},
                    ],
                    scope=PlanExecutionScope.SINGLE_STEP,
                )
            )

    def test_single_step_waiting_for_user_ends_current_turn(self) -> None:
        model = QueueModel(
            [
                [
                    self._update_request(
                        "plan_1",
                        _plan_items("waiting_user", "pending"),
                        scope="single_step",
                    )
                ],
                [TextDelta("请先提供所需信息。")],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)

        outcome = agent.chat("只执行需要确认的第一步")

        self.assertEqual(outcome.status, RunStatus.WAITING_USER)
        self.assertTrue(agent.current_plan.waiting_for_user)
        self.assertEqual(len(model.message_batches), 2)

    def test_single_step_discards_tools_requested_after_target_ends(self) -> None:
        model = QueueModel(
            [
                [
                    self._update_request(
                        "plan_1",
                        _plan_items(),
                        scope="single_step",
                    )
                ],
                [
                    self._update_request(
                        "plan_2",
                        _plan_items("completed", "pending"),
                        scope="single_step",
                    )
                ],
                [
                    ToolCallRequest(
                        id="extra_tool",
                        name="calculator",
                        arguments='{"expression":"1 + 1"}',
                    )
                ],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)

        outcome = agent.chat("只执行第一步")

        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(outcome.tool_calls_completed, 2)
        self.assertNotIn("extra_tool", [record.call_id for record in outcome.tool_records])
        self.assertIs(
            agent.current_plan.steps[1].status,
            PlanStepStatus.PENDING,
        )

    def test_waiting_plan_pauses_and_resumes_across_turns(self) -> None:
        waiting_items = _plan_items("waiting_user", "pending")
        model = QueueModel(
            [
                [self._update_request("plan_1", waiting_items)],
                [TextDelta("请提供数据文件。")],
                [
                    self._update_request(
                        "plan_2",
                        _plan_items("completed", "completed"),
                    )
                ],
                [TextDelta("已根据文件完成分析。")],
            ]
        )
        agent = Agent(self.settings, model=model, session_store=self.store)

        first = agent.chat("分析数据")
        plan_id = agent.current_plan.id
        second = agent.chat("文件已经放好了")

        self.assertEqual(first.status, RunStatus.WAITING_USER)
        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertEqual(agent.current_plan.id, plan_id)
        self.assertEqual(agent.current_plan.revision, 2)
        self.assertTrue(agent.current_plan.terminal)
        self.assertIn(plan_id, model.message_batches[2][0]["content"])

    def test_execution_plan_stops_only_at_configured_step_limit(self) -> None:
        model = QueueModel(
            [
                [self._update_request("plan_1", _plan_items())],
                [TextDelta("第一次提前停止。")],
                [TextDelta("第二次提前停止。")],
            ]
        )
        agent = Agent(
            replace(
                self.settings,
                max_agent_steps=3,
                max_finalization_steps=0,
            ),
            model=model,
            session_store=self.store,
        )

        outcome = agent.chat("执行复杂任务")

        self.assertEqual(outcome.status, RunStatus.STEP_LIMIT_REACHED)
        self.assertEqual(outcome.steps_completed, 3)
        self.assertFalse(agent.current_plan.terminal)
        self.assertEqual(
            self.store.load(agent.session_id).plan,
            agent.current_plan,
        )

    def test_finalization_can_close_plan_and_return_final_answer(self) -> None:
        model = QueueModel(
            [
                [self._update_request("plan_1", _plan_items())],
                [
                    self._update_request(
                        "plan_2",
                        _plan_items("completed", "completed"),
                    )
                ],
                [TextDelta("测试已经通过，任务完成。")],
            ]
        )
        agent = Agent(
            replace(
                self.settings,
                max_agent_steps=1,
                max_finalization_steps=2,
            ),
            model=model,
            session_store=self.store,
        )

        outcome = agent.chat("执行复杂任务")

        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(outcome.steps_completed, 3)
        self.assertTrue(agent.current_plan.terminal)
        self.assertEqual(outcome.final_text, "测试已经通过，任务完成。")
        self.assertIn("收尾", model.message_batches[1][-1]["content"])
        self.assertEqual(
            [schema["function"]["name"] for schema in model.schemas],
            ["update_plan"],
        )

    def test_replacing_unfinished_plan_requires_reason_and_new_identity(self) -> None:
        agent = Agent(
            self.settings,
            model=QueueModel([]),
            session_store=self.store,
        )
        first = agent._commit_plan(TaskPlan.create(_plan_items()))

        with self.assertRaisesRegex(ValueError, "必须提供 explanation"):
            agent._commit_plan(
                TaskPlan.create(_plan_items("pending", "pending")),
                True,
            )

        replacement = agent._commit_plan(
            TaskPlan.create(
                _plan_items("pending", "pending"),
                explanation="用户切换到新的任务。",
            ),
            True,
        )

        self.assertNotEqual(replacement.id, first.id)
        self.assertEqual(replacement.revision, 1)

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
        self.assertIn("执行计划 · 连续 v2", output.getvalue())
        self.assertIn("[>] 1. 读取项目结构", output.getvalue())


if __name__ == "__main__":
    unittest.main()
