"""验证工具注册、结构导出和参数分发。"""

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

    def test_schema_mutation_does_not_pollute_tool_definition(self) -> None:
        """parameters 是类属性；消费方改 schema 不得影响工具定义。"""
        calculator = self.registry.get("calculator")
        before = json.dumps(calculator.parameters, sort_keys=True)

        schema = calculator.schema
        schema["function"]["parameters"]["properties"]["expression"]["type"] = "hacked"
        schema["function"]["parameters"].pop("required", None)

        self.assertEqual(json.dumps(calculator.parameters, sort_keys=True), before)

    def test_tool_metadata_describes_execution_policy(self) -> None:
        calculator = self.registry.get("calculator")

        self.assertEqual(calculator.effect, ToolEffect.READ_ONLY)
        self.assertTrue(calculator.retryable)
        self.assertTrue(calculator.idempotent)
        self.assertFalse(calculator.supports_rollback)

    def test_json_arguments_are_dispatched(self) -> None:
        tool, arguments = self.registry.prepare(
            "calculator", '{"expression":"12 * 3"}'
        )
        self.assertEqual(tool.execute(arguments), "36")

    def test_invalid_json_is_rejected(self) -> None:
        with self.assertRaises(ToolExecutionError):
            self.registry.prepare("calculator", "{invalid")

    def test_unknown_tool_is_rejected(self) -> None:
        with self.assertRaises(ToolNotFoundError):
            self.registry.prepare("missing", "{}")

    def test_current_time_returns_structured_data(self) -> None:
        tool, arguments = self.registry.prepare(
            "get_current_time", '{"timezone":"Asia/Shanghai"}'
        )
        result = json.loads(tool.execute(arguments))
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertIn("datetime", result)

        tool, arguments = self.registry.prepare("get_current_time", "{}")
        default_result = json.loads(tool.execute(arguments))
        self.assertEqual(default_result["timezone"], "Asia/Shanghai")
        self.assertTrue(default_result["datetime"].endswith("+08:00"))


if __name__ == "__main__":
    unittest.main()
