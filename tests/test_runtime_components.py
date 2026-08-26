"""验证拆分后的计划运行时和 GUI 后台执行组件。"""

import queue
import threading
import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.plan_runtime import PlanRuntime
from deepseek_agent.planning import TaskPlan
from deepseek_agent.runtime import RunStatus, TurnOutcome
from deepseek_agent.ui.chat_runner import AgentChatRunner


class PlanRuntimeTests(unittest.TestCase):
    """验证计划状态机不依赖 Agent 也能独立工作。"""

    def test_commit_tracks_single_step_target_and_injects_runtime_context(self) -> None:
        runtime = PlanRuntime()
        initial = TaskPlan.create(
            [
                {"step": "读取代码", "status": "pending"},
                {"step": "整理结论", "status": "pending"},
            ],
            scope="single_step",
        )
        committed = runtime.commit(initial)
        runtime.begin_turn()
        updated = TaskPlan.create(
            [
                {"step": "读取代码", "status": "in_progress"},
                {"step": "整理结论", "status": "pending"},
            ],
            scope="single_step",
        )

        runtime.commit(updated)
        original_messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "开始"},
        ]
        prepared = runtime.attach_to_messages(
            original_messages,
            force_continuation=False,
        )

        self.assertEqual(committed.revision, 1)
        self.assertEqual(runtime.target, "读取代码")
        self.assertIn("[运行时计划状态]", prepared[0]["content"])
        self.assertEqual(original_messages[0]["content"], "system")


class FakeRunnerAgent:
    """提供 AgentChatRunner 所需的最小 Agent 接口。"""

    last_turn_outcome = None
    last_turn_history_preserved = False

    def chat(self, prompt: str, **callbacks: object) -> TurnOutcome:
        on_text = callbacks["on_text"]
        assert callable(on_text)
        on_text(f"收到：{prompt}")
        return TurnOutcome(
            status=RunStatus.WAITING_USER,
            final_text="请补充信息。",
            steps_completed=1,
            tool_calls_completed=0,
            history_preserved=True,
        )


class AgentChatRunnerTests(unittest.TestCase):
    """验证后台执行组件能够稳定映射流式和终止事件。"""

    def test_runner_maps_agent_result_to_gui_events(self) -> None:
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        runner = AgentChatRunner(FakeRunnerAgent(), events, threading.Event())

        runner.run("继续")

        self.assertEqual(events.get_nowait(), ("text", "收到：继续"))
        event_name, outcome = events.get_nowait()
        self.assertEqual(event_name, "waiting_user")
        self.assertEqual(outcome.status, RunStatus.WAITING_USER)


if __name__ == "__main__":
    unittest.main()
