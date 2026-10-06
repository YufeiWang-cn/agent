"""回归验证长任务记忆、计划暂停、模型协议和中断保存。"""

import io
import json
import tempfile
import time
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.agent import Agent, AgentCancelledError
from deepseek_agent.cli import CliApplication
from deepseek_agent.config import Settings
from deepseek_agent.context import ContextManager, estimate_messages_tokens
from deepseek_agent.memory import JsonProjectStore, JsonSessionStore
from deepseek_agent.models import DeepSeekModel, ReasoningDelta, TextDelta, ToolCallRequest
from deepseek_agent.planning import TaskPlan
from deepseek_agent.runtime import AgentEventType, RunStatus
from deepseek_agent.tool_execution import ToolExecutionStatus
from deepseek_agent.tools import RunCommandTool, Tool, ToolEffect, ToolRegistry
from deepseek_agent.workspace import WorkspaceGuard


class ScriptedModel:
    model_name = "regression-model"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def stream(self, messages, tools):
        self.requests.append(deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        yield from response


class AllowConfirmer:
    def __init__(self):
        self.calls = []

    def confirm(self, tool, arguments):
        self.calls.append(tool.name)
        return True


def plan_request(call_id, status, scope="single_step"):
    return ToolCallRequest(call_id, "update_plan", json.dumps({
        "kind": "execution", "scope": scope,
        "plan": [{"step": "first", "status": status},
                 {"step": "second", "status": "pending"}],
    }))


def chunk(*, reasoning=None, text=None, tool=False):
    calls = []
    if tool:
        calls = [SimpleNamespace(index=0, id="calc", function=SimpleNamespace(
            name="calculator", arguments='{"expression":"1+1"}'))]
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(
        finish_reason="tool_calls" if tool else "stop",
        delta=SimpleNamespace(content=text, reasoning_content=reasoning, tool_calls=calls),
    )])


class RuntimeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = JsonSessionStore(self.root / "state" / "sessions")
        self.projects = JsonProjectStore(self.root / "state" / "projects.json")
        self.settings = Settings(
            api_key="offline", base_url="https://example.invalid", model="regression-model",
            system_prompt="system", workspace_root=self.root,
        )
        self.confirmer = AllowConfirmer()

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, model, *, settings=None, tools=None):
        return Agent(settings or self.settings, model=model, tools=tools,
                     session_store=self.store, project_store=self.projects,
                     confirmer=self.confirmer)

    @staticmethod
    def assert_complete_tool_chain(test, messages):
        requested = [call["id"] for m in messages for call in m.get("tool_calls", [])]
        results = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
        test.assertCountEqual(requested, results)

    def test_control_message_preserves_large_real_turn_and_counts_its_budget(self):
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old" * 1000},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "original goal"},
            {"role": "assistant", "content": None, "tool_calls": [
                ToolCallRequest("read", "read_text_file", "{}").as_message_dict()]},
            {"role": "tool", "tool_call_id": "read", "content": "证据" * 5000},
            {"role": "assistant", "content": "intermediate"},
        ]
        control = [{"role": "user", "content": "runtime continuation"}]
        before = deepcopy(history)
        window = ContextManager(8000).prepare(history, trailing_messages=control)
        self.assertEqual(window.messages, [history[0], *history[3:], *control])
        self.assertEqual(window.omitted_messages, 2)
        self.assertEqual(window.estimated_tokens, estimate_messages_tokens(window.messages))
        self.assertEqual(window.total_estimated_tokens, estimate_messages_tokens(history + control))
        self.assert_complete_tool_chain(self, window.messages)
        window.messages[1]["content"] = "modified snapshot"
        self.assertEqual(history, before)

    def test_control_budget_can_remove_old_turn_without_removing_latest_goal(self):
        history = [{"role": "system", "content": "system"},
                   {"role": "user", "content": "old"},
                   {"role": "assistant", "content": "answer"},
                   {"role": "user", "content": "latest"}]
        control = [{"role": "user", "content": "control" * 100}]
        manager = ContextManager(estimate_messages_tokens(history))
        self.assertEqual(manager.prepare(history).omitted_messages, 0)
        self.assertEqual(manager.prepare(history, trailing_messages=control).messages,
                         [history[0], history[-1], *control])

    def test_repeated_continuations_keep_original_goal_and_all_tool_results(self):
        (self.root / "large.txt").write_text("确切证据" * 3000, encoding="utf-8")
        read = ToolCallRequest("read", "read_text_file", '{"path":"large.txt"}')
        model = ScriptedModel([
            [plan_request("plan", "in_progress", "entire_plan"), read],
            [TextDelta("continue once")], [TextDelta("continue twice")],
            [plan_request("pause", "waiting_user", "entire_plan")],
            [TextDelta("need user input")],
        ])
        agent = self.agent(model)
        outcome = agent.chat("original user goal")
        self.assertEqual(outcome.status, RunStatus.WAITING_USER)
        for request in model.requests[1:]:
            self.assertTrue(any(m.get("content") == "original user goal" for m in request))
            evidence = [m["content"] for m in request if m.get("tool_call_id") == "read"]
            self.assertEqual(len(evidence), 1)
            self.assertIn("确切证据", evidence[0])
            self.assert_complete_tool_chain(self, request)
        persisted = self.store.load(agent.session_id)
        self.assertFalse(any(m.get("role") == "user" and "[运行时控制]" in m.get("content", "")
                             for m in persisted.messages))
        restarted = self.agent(ScriptedModel([[TextDelta("reloaded")]]))
        self.assertEqual(restarted.history(), agent.history())

    def test_finalization_keeps_large_tool_evidence_and_original_goal(self):
        (self.root / "large.txt").write_text("收尾证据" * 3000, encoding="utf-8")
        read = ToolCallRequest("read", "read_text_file", '{"path":"large.txt"}')
        model = ScriptedModel([[read], [TextDelta("final answer")]])
        agent = self.agent(model, settings=replace(self.settings, max_agent_steps=1))
        outcome = agent.chat("original finalization goal")
        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        request = model.requests[1]
        self.assertTrue(any(m.get("content") == "original finalization goal" for m in request))
        self.assertTrue(any("收尾证据" in m.get("content", "") for m in request if m["role"] == "tool"))
        self.assertIn("只能收尾", request[-1]["content"])
        self.assert_complete_tool_chain(self, request)

    def test_normal_continuation_and_finalization_have_same_real_history(self):
        agent = self.agent(ScriptedModel([]))
        agent._conversation.add_user("original")
        agent._conversation.add_assistant("材料" * 5000)
        agent._current_plan = TaskPlan.create([
            {"step": "first", "status": "in_progress"},
            {"step": "second", "status": "pending"}])
        normal = agent._prepare_model_messages(force_plan_continuation=False)
        for options in ({"force_plan_continuation": True},
                        {"force_plan_continuation": False, "finalization": True}):
            request = agent._prepare_model_messages(**options)
            self.assertEqual(request[1:-1], normal[1:])

    def test_batch_stops_at_completed_failed_skipped_and_waiting_boundaries(self):
        for status, scope in [("completed", "single_step"), ("failed", "single_step"),
                              ("skipped", "single_step"), ("waiting_user", "single_step"),
                              ("waiting_user", "entire_plan")]:
            with self.subTest(status=status, scope=scope):
                model = ScriptedModel([
                    [plan_request("start", "in_progress", scope)],
                    [plan_request("boundary", status, scope),
                     ToolCallRequest("write", "write_text_file", '{"path":"must_not_exist.txt","content":"bad"}'),
                     ToolCallRequest("invalid", "not_registered", "bad json")],
                    [TextDelta("paused")],
                ])
                agent = self.agent(model)
                agent.start_new_session()
                outcome = agent.chat("respect boundary")
                self.assertFalse((self.root / "must_not_exist.txt").exists())
                self.assertEqual(outcome.tool_records[-1].status, ToolExecutionStatus.SKIPPED)
                self.assertEqual(outcome.tool_records[-2].status, ToolExecutionStatus.SKIPPED)
                self.assertFalse(outcome.tool_records[-2].execution_started)
                self.assertNotIn("write_text_file", self.confirmer.calls)
                self.assertEqual(outcome.status, RunStatus.WAITING_USER if status == "waiting_user"
                                 else RunStatus.COMPLETED)
                self.assert_complete_tool_chain(self, agent.history())

    def test_waiting_plan_can_resume_on_the_next_user_turn(self):
        write = ToolCallRequest("write", "write_text_file", '{"path":"resumed.txt","content":"ok"}')
        model = ScriptedModel([
            [plan_request("wait", "waiting_user", "entire_plan")], [TextDelta("question")],
            [plan_request("resume", "in_progress", "entire_plan"), write],
            [plan_request("pause", "waiting_user", "entire_plan")], [TextDelta("question 2")],
        ])
        agent = self.agent(model)
        self.assertEqual(agent.chat("first").status, RunStatus.WAITING_USER)
        self.assertEqual(agent.chat("answer").status, RunStatus.WAITING_USER)
        self.assertEqual((self.root / "resumed.txt").read_text(encoding="utf-8"), "ok")

    def test_keyboard_interrupt_before_output_rolls_back_and_allows_retry(self):
        agent = self.agent(ScriptedModel([KeyboardInterrupt(), [TextDelta("retry")]]))
        with self.assertRaises(AgentCancelledError) as caught:
            agent.chat("cancel me")
        self.assertFalse(caught.exception.tool_records_preserved)
        self.assertEqual(agent.history(), [])
        self.assertEqual(agent.run_status, RunStatus.CANCELLED)
        self.assertEqual(agent.chat("retry").final_text, "retry")

    def test_keyboard_interrupt_after_file_write_saves_records_and_restores(self):
        write = ToolCallRequest("write", "write_text_file", '{"path":"completed.txt","content":"done"}')
        agent = self.agent(ScriptedModel([[write], KeyboardInterrupt()]))
        with self.assertRaises(AgentCancelledError) as caught:
            agent.chat("write then interrupt")
        self.assertTrue(caught.exception.tool_records_preserved)
        self.assertEqual(agent.run_status, RunStatus.CANCELLED)
        persisted = self.store.load(agent.session_id)
        self.assertEqual(persisted.messages[1:], agent.history())
        self.assertTrue(any(m.get("tool_call_id") == "write" for m in persisted.messages))
        restarted = self.agent(ScriptedModel([]))
        self.assertEqual(restarted.history(), agent.history())

    def test_keyboard_interrupt_inside_tool_preserves_unknown_effect_and_pending_calls(self):
        root = self.root

        class InterruptedWrite(Tool):
            name = "interrupted_write"
            description = "write then interrupt"
            parameters = {}
            effect = ToolEffect.IRREVERSIBLE_WRITE

            def execute(self, arguments):
                (root / "partial.txt").write_text("already changed", encoding="utf-8")
                raise KeyboardInterrupt()

        agent = self.agent(ScriptedModel([[
            ToolCallRequest("partial", "interrupted_write", "{}"),
            ToolCallRequest("later", "interrupted_write", "{}"),
        ]]), tools=ToolRegistry([InterruptedWrite()]))
        with self.assertRaises(AgentCancelledError) as caught:
            agent.chat("interrupt inside tool")
        self.assertTrue(caught.exception.tool_records_preserved)
        record = agent.last_turn_outcome.tool_records[0]
        self.assertEqual(record.status, ToolExecutionStatus.RESULT_UNKNOWN)
        self.assertTrue(record.may_have_side_effect)
        self.assertIn("结果未知", record.model_result)
        self.assert_complete_tool_chain(self, agent.history())
        self.assertEqual(self.store.load(agent.session_id).messages[1:], agent.history())

    def test_keyboard_interrupt_in_confirmation_does_not_execute_tool(self):
        write = ToolCallRequest("write", "write_text_file", '{"path":"denied.txt","content":"bad"}')
        agent = self.agent(ScriptedModel([[write]]))
        with patch.object(self.confirmer, "confirm", side_effect=KeyboardInterrupt):
            with self.assertRaises(AgentCancelledError):
                agent.chat("interrupt confirmation")
        self.assertFalse((self.root / "denied.txt").exists())
        self.assertEqual(agent.last_turn_outcome.status, RunStatus.CANCELLED)
        self.assertFalse(agent.last_turn_outcome.tool_records[0].execution_started)

    def test_cli_reports_cancel_and_remains_ready(self):
        agent = self.agent(ScriptedModel([KeyboardInterrupt(), [TextDelta("ready")]]))
        app = CliApplication(agent)
        output = io.StringIO()
        with patch("sys.stdout", output):
            app._chat("cancel")
            app._chat("next")
        self.assertIn("[已停止]", output.getvalue())
        self.assertIn("ready", output.getvalue())
        self.assertNotIn("调用失败", output.getvalue())

    def test_interrupted_command_terminates_child_process(self):
        (self.root / "wait.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        tool = RunCommandTool(WorkspaceGuard(self.root, 1000))
        real_sleep = time.sleep
        interrupted = False

        def interrupt_once(delay):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return real_sleep(delay)

        with patch("deepseek_agent.tools.run_command.time.sleep", side_effect=interrupt_once):
            with patch.object(tool, "_terminate_process", wraps=tool._terminate_process) as terminate:
                with self.assertRaises(KeyboardInterrupt):
                    tool.execute({"command": ["python", "wait.py"]})
        terminate.assert_called_once()
        self.assertIsNotNone(terminate.call_args.args[0].poll())

    def provider(self, responses):
        requests = []
        chunks = iter(responses)

        def create(**request):
            requests.append(deepcopy(request))
            return iter(next(chunks))

        model = object.__new__(DeepSeekModel)
        model._model_name = "deepseek-v4-flash"
        model._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return model, requests

    def test_reasoning_fragments_are_forwarded_only_as_internal_protocol_events(self):
        model, _ = self.provider([[chunk(reasoning="part 1"), chunk(reasoning="part 2", text="answer")]])
        self.assertEqual(list(model.stream([], [])),
                         [ReasoningDelta("part 1"), ReasoningDelta("part 2"), TextDelta("answer")])

    def test_reasoning_round_trips_tools_text_restart_and_next_user_turn(self):
        model, requests = self.provider([
            [chunk(reasoning="first "), chunk(reasoning="tool reasoning", tool=True)],
            [chunk(reasoning="answer reasoning", text="2")],
        ])
        agent = self.agent(model)
        text = []
        events = []
        agent.chat("1+1", on_text=text.append, on_event=events.append)
        tool_message = next(m for m in requests[1]["messages"] if m.get("tool_calls"))
        self.assertEqual(tool_message["reasoning_content"], "first tool reasoning")
        self.assertEqual(text, ["2"])
        self.assertEqual([e.content for e in events if e.type is AgentEventType.TEXT_DELTA], ["2"])
        self.assertNotIn("extra_body", requests[1])
        next_model, next_requests = self.provider([[chunk(reasoning="next reasoning", text="next")]])
        restarted = self.agent(next_model)
        restarted.chat("next question")
        assistants = [m for m in next_requests[0]["messages"] if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants],
                         ["first tool reasoning", "answer reasoning"])
        self.assertNotIn("extra_body", next_requests[0])

    def test_legacy_history_disables_thinking_without_discarding_history(self):
        model, requests = self.provider([[chunk(text="new answer")]])
        agent = self.agent(model)
        agent._conversation.add_user("legacy question")
        agent._conversation.add_assistant("legacy answer")
        agent.chat("new question")
        self.assertEqual(requests[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertTrue(any(m.get("content") == "legacy answer" for m in requests[0]["messages"]))
        self.assertEqual(agent.history()[-1]["reasoning_content"], "")


if __name__ == "__main__":
    unittest.main()
