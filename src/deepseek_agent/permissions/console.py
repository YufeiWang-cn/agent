from ..tools import Tool


class ConsoleToolConfirmer:
    def confirm(self, tool: Tool, arguments: str) -> bool:
        print(
            "\n[需要确认]\n"
            f"工具：{tool.name}\n"
            f"说明：{tool.description}\n"
            f"参数：{arguments}"
        )
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
