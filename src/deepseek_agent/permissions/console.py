"""在命令行中展示工具参数并请求用户确认。"""

import json

from ..tools import Tool


class ConsoleToolConfirmer:
    """通过终端输入确认可能产生副作用的工具调用。"""

    def confirm(self, tool: Tool, arguments: str) -> bool:
        formatted_arguments, diff = self._format_arguments(arguments)
        print(
            "\n[需要确认]\n"
            f"工具：{tool.name}\n"
            f"说明：{tool.description}\n"
            f"参数：{formatted_arguments}"
        )
        if diff:
            print(f"修改差异：\n{diff}", end="" if diff.endswith("\n") else "\n")
        while True:
            try:
                answer = input("允许执行吗？[y/N]：").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False

            if answer in {"y", "yes"}:
                return True
            if answer in {"", "n", "no"}:
                return False
            print("请输入 y 或 n。")

    @staticmethod
    def _format_arguments(arguments: str) -> tuple[str, str | None]:
        """从确认参数中提取补丁差异，并格式化剩余 JSON。"""
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return arguments, None
        if not isinstance(parsed, dict):
            return arguments, None

        diff = parsed.pop("diff", None)
        if not isinstance(diff, str) or not diff:
            diff = None
        return json.dumps(parsed, ensure_ascii=False, indent=2), diff
