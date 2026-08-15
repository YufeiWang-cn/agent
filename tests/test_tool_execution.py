import unittest
from datetime import datetime, timedelta, timezone

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.models import ToolCallRequest
from deepseek_agent.tool_execution import (
    ToolExecutionStatus,
    ToolExecutor,
)
from deepseek_agent.tools import (
    JsonObject,
    Tool,
    ToolEffect,
    ToolExecutionError,
    ToolRegistry,
)


class StaticConfirmer:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = 0

    def confirm(self, tool: Tool, arguments: str) -> bool:
        self.calls += 1
        return self.allowed


class ProtocolTool(Tool):
    name = "protocol_tool"
    description = "用于验证统一工具执行协议。"
    parameters: JsonObject = {"type": "object", "properties": {}}

    def __init__(
        self,
        *,
        effect: ToolEffect = ToolEffect.READ_ONLY,
        requires_confirmation: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.effect = effect
        self.requires_confirmation = requires_confirmation
        self.error = error
        self.executions = 0

    def execute(self, arguments: JsonObject) -> str:
        self.executions += 1
        if self.error is not None:
            raise self.error
        return "执行成功"


class SequenceClock:
    def __init__(self) -> None:
        self._values = iter(
            [
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 1, tzinfo=timezone.utc)
                + timedelta(milliseconds=250),
            ]
        )

    def __call__(self) -> datetime:
        return next(self._values)


class ToolExecutorTests(unittest.TestCase):
    def request(self, arguments: str = "{}") -> ToolCallRequest:
        return ToolCallRequest(
            id="call_protocol",
            name="protocol_tool",
            arguments=arguments,
        )

    def test_success_returns_complete_execution_record(self) -> None:
        tool = ProtocolTool()
        tool.retryable = True
        tool.idempotent = True
        executor = ToolExecutor(
            ToolRegistry([tool]),
            StaticConfirmer(True),
            clock=SequenceClock(),
        )

        record = executor.execute(self.request())

        self.assertEqual(record.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(record.arguments, {})
        self.assertEqual(record.model_result, "执行成功")
        self.assertEqual(record.duration_seconds, 0.25)
        self.assertFalse(record.confirmation_requested)
        self.assertTrue(record.execution_started)
        self.assertFalse(record.may_have_side_effect)

    def test_rejected_call_never_starts_tool_execution(self) -> None:
        tool = ProtocolTool(
            effect=ToolEffect.IRREVERSIBLE_WRITE,
            requires_confirmation=True,
        )
        confirmer = StaticConfirmer(False)
        states: list[bool] = []
        executor = ToolExecutor(ToolRegistry([tool]), confirmer)

        record = executor.execute(
            self.request(),
            on_confirmation_state=states.append,
        )

        self.assertEqual(record.status, ToolExecutionStatus.REJECTED)
        self.assertEqual(states, [True, False])
        self.assertEqual(tool.executions, 0)
        self.assertTrue(record.confirmation_requested)
        self.assertFalse(record.confirmation_granted)
        self.assertFalse(record.execution_started)
        self.assertFalse(record.may_have_side_effect)

    def test_invalid_arguments_fail_before_execution(self) -> None:
        tool = ProtocolTool(effect=ToolEffect.IRREVERSIBLE_WRITE)
        executor = ToolExecutor(ToolRegistry([tool]), StaticConfirmer(True))

        record = executor.execute(self.request("{invalid"))

        self.assertEqual(record.status, ToolExecutionStatus.FAILED)
        self.assertEqual(tool.executions, 0)
        self.assertFalse(record.execution_started)
        self.assertFalse(record.may_have_side_effect)
        self.assertIn("参数不是有效 JSON", record.model_result)

    def test_known_write_error_has_known_failed_result(self) -> None:
        tool = ProtocolTool(
            effect=ToolEffect.IRREVERSIBLE_WRITE,
            error=ToolExecutionError("输入不符合要求"),
        )
        executor = ToolExecutor(ToolRegistry([tool]), StaticConfirmer(True))

        record = executor.execute(self.request())

        self.assertEqual(record.status, ToolExecutionStatus.FAILED)
        self.assertTrue(record.execution_started)
        self.assertFalse(record.may_have_side_effect)
        self.assertEqual(record.error_message, "输入不符合要求")

    def test_tool_can_mark_expected_error_result_as_unknown(self) -> None:
        tool = ProtocolTool(
            effect=ToolEffect.IRREVERSIBLE_WRITE,
            error=ToolExecutionError(
                "写入完成后无法读取结果",
                side_effect_possible=True,
            ),
        )
        executor = ToolExecutor(ToolRegistry([tool]), StaticConfirmer(True))

        record = executor.execute(self.request())

        self.assertEqual(record.status, ToolExecutionStatus.RESULT_UNKNOWN)
        self.assertTrue(record.may_have_side_effect)

    def test_read_tool_error_has_known_failed_result(self) -> None:
        tool = ProtocolTool(error=ToolExecutionError("输入不符合要求"))
        executor = ToolExecutor(ToolRegistry([tool]), StaticConfirmer(True))

        record = executor.execute(self.request())

        self.assertEqual(record.status, ToolExecutionStatus.FAILED)
        self.assertTrue(record.execution_started)
        self.assertFalse(record.may_have_side_effect)

    def test_unexpected_write_error_marks_result_as_unknown(self) -> None:
        tool = ProtocolTool(
            effect=ToolEffect.IRREVERSIBLE_WRITE,
            error=RuntimeError("连接突然中断"),
        )
        executor = ToolExecutor(ToolRegistry([tool]), StaticConfirmer(True))

        record = executor.execute(self.request())

        self.assertEqual(record.status, ToolExecutionStatus.RESULT_UNKNOWN)
        self.assertTrue(record.may_have_side_effect)
        self.assertFalse(record.can_retry_safely)
        self.assertEqual(record.error_message, "连接突然中断")

    def test_unexpected_read_error_has_no_side_effect(self) -> None:
        tool = ProtocolTool(error=RuntimeError("读取失败"))
        executor = ToolExecutor(ToolRegistry([tool]), StaticConfirmer(True))

        record = executor.execute(self.request())

        self.assertEqual(record.status, ToolExecutionStatus.FAILED)
        self.assertFalse(record.may_have_side_effect)

    def test_before_execution_failure_prevents_tool_from_running(self) -> None:
        tool = ProtocolTool(effect=ToolEffect.IRREVERSIBLE_WRITE)
        executor = ToolExecutor(ToolRegistry([tool]), StaticConfirmer(True))

        record = executor.execute(
            self.request(),
            before_execution=lambda _start: (_ for _ in ()).throw(
                OSError("journal unavailable")
            ),
        )

        self.assertEqual(tool.executions, 0)
        self.assertEqual(record.status, ToolExecutionStatus.FAILED)
        self.assertFalse(record.execution_started)
        self.assertFalse(record.may_have_side_effect)
        self.assertIn("journal unavailable", record.model_result)


if __name__ == "__main__":
    unittest.main()
