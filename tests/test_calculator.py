"""验证计算工具支持的表达式和安全限制。"""

import unittest

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.tools import CalculatorTool, ToolExecutionError


class CalculatorToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = CalculatorTool()

    def test_arithmetic_expression(self) -> None:
        result = self.tool.execute({"expression": "(128 * 37) + 5"})
        self.assertEqual(result, "4741")

    def test_division_by_zero_is_rejected(self) -> None:
        with self.assertRaises(ToolExecutionError):
            self.tool.execute({"expression": "1 / 0"})

    def test_function_call_is_rejected(self) -> None:
        with self.assertRaises(ToolExecutionError):
            self.tool.execute({"expression": "__import__('os').getcwd()"})

    def test_excessive_exponent_is_rejected(self) -> None:
        with self.assertRaises(ToolExecutionError):
            self.tool.execute({"expression": "2 ** 1000"})


if __name__ == "__main__":
    unittest.main()
