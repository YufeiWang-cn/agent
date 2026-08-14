import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _path_setup import add_project_root_to_path

add_project_root_to_path()

from src.deepseek_agent import (
    Agent,
    AgentCancelledError,
    AgentEventType,
    RunStatus,
    ToolEffect,
    ToolExecutionStatus,
)
from src.deepseek_agent.config import Settings
from src.deepseek_agent.conversation import Message
from src.deepseek_agent.memory import JsonProjectStore, JsonSessionStore
from src.deepseek_agent.models import StreamEvent, TextDelta, ToolCallRequest
from src.deepseek_agent.tools import ToolRegistry, WriteTextFileTool
from src.deepseek_agent.workspace import WorkspaceGuard


class CallbackModel:
    model_name = "callback-model"
    available_models = ("callback-model", "alternate-model")

    def __init__(self, events: list[list[StreamEvent]]) -> None:
        self._events = events
        self.calls: list[list[Message]] = []

    def stream(self, messages, tools):
        self.calls.append(list(messages))
        return iter(self._events.pop(0))

    def select_model(self, model_name: str) -> None:
        if model_name not in self.available_models:
            raise ValueError("unsupported model")
        self.model_name = model_name


class AllowAllConfirmer:
    @staticmethod
    def confirm(_tool, _arguments: str) -> bool:
        return True


class FailingAfterToolModel:
    model_name = "callback-model"
    available_models = ("callback-model",)

    def __init__(self, request: ToolCallRequest) -> None:
        self._request = request
        self._calls = 0

    def stream(self, messages, tools):
        self._calls += 1
        if self._calls == 1:
            return iter([self._request])
        raise RuntimeError("provider down")

    def select_model(self, model_name: str) -> None:
        return None


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = JsonSessionStore(Path(self.temporary_directory.name))
        self.project_store = JsonProjectStore(
            Path(self.temporary_directory.name) / "projects.json"
        )
        self.settings = Settings(
            api_key="test",
            base_url="https://example.invalid",
            model="callback-model",
            system_prompt="system",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_chat_reports_streaming_text_through_callback(self) -> None:
        model = CallbackModel([[TextDelta("你"), TextDelta("好")]])
        agent = Agent(self.settings, model=model, session_store=self.store)
        parts: list[str] = []

        outcome = agent.chat("测试", on_text=parts.append)

        self.assertEqual(parts, ["你", "好"])
        self.assertEqual(agent.history()[-1]["content"], "你好")
        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.final_text, "你好")
        self.assertEqual(outcome.steps_completed, 1)
        self.assertEqual(outcome.tool_calls_completed, 0)
        self.assertTrue(outcome.history_preserved)
        self.assertIs(agent.last_turn_outcome, outcome)
        self.assertEqual(agent.run_status, RunStatus.COMPLETED)

    def test_chat_emits_structured_runtime_events(self) -> None:
        request = ToolCallRequest(
            id="call_event",
            name="calculator",
            arguments='{"expression":"2 + 3"}',
        )
        model = CallbackModel([[request], [TextDelta("结果是 5")]])
        agent = Agent(self.settings, model=model, session_store=self.store)
        events = []

        outcome = agent.chat("计算", on_event=events.append)

        self.assertEqual(
            [event.type for event in events],
            [
                AgentEventType.TURN_STARTED,
                AgentEventType.TOOL_CALL,
                AgentEventType.TOOL_RESULT,
                AgentEventType.TEXT_DELTA,
            ],
        )
        self.assertIs(events[1].tool_call, request)
        self.assertEqual(events[2].tool_result, "5")
        self.assertEqual(
            events[2].tool_record.status,
            ToolExecutionStatus.SUCCEEDED,
        )
        self.assertEqual(events[2].tool_record.effect, ToolEffect.READ_ONLY)
        self.assertEqual(events[3].content, "结果是 5")
        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(outcome.steps_completed, 2)
        self.assertEqual(outcome.tool_calls_completed, 1)
        self.assertEqual(outcome.tool_records, (events[2].tool_record,))

    def test_model_can_be_switched_for_future_requests(self) -> None:
        model = CallbackModel([])
        agent = Agent(self.settings, model=model, session_store=self.store)

        self.assertEqual(agent.available_models, model.available_models)
        agent.select_model("alternate-model")

        self.assertEqual(agent.model_name, "alternate-model")

    def test_chat_reports_tool_call_and_result(self) -> None:
        request = ToolCallRequest(
            id="call_1",
            name="calculator",
            arguments='{"expression":"2 + 3"}',
        )
        model = CallbackModel([[request], [TextDelta("结果是 5")]])
        agent = Agent(self.settings, model=model, session_store=self.store)
        calls: list[str] = []
        results: list[str] = []

        agent.chat(
            "计算",
            on_tool_call=lambda value: calls.append(value.name),
            on_tool_result=lambda value, result: results.append(
                f"{value.name}:{result}"
            ),
        )

        self.assertEqual(calls, ["calculator"])
        self.assertEqual(results, ["calculator:5"])

    def test_step_limit_is_not_reported_as_success(self) -> None:
        requests = [
            [
                ToolCallRequest(
                    id=f"call_{index}",
                    name="calculator",
                    arguments='{"expression":"2 + 3"}',
                )
            ]
            for index in range(5)
        ]
        model = CallbackModel(requests)
        agent = Agent(self.settings, model=model, session_store=self.store)

        outcome = agent.chat("持续调用工具")

        self.assertEqual(outcome.status, RunStatus.STEP_LIMIT_REACHED)
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.steps_completed, 5)
        self.assertEqual(outcome.tool_calls_completed, 5)
        self.assertIn("最大执行步数", outcome.final_text)
        self.assertIs(agent.last_turn_outcome, outcome)

    def test_tool_result_is_recorded_before_callback_failure(self) -> None:
        request = ToolCallRequest(
            id="call_callback",
            name="calculator",
            arguments='{"expression":"2 + 3"}',
        )
        model = CallbackModel([[request]])
        agent = Agent(self.settings, model=model, session_store=self.store)

        def fail_after_tool(_request, _result: str) -> None:
            raise RuntimeError("UI callback failed")

        with self.assertRaisesRegex(RuntimeError, "UI callback failed"):
            agent.chat("计算", on_tool_result=fail_after_tool)

        self.assertTrue(agent.last_turn_history_preserved)
        self.assertEqual(
            [message["role"] for message in agent.history()],
            ["user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(agent.history()[2]["content"], "5")

    def test_cancelled_chat_rolls_back_incomplete_turn(self) -> None:
        model = CallbackModel([[TextDelta("部分"), TextDelta("回答")]])
        agent = Agent(self.settings, model=model, session_store=self.store)
        cancelled = False

        def collect(_content: str) -> None:
            nonlocal cancelled
            cancelled = True

        with self.assertRaises(AgentCancelledError):
            agent.chat(
                "会被停止",
                on_text=collect,
                should_cancel=lambda: cancelled,
            )

        self.assertEqual(agent.history(), [])
        self.assertEqual(agent.last_turn_outcome.status, RunStatus.CANCELLED)
        self.assertFalse(agent.last_turn_outcome.history_preserved)
        self.assertEqual(agent.last_turn_outcome.tool_calls_completed, 0)

    def test_cancelled_before_first_model_call_reports_zero_steps(self) -> None:
        model = CallbackModel([])
        agent = Agent(self.settings, model=model, session_store=self.store)

        with self.assertRaises(AgentCancelledError) as caught:
            agent.chat("立即停止", should_cancel=lambda: True)

        self.assertEqual(caught.exception.outcome.status, RunStatus.CANCELLED)
        self.assertEqual(caught.exception.outcome.steps_completed, 0)
        self.assertEqual(agent.history(), [])
        self.assertEqual(agent.run_status, RunStatus.CANCELLED)

    def test_cancel_after_file_write_preserves_tool_records(self) -> None:
        workspace = Path(self.temporary_directory.name) / "workspace"
        workspace.mkdir()
        first_path = workspace / "first.txt"
        second_path = workspace / "second.txt"
        requests = [
            ToolCallRequest(
                id="call_1",
                name="write_text_file",
                arguments='{"path":"first.txt","content":"已写入"}',
            ),
            ToolCallRequest(
                id="call_2",
                name="write_text_file",
                arguments='{"path":"second.txt","content":"不应写入"}',
            ),
        ]
        model = CallbackModel([requests])
        guard = WorkspaceGuard(workspace, max_file_size=1_000)
        agent = Agent(
            self.settings,
            model=model,
            tools=ToolRegistry([WriteTextFileTool(guard)]),
            session_store=self.store,
            confirmer=AllowAllConfirmer(),
        )

        with self.assertRaises(AgentCancelledError) as caught:
            agent.chat(
                "写入两个文件",
                should_cancel=first_path.exists,
            )

        self.assertTrue(caught.exception.tool_records_preserved)
        self.assertIs(caught.exception.outcome, agent.last_turn_outcome)
        self.assertEqual(
            caught.exception.outcome.status,
            RunStatus.CANCELLED,
        )
        self.assertEqual(caught.exception.outcome.tool_calls_completed, 1)
        self.assertEqual(first_path.read_text(encoding="utf-8"), "已写入")
        self.assertFalse(second_path.exists())
        history = agent.history()
        self.assertEqual(
            [message["role"] for message in history],
            ["user", "assistant", "tool", "tool", "assistant"],
        )
        results = {
            message["tool_call_id"]: message["content"]
            for message in history
            if message["role"] == "tool"
        }
        self.assertIn('"path": "first.txt"', results["call_1"])
        self.assertIn("未执行", results["call_2"])
        self.assertIn("记录已保留", history[-1]["content"])
        restored = self.store.latest()
        self.assertIsNotNone(restored)
        self.assertEqual(restored.messages[1:], history)

    def test_model_error_after_file_write_preserves_tool_records(self) -> None:
        workspace = Path(self.temporary_directory.name) / "error-workspace"
        workspace.mkdir()
        target = workspace / "changed.txt"
        request = ToolCallRequest(
            id="call_error",
            name="write_text_file",
            arguments='{"path":"changed.txt","content":"changed"}',
        )
        agent = Agent(
            self.settings,
            model=FailingAfterToolModel(request),
            tools=ToolRegistry(
                [WriteTextFileTool(WorkspaceGuard(workspace, 1_000))]
            ),
            session_store=self.store,
            confirmer=AllowAllConfirmer(),
        )

        with self.assertRaises(Exception):
            agent.chat("修改后让模型失败")

        self.assertTrue(agent.last_turn_history_preserved)
        self.assertEqual(agent.last_turn_outcome.status, RunStatus.FAILED)
        self.assertEqual(agent.last_turn_outcome.tool_calls_completed, 1)
        self.assertEqual(
            agent.last_turn_outcome.error_message,
            "模型调用发生未预期错误。",
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "changed")
        self.assertEqual(
            [message["role"] for message in agent.history()],
            ["user", "assistant", "tool", "assistant"],
        )
        self.assertIn("调用失败", agent.history()[-1]["content"])

    def test_tool_turn_raises_when_its_records_cannot_be_saved(self) -> None:
        request = ToolCallRequest(
            id="call_save",
            name="calculator",
            arguments='{"expression":"2 + 3"}',
        )
        model = CallbackModel([[request], [TextDelta("结果是 5")]])
        agent = Agent(self.settings, model=model, session_store=self.store)

        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                agent.chat("计算")

        self.assertTrue(agent.last_turn_history_preserved)
        self.assertIn("调用失败", agent.history()[-1]["content"])
        self.assertEqual(agent.last_turn_outcome.status, RunStatus.FAILED)
        self.assertEqual(agent.last_turn_outcome.error_message, "disk full")
        self.assertEqual(agent.run_status, RunStatus.FAILED)

    def test_failed_clear_keeps_memory_and_disk_unchanged(self) -> None:
        model = CallbackModel([[TextDelta("回答")]])
        agent = Agent(self.settings, model=model, session_store=self.store)
        agent.chat("应保留的问题")
        history_before = agent.history()

        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                agent.clear_conversation()

        self.assertEqual(agent.history(), history_before)
        self.assertEqual(self.store.latest().messages[1:], history_before)

    def test_failed_new_session_does_not_switch_current_session(self) -> None:
        model = CallbackModel([[TextDelta("回答")]])
        agent = Agent(self.settings, model=model, session_store=self.store)
        agent.chat("原会话")
        session_id = agent.session_id
        history_before = agent.history()

        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                agent.start_new_session()

        self.assertEqual(agent.session_id, session_id)
        self.assertEqual(agent.history(), history_before)

    def test_cli_clear_uses_transactional_public_method(self) -> None:
        model = CallbackModel([[TextDelta("回答")]])
        agent = Agent(self.settings, model=model, session_store=self.store)
        agent.chat("应保留的问题")
        history_before = agent.history()

        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            self.assertTrue(agent._handle_command("/clear"))

        self.assertEqual(agent.history(), history_before)

    def test_cli_new_does_not_switch_when_current_session_save_fails(self) -> None:
        model = CallbackModel([[TextDelta("回答")]])
        agent = Agent(self.settings, model=model, session_store=self.store)
        agent.chat("原会话")
        session_id = agent.session_id
        history_before = agent.history()

        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            self.assertTrue(agent._handle_command("/new"))

        self.assertEqual(agent.session_id, session_id)
        self.assertEqual(agent.history(), history_before)

    def test_public_session_methods_create_clear_and_load(self) -> None:
        model = CallbackModel([[TextDelta("回答")]])
        agent = Agent(self.settings, model=model, session_store=self.store)
        first_session_id = agent.session_id
        agent.chat("问题", on_text=lambda _content: None)

        second_session = agent.start_new_session()
        self.assertNotEqual(second_session.id, first_session_id)
        self.assertEqual(agent.history(), [])

        loaded = agent.load_session(first_session_id)
        self.assertEqual(loaded.id, first_session_id)
        self.assertEqual(agent.history()[0]["content"], "问题")

        agent.clear_conversation()
        self.assertEqual(agent.history(), [])

    def test_project_and_session_management_methods(self) -> None:
        model = CallbackModel([])
        agent = Agent(
            self.settings,
            model=model,
            session_store=self.store,
            project_store=self.project_store,
        )
        original_session_id = agent.session_id

        project = agent.create_project("Agent 项目")
        renamed = agent.rename_session(original_session_id, "界面开发")
        moved = agent.move_session(original_session_id, project.id)

        self.assertEqual(agent.list_projects(), [project])
        self.assertEqual(renamed.title, "界面开发")
        self.assertEqual(moved.project_id, project.id)

        deleted_id = agent.delete_session(original_session_id)

        self.assertEqual(deleted_id, original_session_id)
        self.assertNotEqual(agent.session_id, original_session_id)

    def test_deleting_project_moves_its_sessions_to_unassigned(self) -> None:
        model = CallbackModel([])
        agent = Agent(
            self.settings,
            model=model,
            session_store=self.store,
            project_store=self.project_store,
        )
        project = agent.create_project("待删除项目")
        agent.move_session(agent.session_id, project.id)

        renamed = agent.rename_project(project.id, "重命名项目")
        deleted = agent.delete_project(project.id)

        self.assertEqual(renamed.id, project.id)
        self.assertEqual(deleted.name, "重命名项目")
        self.assertEqual(agent.list_projects(), [])
        current = next(
            session
            for session in agent.list_sessions()
            if session.id == agent.session_id
        )
        self.assertIsNone(current.project_id)

    def test_failed_project_delete_restores_session_membership(self) -> None:
        model = CallbackModel([])
        agent = Agent(
            self.settings,
            model=model,
            session_store=self.store,
            project_store=self.project_store,
        )
        project = agent.create_project("不可删除项目")
        agent.move_session(agent.session_id, project.id)

        with patch.object(
            self.project_store,
            "delete",
            side_effect=OSError("project store unavailable"),
        ):
            with self.assertRaises(OSError):
                agent.delete_project(project.id)

        current = self.store.load(agent.session_id)
        self.assertEqual(current.project_id, project.id)
        self.assertEqual(agent.list_projects(), [project])


if __name__ == "__main__":
    unittest.main()
