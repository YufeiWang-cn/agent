"""提供不执行任意 Python 代码的受限数学表达式计算工具。"""

import ast
import math
import operator
from collections.abc import Callable

from .base import JsonObject, Tool, ToolExecutionError


Number = int | float
MAX_EXPRESSION_LENGTH = 200
MAX_ABS_RESULT = 10**100
MAX_EXPONENT = 100

BINARY_OPERATORS: dict[type[ast.operator], Callable[[Number, Number], Number]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Number], Number]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class CalculatorTool(Tool):
    """只解析允许的抽象语法树节点，并限制指数和结果范围。"""

    name = "calculator"
    description = "安全计算数学表达式，支持加减乘除、整除、取余、乘方和括号。"
    retryable = True
    idempotent = True
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "需要计算的数学表达式，例如 (128 * 37) + 5。",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    }

    def execute(self, arguments: JsonObject) -> str:
        expression = arguments.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ToolExecutionError("calculator 需要非空字符串参数 expression。")

        expression = expression.strip()
        if len(expression) > MAX_EXPRESSION_LENGTH:
            raise ToolExecutionError("数学表达式过长。")

        try:
            tree = ast.parse(expression, mode="eval")
            result = self._evaluate(tree.body)
        except SyntaxError as error:
            raise ToolExecutionError("数学表达式语法错误。") from error
        except ZeroDivisionError as error:
            raise ToolExecutionError("不能除以零。") from error
        except (OverflowError, ValueError) as error:
            raise ToolExecutionError(f"计算失败：{error}") from error

        self._validate_result(result)
        return str(result)

    def _evaluate(self, node: ast.AST) -> Number:
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolExecutionError("表达式只能包含数字。")
            self._validate_result(value)
            return value

        if isinstance(node, ast.BinOp):
            binary_operation = BINARY_OPERATORS.get(type(node.op))
            if binary_operation is None:
                raise ToolExecutionError("表达式包含不支持的运算符。")

            left = self._evaluate(node.left)
            right = self._evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
                raise ToolExecutionError("乘方指数过大。")

            result = binary_operation(left, right)
            self._validate_result(result)
            return result

        if isinstance(node, ast.UnaryOp):
            unary_operation = UNARY_OPERATORS.get(type(node.op))
            if unary_operation is None:
                raise ToolExecutionError("表达式包含不支持的一元运算符。")
            result = unary_operation(self._evaluate(node.operand))
            self._validate_result(result)
            return result

        raise ToolExecutionError("表达式包含不安全或不支持的语法。")

    @staticmethod
    def _validate_result(value: Number) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ToolExecutionError("计算结果不是有限数值。")
        if abs(value) > MAX_ABS_RESULT:
            raise ToolExecutionError("计算结果过大。")
