import json
import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.tools import (
    CalculatorTool,
    DateTimeTool,
    ToolExecutionError,
    ToolEffect,
    ToolNotFoundError,
    ToolRegistry,
)


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry([CalculatorTool(), DateTimeTool()])

    def test_schemas_are_exposed_to_the_model(self) -> None:
        names = [schema["function"]["name"] for schema in self.registry.schemas]
        self.assertEqual(names, ["calculator", "get_current_time"])

    def test_tool_metadata_describes_execution_policy(self) -> None:
        calculator = self.registry.get("calculator")

        self.assertEqual(calculator.effect, ToolEffect.READ_ONLY)
        self.assertTrue(calculator.retryable)
        self.assertTrue(calculator.idempotent)
        self.assertFalse(calculator.supports_rollback)

    def test_json_arguments_are_dispatched(self) -> None:
        result = self.registry.execute("calculator", '{"expression":"12 * 3"}')
        self.assertEqual(result, "36")

    def test_invalid_json_is_rejected(self) -> None:
        with self.assertRaises(ToolExecutionError):
            self.registry.execute("calculator", "{invalid")

    def test_unknown_tool_is_rejected(self) -> None:
        with self.assertRaises(ToolNotFoundError):
            self.registry.execute("missing", "{}")

    def test_current_time_returns_structured_data(self) -> None:
        result = json.loads(
            self.registry.execute(
                "get_current_time", '{"timezone":"Asia/Shanghai"}'
            )
        )
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertIn("datetime", result)


if __name__ == "__main__":
    unittest.main()
