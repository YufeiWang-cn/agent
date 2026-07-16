from .config import Settings
from .conversation import Conversation
from .models import ChatModel, DeepSeekModel


EXIT_COMMANDS = {"/exit", "exit", "quit", "q", "退出"}


class Agent:
    def __init__(self, settings: Settings, model: ChatModel | None = None) -> None:
        self._model = model or DeepSeekModel(settings)
        self._conversation = Conversation(settings.system_prompt)
        self._running = True

    def run(self) -> None:
        self._print_welcome()

        while self._running:
            try:
                prompt = input("\n你：").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n对话已结束。")
                break

            if not prompt:
                continue

            if self._handle_command(prompt):
                continue

            self._chat(prompt)

    def _chat(self, prompt: str) -> None:
        self._conversation.add_user(prompt)
        answer_parts: list[str] = []

        try:
            print("\n助手：", end="", flush=True)
            for content in self._model.stream(self._conversation.messages):
                print(content, end="", flush=True)
                answer_parts.append(content)
            print()
        except Exception as error:
            self._conversation.remove_last_user()
            print(f"\n调用失败：{error}")
            return

        self._conversation.add_assistant("".join(answer_parts))

    def _handle_command(self, prompt: str) -> bool:
        command = prompt.lower()

        if command in EXIT_COMMANDS:
            print("对话已结束。")
            self._running = False
            return True

        if command == "/help":
            self._print_help()
            return True

        if command == "/clear":
            self._conversation.clear()
            print("当前对话上下文已清空。")
            return True

        if command == "/history":
            self._print_history()
            return True

        if command == "/model":
            print(f"当前模型：{self._model.model_name}")
            return True

        if command.startswith("/"):
            print("未知命令。输入 /help 查看可用命令。")
            return True

        return False

    def _print_welcome(self) -> None:
        print(f"DeepSeek Agent 已启动，当前模型：{self._model.model_name}")
        print("输入 /help 查看命令，输入 /exit 结束对话。")

    @staticmethod
    def _print_help() -> None:
        print(
            "可用命令：\n"
            "  /help     显示帮助\n"
            "  /clear    清空当前对话上下文\n"
            "  /history  查看当前对话历史\n"
            "  /model    查看当前模型\n"
            "  /exit     退出程序"
        )

    def _print_history(self) -> None:
        history = self._conversation.visible_history()
        if not history:
            print("当前还没有对话记录。")
            return

        role_names = {"user": "你", "assistant": "助手"}
        for message in history:
            role = role_names.get(message["role"], message["role"])
            print(f"{role}：{message['content']}")
